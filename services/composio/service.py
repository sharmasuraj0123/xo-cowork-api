"""
Composio SDK wrapper.

Thin proxy over the Composio Python SDK. No local persistence — Composio
itself is the source of truth for which toolkits a user has connected and
which tools are available. See docs/composio-xo-swarm-api-migration.md for
the future xo-swarm-api projection layer.

Environment:
- COMPOSIO_API_KEY                       required for any call to succeed
- COMPOSIO_AUTH_CONFIG_<TOOLKIT>         per-toolkit auth_config_id from the dashboard
- COMPOSIO_CALLBACK_URL                  OAuth callback URL Composio redirects to

Every public function here is per-user: ``user_id`` is a real XO user id and is
mandatory. There is no shared/sentinel user — passing an empty one raises.
Isolation is Composio's: a session created with ``user_id`` only ever sees that
user's connected accounts.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


def _require_user_id(user_id: Optional[str], what: str) -> str:
    """Guard: every Composio call is scoped to one real user.

    Raises rather than substituting a fallback — a missing user_id here used to
    silently become the shared ``default_user`` bucket, which is exactly the
    cross-tenant mixing this module must not do.
    """
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError(
            f"composio.{what}: a real user_id is required (got {user_id!r})."
        )
    return uid


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
    "googlesheets":    ToolkitMeta("GOOGLESHEETS",    "Google Sheets",    ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_GOOGLESHEETS"}),
    "googledocs":      ToolkitMeta("GOOGLEDOCS",      "Google Docs",      ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_GOOGLEDOCS"}),
    "googleslides":    ToolkitMeta("GOOGLESLIDES",    "Google Slides",    ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_GOOGLESLIDES"}),
    "googlemeet":      ToolkitMeta("GOOGLEMEET",      "Google Meet",      ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_GOOGLEMEET"}),
    "figma":           ToolkitMeta("FIGMA",           "Figma",            ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_FIGMA"}),
    "dropbox":         ToolkitMeta("DROPBOX",         "Dropbox",          ("OAUTH2",),            {"OAUTH2": "COMPOSIO_AUTH_CONFIG_DROPBOX"}),
    "browserbase":     ToolkitMeta("BROWSERBASE_TOOL", "Browserbase",     ("API_KEY",),           {"API_KEY": "COMPOSIO_AUTH_CONFIG_BROWSERBASE"}),
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
            "Install it from requirements.txt (pinned to >=0.18,<0.19 — the "
            "0.7.x range carries GHSA-3mwv-j45g-vp3w; do not install it)."
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

    `user_id` does not affect the *catalogue* — `get_raw_composio_tools`
    returns the same action list regardless of which user is authenticated.
    It does select whose enable/disable prefs are applied below, so the
    `enabled` field (and the agent-path filtering) is per-user.

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
    meta = toolkit_meta(toolkit_id)
    client = _composio()
    try:
        # limit=200 surfaces every action in the largest catalogues we expose
        # (Google Calendar ~48, Gmail ~30). Without this the SDK
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
    from services.composio import action_prefs as composio_action_prefs  # noqa: PLC0415
    from services.composio import categories as composio_categories  # noqa: PLC0415

    out: list[dict[str, Any]] = []
    for t in tools:
        slug = _attr(t, "slug", default="") or _attr(t, "name", default="")
        enabled = composio_action_prefs.is_action_enabled(toolkit_id, slug, user_id)
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


# Composio sessions persist server-side and the API documents no TTL, so a
# session we forget is a session that lives forever. Two consequences drive the
# store below:
#
#   1. The id is persisted to disk, not just held in memory. Composio's docs
#      say to "store the session ID and reuse it with composio.use()"; an
#      in-memory-only cache re-mints one session per user on every restart and
#      abandons the old one.
#   2. invalidate_session() deletes the remote session before dropping the id,
#      so the connect/disconnect path stops orphaning one session per toggle.
#
# The same file also holds each user's **agent proxy token** — the opaque id the
# loopback MCP proxy resolves back to a user. It is a random 256-bit value, not
# a signed one: there is nothing to forge and therefore no signing secret to
# configure. It is deliberately *stable per user* and outlives the session id,
# so an agent config written once stays valid across connector toggles and
# restarts; only the session behind it is re-minted.
#
# File shape:
#   {"version": 2,
#    "sessions":     {user_id: session_id},
#    "proxy_tokens": {proxy_token: user_id}}
# A v1 document (sessions only) is read and upgraded on the next write. Same
# lock + atomic-write discipline as action_prefs.py.
#
# This file DOES hold a secret now: a proxy token grants agent-level tool access
# for its user, so it is chmod 0600 and lives under data/ (gitignored).

_SESSIONS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "composio_sessions.json"

# In-process mirrors of the file. Populated on first use; kept in sync on write.
_SESSION_IDS: dict[str, str] = {}
_PROXY_TOKENS: dict[str, str] = {}   # proxy_token -> user_id
_SESSIONS_LOADED = False


def _load_store() -> tuple[dict[str, str], dict[str, str]]:
    """Read the persisted store as ``(sessions, proxy_tokens)``."""
    from services.cowork_agent.visualizer.reader import read_json  # noqa: PLC0415

    data = read_json(_SESSIONS_PATH)
    if not isinstance(data, dict):
        return {}, {}

    def _str_map(raw: object) -> dict[str, str]:
        if not isinstance(raw, dict):
            return {}
        return {
            str(k): str(v)
            for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, str) and k and v
        }

    # v1 had only "sessions"; the tokens map is simply absent and gets minted
    # on demand, so no migration step is required.
    return _str_map(data.get("sessions")), _str_map(data.get("proxy_tokens"))


def _ensure_sessions_loaded() -> None:
    global _SESSIONS_LOADED
    if _SESSIONS_LOADED:
        return
    try:
        sessions, tokens = _load_store()
        _SESSION_IDS.update(sessions)
        _PROXY_TOKENS.update(tokens)
    except Exception as exc:  # never let a bad cache file break tool access
        log.warning("composio: could not read session store: %s", exc)
    _SESSIONS_LOADED = True


def _write_store(mutate) -> None:
    """Apply ``mutate(sessions, proxy_tokens)`` under the file lock and save.

    Best-effort: a failure costs a re-mint on the next restart, never a failed
    request.
    """
    from services.cowork_agent.visualizer.atomic_write import write_json_atomic  # noqa: PLC0415
    from services.cowork_agent.visualizer.flock import locked  # noqa: PLC0415

    try:
        with locked(_SESSIONS_PATH):
            # Re-read inside the lock so a concurrent worker's entry survives.
            sessions, tokens = _load_store()
            mutate(sessions, tokens)
            write_json_atomic(
                _SESSIONS_PATH,
                {"version": 2, "sessions": sessions, "proxy_tokens": tokens},
            )
        # Tokens are credentials — keep the file owner-only.
        try:
            _SESSIONS_PATH.chmod(0o600)
        except OSError:
            pass
    except Exception as exc:
        log.warning("composio: could not persist session store: %s", exc)


def _persist_session_id(user_id: str, session_id: Optional[str]) -> None:
    """Write (or remove) one user's session id."""
    def _mutate(sessions: dict[str, str], _tokens: dict[str, str]) -> None:
        if session_id:
            sessions[user_id] = session_id
        else:
            sessions.pop(user_id, None)

    _write_store(_mutate)


