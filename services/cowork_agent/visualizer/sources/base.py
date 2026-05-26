"""Source protocol — what every runtime source must implement.

The watcher main loop walks a list of :class:`Source` instances each
tick, calls :meth:`Source.poll_events` to drain the runtime's event
stream into normalised :class:`events.Event` objects, and (optionally)
calls :meth:`Source.poll_presence` for the live "who's open right
now" snapshot.

Keeping the protocol minimal means new runtimes (Codex, future
tools) plug in with a single file under :mod:`sources`.
"""

from __future__ import annotations

from typing import Iterator, Protocol

from services.cowork_agent.visualizer.ingest.events import Event


class PresenceRow(dict):
    """Loose alias for the dict shape ``poll_presence`` returns:
    ``{session_id, runtime, agent, opened_at, last_activity_at,
    entrypoint, project_id}`` (plus optional fields). The activity
    sink decides which keys it needs."""


class Source(Protocol):
    """Every runtime source implements this protocol."""

    name: str  # adapter directory name, e.g. "claude_code", "openclaw", "hermes"

    def poll_events(self) -> Iterator[Event]:
        """Yield events observed since the last call. Stateless from
        the caller's POV — the source owns its own offset / pairing
        state."""
        ...

    def poll_presence(self) -> list[dict]:
        """Return a list of currently-open session rows (one per live
        runtime process). Empty list is a valid "no sessions open"
        answer."""
        ...
