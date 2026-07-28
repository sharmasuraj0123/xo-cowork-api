from __future__ import annotations

import json

# ── tool vocabulary ──────────────────────────────────────────────────────────
# Claude Code emits PascalCase tool names. The client renders a *fixed*
# lowercase vocabulary (``TOOL_ICONS`` + the ``getToolTitle``/``getToolLabel``
# switches in the frontend's activity panel) and falls through to
# ``default: return data.tool`` for anything else — an unmapped name is
# rendered RAW. Two consequences drive this table:
#
#   1. Privacy. ``mcp__<server>__<tool>`` names embed MCP server and tenant
#      identifiers (e.g. ``mcp__acme_internal_crm__lookup_customer``).
#      ``visualizer/ingest/pii_filter.py:46-52`` keeps its own frozen tool
#      allowlist and drops ``mcp__*`` entirely; this table follows the same
#      policy. It deliberately does NOT import that module: it is core code and
#      importing it from an adapter would extend a pre-existing modularity
#      violation rather than fix it.
#   2. Rendering. Anything outside the client's vocabulary shows up verbatim.
#
# So: an explicit allowlist with a generic fallback. A raw tool name never
# reaches the wire *on the SSE path*, whatever the CLI decides to call its next
# tool.
#
# SCOPE — read before quoting this as a privacy guarantee. It covers the live
# SSE stream only. The message-history endpoint is a SECOND path to the same
# client and it is NOT covered: ``services/cowork_agent/engine/messages.py``
# (:173 and :360) builds each history tool part as ``block.get("name")`` with
# the raw CLI name in both ``tool`` and ``state.title``, plus the full raw
# ``input``. The client refetches history the moment the turn ends
# (``use-sse.ts`` DONE handler -> ``invalidateQueries(messages.list)``), so
# today a Bash step is labelled "Running a command" during the turn and flips to
# the raw "Bash" — with its arguments rendered verbatim in a <pre> — about a
# second later. Closing that requires the same mapping in ``messages.py``, which
# is core code outside this adapter and outside this change's scope. Until then:
# this table fixes the live path and reduces exposure; it does not make the
# end-to-end property true.
#
#   CLI name  ->  (client tool, human title)
_TOOL_MAP: dict[str, tuple[str, str]] = {
    "Bash": ("bash", "Running a command"),
    "Read": ("read", "Reading files"),
    "Edit": ("edit", "Editing files"),
    "MultiEdit": ("multiedit", "Editing files"),
    "Write": ("write", "Writing files"),
    "NotebookEdit": ("edit", "Editing a notebook"),
    "Glob": ("glob", "Searching the workspace"),
    "Grep": ("grep", "Searching the workspace"),
    "WebFetch": ("web_fetch", "Fetching a page"),
    "WebSearch": ("web_search", "Searching the web"),
    # Both names exist in the wild for the subagent tool; both are `task`.
    "Task": ("task", "Delegating to a subagent"),
    "Agent": ("task", "Delegating to a subagent"),
    "TodoWrite": ("todo", "Updating the plan"),
    "AskUserQuestion": ("question", "Asking a question"),
    "ExitPlanMode": ("question", "Presenting a plan"),
}

# Anything not in the allowlist. ``tool`` is outside the client's icon
# vocabulary on purpose — it renders as the neutral default, and the supplied
# title wins over the switch anyway (both label functions start with
# ``if (title) return title``).
_GENERIC_TOOL: tuple[str, str] = ("tool", "Working…")

# Deltas that carry user-visible assistant text. NEVER ``input_json_delta``
# (partial tool arguments = PII) and never ``signature_delta`` (base64 blob).
_TEXTUAL_DELTAS = frozenset({"text_delta"})

