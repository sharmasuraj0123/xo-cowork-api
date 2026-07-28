"""
Shared in-memory state for the cowork_agent chat streaming path.

Two registries, with a clean split of responsibilities:

``active_streams``
    stream_id -> { session_id, text, session_key } or { task, prefetched }

    The HAND-OFF RECORD between ``POST /api/chat/prompt`` and the FIRST
    ``GET /api/chat/stream/{id}``. It carries what a producer needs in order to
    start. It is *pending intent*, nothing more.

``turns``
    stream_id -> :class:`Turn`

    The LIFECYCLE of a turn that someone has actually connected to: its replay
    ring, its monotonic event-id space, its terminal state, its producer task
    and its live subscribers.

The invariant: a stream_id lives in ``active_streams`` OR in ``turns``, never
usefully in both.

Process-local; not persisted. Incompatible with uvicorn --workers > 1 (same
constraint as the bridge service this was migrated from). That constraint got
sharper with the Turn registry: a Turn lives in the worker that created it, so
a reconnect MUST land on the same process or it will be told "Stream not
found" — which, unlike the pre-Turn behaviour, is now the *only* answer the
other worker can give.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from itertools import islice
from typing import Callable

active_streams: dict[str, dict] = {}

# ── Tunables ─────────────────────────────────────────────────────────────────

# Replay ring bounds, per turn. CHARS is the bound that should normally bind;
# FRAMES is only a sanity cap. Sized against real turns: a 24-tool dispatcher
# turn is 57 frames / 13 KB and the largest single line this CLI has ever
# produced (165 KB) becomes ONE text-delta frame, so 256 KiB retains a whole
# pathological answer plus a full tool timeline. FRAMES is 4096 rather than
# 1024 because an adapter-owned lane may re-chunk its answer very finely
# (openclaw's prefetch replay emits ~4 characters per frame); at 1024 the frame
# cap would bind after ~4 KB of answer text.
#
# READ THIS BEFORE SHRINKING EITHER NUMBER. These bound REPLAY DEPTH — how far
# back a reconnecting client can be served — and nothing else. They are NOT a
# bound on answer size, and hitting them must never cost a connected client
# content. Two properties enforce that, and both are load-bearing:
#   * `append` never evicts the frame it just added, so an oversized frame is
#     delivered rather than converted into a `desync` (see the note there); and
#   * both producers in routers/cowork_agent/chat.py yield to the event loop
#     after every append, so subscribers drain as the turn runs and the ring
#     holds a sliding window, not the whole turn. Without that a fast
#     non-awaiting generator buffers everything before any reader wakes and the
#     cap binds on the FIRST connection — which is how a normal-length openclaw
#     prefetch answer used to self-evict.
# `desync` is the exception, not a routine outcome: nothing in this repo
# consumes that event, so an eviction a live client can see is content loss.
TURN_BUFFER_FRAMES = 4096
# Counted in CHARACTERS, not encoded bytes — deliberately, so appending a frame
# stays O(1) with no encode pass. For ASCII the two are identical; for a
# CJK/emoji-heavy answer CPython's compact-unicode representation uses 2-4
# bytes per character, so the true resident size of a maxed-out ring can be
# several times this number. Sized with that headroom in mind.
TURN_BUFFER_CHARS = 256 * 1024

TURN_RETENTION = 600        # keep a finished turn replayable this long
TURN_MAX_RUNTIME = 3600     # a turn running longer than this is presumed wedged
# Ceiling on RETAINED (finished, unread) turns only — NOT on the registry as a
# whole. `_evict_overflow` will never drop a running turn or one with a live
# reader, so exceeding this number is normal and expected under load and the
# dict simply grows past it. Size the process against the real bound, which is
# "concurrent turns started within TURN_MAX_RUNTIME", not against this number:
# 300 simultaneously-running turns leave 300 in the registry, each holding a
# ring of up to TURN_BUFFER_CHARS. Measured, not assumed — see
# `_evict_overflow`'s docstring, which has always been the accurate statement.
MAX_TURNS = 256
UNCLAIMED_STREAM_TTL = 1800  # active_streams entry no GET ever claimed
JANITOR_INTERVAL = 60

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
ABORTED = "aborted"
_TERMINAL = frozenset({COMPLETED, FAILED, ABORTED})


class TurnHandle:
    """A cooperative abort signal for one turn.

    ``abort("client")`` — the only thing POST /api/chat/abort ever calls — is
    STATE-ONLY: it flips a flag and wakes anyone waiting on it. That is
    deliberate and load-bearing. The client POSTs /api/chat/abort on component
    unmount and on session switch, not only from a Stop button, and the server
    cannot tell those apart. Killing a process on it would SIGKILL live work
    every time a user switches sessions.

    NOTHING REGISTERS A KILLER, and that is now a decision rather than an
    accident. The CLI adapters DO own a process and DO kill it — at their own
    per-stream wall clock, which they run and fire themselves
    (``adapters/process_owner.py``). An intermediate design had them also
    register a killer here, gated on an abort reason no caller produces; four
    reviewers independently observed that this was unreachable code whose one
    real effect was to arm a SIGKILL primitive on the exact object the abort
    endpoint fires. It was removed. The guarantee that ``abort()`` cannot signal
    a process now holds because there is no path, not because a string
    comparison is right.

    The killer list stays as the seam for a lane that genuinely owns a process
    AND a client that can distinguish "stop" from "I navigated away". Until both
    are true, the honest answer to "should this kill" is no.

    If you add a new abort reason, the question to answer is not "is this an
    abort" but "can an unmount or a session switch produce it".
    """

    __slots__ = ("_event", "_killers", "reason")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._killers: list[Callable[[str], None]] = []
        self.reason: str | None = None

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    async def wait_aborted(self) -> None:
        await self._event.wait()

    def register_killer(self, killer: Callable[[str], None]) -> None:
        """Add a killer. It receives the abort REASON and must gate on it.

        Nothing calls this today (see the class docstring). Two traps for
        whoever first does:

        * The immediate invocation below. Registering onto an already-aborted
          handle fires the killer straight away with whatever reason latched it
          — in practice "client", from an abort that landed during the spawn.
          A killer that ignores its argument therefore kills on a client abort
          even though it was never wired to that endpoint.
        * There is no unregister. A handle lives in ``turns`` for
          TURN_RETENTION after the turn ends, so a killer closure holding a
          subprocess handle keeps that transport and its pipes alive for the
          whole retention window. Add the removal in the same change as the
          registration.
        """
        self._killers.append(killer)
        if self._event.is_set():
            self._invoke(killer)

    def abort(self, reason: str = "client") -> bool:
        if self._event.is_set():
            return False
        self.reason = reason
        self._event.set()
        for killer in list(self._killers):
            self._invoke(killer)
        return True

    def _invoke(self, killer) -> None:
        try:
            killer(self.reason or "client")
        except Exception as exc:
            print(f"[chat_state] turn killer failed: {exc!r}")


@dataclass
class Turn:
    """One agent turn: a bounded replay ring plus its terminal state.

    Event ids are allocated by :meth:`next_id` and NEVER rewind — a client that
    has reached id 47 can only ever be sent 48 upward, or an id-less frame
    (heartbeat / desync / error), because the client assigns ``lastEventId``
    from any ``id:`` line it sees.

    Terminal state (:meth:`finish`) is written ONLY by the producer task (and by
    the abort endpoint / janitor, which are also not connections). A subscriber
    generator being closed — i.e. a client disconnecting — must never touch it.
    """

    stream_id: str
    session_id: str | None = None
    state: str = RUNNING
    created_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    frames: deque = field(default_factory=deque)
    last_id: int = 0
    #: highest id evicted from the ring; frames[0] always has id first_id + 1
    first_id: int = 0
    terminal_frame: str | None = None
    task: asyncio.Task | None = None
    handle: TurnHandle = field(default_factory=TurnHandle)
    subscribers: set = field(default_factory=set)
    _chars: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    def next_id(self) -> int:
        self.last_id += 1
        return self.last_id

    def append(self, event_id: int, frame: str) -> None:
        if self.terminal:
            return
        self.frames.append((event_id, frame))
        self._chars += len(frame)
        # `> 1`, NOT `while self.frames`: the newest frame is never a candidate
        # for its own eviction. With `while self.frames` a single frame larger
        # than TURN_BUFFER_CHARS popped ITSELF on the append that added it and
        # set first_id to its own id, so it reached nobody — not even a
        # subscriber that had been connected the whole time, which instead got a
        # `desync` where its content should have been. That is not a buffering
        # trade-off, it is silent content loss: before the ring existed the
        # frame went straight to the socket at any size. Reachable, not
        # theoretical — subprocess_io.STREAM_LIMIT admits stream-json lines up
        # to 1 MiB and claude_code turns one assistant text block into ONE
        # text-delta frame, and the result-fallback path emits an entire answer
        # as a single token event.
        #
        # Consequence, deliberately accepted: the ring's resident size is
        # max(TURN_BUFFER_CHARS, largest single frame), not TURN_BUFFER_CHARS.
        # An oversized frame is bounded by the adapter's own line limit and is
        # evicted normally by the NEXT append, so it is transient. Retaining one
        # such frame costs the same memory the pre-ring code held while writing
        # it to the socket; dropping it costs the user their answer.
        while len(self.frames) > 1 and (len(self.frames) > TURN_BUFFER_FRAMES
                                        or self._chars > TURN_BUFFER_CHARS):
            evicted_id, evicted = self.frames.popleft()
            self._chars -= len(evicted)
            self.first_id = evicted_id
        self.wake()

    def wake(self) -> None:
        """Nudge every live subscriber. The queue is a wakeup channel, not a
        frame carrier, so dropping a nudge into a full queue is harmless: the
        subscriber re-drains the ring from its own cursor on the next wakeup."""
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def finish(self, state: str) -> None:
        if self.terminal:
            return
        self.state = state
        self.finished_at = time.monotonic()
        self.wake()

    def frames_after(self, cursor: int) -> list[tuple[int, str]]:
        """Retained frames with id > cursor, oldest first.

        Ids are contiguous within the ring, so the tail is taken arithmetically
        rather than by scanning and comparing every retained frame — this runs
        once per subscriber wakeup, which at 4096 retained frames is otherwise
        a lot of pointless work.

        Returns a materialised list on purpose: the caller yields between
        elements, and the producer may append (and therefore evict) during that
        suspension, which would invalidate a live iterator over the deque.
        """
        offset = cursor - self.first_id
        if offset <= 0:
            return list(self.frames)
        if offset >= len(self.frames):
            return []
        return list(islice(self.frames, offset, None))

    def evicted_past(self, cursor: int) -> bool:
        """True when frames this client has not seen are already gone."""
        return self.first_id > cursor


turns: dict[str, Turn] = {}


def new_turn(stream_id: str, session_id: str | None = None) -> Turn:
    """Register a Turn. MUST stay a synchronous ``def``: the router relies on
    there being no await between its ``turns.get`` miss and this call, which is
    what makes a React StrictMode double-mount attach to one producer instead of
    racing two."""
    turn = Turn(stream_id=stream_id, session_id=session_id)
    turns[stream_id] = turn
    _evict_overflow()
    return turn


def _evict_overflow() -> None:
    """Bound the registry. Only finished, unsubscribed turns are ever dropped —
    a running turn or one with a live reader is never evicted, however old."""
    while len(turns) > MAX_TURNS:
        victim = min(
            (t for t in turns.values() if t.terminal and not t.subscribers),
            key=lambda t: t.finished_at or 0.0,
            default=None,
        )
        if victim is None:
            return
        turns.pop(victim.stream_id, None)


def sweep(emit_terminal: Callable[[Turn], None] | None = None) -> None:
    """One janitor pass. Safe to call by hand (tests do).

    ``emit_terminal`` is called on a turn that is being force-finished, BEFORE
    its state flips, so the router can put a real terminal frame in the ring.
    Without it a later reconnect would find a terminal turn with no terminal
    frame, yield nothing, and the client would treat the empty 200 as an error
    and reconnect in a loop.
    """
    now = time.monotonic()

    # 1. active_streams entries that no GET ever claimed. Lazily stamped so
    #    adapters that write the dict directly need no change.
    for stream_id, info in list(active_streams.items()):
        if stream_id in turns:
            continue
        first_seen = info.get("_first_seen")
        if first_seen is None:
            info["_first_seen"] = now
        elif now - first_seen > UNCLAIMED_STREAM_TTL:
            # NOTE: for an adapter that stashed a prefetch task in this record,
            # dropping it discards the result but not the task — the loop holds
            # a strong reference, so it still completes and still tees its
            # transcript. At a 30-minute TTL that is an acceptable trade; adding
            # a cancel path here would be a process-lifecycle change.
            active_streams.pop(stream_id, None)

    # 2. Turns.
    for turn in list(turns.values()):
        if not turn.terminal and now - turn.created_at > TURN_MAX_RUNTIME:
            # A turn alive for an hour is presumed wedged. This is the ONLY
            # place in this module that touches a producer task, and it is NOT
            # a process kill path: cancelling the producer closes the adapter's
            # generator, whose own ``finally`` drains the child's stdout in the
            # background so it can exit on its own. No signal is sent to any
            # process here. Do not "unify" this with a stream timeout that
            # kills — they are different mechanisms with different blast radii.
            #
            # STILL TRUE now that a per-stream wall clock exists: no killer is
            # registered on any handle, so this ``abort`` reaches no signal
            # whatever reason it carries.
            #
            # The two mechanisms interact in exactly one place, and it is
            # handled on the adapter side rather than assumed here. Cancelling
            # the task below runs the adapter's ``finally`` while its child may
            # still be alive; ``StreamWatchdog.disarm`` therefore refuses to
            # cancel a wall clock whose process is still running, so this sweep
            # cannot strip an orphan of its only bound. Do NOT restore an
            # argument of the form "1800 < 3600 so the clock has already fired"
            # — ``stream_timeout_seconds`` is operator-configurable (and can be
            # 0, meaning no clock at all), so the ordering is not guaranteed.
            turn.handle.abort("timeout")
            if emit_terminal is not None:
                try:
                    emit_terminal(turn)
                except Exception as exc:
                    print(f"[chat_state] terminal frame for {turn.stream_id} failed: {exc!r}")
            if turn.task is not None and not turn.task.done():
                turn.task.cancel()
            turn.finish(FAILED)
        elif (turn.terminal and turn.finished_at is not None
              and now - turn.finished_at > TURN_RETENTION and not turn.subscribers):
            turns.pop(turn.stream_id, None)

    _evict_overflow()


_janitor: asyncio.Task | None = None
_sweep_hook: Callable[[Turn], None] | None = None


def set_sweep_hook(hook: Callable[[Turn], None] | None) -> None:
    """Install the callback :func:`sweep` uses to emit a terminal frame."""
    global _sweep_hook
    _sweep_hook = hook


def ensure_janitor() -> None:
    """Start the background sweep once per event loop.

    Loop-aware on purpose: a module global that remembers a task belonging to
    the FIRST loop that ever ran would never be done and never be rescheduled,
    so under pytest's function-scoped loops the sweep would silently stop
    running after the first test.
    """
    global _janitor
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if (_janitor is not None and not _janitor.done()
            and _janitor.get_loop() is loop):
        return
    _janitor = loop.create_task(_janitor_loop())


async def _janitor_loop() -> None:
    while True:
        await asyncio.sleep(JANITOR_INTERVAL)
        try:
            sweep(_sweep_hook)
        except Exception as exc:
            print(f"[chat_state] janitor sweep failed: {exc!r}")
