"""
Composio SDK wrapper.

Thin proxy over the Composio Python SDK. No local persistence — Composio
itself is the source of truth for which toolkits a user has connected and
which tools are available. See docs/composio-xo-swarm-api-migration.md for
the future xo-swarm-api projection layer.

Environment:
- COMPOSIO_API_KEY                       required for any call to succeed
- COMPOSIO_AUTH_CONFIG_<TOOLKIT>         per-toolkit auth_config_id from the dashboard
- COMPOSIO_AUTH_CONFIG_STRIPE_OAUTH      Stripe has two auth schemes; the OAuth one
- COMPOSIO_AUTH_CONFIG_STRIPE_APIKEY     and the API-key one
- COMPOSIO_CALLBACK_URL                  OAuth callback URL Composio redirects to
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Toolkit catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolkitMeta:
    slug: str                       # Composio toolkit slug, e.g. "GMAIL"
    display_name: str
    schemes: tuple[str, ...]        # ordered: ("OAUTH2",) or ("OAUTH2", "API_KEY")
    auth_env_keys: dict[str, str]   # {scheme: env_var_name_for_auth_config_id}


TOOLKITS: dict[str, ToolkitMeta] = {
    "gmail":           ToolkitMeta("GMAIL",           "Gmail",            ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_GMAIL"}),
    "googlecalendar":  ToolkitMeta("GOOGLECALENDAR",  "Google Calendar",  ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_GOOGLECALENDAR"}),
    "notion":          ToolkitMeta("NOTION",          "Notion",           ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_NOTION"}),
    "stripe":          ToolkitMeta("STRIPE",          "Stripe",           ("OAUTH2", "API_KEY"),  {
        "OAUTH2":  "COMPOSIO_AUTH_CONFIG_STRIPE_OAUTH",
        "API_KEY": "COMPOSIO_AUTH_CONFIG_STRIPE_APIKEY",
    }),
    "supabase":        ToolkitMeta("SUPABASE",        "Supabase",         ("API_KEY",),           {"API_KEY": "COMPOSIO_AUTH_CONFIG_SUPABASE"}),
    "digitalocean":    ToolkitMeta("DIGITALOCEAN",    "DigitalOcean",     ("API_KEY",),           {"API_KEY": "COMPOSIO_AUTH_CONFIG_DIGITALOCEAN"}),
    "youtube":         ToolkitMeta("YOUTUBE",         "YouTube",          ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_YOUTUBE"}),
    "miro":            ToolkitMeta("MIRO",            "Miro",             ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_MIRO"}),
    "canva":           ToolkitMeta("CANVA",           "Canva",            ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_CANVA"}),
}


def toolkit_meta(toolkit_id: str) -> ToolkitMeta:
    meta = TOOLKITS.get(toolkit_id.lower())
    if meta is None:
        raise ValueError(f"Unknown toolkit: {toolkit_id!r}. Known: {sorted(TOOLKITS)}")
    return meta


def _auth_config_id_for(toolkit_id: str, scheme: str) -> str:
    meta = toolkit_meta(toolkit_id)
    env_key = meta.auth_env_keys.get(scheme.upper())
    if not env_key:
        raise ValueError(
            f"Toolkit {meta.slug} does not support auth scheme {scheme!r}. "
            f"Supported: {meta.schemes}"
        )
    value = os.getenv(env_key, "").strip()
    if not value:
        raise RuntimeError(
            f"Composio auth config for {meta.slug}/{scheme} is not configured. "
            f"Set {env_key} in the environment (see Composio dashboard)."
        )
    return value


# ---------------------------------------------------------------------------
# SDK client (lazy singleton)
# ---------------------------------------------------------------------------

_client: Any = None


def _composio():
    """Return a singleton Composio client. Imports the SDK lazily so the
    module can load even when `composio` is not installed (the router will
    return a configuration error at request time instead of crashing at boot).
    """
    global _client
    if _client is not None:
        return _client
    api_key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("COMPOSIO_API_KEY is not set in the environment.")
    try:
        from composio import Composio  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "The `composio` Python package is not installed. "
            "Add `composio>=0.7` to requirements.txt and pip install."
        ) from exc
    _client = Composio(api_key=api_key)
    return _client


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """Tolerant attribute getter — accepts either dataclass-like objects or
    dicts (Composio's SDK has shifted between these between versions)."""
    for name in names:
        if obj is None:
            return default
        if isinstance(obj, dict):
            if name in obj:
                obj = obj[name]
                continue
            return default
        if hasattr(obj, name):
            obj = getattr(obj, name)
            continue
        return default
    return obj


def _callback_url() -> str:
    return os.getenv(
        "COMPOSIO_CALLBACK_URL",
        "http://127.0.0.1:5002/api/connectors/composio/callback",
    ).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def initiate_connection(
    user_id: str,
    toolkit_id: str,
    auth_scheme: str = "OAUTH2",
    api_key: Optional[str] = None,
    redirect_uri: Optional[str] = None,
) -> dict[str, Any]:
    """Kick off a Composio connection flow for the user.

    OAuth: returns {"auth_url": "<provider-consent-url>", "connection_request_id": "..."}.
        Uses the v3 `link()` endpoint (Composio retired `initiate()` for
        managed-OAuth configs on 2026-05-08).
    API_KEY: returns {"auth_url": null, "connection_request_id": "..."}.
        Still on `initiate()`, which the docstring marks as supported for
        non-OAuth schemes indefinitely.
    """
    meta = toolkit_meta(toolkit_id)
    scheme = auth_scheme.upper()
    auth_config_id = _auth_config_id_for(toolkit_id, scheme)
    callback = redirect_uri or _callback_url()
    client = _composio()

    if scheme == "API_KEY":
        if not api_key:
            raise ValueError(
                f"{meta.slug}: API_KEY scheme requires `api_key` in the request body."
            )
        request = client.connected_accounts.initiate(  # type: ignore[attr-defined]
            user_id=user_id,
            auth_config_id=auth_config_id,
            callback_url=callback,
            config={"auth_scheme": "API_KEY", "val": {"status": "ACTIVE", "api_key": api_key}},
        )
    else:
        # OAuth1 / OAuth2 / DCR_OAUTH — all routed through link().
        request = client.connected_accounts.link(  # type: ignore[attr-defined]
            user_id=user_id,
            auth_config_id=auth_config_id,
            callback_url=callback,
        )
    return {
        "auth_url": _attr(request, "redirect_url"),
        "connection_request_id": _attr(request, "id"),
    }


def check_connection(connection_request_id: str) -> dict[str, Any]:
    """Non-blocking status check. Returns
    {"status": "PENDING|ACTIVE|FAILED", "connected_account_id": str|None}.
    """
    client = _composio()
    try:
        # Different SDK versions: some expose `.get(id)`, some `.retrieve(id)`.
        get_fn = (
            getattr(client.connected_accounts, "get", None)             # type: ignore[attr-defined]
            or getattr(client.connected_accounts, "retrieve", None)     # type: ignore[attr-defined]
        )
        if get_fn is None:
            raise RuntimeError("Composio SDK has no connected_accounts.get/retrieve method.")
        record = get_fn(connection_request_id)
    except Exception as exc:
        log.warning("composio: check_connection failed: %s", exc)
        return {"status": "FAILED", "connected_account_id": None, "error": str(exc)}

    return {
        "status": _attr(record, "status", default="PENDING"),
        "connected_account_id": _attr(record, "id"),
    }


def list_connections(user_id: str) -> list[dict[str, Any]]:
    """Return all of this user's connected accounts as a flat list.
    Each item is {toolkit, connected_account_id, status, scheme}.
    """
    client = _composio()
    try:
        page = client.connected_accounts.list(user_ids=[user_id])  # type: ignore[attr-defined]
    except Exception as exc:
        log.warning("composio: list_connections failed for user=%s: %s", user_id, exc)
        return []

    items = _attr(page, "items", default=page) or []
    out: list[dict[str, Any]] = []
    for it in items:
        # Current SDK: `it.toolkit` is a nested object/dict with `.slug`.
        # Older shapes fell back on `it.toolkit_slug` or `it.app` as strings.
        toolkit = (
            _attr(it, "toolkit", "slug", default="")
            or _attr(it, "toolkit_slug", default="")
            or _attr(it, "app", default="")
        )
        out.append({
            "toolkit": str(toolkit).upper() or None,
            "connected_account_id": _attr(it, "id"),
            "status": _attr(it, "status", default="UNKNOWN"),
            "scheme": _attr(it, "auth_scheme", default=None),
        })
    return out


def disconnect(connected_account_id: str) -> bool:
    """Revoke a Composio connection. Returns True on success."""
    client = _composio()
    try:
        delete_fn = (
            getattr(client.connected_accounts, "delete", None)         # type: ignore[attr-defined]
            or getattr(client.connected_accounts, "remove", None)      # type: ignore[attr-defined]
        )
        if delete_fn is None:
            raise RuntimeError("Composio SDK has no connected_accounts.delete/remove method.")
        delete_fn(connected_account_id)
        return True
    except Exception as exc:
        log.warning("composio: disconnect failed: %s", exc)
        return False


def list_tools(
    user_id: str,
    toolkit_id: str,
    *,
    include_disabled: bool = False,
) -> list[dict[str, Any]]:
    """List action slugs + parameter schemas available for a toolkit.

    `user_id` is unused by the Composio v0.13 SDK — `get_raw_composio_tools`
    returns the action catalogue regardless of which user is authenticated.
    Kept on the signature for parity with the other helpers in this module.

    Returns per-tool: slug, name, description, parameters, enabled. For
    toolkits with a Write/Read classification (Google Calendar today) a
    `category` field is also set to `"read"` or `"write"`.

    `include_disabled` toggles between two consumer modes:

    - `False` (default) — agent path. Filters out any actions the user
      has explicitly disabled via the Connectors UI. The agent never
      sees them in `composio_list_tools` output and so never proposes
      tool_use blocks against them.
    - `True` — UI / admin path. Returns the full catalogue with the
      `enabled` field flipped accordingly. The Connectors UI uses this
      to render toggle switches for every action.
    """
    del user_id  # unused — see docstring
    meta = toolkit_meta(toolkit_id)
    client = _composio()
    try:
        # limit=200 surfaces every action in the largest catalogues we expose
        # (Stripe ~200, Google Calendar ~48, Gmail ~30). Without this the SDK
        # defaults to 20 — alphabetically truncating popular actions like
        # GOOGLECALENDAR_DELETE_EVENT out of the listing.
        tools = client.tools.get_raw_composio_tools(  # type: ignore[attr-defined]
            toolkits=[meta.slug], limit=200,
        )
    except Exception as exc:
        log.warning("composio: list_tools failed (toolkit=%s): %s", meta.slug, exc)
        return []

    # Local imports — these modules transitively import composio_service
    # in some paths, and a top-of-file import would create a cycle.
    from services import composio_action_prefs, composio_categories  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for t in tools:
        slug = _attr(t, "slug", default="") or _attr(t, "name", default="")
        enabled = composio_action_prefs.is_action_enabled(toolkit_id, slug)
        if not include_disabled and not enabled:
            continue
        entry: dict[str, Any] = {
            "slug": slug,
            "name": _attr(t, "name", default=""),
            "description": _attr(t, "description", default=""),
            "parameters": _attr(t, "input_parameters", default={}),
            "enabled": enabled,
        }
        category = composio_categories.classify(toolkit_id, slug)
        if category is not None:
            entry["category"] = category
        out.append(entry)
    return out


def _toolkit_id_for_slug(slug: str) -> Optional[str]:
    """Reverse-lookup a toolkit id from an action slug.

    Action slugs are uppercase-prefixed with the toolkit's Composio slug
    (e.g. `GOOGLECALENDAR_DELETE_EVENT` → toolkit `googlecalendar`,
    Composio slug `GOOGLECALENDAR`). Returns the lower-case toolkit id
    if it matches one in `TOOLKITS`, otherwise None — the caller treats
    unknown toolkits as "no preference applies" and lets the SDK call go
    through.
    """
    if not isinstance(slug, str):
        return None
    upper = slug.upper()
    for toolkit_id, meta in TOOLKITS.items():
        prefix = meta.slug + "_"
        if upper.startswith(prefix) or upper == meta.slug:
            return toolkit_id
    return None


def execute_tool(user_id: str, tool_slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run a single Composio action and return its result payload.

    Honours the per-action enable/disable preferences from
    `services.composio_action_prefs`. If the slug's toolkit has the
    action explicitly disabled, returns a `successful=False` payload
    immediately — Composio is never called. This is a fail-safe for the
    case where the model picks a disabled slug despite it not being in
    `composio_list_tools` output (older context, hallucinated slug, etc).

    `dangerously_skip_version_check=True` is the documented escape hatch for
    direct .tools.execute() calls when the host app hasn't pinned a specific
    toolkit version via `toolkit_versions=...` on the Composio client config
    or via the COMPOSIO_TOOLKIT_VERSION_<SLUG> env vars. We don't pin
    versions today — pinning here would require keeping the catalog in sync.
    """
    from services import composio_action_prefs  # noqa: PLC0415  — see list_tools

    toolkit_id = _toolkit_id_for_slug(tool_slug)
    if toolkit_id and not composio_action_prefs.is_action_enabled(toolkit_id, tool_slug):
        return {
            "successful": False,
            "data": None,
            "error": f"Action '{tool_slug}' is disabled by user preference.",
        }

    client = _composio()
    result = client.tools.execute(  # type: ignore[attr-defined]
        slug=tool_slug,
        arguments=arguments,
        user_id=user_id,
        dangerously_skip_version_check=True,
    )
    return {
        "successful": _attr(result, "successful", "success", default=True),
        "data": _attr(result, "data", default=result),
        "error": _attr(result, "error", default=None),
    }


_MCP_CONFIG_NAME = "xo-cowork-api"
_mcp_config_id_cache: Optional[str] = None


def _get_or_create_mcp_config_id() -> Optional[str]:
    """Resolve (and cache) the stable MCP server config id for this app.

    Composio's `client.create(user_id=...).mcp.url` returns an ephemeral
    Tool Router session bound to its own connection sandbox — connections
    made through the cowork-api UI are NOT visible to that session. The
    `client.mcp.*` family instead manages persistent server configs whose
    per-user URLs surface the user's actual account-level connections.

    We keep a single config (named "xo-cowork-api") that lists all toolkits
    in TOOLKITS, look it up by name on first use, create it if missing.
    """
    global _mcp_config_id_cache
    if _mcp_config_id_cache:
        return _mcp_config_id_cache

    client = _composio()
    try:
        listing = client.mcp.list(name=_MCP_CONFIG_NAME)
        items = listing.get("items", []) if isinstance(listing, dict) else []
        for s in items:
            sd = s.model_dump() if hasattr(s, "model_dump") else s
            if isinstance(sd, dict) and sd.get("name") == _MCP_CONFIG_NAME and sd.get("id"):
                _mcp_config_id_cache = sd["id"]
                return _mcp_config_id_cache
    except Exception as exc:
        log.warning("composio: mcp.list failed: %s", exc)

    toolkits = [meta.slug.lower() for meta in TOOLKITS.values()]
    try:
        cfg = client.mcp.create(
            name=_MCP_CONFIG_NAME,
            toolkits=toolkits,
            manually_manage_connections=False,
        )
        cfg_d = cfg.model_dump() if hasattr(cfg, "model_dump") else cfg
        _mcp_config_id_cache = cfg_d.get("id") if isinstance(cfg_d, dict) else None
        return _mcp_config_id_cache
    except Exception as exc:
        log.warning("composio: mcp.create failed: %s", exc)
        return None


def get_mcp_url(user_id: str) -> Optional[str]:
    """Per-user hosted MCP URL — the agent talks to Composio through this.

    Uses the persistent MCP server config (not the ephemeral Tool Router),
    so the returned URL surfaces the user's existing account-level
    connections instead of starting in an empty sandbox.
    """
    try:
        cfg_id = _get_or_create_mcp_config_id()
        if not cfg_id:
            return None
        inst = _composio().mcp.generate(user_id=user_id, mcp_config_id=cfg_id)
        inst_d = inst.model_dump() if hasattr(inst, "model_dump") else inst
        if isinstance(inst_d, dict):
            return inst_d.get("url") or inst_d.get("mcp_url")
        return _attr(inst, "url") or _attr(inst, "mcp_url")
    except Exception as exc:
        log.warning("composio: get_mcp_url failed for user=%s: %s", user_id, exc)
        return None


# ---------------------------------------------------------------------------
# Gateway install — openclaw / hermes / claude-code
# ---------------------------------------------------------------------------
#
# These gateways accept tool/MCP wiring at config time, NOT per request. We
# point each one at our local meta-tool MCP server (services/cowork_mcp.py)
# instead of Composio's hosted per-user URL. That URL would otherwise
# register every action of every connected toolkit (Stripe alone ships 200+),
# blowing past Kimi's 262k context window. The meta-tool surface keeps the
# gateway prompt at 2 composio tools; the agent fetches a toolkit's catalogue
# on demand via composio_list_tools when it actually needs to act.
#
# The exact config keys below are best-guess based on the gateway's general
# plugin/MCP conventions; verify against the running gateway's docs before
# relying on them in production.


def get_meta_mcp_url() -> str:
    """URL of the local /mcp/cowork meta-tool MCP server.

    Reads HOST/PORT exactly the way server.py's __main__ block does so a
    single env override (PORT=5003, …) propagates without touching this
    file. Always returns 127.0.0.1 — gateways live on the same box, and
    binding the published URL to whatever HOST is set to (often 0.0.0.0)
    would route gateway → wildcard, which doesn't always resolve cleanly.
    """
    port = int(os.getenv("PORT", "5002"))
    # Trailing slash — Starlette mounts return 307 on the no-slash form and
    # not every gateway's MCP client follows redirects.
    return f"http://127.0.0.1:{port}/mcp/cowork/"

def install_into_openclaw(user_id: str) -> dict[str, Any]:
    """Point OpenClaw at the local meta-tool MCP server.

    Writes `mcp.servers.cowork` in ~/.openclaw/openclaw.json with the URL
    of services/cowork_mcp.py. The `user_id` argument is unused — kept on
    the signature so the HTTP route in routers/cowork_agent/composio.py
    and the parallel install_into_hermes() / write_session_mcp_config()
    callers stay uniform. The meta-tool server resolves the user itself
    (single-tenant "default_user" today; header-based dispatch later).

    Drops the legacy `mcp.servers.composio` and `plugins.entries.composio`
    entries — the latter generated a recurring "plugin not found" warning
    on every gateway boot.
    """
    config_path = Path(os.path.expanduser("~/.openclaw/openclaw.json"))
    if not config_path.exists():
        return {"ok": False, "error": f"OpenClaw config not found at {config_path}"}

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"Failed to read OpenClaw config: {exc}"}

    # transport: "streamable-http" forces OpenClaw onto the newer transport.
    # ("http" is rejected by the schema; "sse" would be SSE.) The cowork MCP
    # server only speaks streamable-http.
    mcp_section = data.setdefault("mcp", {})
    servers = mcp_section.setdefault("servers", {})
    servers["cowork"] = {
        "url": get_meta_mcp_url(),
        "enabled": True,
        "transport": "streamable-http",
    }
    servers.pop("composio", None)

    plugins = data.get("plugins") or {}
    entries = plugins.get("entries") or {}
    entries.pop("composio", None)

    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(config_path)
    return {"ok": True, "config_path": str(config_path), "restart_required": True}


def install_into_hermes(user_id: str) -> dict[str, Any]:
    """Point Hermes at the local meta-tool MCP server.

    Writes `mcp_servers.cowork` in ~/.hermes/config.yaml. See
    install_into_openclaw() for the rationale; `user_id` is unused.
    """
    config_path = Path(os.path.expanduser("~/.hermes/config.yaml"))
    if not config_path.exists():
        return {"ok": False, "error": f"Hermes config not found at {config_path}"}

    try:
        import yaml  # type: ignore
    except ImportError:
        return {"ok": False, "error": "PyYAML not installed; cannot edit Hermes config."}

    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {"ok": False, "error": f"Failed to read Hermes config: {exc}"}

    servers = data.setdefault("mcp_servers", {})
    servers["cowork"] = {
        "url": get_meta_mcp_url(),
        "enabled": True,
        "transport": "streamable-http",
    }
    servers.pop("composio", None)

    tmp = config_path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    tmp.replace(config_path)
    return {"ok": True, "config_path": str(config_path), "restart_required": True}
