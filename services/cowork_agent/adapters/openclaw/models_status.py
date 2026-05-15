"""
OpenClaw Models Status

Runs `openclaw models status --json` and returns a minimal {default, models[]}
view. Per-model status is derived from provider auth state, oauth profile
expiry, and unusableProfiles.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Any, Dict, List, Optional

OPENCLAW_BIN_ENV = "OPENCLAW_BIN"
DEFAULT_BIN = "openclaw"
DEFAULT_TIMEOUT_SECONDS = 15.0


class OpenclawStatusError(Exception):
    """CLI invocation/parse failure. `code` maps to an HTTP status in the router."""

    def __init__(self, message: str, *, code: str, detail: Optional[str] = None):
        super().__init__(message)
        self.code = code  # binary_not_found | timeout | execution_failed | invalid_json
        self.detail = detail


def _resolve_binary() -> str:
    configured = (os.getenv(OPENCLAW_BIN_ENV, "") or "").strip()
    return configured or shutil.which(DEFAULT_BIN) or DEFAULT_BIN


async def fetch_raw_status(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Dict[str, Any]:
    binary = _resolve_binary()
    if os.path.isabs(binary) and not os.path.isfile(binary):
        raise OpenclawStatusError(
            f"openclaw binary not found at {binary}", code="binary_not_found", detail=binary
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "models", "status", "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as e:
        raise OpenclawStatusError(
            f"openclaw binary unavailable: {binary}", code="binary_not_found", detail=str(e)
        )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        raise OpenclawStatusError(f"openclaw timed out after {timeout}s", code="timeout")

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        raise OpenclawStatusError(
            f"openclaw exited with code {proc.returncode}",
            code="execution_failed",
            detail=err or None,
        )

    text = (stdout or b"").decode("utf-8", errors="replace").strip()
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
