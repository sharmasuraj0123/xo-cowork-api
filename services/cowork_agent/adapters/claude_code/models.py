"""
claude_code model listing (`/api/models`).

claude_code has no native model store of its own, so it surfaces the same
agent rows as openclaw (scanning ``~/.openclaw/agents/``), labelled with
claude_code's own model prefix and provider name (``list_models`` reads the
*active* agent for those). This re-export preserves the historical behavior
where "everything that isn't hermes" used the openclaw listing.
"""

from __future__ import annotations

from services.cowork_agent.adapters.openclaw.models import list_models

__all__ = ["list_models"]
