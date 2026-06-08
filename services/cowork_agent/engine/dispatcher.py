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
