"""
OpenClaw Models Status

Runs `openclaw models status --json` and returns a minimal {default, models[]}
view. Per-model status is derived from provider auth state, oauth profile
expiry, and unusableProfiles.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from services.cowork_agent.adapters.cli_status import (
    CliStatusError as OpenclawStatusError,
    resolve_binary,
    run_cli,
)

OPENCLAW_BIN_ENV = "OPENCLAW_BIN"
DEFAULT_BIN = "openclaw"
DEFAULT_TIMEOUT_SECONDS = 15.0


async def fetch_raw_status(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Dict[str, Any]:
    binary = resolve_binary(OPENCLAW_BIN_ENV, DEFAULT_BIN)
    result = await run_cli(
        binary, ("models", "status", "--json"), timeout=timeout, label="openclaw"
    )

    if result.returncode != 0:
        raise OpenclawStatusError(
            f"openclaw exited with code {result.returncode}",
            code="execution_failed",
            detail=result.stderr or None,
        )

    text = result.stdout
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError as e:
        raise OpenclawStatusError("openclaw returned invalid JSON", code="invalid_json", detail=str(e))

    if not isinstance(parsed, dict):
        raise OpenclawStatusError("openclaw returned empty or non-object output", code="invalid_json")

    return parsed


def _status_for_provider(
    provider: str,
    providers_idx: Dict[str, Dict[str, Any]],
    oauth_idx: Dict[str, List[Dict[str, Any]]],
    unusable_ids: set,
    missing_in_use: List[str],
) -> str:
    """Strict status derivation. error > warn > ok."""
    if provider and provider in (missing_in_use or []):
        return "error"

    entry = providers_idx.get(provider)
    if entry is None:
        # Provider has no record in the auth store at all.
        return "error" if provider else "ok"
    if (entry.get("effective") or {}).get("kind") == "missing":
        return "error"

    profiles = oauth_idx.get(provider, [])
    if profiles:
        statuses = [(p.get("status") or "").lower() for p in profiles]
        if all(s in ("expired", "missing") for s in statuses):
            return "error"
        if all((p.get("profileId") or "") in unusable_ids for p in profiles):
            return "error"
        if any(s == "expiring" for s in statuses):
            return "warn"
        if any((p.get("profileId") or "") in unusable_ids for p in profiles):
            return "warn"
    return "ok"


def build_status_view(raw: Dict[str, Any]) -> Dict[str, Any]:
    auth = raw.get("auth") or {}
    providers_idx = {p.get("provider"): p for p in (auth.get("providers") or []) if p.get("provider")}
    oauth_idx: Dict[str, List[Dict[str, Any]]] = {}
    for p in (auth.get("oauth") or {}).get("profiles") or []:
        if p.get("provider"):
            oauth_idx.setdefault(p["provider"], []).append(p)
    unusable_ids = {u.get("profileId") for u in auth.get("unusableProfiles") or [] if u.get("profileId")}
    missing_in_use = auth.get("missingProvidersInUse") or []

    default_id = raw.get("resolvedDefault") or raw.get("defaultModel") or ""

    seen: set = set()
    models: List[Dict[str, str]] = []
    for mid in [default_id, *(raw.get("fallbacks") or []), *(raw.get("allowed") or [])]:
        if not mid or mid in seen:
            continue
        seen.add(mid)
        provider = mid.split("/", 1)[0] if "/" in mid else ""
        models.append({
            "id": mid,
            "status": _status_for_provider(provider, providers_idx, oauth_idx, unusable_ids, missing_in_use),
        })

    return {"default": default_id or None, "models": models}


async def get_models_status(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Dict[str, Any]:
    return build_status_view(await fetch_raw_status(timeout=timeout))
