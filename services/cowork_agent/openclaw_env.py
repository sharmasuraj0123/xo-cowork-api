import warnings
warnings.warn(
    "openclaw_env.py is deprecated. Use AgentDispatcher from dispatcher.py.",
    DeprecationWarning,
    stacklevel=2,
)

"""
Helpers for `~/.openclaw/.env`.

Two access patterns live on top of this file:

* `routes/secrets.py` — whole-file read/write (the Settings → Env Vars UI).
  Uses `load_env_entries` / `save_env_entries`, which operate on a
  `[{key, value}, ...]` view and do NOT preserve comments or blank lines
  (the UI has no representation for them).
* `routes/config_routes.py` — single-key upsert (the onboarding "Save Key"
  flow). Uses `upsert_env_entry`, a line-level edit that preserves
  comments, blank lines, and every other entry untouched.
"""

import re
from pathlib import Path

from services.cowork_agent.agent_registry import get_default_agent

ENV_FILE: Path = get_default_agent().env_file


def parse_env_file(text: str) -> list[dict]:
    """Parse a .env file into a list of {key, value} dicts (skips blanks and comments)."""
    entries: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            entries.append({"key": key.strip(), "value": value.strip()})
    return entries


def serialize_env_file(entries: list[dict]) -> str:
    """Serialize a list of {key, value} dicts back to .env file text."""
    lines = [f"{e['key']}={e['value']}" for e in entries if e.get("key", "").strip()]
    return "\n".join(lines) + ("\n" if lines else "")


def load_env_entries() -> list[dict]:
    """Return the current entries, or [] if the file does not yet exist."""
    if not ENV_FILE.exists():
        return []
    return parse_env_file(ENV_FILE.read_text(errors="replace"))


def save_env_entries(entries: list[dict]) -> None:
    """Overwrite the .env file with the provided entries (creates parent dirs)."""
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(serialize_env_file(entries))


def upsert_env_entry(key: str, value: str) -> None:
    """Insert or replace a single key=value pair with a line-level edit.

    Scans lines for the key NAME only (never inspects or string-matches the
    existing value). Commented-out rows (`#` as the first non-whitespace
    char) are skipped so they act as examples, not live entries. Everything
    else in the file — comments, blank lines, unrelated keys, original
    ordering, line endings — is preserved.

    If the file has duplicate rows for the same key (already a broken
    state), only the first live occurrence is replaced.
    """
    key = key.strip()
    if not key:
        raise ValueError("env key cannot be empty")

    new_line = f"{key}={value}"
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not ENV_FILE.exists():
        ENV_FILE.write_text(new_line + "\n")
        return

    # `newline=""` disables universal-newlines translation so CRLF/CR files
    # round-trip unchanged. `splitlines(keepends=True)` then preserves the
    # original terminator on each line. (`Path.read_text` only grew the
    # `newline` kwarg in 3.13, so open the file explicitly.)
    with ENV_FILE.open("r", errors="replace", newline="") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)

    # Anchor on the key name followed by optional whitespace and '='.
    # `re.escape` guards against regex metachars in the key; the trailing
    # `[ \t]*=` prevents prefix matches (`FOO` must not match `FOO_BAR=`).
    key_pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=")

    for i, line in enumerate(lines):
        # Skip commented rows — `# FOO=...` is documentation, not an entry.
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
            ENV_FILE.write_text("".join(lines))
            return

    # Not found — append. Ensure the previous content ends with a newline
    # so the new entry lands on its own line.
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] = lines[-1] + "\n"
    lines.append(new_line + "\n")
    ENV_FILE.write_text("".join(lines))