# ``rate_limit_info.status`` values that mean "your request did NOT go through".
# A BLOCK-list, not `!= "allowed"`, because the enum has more than two members:
# `allowed_warning` is the *approaching-the-cap, request-allowed* value (it sits
# beside "You're close to your usage limit" in the 2.1.220 string table, 15
# occurrences) and it fires on every turn once a user is near their limit.
# Treating it as a block put a permanent, false "Rate limited — waiting" banner
# in front of exactly the users least able to tell it was wrong, while their
# turn streamed normally behind it. Only "allowed" appears in the committed
# corpus, so no fixture covers this — hence the explicit list.
#
# Unknown/new statuses default to SILENT on purpose: a missing banner degrades
# to "no extra information" and a genuine hard block still surfaces through the
# `result`/`error` path, whereas a wrong banner is actively misleading on every
# single turn. Add a value here only with evidence that it means "blocked".
_RATE_LIMIT_BLOCKED = frozenset({"rejected"})


def _map_tool(name: object) -> tuple[str, str]:
    """Map a CLI tool name onto (client tool, title). Unknown -> generic."""
    if isinstance(name, str):
        return _TOOL_MAP.get(name, _GENERIC_TOOL)
    return _GENERIC_TOOL


def _result_error_message(event: dict) -> str:
    """Extract the failure reason from a ``result`` event with ``is_error``.

    Verified against ``tests/fixtures/captures/result_is_error.jsonl``: that
    shape (``subtype: "error_max_turns"``) has **no** ``result`` key at all —
    the message lives in ``errors: ["Reached maximum number of turns (1)"]``.
    Reading only ``result`` is why every such turn used to surface the generic
    "Claude Code error" with the reason dropped. ``result`` is still consulted
    as a fallback because other error subtypes do carry it.
    """
    parts: list[str] = []
    errors = event.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                raw = item.get("message") or item.get("error") or ""
                text = raw.strip() if isinstance(raw, str) else ""
            else:
                text = ""
            if text:
                parts.append(text)
    if parts:
        return "; ".join(parts)

    fallback = event.get("result")
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()

    subtype = event.get("subtype")
    if isinstance(subtype, str) and subtype.strip():
        return f"Claude Code error ({subtype.strip()})"
    return "Claude Code error"


def parse_stream_line(raw: bytes, *, partial_messages: bool = False) -> dict | None:
    """Never-raising wrapper around :func:`_parse_stream_line`.

    ``adapter.py`` calls this unguarded from inside ``async for raw_line in
    proc.stdout``; anything that escapes here unwinds ``Adapter.stream()`` and
    ``chat.py``'s ``except Exception`` turns it into a single ``agent-error``
    frame followed by ``done`` — the rest of the answer, the ``result`` record,
    usage and the model id are all lost. One bad line must cost one line, not
    the turn, so the contract is enforced structurally rather than by hoping the
    branch bodies below stay total. ``_parse_stream_line`` still guards each
    known hazard precisely; this net exists for the ones nobody has found yet.

    ``BaseException`` (``CancelledError``, ``KeyboardInterrupt``) deliberately
    still propagates.
    """
    try:
        return _parse_stream_line(raw, partial_messages=partial_messages)
    except Exception:
        return None


