from __future__ import annotations
import json
import pathlib
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator


class BaseAgentAdapter(ABC):
    """
    All agent adapters must subclass this.
    'config' is a plain dict loaded by settings.load_agent_config(adapter_name).
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config

    # ── Abstract (must implement) ──────────────────────────────────────────────

    @abstractmethod
    async def run(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Non-streaming execution.
        Must return a dict with at minimum:
          { "message": str, "native_session_id": str | None }
        """

    @abstractmethod
    async def stream(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Streaming execution.
        Must yield dicts of shape { "type": "token", "token": str }
        and end with exactly one { "done": True, "native_session_id": str | None }.
        """

    # ── Concrete (override when needed) ───────────────────────────────────────

    async def setup(self) -> bool:
        """One-time credential or gateway setup. Return True when ready."""
        return True

    async def health(self) -> dict[str, Any]:
        """Lightweight liveness check surfaced by /health."""
        return {"ok": True}

    def load_commands(self) -> dict[str, Any]:
        """Read config/agents/{adapter_name}/commands.json. Returns {} if absent."""
        p = pathlib.Path("config/agents") / self.adapter_name / "commands.json"
        if p.exists():
            return json.loads(p.read_text())
        return {}

    # ── Agent CRUD (default: not supported) ────────────────────────────────────
    #
    # The shared /api/agents/* routes dispatch through these methods. Backends
    # that own a notion of "agent" (openclaw, hermes profiles) override them;
    # backends that don't inherit the defaults below.

    async def list_agents(self) -> list[dict[str, Any]]:
        """Return AgentInfo dicts for the shared GET /api/agents.

        Default returns []. Override to expose this backend's agents.
        """
        return []

    async def get_agent_detail(self, agent_id: str) -> dict[str, Any] | None:
        """Return the full agent snapshot for GET /api/agents/{agent_id}.

        Default returns None (i.e. "this backend doesn't own that id").
        Override to expose this backend's per-agent detail.
        """
        return None

    async def create_agent(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create a backend-specific agent. Returns the created AgentInfo.

        Default raises NotImplementedError. Override when the backend
        supports agent creation.
        """
        raise NotImplementedError(
            f"{self.adapter_name!r} adapter does not support agent creation"
        )

    async def update_agent(self, agent_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Patch agent metadata. Returns the updated AgentInfo.

        Default raises NotImplementedError.
        """
        raise NotImplementedError(
            f"{self.adapter_name!r} adapter does not support agent updates"
        )

    async def delete_agent(self, agent_id: str) -> bool:
        """Remove an agent. Returns True on success.

        Default raises NotImplementedError.
        """
        raise NotImplementedError(
            f"{self.adapter_name!r} adapter does not support agent deletion"
        )

    # ── Session & message retrieval (default: empty) ───────────────────────────
    #
    # Shared /api/sessions and /api/messages dispatch through these. The
    # generic router merges results from every registered adapter.

    async def list_sessions(self) -> list[dict[str, Any]]:
        """Return SessionResponse dicts for the shared GET /api/sessions.

        Default returns []. Override to expose this backend's sessions.
        """
        return []

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return MessageResponse dicts for /api/messages/{session_id}.

        Default returns []. Override to expose this backend's transcripts.
        """
        return []

    # ── Usage aggregation (default: empty) ─────────────────────────────────────

    async def aggregate_usage(self, days: int = 30) -> dict[str, Any]:
        """Return a usage rollup contribution for the shared /api/usage.

        Default returns {}. Override to contribute this backend's
        tokens/cost; callers merge per-adapter rollups into a single
        response.
        """
        return {}

    # ── Extension hooks (configuration time, not request time) ─────────────────
    #
    # Both are typed as ``Any`` here so this module stays import-light
    # (no fastapi dependency, no cross-package imports).

    def extra_routers(self) -> list[Any]:
        """Backend-specific APIRouter instances to mount when this adapter
        is registered.

        Returns ``list[fastapi.APIRouter]``. Default returns []. Used by
        the server bootstrap to mount adapter-owned routes without the
        generic router layer needing to know about each backend.
        """
        return []

    def secrets_scope(self) -> Any | None:
        """BFF secrets handle for /api/secrets/* — wraps the backend's
        env-file store.

        Returns whatever the adapter wants (typically a ``SecretsScope``
        instance) or ``None`` when this backend has no secrets surface.
        Default returns None.
        """
        return None

    # ── Required class attribute ───────────────────────────────────────────────

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Snake-case name matching the config/agents/ directory, e.g. 'claude_code'."""

