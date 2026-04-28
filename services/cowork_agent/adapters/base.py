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

    # ── Required class attribute ───────────────────────────────────────────────

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Snake-case name matching the config/agents/ directory, e.g. 'claude_code'."""
