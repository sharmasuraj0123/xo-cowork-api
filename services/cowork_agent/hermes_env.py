"""
Single-key upsert against ``~/.hermes/.env``.

Mirrors the line-level edit logic of ``openclaw_env.upsert_env_entry`` but
anchored explicitly to the **hermes** manifest (``get_agent("hermes")``),
not the active default agent. This is deliberate: a route named
``/api/config/hermes/...`` must always target ``~/.hermes/.env`` regardless
of which backend is currently active. Re-anchoring the env file by the
active default would mean an openclaw-named route could overwrite hermes
secrets when ``DEFAULT_AGENT=hermes`` (and vice versa) — the same foot-gun
the settings-module re-anchoring closed for the OPENCLAW_* constants.
"""
from __future__ import annotations

import re
from pathlib import Path

from services.cowork_agent.agent_registry import get_agent

_HERMES_ENV_FILE: Path = get_agent("hermes").env_file


def upsert_hermes_env_entry(key: str, value: str) -> None:
    """Insert or replace a single ``key=value`` pair in ``~/.hermes/.env``.

    Scans lines for the key NAME only (never inspects the existing value).
    Commented-out rows (``#`` as the first non-whitespace char) are skipped
    so they act as examples, not live entries. Everything else — comments,
    blank lines, unrelated keys, original ordering, line endings — is
    preserved untouched.

    If the file has duplicate live rows for the same key (already a broken
    state), only the first occurrence is replaced.
    """
    key = key.strip()
    if not key:
        raise ValueError("env key cannot be empty")

    new_line = f"{key}={value}"
    _HERMES_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not _HERMES_ENV_FILE.exists():
        _HERMES_ENV_FILE.write_text(new_line + "\n")
        return

    # `newline=""` disables universal-newlines translation so CRLF/CR
    # files round-trip unchanged. `splitlines(keepends=True)` then
    # preserves the original terminator on each line.
    with _HERMES_ENV_FILE.open("r", errors="replace", newline="") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)

    # Anchor on the key name followed by optional whitespace and '='.
    # `re.escape` guards against regex metachars in the key; the trailing
    # `[ \t]*=` prevents prefix matches (``FOO`` mustn't match ``FOO_BAR=``).
    key_pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=")

    for i, line in enumerate(lines):
        # Skip commented rows — ``# FOO=...`` is documentation, not an entry.
        if line.lstrip().startswith("#"):
            continue
        if key_pattern.match(line):
            trailing = ""
            if line.endswith("\r\n"):
                trailing = "\r\n"
            elif line.endswith("\n"):
                trailing = "\n"
            elif line.endswith("\r"):
                trailing = "\r"
            lines[i] = new_line + trailing
            _HERMES_ENV_FILE.write_text("".join(lines))
            return

    # Not found — append. Ensure the previous content ends with a newline
    # so the new entry lands on its own line.
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] = lines[-1] + "\n"
    lines.append(new_line + "\n")
    _HERMES_ENV_FILE.write_text("".join(lines))