def _parse_stream_line(raw: bytes, *, partial_messages: bool = False) -> dict | None:
    """
    Decode one raw line from Claude Code's stream-json output.
    Returns a normalised event dict or None to skip.

    ``partial_messages`` MUST match whether the caller actually put
    ``--include-partial-messages`` on the command line. It selects exactly ONE
    source of assistant text: with the flag on, the CLI emits the same answer
    twice — once as ``stream_event``/``text_delta`` and once as
    ``assistant``/``text``, byte-identically (verified on claude 2.1.220,
    ``partial_messages.jsonl``: 160 B == 160 B). Reading both renders every
    sentence twice. It defaults to False, which is what the adapter does today.

    Must not raise — see :func:`parse_stream_line`. This is the only thing
    between a corrupt or hostile stdout line and a lost turn.
    """
    try:
        line = raw.decode("utf-8").strip()
    except (UnicodeDecodeError, AttributeError):
        return None

    if not line:
        return None

    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        # NOT just json.JSONDecodeError. `json.loads` has two other documented
        # failure modes, both reachable on a line far under the 64 KiB
        # StreamReader limit and therefore genuinely on the wire:
        #   * ValueError "Exceeds the limit (4300 digits) for integer string
        #     conversion" — CPython's `sys.int_max_str_digits`. A ~4.3 KB run of
        #     digits inside any tool input triggers it. JSONDecodeError is a
        #     ValueError subclass, so this one clause covers both.
        #   * RecursionError from the C scanner at ~9998 levels of nesting, i.e.
        #     a ~20 KB line. A structured MCP `tool_result` is shaped by a
        #     third-party server, so its depth is not ours to assume.
        return None

    # `[]`, `123`, `null`, `true`, `"s"` are all valid JSON. Without this guard
    # every one of them raises AttributeError on the next line and takes the
    # turn down with it — same blast radius as the 64 KiB ValueError.
    if not isinstance(event, dict):
        return None

    etype = event.get("type", "")

    # Events produced *inside* a Task subagent carry a non-null
    # ``parent_tool_use_id``. Their tool activity is useful progress and is
    # forwarded; their text is the subagent's monologue, not this turn's
    # answer, and must never be spliced into the reply.
    from_subagent = event.get("parent_tool_use_id") is not None

    if etype == "system" and event.get("subtype") == "init":
        # First event Claude CLI emits in stream-json mode — carries the
        # native session_id BEFORE any tokens. Surface it as an internal
        # event so the adapter can persist the nativeSessionId mapping
        # before any chance of SSE cancellation.
        sid = event.get("session_id") or event.get("sessionId")
        if sid:
            return {"type": "session_id", "session_id": sid}
        return None

    if etype == "content_block_delta":
        delta = event.get("delta") or {}
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            text = delta.get("text", "")
            # `and not from_subagent`: the suppression rule is a property of the
            # parser, not of one branch. Unreachable on 2.1.220 (no top-level
            # content_block_delta exists in the corpus — asserted by
            # test_no_double_render_with_partial_messages) but an invariant that
            # holds on three of four text branches is not an invariant.
            # isinstance: `"".join`/concatenation on a non-str is how the
            # assistant branch used to die; same value, same guard.
            if isinstance(text, str) and text and not from_subagent:
                return {"type": "token", "token": text}
        return None

    if etype == "stream_event":
        # The partial-message envelope. Silent unless the caller asked for it,
        # because the ``assistant`` branch below owns assistant text otherwise.
        if not partial_messages:
            return None
        inner = event.get("event") or {}
        if not isinstance(inner, dict) or inner.get("type") != "content_block_delta":
            return None
        delta = inner.get("delta") or {}
        if not isinstance(delta, dict):
            return None
        text = delta.get("text")
        if (
            delta.get("type") in _TEXTUAL_DELTAS
            and isinstance(text, str)   # same guard as the other three branches
            and text
            and not from_subagent
        ):
            return {"type": "token", "token": text}
        # No per-delta "thinking" ping here — that is SSE spam. The assistant
        # branch gives exactly one pulse per thinking block.
        return None

    if etype == "assistant":
        message = event.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return None

        parts: list[str] = []
        tools: list[tuple[object, object]] = []
        saw_thinking = False
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                # isinstance, not just truthiness. Every other nesting level in
                # this branch is type-guarded (non-dict message :178, non-list
                # content :179, non-dict block :186) — this was the one hole:
                # `{"type":"text","text":5}` appended an int and `"".join(parts)`
                # then raised TypeError out of the parser, killing the turn.
                if isinstance(text, str) and text:
                    parts.append(text)
            elif btype == "tool_use":
                tools.append((block.get("name"), block.get("id")))
            elif btype == "thinking":
                saw_thinking = True

        # Text wins and is NEVER dropped. 2.1.220 emits one block per event
        # (corpus: 4,951 messages, 0 exceptions), so a text+tool_use collision
        # is unreachable today; if that ever changes, losing a progress label
        # beats losing part of the answer.
        if parts and not partial_messages and not from_subagent:
            return {"type": "token", "token": "".join(parts)}

        if tools:
            name, call_id = tools[0]
            # The client's TOOL_START handler is `if (data.tool && data.call_id)`
            # — both must be truthy or the frame renders nothing at all.
            if call_id:
                tool, title = _map_tool(name)
                return {"type": "tool-call", "tool": tool, "call_id": call_id, "title": title}
            return None

        if saw_thinking:
            return {"type": "reasoning", "title": "Thinking"}
        return None

    if etype == "user":
        message = event.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            # Plain-string prompt echo — nothing to report.
            return None

        results = [
            b for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        if not results:
            return None

        # One event per line: report the FIRST block, exactly as the assistant
        # branch reports the first ``tool_use``. Keeping the same rule on both
        # sides is what guarantees a start and its result always pair up — a
        # started call that never resolves leaves a spinner running until the
        # turn ends. 2.1.220 emits one block per event (corpus: 4,951 messages,
        # 0 exceptions), so a multi-block message is unreachable today; if it
        # appears, the extra blocks are simply not rendered rather than
        # half-rendered.
        block = results[0]
        call_id = block.get("tool_use_id")
        if not call_id:
            return None

        # ``is_error`` lives ON THE BLOCK (corpus: 2,396 blocks — 1250 False,
        # 48 True, 1119 absent); never on the message and never at top level.
        # The distinction is not cosmetic: a ``tool-result`` frame flips the
        # step to *completed* client-side, so a failed step reported that way
        # renders a green check labelled "Step failed". Failures must go out as
        # ``tool-error``.
        if block.get("is_error"):
            # ``title`` is inert in the client's TOOL_ERROR handler (it reads
            # only output/error_message); kept so the internal event is
            # self-describing. No ``output``: tool_result content is raw file
            # contents / command output, i.e. PII.
            return {"type": "tool-error", "call_id": call_id, "title": "Step failed"}

        # Deliberately no ``title``: the client does `title ?? p.state.title`,
        # so sending one here would overwrite the informative start label
        # ("Reading files") with a generic "Step complete".
        return {"type": "tool-result", "call_id": call_id}

    if etype == "rate_limit_event":
        info = event.get("rate_limit_info") or {}
        status = info.get("status") if isinstance(info, dict) else None
        if isinstance(status, str) and status in _RATE_LIMIT_BLOCKED:
            # Reuses the router's existing ``model-loading`` branch: a rate-limit
            # wait is exactly the "we are blocked, not frozen" state that event
            # already represents, and it needs no new client vocabulary.
            return {"type": "model-loading", "label": "Rate limited — waiting"}
        # Everything else — "allowed" (every turn), "allowed_warning" (every
        # turn once you are near the cap), and any status a future CLI invents
        # — stays silent. See _RATE_LIMIT_BLOCKED.
        return None

    if etype == "result":
        if event.get("is_error"):
            return {"type": "error", "error": _result_error_message(event)}
        return {
            "type": "result",
            "result": event.get("result", ""),
            "session_id": event.get("session_id"),
            "usage": event.get("usage") or {},
            "model": event.get("model", ""),
        }

    if etype == "text":
        text = event.get("text", "") or event.get("content", "")
        # Same two guards as the other three text branches. Without isinstance,
        # `{"type":"text","text":5}` put `{"token": 5}` on the wire; chat.py
        # serialises it verbatim and the client's `streamingText + text` splices
        # a literal "5" into the visible answer. Degrades rather than crashes,
        # which is exactly why it would have shipped unnoticed.
        if isinstance(text, str) and text and not from_subagent:
            return {"type": "token", "token": text}
        return None

    if etype == "error":
        return {"type": "error", "error": event.get("error", event.get("message", "unknown error"))}

    return None
