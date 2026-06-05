"""
Hermes chat capability.

Hermes runs through the shared AgentDispatcher, so it needs no custom prompt
handler. It only contributes ``resolve_agent_id`` — the hermes picker sends
``model: "hermes/<profile>"`` and the profile name becomes the agent_id used
for gateway routing. The chat router calls this generically via the chat
capability, so it doesn't special-case hermes.
"""
from __future__ import annotations

from services.cowork_agent.streaming import hermes_profile_from_prompt_body


def resolve_agent_id(body: dict) -> str | None:
    """Resolve the hermes profile from the prompt body's model string."""
    return hermes_profile_from_prompt_body(body)