def proxy_token_for_user(user_id: str) -> str:
    """Return this user's stable agent proxy token, minting one if needed.

    Opaque and random — the proxy resolves it by lookup, so a local process
    cannot construct another user's token, and no shared signing secret exists
    to manage or rotate.
    """
    uid = _require_user_id(user_id, "proxy_token_for_user")
    _ensure_sessions_loaded()
    for token, owner in _PROXY_TOKENS.items():
        if owner == uid:
            return token

    token = secrets.token_urlsafe(32)
    _PROXY_TOKENS[token] = uid

    def _mutate(_sessions: dict[str, str], tokens: dict[str, str]) -> None:
        # Another worker may have minted one first; prefer the persisted value.
        for existing, owner in tokens.items():
            if owner == uid:
                _PROXY_TOKENS.pop(token, None)
                _PROXY_TOKENS[existing] = uid
                return
        tokens[token] = uid

    _write_store(_mutate)
    # _mutate may have adopted a peer's token; return whatever we now hold.
    for tok, owner in _PROXY_TOKENS.items():
        if owner == uid:
            return tok
    return token


def user_for_proxy_token(token: str) -> Optional[str]:
    """Resolve an agent proxy token back to its user, or None if unknown."""
    if not token:
        return None
    _ensure_sessions_loaded()
    user_id = _PROXY_TOKENS.get(token)
    if user_id:
        return user_id
    # Miss: another worker may have minted it since we loaded. Re-read once.
    try:
        _, tokens = _load_store()
    except Exception:
        return None
    _PROXY_TOKENS.update(tokens)
    return _PROXY_TOKENS.get(token)


def _delete_remote_session(session_id: str, user_id: str) -> None:
    """Ask Composio to drop the session. Best-effort — an unreachable API must
    not block the local eviction that called us."""
    try:
        _composio().sessions.delete(session_id)
        log.info("composio: deleted session %s for user=%s", session_id, user_id)
    except Exception as exc:
        log.warning(
            "composio: could not delete session %s for user=%s (it may linger "
            "server-side): %s", session_id, user_id, exc,
        )


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
    """Evict this user's session. Call after any Connectors UI state change
    (connect / disconnect / status flip) so the next agent turn re-mints a
    session with the updated pin map.

    The remote session is deleted, not just forgotten: Composio sessions have
    no documented expiry, so dropping the id alone would leave one orphan per
    toggle. A no-op for an empty user_id — eviction is best-effort and never
    worth raising over.
    """
    if not user_id:
        return
    _ensure_sessions_loaded()
    session_id = _SESSION_IDS.pop(user_id, None)
    _persist_session_id(user_id, None)
    if session_id:
        _delete_remote_session(session_id, user_id)


