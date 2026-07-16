"""
antigravity model listing (`/api/models`).

`/api/models` returns **one row per agent/profile** under the active backend in a
shape shared by every agent (``{id, name, provider_id, capabilities, pricing,
metadata}``) — the frontend model selector is written against exactly that
contract, so a deviating shape breaks it (blank/"something went wrong" selector).

antigravity has no native agent store of its own, so — like claude_code — it
re-exports openclaw's listing (which scans ``~/.openclaw/agents/`` and falls back
to a single ``<prefix>/main`` row). ``list_models`` reads the *active* agent for
the id prefix (``antigravity/…``) and provider label, so rows come back correctly
tagged for antigravity.

Note: the agy *LLM* catalog (Gemini/Claude/GPT-OSS via ``agy models``) is a
different concept from these agent rows and is intentionally NOT surfaced here —
doing so would break the shared ``/api/models`` contract. The active LLM is the
agy default; see the adapter's ``_model`` / manifest ``models.default``.
"""

from __future__ import annotations

from services.cowork_agent.adapters.openclaw.models import list_models

__all__ = ["list_models"]
