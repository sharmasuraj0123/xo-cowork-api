"""
The status sources streamed by `GET /api/status/stream` (v1).

Two categories:

* ``models`` — model-provider connection state, taken from the active agent's
  ``providers_status`` capability. Resolved through the capability loader, so
  this module never names a backend and an agent without that capability simply
  contributes an empty category instead of failing the stream.
* ``data`` — external service connectors (github, vercel, gdrive, onedrive).

Every probe is projected down to the minimum the frontend renders — a
``connected`` boolean or a ``status`` string. The upstream calls return more
(usernames, avatar URLs, scopes, tokens), and none of it belongs on a feed that
re-sends every few seconds: it is bandwidth, it is noise, and some of it is
sensitive. The detail endpoints (``/providers/status``,
``/api/connectors/*/status``) still exist for when a screen actually needs it.

Intervals are set from what a probe costs, not from how fresh we would like it
to be. A change the user *causes* is picked up immediately by the invalidation
middleware (see ``owns``), so these intervals only bound how long external
drift can go unnoticed:

    providers  30s   subprocess (an ``auth status`` CLI call)
    github     60s   HTTPS round-trip, 5000/hour rate limit
    vercel     60s   HTTPS round-trip
    gdrive     60s   ``rclone`` subprocess
    onedrive   60s   ``rclone`` subprocess

Adding a connector here is one ``register()`` call — no change to the collector,
the route, or the payload assembly. ``manus`` is deliberately not registered: it
exists as a connector but was not in the requested v1 payload, and adding it is
a one-liner when it is wanted.
"""

from __future__ import annotations

from typing import Any

from services.cowork_agent.adapters.loader import try_load_capability

from .registry import StatusSource, get, register

#: Probe intervals in seconds, keyed by source name (see module docstring).
INTERVALS = {
    "providers": 30.0,
    "github": 60.0,
    "vercel": 60.0,
    "gdrive": 60.0,
    "onedrive": 60.0,
}


# ── models ───────────────────────────────────────────────────────────────────


async def probe_providers() -> dict[str, Any]:
    """Model-provider connection state for the active agent.

    Flattens the capability's ``oauth`` and ``api_keys`` blocks into one map of
    ``{provider: {"connected": bool}}``. Both blocks answer the same question —
    "can this workspace reach that model?" — so the stream presents them as one
    dict; ``/providers/status`` remains the place to see which mechanism a
    provider uses.

    A missing capability yields ``{}``: an agent with no provider-status source
    contributes nothing rather than breaking the other categories. That matches
    the routers' existing rule of degrading an optional-but-absent capability to
    an empty shape rather than a 501.
    """
    module = try_load_capability("providers_status")
    if module is None:
        return {}

    result = await module.get_providers_status()
    if not isinstance(result, dict):
        raise TypeError(f"providers_status returned {type(result).__name__}, expected a mapping")

    merged: dict[str, Any] = {}
    for block in ("oauth", "api_keys"):
        section = result.get(block)
        if not isinstance(section, dict):
            continue
        for provider, value in section.items():
            if isinstance(value, dict):
                merged[provider] = {"connected": bool(value.get("connected"))}
    return merged


# ── data: token-based connectors ─────────────────────────────────────────────


def _status_only(name: str, result: Any) -> dict[str, Any]:
    """Project a connector status dict down to ``{name: {"status": ...}}``.

    Raises when the upstream result carries no ``status``, which the collector
    treats as a failed probe — keeping the previous value instead of inventing
    one. Guessing ``needs_auth`` from a malformed result would show the user a
    disconnect that never happened.
    """
    if not isinstance(result, dict) or not result.get("status"):
        raise ValueError(f"{name} status probe returned no status field")
    return {name: {"status": result["status"]}}


async def probe_github() -> dict[str, Any]:
    from services.cowork_agent.connectors import github_connector

    return _status_only("github", await github_connector.get_status())


async def probe_vercel() -> dict[str, Any]:
    from services.cowork_agent.connectors import vercel_connector

    return _status_only("vercel", await vercel_connector.get_status())


# ── data: rclone-backed connectors ───────────────────────────────────────────


async def probe_gdrive() -> dict[str, Any]:
    """Google Drive has no status endpoint, so derive it from configured remotes.

    ``connected`` means rclone is reachable and at least one Drive remote exists.
    An unreachable rclone raises rather than reporting ``needs_auth``: "we cannot
    ask" is not the same as "the user is not connected", and only one of those
    should ever turn the UI red.
    """
    from services.cowork_agent.connectors.gdrive_rclone import (
        list_drive_remotes,
        rclone_available,
    )

    if not await rclone_available():
        raise RuntimeError("rclone unavailable")
    remotes = await list_drive_remotes()
    return {"gdrive": {"status": "connected" if remotes else "needs_auth"}}


async def probe_onedrive() -> dict[str, Any]:
    """OneDrive equivalent of :func:`probe_gdrive`."""
    from services.cowork_agent.connectors.onedrive_rclone import (
        list_onedrive_remotes,
        rclone_available,
    )

    if not await rclone_available():
        raise RuntimeError("rclone unavailable")
    remotes = await list_onedrive_remotes()
    return {"onedrive": {"status": "connected" if remotes else "needs_auth"}}


# ── registration ─────────────────────────────────────────────────────────────

#: ``owns`` prefixes per source: a successful non-GET request under any of them
#: means that source is stale and is re-probed at once.
_DEFAULTS: tuple[StatusSource, ...] = (
    StatusSource(
        category="models",
        name="providers",
        interval=INTERVALS["providers"],
        probe=probe_providers,
        # Provider keys are set/cleared here; the two CLI auth-setup routers
        # complete an OAuth login, which also flips connection state.
        owns=("/api/config/providers", "/claude", "/codex"),
    ),
    StatusSource(
        category="data",
        name="github",
        interval=INTERVALS["github"],
        probe=probe_github,
        owns="/api/connectors/github",
    ),
    StatusSource(
        category="data",
        name="vercel",
        interval=INTERVALS["vercel"],
        probe=probe_vercel,
        owns="/api/connectors/vercel",
    ),
    StatusSource(
        category="data",
        name="gdrive",
        interval=INTERVALS["gdrive"],
        probe=probe_gdrive,
        owns="/api/connectors/gdrive",
    ),
    StatusSource(
        category="data",
        name="onedrive",
        interval=INTERVALS["onedrive"],
        probe=probe_onedrive,
        owns="/api/connectors/onedrive",
    ),
)


def register_default_sources() -> None:
    """Register the v1 sources. Idempotent, so a repeated startup is harmless."""
    for source in _DEFAULTS:
        if get(source.name) is None:
            register(source)


__all__ = [
    "INTERVALS",
    "probe_gdrive",
    "probe_github",
    "probe_onedrive",
    "probe_providers",
    "probe_vercel",
    "register_default_sources",
]
