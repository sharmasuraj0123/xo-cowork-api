"""
Composio integration package — everything Composio lives here.

- service.py           SDK wrapper: toolkit catalog, connections, sessions, MCP entry
- identity.py          multi-tenant identity (bearer validation, signed proxy tokens)
- session_identity.py  backend-minted opaque session ids for multi-tenant mode
- action_prefs.py      per-user per-action enable/disable store
- categories.py        Read-vs-Write classification of toolkit actions
- router.py            REST endpoints under /api/connectors/composio
- mcp_proxy.py         loopback MCP reverse proxy at /mcp/cowork-proxy

Deliberately no eager re-exports: submodules import each other lazily
(function-scoped) to break cycles, and an eager import here would reintroduce
load-order sensitivity. Import submodules explicitly, e.g.
``from services.composio import service as composio_service``.

The only Composio-touching code outside this package is the per-agent MCP
wiring, which the modularity invariant pins to the adapter trees:
``services/cowork_agent/adapters/<agent>/mcp_install.py`` (openclaw, hermes)
and ``adapters/claude_code/mcp_config.py``.
"""
