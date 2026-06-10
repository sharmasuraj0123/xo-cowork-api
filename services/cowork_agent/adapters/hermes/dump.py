"""
Hermes dump parser.

`hermes dump` prints a fixed-format text block (not JSON). We parse it into a
nested dict so the per-section view builders can derive the same status shapes
the openclaw adapters return.

Three line shapes only:

1. ``key: value`` at column 0 → top-level scalar (e.g. ``model: kimi-k2.5``).
2. ``section_name:`` at column 0 followed by indented lines → nested dict
   (``api_keys``, ``features``, ``config_overrides``).
3. ``--- hermes dump ---`` / ``--- end dump ---`` framing → skipped.

Indented values that look like Python literals (``[...]``, ``{...}``, ``True``,
``False``, ints) are coerced via ``ast.literal_eval`` so ``fallback_providers``
comes back as a list of dicts instead of a raw string.

The CLI invocation mirrors the openclaw status adapter contract:

- ``HERMES_BIN`` env override; PATH lookup otherwise.
- 30s default timeout.
- Errors raise ``HermesStatusError`` with a code that the router maps to HTTP
  status (``binary_not_found`` → 503, ``timeout`` → 504, ``execution_failed`` →
  502, ``invalid_output`` → 502).
"""

from __future__ import annotations

import ast
import re
from typing import Any, Optional

from services.cowork_agent.adapters.cli_status import (
    CliStatusError as HermesStatusError,
    resolve_binary,
    run_cli,
)

HERMES_BIN_ENV = "HERMES_BIN"
DEFAULT_BIN = "hermes"
DEFAULT_TIMEOUT_SECONDS = 30.0

_FRAMING_RE = re.compile(r"^---\s*(hermes dump|end dump)\s*---\s*$")
# A top-level scalar / section-header line: "name:" possibly followed by a value.
# At column 0, the leading word can include letters, digits, and underscores.
_TOPLEVEL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):\s*(.*)$")
# Indented "key value" line. Hermes uses padded columns:
# `  openrouter           not set`. Treat the first run of non-whitespace as the
# key and the rest as the value.
_INDENTED_RE = re.compile(r"^\s+(\S+)\s*(.*)$")


def _coerce(value: str) -> Any:
    """Try Python-literal coercion (handles lists/dicts/bools/ints).
    Falls through to the raw string for plain text like ``set`` / ``not set``.
    """
    stripped = value.strip()
    if not stripped:
        return ""
    # ``ast.literal_eval`` accepts only the safe subset (no calls). Anything that
    # isn't a literal expression raises and we keep the original string.
    try:
        return ast.literal_eval(stripped)
    except (ValueError, SyntaxError):
        return stripped


def parse_dump(text: str) -> dict[str, Any]:
    """Parse `hermes dump` output into a nested dict.

    Unknown line shapes are skipped silently — the dump format is intentionally
    plain-text and may evolve. Callers should treat missing keys as ``None``.
    """
    result: dict[str, Any] = {}
    current_section: Optional[dict[str, Any]] = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            current_section = None  # blank line ends a section block
            continue
        if _FRAMING_RE.match(line):
            current_section = None
            continue

        # Indented? Part of the most recent section.
        if line.startswith((" ", "\t")):
            if current_section is None:
                continue
            m = _INDENTED_RE.match(line)
            if not m:
                continue
            key, value = m.group(1), m.group(2)
            # `api_keys:` uses column-aligned `key   value` (no colon);
            # `features:` / `config_overrides:` use `key: value`. Normalise so
            # downstream consumers see the same key shape in both cases.
            key = key.rstrip(":")
            current_section[key] = _coerce(value)
            continue

        # Column-0: either a top-level scalar or the opening of a section.
        m = _TOPLEVEL_RE.match(line)
        if not m:
            current_section = None
            continue
        key, value = m.group(1), m.group(2)
        if value:
            # `key: value` on a single line → top-level scalar.
            result[key] = _coerce(value)
            current_section = None
        else:
            # `section_name:` followed by indented lines.
            section: dict[str, Any] = {}
            result[key] = section
            current_section = section

    return result


async def fetch_dump(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run `hermes dump` and return the parsed nested dict.

    Mirrors the openclaw status adapters' error contract so the unified
    `/models/status` and `/channels/status` routes can catch a single class.
    """
    binary = resolve_binary(HERMES_BIN_ENV, DEFAULT_BIN)
    result = await run_cli(binary, ("dump",), timeout=timeout, label="hermes")

    out = result.stdout
    err = result.stderr

    if result.returncode != 0:
        raise HermesStatusError(
            f"hermes exited with code {result.returncode}",
            code="execution_failed",
            detail=err or out[:300] or None,
        )

    parsed = parse_dump(out)
    if not parsed:
        raise HermesStatusError(
            "hermes dump returned no parseable content",
            code="invalid_output",
            detail=(out or err)[:300] or None,
        )
    return parsed
