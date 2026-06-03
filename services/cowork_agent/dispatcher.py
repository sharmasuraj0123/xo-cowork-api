from __future__ import annotations
from typing import Any, AsyncIterator

from services.cowork_agent.adapter_registry import get_adapter
from services.cowork_agent.settings import load_agent_config


class AgentDispatcher:
    """
    Thin orchestration layer used by routers.
    Routers import AgentDispatcher, not individual adapters.
    """

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        config = load_agent_config(agent_name)
        self.adapter = get_adapter(agent_name, config)

    async def ask(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.adapter.run(question, session_id, **kwargs)

    async def stream(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in self.adapter.stream(question, session_id, **kwargs):
            yield event

    async def health(self) -> dict[str, Any]:
        return await self.adapter.health()

    # ── Read-side proxies (Phase 5) ───────────────────────────────────────────

    async def list_agents(self) -> list[dict[str, Any]]:
        return await self.adapter.list_agents()

    async def get_agent_detail(self, agent_id: str) -> dict[str, Any] | None:
        return await self.adapter.get_agent_detail(agent_id)

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await self.adapter.list_sessions()

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        return await self.adapter.list_messages(session_id)

    # ── Write-side proxies ────────────────────────────────────────────────────

    async def create_agent(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.adapter.create_agent(body)

    async def update_agent(self, agent_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        return await self.adapter.update_agent(agent_id, patch)

    async def delete_agent(self, agent_id: str) -> bool:
        return await self.adapter.delete_agent(agent_id)

    # ── Configuration-time hooks ──────────────────────────────────────────────

    def extra_routers(self) -> list[Any]:
        return self.adapter.extra_routers()

    def secrets_scope(self) -> Any | None:
        return self.adapter.secrets_scope()

    # ── Chat fast-path proxies (Phase 6d.3) ───────────────────────────────────

    async def prepare_stream(
        self,
        text: str,
        session_id: str | None,
        body: dict[str, Any],
        is_new_session: bool,
        agent_id: str | None,
    ) -> dict[str, Any] | None:
        return await self.adapter.prepare_stream(
            text, session_id, body, is_new_session, agent_id
        )

    def fast_path_stream(
        self,
        stream_info: dict[str, Any],
        stream_id: str,
    ) -> Any | None:
        return self.adapter.fast_path_stream(stream_info, stream_id)