def get_session(user_id: str):
    """Return Composio's session object for `user_id`.

    Reuses an existing session (`composio.use(session_id)`) when we have its id
    from a previous call, otherwise mints a new one
    (`composio.create(user_id=…, connected_accounts=…)`) with the user's ACTIVE
    Connected Accounts pinned.

    The id survives restarts (see the session store above), so a bounce reuses
    the same session rather than abandoning it. An id the API no longer
    recognises is discarded and replaced.
    """
    user_id = _require_user_id(user_id, "get_session")
    _ensure_sessions_loaded()
    sid = _SESSION_IDS.get(user_id)
    if sid:
        try:
            return _composio().use(sid)
        except Exception as exc:
            log.debug("composio: use(%s) failed for user=%s: %s", sid, user_id, exc)
            # Stale id — Composio has already forgotten it, so there is nothing
            # to delete. Drop it locally and fall through to create.
            _SESSION_IDS.pop(user_id, None)
            _persist_session_id(user_id, None)
    create_kwargs: dict[str, Any] = {"user_id": user_id}
    pinned = _pinned_connected_accounts(user_id)
    if pinned:
        create_kwargs["connected_accounts"] = pinned
    session = _composio().create(**create_kwargs)
    new_id = getattr(session, "session_id", None) or getattr(session, "id", None)
    if new_id:
        _SESSION_IDS[user_id] = str(new_id)
        _persist_session_id(user_id, str(new_id))
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
        _SESSION_IDS.get(user_id, "?"), user_id, url,
    )
    return entry


# ---------------------------------------------------------------------------
# Gateway install — openclaw / hermes
# ---------------------------------------------------------------------------
#
# Every runtime points at xo-cowork-api's loopback MCP proxy, never at Composio
# directly, so COMPOSIO_API_KEY is never written to an agent config file. The
# proxy injects it from .env at request time (services/composio/mcp_proxy.py).
#
# The URL carries an opaque per-user token — an MCP client calls the proxy with
# no headers, so the path is the only place identity can ride. The proxy looks
# the token up (no signature, no shared secret) and uses that user's session.
#
# Two layers of isolation, and the outer one is Composio's: the token selects a
# user, and `sessions.create(user_id=...)` already bound that user's session to
# their connected accounts.


def _cowork_proxy_url(user_id: str | None = None) -> str:
    """Loopback URL of the MCP proxy, scoped to ``user_id``.

    ``/mcp/cowork-proxy/u/<opaque-token>``. The token is stable per user, so a
    config written once survives connector toggles and restarts — only the
    Composio session behind it is re-minted.
    """
    uid = _require_user_id(user_id, "_cowork_proxy_url")
    port = int(os.getenv("PORT", "5002"))
    return f"http://127.0.0.1:{port}/mcp/cowork-proxy/u/{proxy_token_for_user(uid)}"


def install_into_gateway(user_id: str, agent: str) -> dict[str, Any]:
    """Write this user's cowork MCP entry into ``agent``'s gateway config.

    Core stays agent-agnostic: the per-agent config format and path live in
    that agent's ``mcp_install`` capability
    (``services/cowork_agent/adapters/<agent>/mcp_install.py``), resolved here
    through the loader. An agent that ships no such module simply does not
    support gateway-side MCP install — claude_code, for one, takes a
    per-session ``--mcp-config`` instead and needs nothing written.

    Returns the capability's ``{ok, ...}`` result, or an ``ok: False`` shape
    when the agent has no gateway install path.
    """
    from services.cowork_agent.adapters.loader import try_load_capability

    mod = try_load_capability("mcp_install", agent=agent)
    if mod is None or not hasattr(mod, "install"):
        return {
            "ok": False,
            "error": (
                f"Agent '{agent}' does not support gateway MCP install "
                "(no mcp_install capability)."
            ),
        }
    try:
        proxy_url = _cowork_proxy_url(user_id)
    except Exception as exc:
        # No user id. Report it as a normal not-ok result (the route turns it
        # into a 422) rather than a 500 — actionable configuration, not a crash.
        log.warning("composio: gateway install could not build proxy URL: %s", exc)
        return {"ok": False, "error": str(exc)}
    return mod.install(proxy_url)


def gateway_install_agents() -> list[str]:
    """Names of installed adapters that expose an ``mcp_install`` capability.

    Used by the refresh-gateway route to validate its ``agent`` parameter
    without naming any agent in core.
    """
    from services.cowork_agent.adapters.loader import try_load_capability
    from services.cowork_agent.registry.adapter_registry import list_adapters

    out: list[str] = []
    for name in list_adapters():
        mod = try_load_capability("mcp_install", agent=name)
        if mod is not None and hasattr(mod, "install"):
            out.append(name)
    return sorted(out)
