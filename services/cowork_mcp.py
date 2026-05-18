"""
Local MCP server exposing two Composio meta-tools to OpenClaw / Hermes /
Claude Code. Gateways used to pull Composio's per-user "fat" URL which
registered ~60+ action tools (Stripe alone contributes 200+), blowing past
Kimi's 262k context window. This server exposes only:

    composio_list_tools(toolkit) -> list of action slugs + schemas
    composio_execute(tool, arguments) -> action result

Gateways carry 2 tools instead of 60+. The agent fetches a toolkit's
catalogue on demand via composio_list_tools when it needs to act.

Mounted from server.py at /mcp/cowork. Streamable-HTTP transport (the
SSE-based default isn't supported by Composio's tool router or some of
the gateways). Stateless — every call is independent; no session.

User resolution is hardcoded to "default_user" (the single-tenant local
sentinel matched by routers.cowork_agent.composio._resolve_user_id).
For multi-user we would route an x-user-id header through the Context.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from services import composio_service

log = logging.getLogger(__name__)


_DEFAULT_USER = "default_user"


mcp_server = FastMCP("xo-cowork", stateless_http=True)
mcp_server.settings.streamable_http_path = "/"


_TOOLKITS_DESC = (
    "gmail (email — read, send, search, draft), "
    "googlecalendar (calendar events, availability), "
    "notion (pages, databases), "
    "stripe (customers, charges, invoices), "
    "supabase (Postgres tables, auth), "
    "digitalocean (droplets, billing), "
    "youtube (videos, channels, playlists), "
    "miro (boards), "
    "canva (designs, brand templates)"
)


_LIST_TOOLS_DESC = f"""Discover the action tools available for an external service via Composio.

ALWAYS call this FIRST whenever the user asks about any of: {_TOOLKITS_DESC}.

Do not assume a toolkit is "not connected" just because its actions don't appear in your top-level tool list — they are deliberately not eagerly registered to keep the prompt small. Call this tool to fetch them on demand.

Returns a list of {{slug, name, description, parameters}}. Actions the user has explicitly disabled on the Connectors page are filtered out — if you don't see an action you expected, tell the user they can re-enable it from the Connectors UI. Pass the `slug` field to composio_execute when you want to run one of the actions.

`toolkit` must be one of: gmail, googlecalendar, notion, stripe, supabase, digitalocean, youtube, miro, canva (lowercase).
"""


@mcp_server.tool(description=_LIST_TOOLS_DESC)
def composio_list_tools(toolkit: str) -> list[dict[str, Any]]:
    composio_service.toolkit_meta(toolkit)
    return composio_service.list_tools(_DEFAULT_USER, toolkit)


@mcp_server.tool()
def composio_execute(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run a Composio action and return its result. Call composio_list_tools
    first to find a `slug` and learn its required arguments.

    If the result has `error` set to a connection / auth error, tell the
    user the integration needs to be (re)authorised at the Connectors page
    — do not retry blindly.

    Args:
        tool: Action slug from composio_list_tools (e.g. GMAIL_FETCH_EMAILS,
            GMAIL_SEND_EMAIL, GOOGLECALENDAR_LIST_EVENTS, NOTION_CREATE_PAGE).
        arguments: Per-action argument object. The expected keys come from
            the action's schema returned by composio_list_tools.

    Returns:
        {successful: bool, data: any, error: any|null}.
    """
    return composio_service.execute_tool(_DEFAULT_USER, tool, arguments)


mcp_app = mcp_server.streamable_http_app()
