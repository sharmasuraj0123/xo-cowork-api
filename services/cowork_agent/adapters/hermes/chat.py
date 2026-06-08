"""
Hermes chat capability.

Hermes runs through the shared AgentDispatcher, so it needs no custom prompt
handler. It only contributes ``resolve_agent_id`` — the hermes picker sends
``model: "<prefix>/<profile>"`` and the profile name becomes the agent_id used
for gateway routing. The chat router calls this generically via the chat
capability, so it doesn't special-case hermes.
"""
from __future__ import annotations

from services.cowork_agent.agent_registry import get_active_agent


def resolve_agent_id(body: dict) -> str | None:
    """Resolve the hermes profile name from a chat request.

    1. Explicit ``agent_id`` in the body — set when the user picks a profile
       from the sidebar.
    2. ``model`` field with this agent's prefix (e.g. ``hermes/aria``); the
       prefix comes from the active agent's manifest (``model_prefix``), the
       same encoding the openclaw parser uses.

    Returns ``None`` if neither identifies a profile; the caller then lets the
    hermes adapter pick its own default.
    """
    explicit_id = body.get("agent_id")
    if isinstance(explicit_id, str) and explicit_id.strip():
        return explicit_id.strip()

    model = body.get("model")
    if isinstance(model, str):
        prefix = get_active_agent().model_prefix.lower()
        lowered = model.strip().lower()
        if lowered.startswith(f"{prefix}/"):
            rest = model.split("/", 1)[1] if "/" in model else ""
            return rest.strip() or None
    return None
