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


# Single-tenant sentinel. Matches the value baked into services/cowork_mcp.py
# and routers/cowork_agent/composio.py's _resolve_user_id() fallback so a
# session created here sees the same connected_accounts a UI-initiated
# Connectors flow created for "default_user".
_DEFAULT_USER_ID = "default_user"


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


# ---------------------------------------------------------------------------
# Session — the unifying Composio abstraction
# ---------------------------------------------------------------------------
#
# Per Composio docs (https://docs.composio.dev/docs/how-composio-works), a
# session is the runtime context for one user. Both access modes —
# session.tools() for in-process Python and session.mcp.url for MCP clients —
# point at the same context. We use only the MCP mode (our three runtimes
# are subprocess MCP clients), and cache the session.id per user so each
# user's one OAuth grant via COMPOSIO_MANAGE_CONNECTIONS is visible across
# every runtime and every chat turn.


_SESSION_IDS: dict[str, str] = {}


def _pinned_connected_accounts(user_id: str) -> dict[str, list[str]]:
    """{toolkit_slug: [connected_account_id, ...]} for every ACTIVE Connected
    Account this user owns. Passed to composio.create() so the Tool Router
    session sees the same connections the Connectors UI lit up.

    Without this, a freshly minted session starts in an empty connection
    sandbox and reports every toolkit as `initiated` — even if the user
    already authorized them via the UI's Connected Accounts flow.
    """
    pinned: dict[str, list[str]] = {}
    try:
        rows = list_connections(user_id)
    except Exception as exc:
        log.warning("composio: list_connections failed while building pin map for user=%s: %s", user_id, exc)
        return pinned
    for row in rows:
        if (row.get("status") or "").upper() != "ACTIVE":
            continue
        slug = (row.get("toolkit") or "").lower()
        cid = row.get("connected_account_id")
        if not slug or not cid:
            continue
        pinned.setdefault(slug, []).append(cid)
    return pinned


def invalidate_session(user_id: str) -> None:
    """Evict the cached session for `user_id`. Call after any Connectors UI
    state change (connect / disconnect / status flip) so the next agent
    turn re-mints a session with the updated pin map."""
    _SESSION_IDS.pop(user_id or _DEFAULT_USER_ID, None)


def get_session(user_id: str):
    """Return Composio's session object for `user_id`.

    Reuses an existing session (`composio.use(session_id)`) when we have
    its id from a previous call, otherwise mints a new one
    (`composio.create(user_id=…, connected_accounts=…)`) with the user's
    ACTIVE Connected Accounts pinned. In-memory cache only — if
    xo-cowork-api restarts, the next call re-mints.
    """
    user_id = user_id or _DEFAULT_USER_ID
    sid = _SESSION_IDS.get(user_id)
    if sid:
        try:
            return _composio().use(sid)
        except Exception as exc:
            log.debug("composio: use(%s) failed for user=%s: %s", sid, user_id, exc)
            _SESSION_IDS.pop(user_id, None)  # stale id; fall through to create
    create_kwargs: dict[str, Any] = {"user_id": user_id}
    pinned = _pinned_connected_accounts(user_id)
    if pinned:
        create_kwargs["connected_accounts"] = pinned
    session = _composio().create(**create_kwargs)
    new_id = getattr(session, "session_id", None) or getattr(session, "id", None)
    if new_id:
        _SESSION_IDS[user_id] = str(new_id)
    return session


def build_mcp_server_entry(user_id: str) -> dict[str, Any]:
    """Emit the canonical MCP server config for `user_id`.

    URL and auth headers come straight from `session.mcp.url` /
    `session.mcp.headers` — no host-prefix matching, no manually
    constructed auth dicts. Every adapter writes this verbatim under the
    server key ``cowork``.
    """
    session = get_session(user_id)
    mcp = getattr(session, "mcp", None)
    url = getattr(mcp, "url", None) if mcp is not None else None
    headers = getattr(mcp, "headers", None) if mcp is not None else None
    # Fall back to _attr in case the SDK shape ever returns dicts.
    if not url:
        url = _attr(session, "mcp", "url") or _attr(session, "url")
    if not headers:
        headers = _attr(session, "mcp", "headers", default=None)
    entry: dict[str, Any] = {"type": "http", "url": str(url)}
    if headers:
        entry["headers"] = dict(headers)
    log.info(
        "composio: session %s for user=%s -> %s",
        _SESSION_IDS.get(user_id or _DEFAULT_USER_ID, "?"), user_id, url,
    )
    return entry


# ---------------------------------------------------------------------------
# Gateway install — openclaw / hermes
# ---------------------------------------------------------------------------
#
# Both gateways accept MCP wiring at config time (not per request). To keep
# the Composio API key out of these on-disk configs, we point both gateways
# at xo-cowork-api's localhost MCP proxy (routers/cowork_agent/mcp_proxy.py)
# rather than at Composio's session URL directly. The proxy resolves user_id
# server-side and injects x-api-key from .env at request time.
#
# Claude Code uses its own per-session mcp.json path
# (services/cowork_agent/adapters/claude_code/mcp_config.py) and is NOT
# routed through the proxy — its config file is per-turn and auto-deleted.


def _cowork_proxy_url() -> str:
    """Localhost URL of the MCP reverse proxy that injects x-api-key
    server-side. Trailing slash matches the routed paths in
    routers/cowork_agent/mcp_proxy.py and avoids 307 redirects."""
    port = int(os.getenv("PORT", "5002"))
    return f"http://127.0.0.1:{port}/mcp/cowork-proxy/"


def install_into_openclaw(user_id: str) -> dict[str, Any]:
    """Write the proxy URL into ~/.openclaw/openclaw.json under
    mcp.servers.cowork. No headers, no API key — the proxy injects it.

    Idempotent. Caller (the refresh-gateway HTTP route) is responsible for
    asking the user to restart OpenClaw.

    `user_id` is accepted for signature parity with the rest of the
    install paths and so the caller-side cache key works; the proxy
    resolves user_id at request time, so this value is not embedded in
    the config.
    """
    del user_id  # value not embedded; see docstring
    config_path = Path(os.path.expanduser("~/.openclaw/openclaw.json"))
    if not config_path.exists():
        return {"ok": False, "error": f"OpenClaw config not found at {config_path}"}

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"Failed to read OpenClaw config: {exc}"}

    entry: dict[str, Any] = {
        "url": _cowork_proxy_url(),
        "transport": "streamable-http",
        "enabled": True,
    }

    mcp_section = data.setdefault("mcp", {})
    servers = mcp_section.setdefault("servers", {})
    servers["cowork"] = entry
    servers.pop("composio", None)
    servers.pop("xo_composio", None)

    plugins = data.get("plugins") or {}
    entries = plugins.get("entries") or {}
    entries.pop("composio", None)

    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(config_path)
    return {"ok": True, "config_path": str(config_path), "restart_required": True}


def install_into_hermes(user_id: str) -> dict[str, Any]:
    """Write the proxy URL into ~/.hermes/config.yaml under
    mcp_servers.cowork. No headers, no API key — the proxy injects it."""
    del user_id  # value not embedded; see install_into_openclaw docstring
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

    entry: dict[str, Any] = {
        "url": _cowork_proxy_url(),
        "transport": "streamable-http",
        "enabled": True,
    }

    servers = data.setdefault("mcp_servers", {})
    servers["cowork"] = entry
    servers.pop("composio", None)
    servers.pop("xo_composio", None)

    tmp = config_path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    tmp.replace(config_path)
    return {"ok": True, "config_path": str(config_path), "restart_required": True}
