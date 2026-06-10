"""
Parametric `.env` I/O for any hermes profile dir.

``hermes_env.py`` is hard-anchored to the default profile (``~/.hermes/.env``)
because its only caller used to be the default-scoped onboarding flow. The
per-profile endpoints under ``/api/agents/hermes/{profile}/...`` need the
same upsert / load / serialize logic but against ``<profile>/.env``, so
this module exposes the file-path as an argument.

The line-level upsert preserves comments, blank lines, ordering, and CRLF
endings exactly like ``upsert_hermes_env_entry``. The parser mirrors the
shape used by ``openclaw_env`` so /api/secrets/env consumers can keep the
same entries list shape across the agent-agnostic and per-profile routes.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict


class EnvEntry(TypedDict):
    key: str
    value: str


def _key_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=")


def upsert_env_entry(env_file: Path, key: str, value: str) -> None:
    """Insert or replace ``KEY=value`` in ``env_file``. Idempotent.

    Skips commented lines so example rows survive untouched. Preserves the
    file's existing line endings (CRLF/CR/LF) on the replaced row.
    """
    key = key.strip()
    if not key:
        raise ValueError("env key cannot be empty")

    new_line = f"{key}={value}"
    env_file.parent.mkdir(parents=True, exist_ok=True)

    if not env_file.exists():
        env_file.write_text(new_line + "\n")
        return

    with env_file.open("r", errors="replace", newline="") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)

    pattern = _key_pattern(key)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if pattern.match(line):
            trailing = ""
            if line.endswith("\r\n"):
                trailing = "\r\n"
            elif line.endswith("\n"):
                trailing = "\n"
            elif line.endswith("\r"):
                trailing = "\r"
            lines[i] = new_line + trailing
            env_file.write_text("".join(lines))
            return

    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] = lines[-1] + "\n"
    lines.append(new_line + "\n")
    env_file.write_text("".join(lines))


def delete_env_entry(env_file: Path, key: str) -> bool:
    """Remove the live row for ``key`` from ``env_file``. Returns True iff
    a row was removed. Comments referencing the key (``# KEY=...``) are
    preserved.
    """
    key = key.strip()
    if not key or not env_file.is_file():
        return False
    with env_file.open("r", errors="replace", newline="") as f:
        lines = f.read().splitlines(keepends=True)

    pattern = _key_pattern(key)
    changed = False
    out: list[str] = []
    for line in lines:
        if line.lstrip().startswith("#"):
            out.append(line)
            continue
        if pattern.match(line):
            changed = True
            continue
        out.append(line)
    if changed:
        env_file.write_text("".join(out))
    return changed


def parse_env_entries(text: str) -> list[EnvEntry]:
    """Parse env-file text into a list of ``{key, value}`` entries.

    Commented lines and blanks are skipped (they stay in the file but
    don't surface as entries). Values keep surrounding quotes stripped
    so the FE sees the same string the gateway will read.
    """
    out: list[EnvEntry] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        out.append({"key": key, "value": value})
    return out


def load_env_entries(env_file: Path) -> list[EnvEntry]:
    if not env_file.is_file():
        return []
    try:
        return parse_env_entries(env_file.read_text(errors="replace"))
    except OSError:
        return []


def list_env_keys(env_file: Path) -> list[str]:
    """Keys-only view — no secret material returned. Mirrors
    ``/api/secrets/env/keys`` for onboarding-style checks."""
    return [e["key"] for e in load_env_entries(env_file) if e.get("value")]


def save_env_entries(env_file: Path, entries: list[EnvEntry]) -> None:
    """Atomic-ish full rewrite for the bulk PUT case (used by
    ``/api/agents/hermes/{profile}/secrets`` PUT). Existing comments and
    blank lines are not preserved — callers using this should be doing a
    full-file replacement; for surgical edits use ``upsert_env_entry``.
    """
    env_file.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{e['key']}={e['value']}" for e in entries)
    if body:
        body += "\n"
    env_file.write_text(body)
