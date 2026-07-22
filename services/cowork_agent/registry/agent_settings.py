"""
Helpers for the **active agent's** JSON ``config_file`` (e.g. settings.json).

Parallel to ``agent_env.py``, but for the coding CLI's own JSON settings file
rather than a ``.env``. Some providers are configured not by writing the
agent's ``.env`` but by merging an ``env`` block into the CLI's settings file,
which the CLI reads at launch (gateway-style API keys are the motivating case).

This module performs that merge/clear/read generically: it only assumes the
target is a JSON file with a top-level ``env`` object. It never names an agent
or a provider — callers pass the ``config_file`` path (resolved from the active
manifest) and the env dict to write.
"""

from __future__ import annotations

import json
from pathlib import Path


def _load_json_obj(path: Path) -> dict:
    """Return the parsed JSON object at ``path``, or ``{}`` if absent/empty.

    Raises ``ValueError`` if the file exists but does not hold a JSON object,
    so a merge never silently clobbers a file that isn't the shape we expect.
    ``json.JSONDecodeError`` from malformed content propagates to the caller.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(
            f"{path} does not contain a JSON object (found {type(data).__name__})"
        )
    return data


def _write_json_obj(path: Path, data: dict) -> None:
    """Write ``data`` as pretty JSON, creating parent dirs; best-effort mode 600.

    The file now carries an auth token, so we tighten permissions where the
    platform supports it (``chmod`` is a no-op / raises on some systems — we
    don't treat that as fatal).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def merge_settings_env(config_file: Path, env: dict[str, str]) -> None:
    """Deep-merge ``env`` into the ``env`` object of a JSON settings file.

    Every other top-level key and every existing ``env`` entry not named in
    ``env`` is preserved. Empty-string values are written as-is: some CLIs
    require an explicitly empty var (e.g. ``ANTHROPIC_API_KEY=""``) to disable a
    competing auth source, so we must not drop them.
    """
    data = _load_json_obj(config_file)
    current = data.get("env")
    if not isinstance(current, dict):
        current = {}
    current.update(env)
    data["env"] = current
    _write_json_obj(config_file, data)


def clear_settings_env(config_file: Path, keys: list[str]) -> None:
    """Remove ``keys`` from the settings file's ``env`` object.

    No-op if the file, the ``env`` object, or the keys are absent. Drops the
    ``env`` object entirely once empty so no stub is left behind.
    """
    if not config_file.exists():
        return
    data = _load_json_obj(config_file)
    env = data.get("env")
    if not isinstance(env, dict):
        return
    removed = False
    for key in keys:
        if key in env:
            del env[key]
            removed = True
    if not removed:
        return
    if env:
        data["env"] = env
    else:
        data.pop("env", None)
    _write_json_obj(config_file, data)


def read_settings_env(config_file: Path) -> dict[str, str]:
    """Return the settings file's ``env`` object as ``{str: str}``, or ``{}``.

    Best-effort for status probes: any read/parse problem yields ``{}`` rather
    than raising, since the probe drives a UI tile, not an operation.
    """
    try:
        data = _load_json_obj(config_file)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    env = data.get("env")
    if not isinstance(env, dict):
        return {}
    return {str(k): str(v) for k, v in env.items()}


__all__ = ["merge_settings_env", "clear_settings_env", "read_settings_env"]
