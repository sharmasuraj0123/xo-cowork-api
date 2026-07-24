"""
Codex models-status view (Twin A — CLI exit-code probe).

Mirrors claude_code's ``models_status`` envelope
(``{default, models:[{id, status}]}``) but keys off the CLI **exit code**, not
JSON: ``codex login status`` has no ``--json`` flag (VERIFIED). It prints plain
text and signals via return code — logged out → ``Not logged in`` with rc=1
(VERIFIED on this box); logged in → rc=0 (the exact string is UNVERIFIED; see
plan §12).

The single derivation rule deliberately FLIPS claude_code's ``rc != 0 → raise``
convention (``claude_code/models_status.py:73-78``): for codex, ``rc=1`` is a
legitimate "logged out" state, not a CLI failure, so it maps to ``status:"error"``
— it must never raise ``execution_failed`` (which the router would surface as HTTP
502). ``run_cli`` still raises ``binary_not_found`` (→ 503) if the binary is
missing and ``timeout`` (→ 504) if the probe hangs; those are real invocation
failures and propagate unchanged.

NOTE (auth quirk, groundtruth §1.5): in some environments ``codex login status``
reports "Not logged in" even though sessions run. This view then reads
``status:"error"`` — the intended graceful degrade (never a 500); the frontend
shows the connect affordance and the user re-auths via ``POST /connect/codex``.
"""
from __future__ import annotations

from typing import Any

from services.cowork_agent.adapters.cli_status import (
    CliStatusError as CodexStatusError,
    resolve_binary,
    run_cli,
)

CODEX_BIN_ENV = "CODEX_CLI_PATH"
DEFAULT_BIN = "codex"
DEFAULT_TIMEOUT_SECONDS = 15.0

# The single model id we surface for codex. Mirrors the ``<prefix>/<model>`` shape
# used by claude_code (``claude_code/claude``). The public default slug
# (``gpt-5-codex``, UNVERIFIED — plan §12) is intentionally NOT used so the row is
# stable regardless of which model the box actually runs.
# TODO(codex): confirm the real default model slug on an authenticated box.
_MODEL_ID = "codex/codex"


def build_status_view(logged_in: bool) -> dict[str, Any]:
    """Translate login state into the common ``{default, models}`` envelope.

    Only ``logged_in`` is consumed, keeping the shape identical to the other
    agents' status adapters."""
    status = "ok" if logged_in else "error"
    return {
        "default": _MODEL_ID,
        "models": [{"id": _MODEL_ID, "status": status}],
    }


async def get_models_status(timeout: float | None = None) -> dict[str, Any]:
    """Run ``codex login status`` and project its exit code into the envelope.

    ``rc == 0`` → logged in → ``"ok"``; any other rc (rc=1 = "Not logged in") →
    ``"error"``. Never raises ``execution_failed`` — for codex a non-zero exit is
    an auth *state*, not a failure. ``binary_not_found``/``timeout`` still
    propagate as :class:`CodexStatusError` and become 503/504 at the router.
    """
    t = DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
    binary = resolve_binary(CODEX_BIN_ENV, DEFAULT_BIN)
    result = await run_cli(binary, ("login", "status"), timeout=t, label="codex")
    return build_status_view(result.returncode == 0)


__all__ = ["CodexStatusError", "build_status_view", "get_models_status"]
