"""
Hermes models-status view.

Returns the same openclaw-shaped envelope (`{default, models[id,status]}`)
plus a hermes-specific `fallback_providers` field, derived from `hermes dump`.

Status mapping:
- Provider's api_key == "set" → "ok"
- Anything else (or provider absent) → "error"

Hermes exposes no expiry information, so "warn" never appears.
"""

from __future__ import annotations

from typing import Any

from services.cowork_agent.adapters.hermes.dump import (
    HermesStatusError,
    fetch_dump,
)


def _status_for_provider(provider: str, api_keys: dict[str, Any]) -> str:
    raw = api_keys.get(provider) if isinstance(api_keys, dict) else None
    return "ok" if isinstance(raw, str) and raw.strip().lower() == "set" else "error"


def _format_model_id(provider: str, model: str) -> str:
    provider = (provider or "").strip()
    model = (model or "").strip()
    if not provider and not model:
        return ""
    if not provider:
        return model
    return f"{provider}/{model}"


def build_status_view(dump: dict[str, Any]) -> dict[str, Any]:
    """Translate a parsed `hermes dump` dict into the openclaw status shape."""
    provider = (dump.get("provider") or "").strip()
    model = (dump.get("model") or "").strip()
    default_id = _format_model_id(provider, model) or None

    api_keys = dump.get("api_keys") or {}
    overrides = dump.get("config_overrides") or {}
    fallbacks_raw = overrides.get("fallback_providers") or []

    # Normalise the fallback list — accept the structured form hermes emits,
    # tolerate the occasional missing field.
    fallback_providers: list[dict[str, str]] = []
    if isinstance(fallbacks_raw, list):
        for entry in fallbacks_raw:
            if not isinstance(entry, dict):
                continue
            fp_provider = (entry.get("provider") or "").strip()
            fp_model = (entry.get("model") or "").strip()
            if not fp_provider and not fp_model:
                continue
            fallback_providers.append({"provider": fp_provider, "model": fp_model})

    # Models list: current default first, then each fallback. Dedupe by id so
    # we never emit the same model twice.
    seen: set[str] = set()
    models: list[dict[str, str]] = []
    if default_id:
        models.append({
            "id": default_id,
            "status": _status_for_provider(provider, api_keys),
        })
        seen.add(default_id)
    for fp in fallback_providers:
        mid = _format_model_id(fp["provider"], fp["model"])
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append({
            "id": mid,
            "status": _status_for_provider(fp["provider"], api_keys),
        })

    return {
        "default": default_id,
        "models": models,
        "fallback_providers": fallback_providers,
    }


async def get_models_status(timeout: float | None = None) -> dict[str, Any]:
    """Fetch `hermes dump` and project it into the openclaw models-status shape."""
    if timeout is None:
        dump = await fetch_dump()
    else:
        dump = await fetch_dump(timeout=timeout)
    return build_status_view(dump)


__all__ = ["HermesStatusError", "build_status_view", "get_models_status"]
