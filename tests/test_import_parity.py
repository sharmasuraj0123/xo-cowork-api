"""Import + route-parity smoke test.

Runs on each PR / push to main (see .github/workflows/ci.yml).

For every supported agent, boot the import path under that ``AGENT_NAME`` and
assert two things:

1. ``import server`` succeeds (exit code 0) — the app assembles without crashing.
2. The app registers at least ``MIN_ROUTES`` routes — a tripwire for a router
   that silently stops registering its endpoints.

Why a subprocess per agent: the active agent is resolved at import time and
Python caches the ``server`` module, so a single process cannot re-import it
under a second ``AGENT_NAME``. A fresh subprocess gives each agent a clean
import. This never enters the FastAPI ``lifespan`` block, so no agent install /
system-dep download is triggered — it runs on a bare Python + FastAPI runner
with no secrets and no live backend.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

AGENTS = ["claude_code", "openclaw", "hermes"]

# v1: assert the app registers *some* routes. Multiple contributors add
# endpoints, so we don't pin exact counts yet. To tighten later (per the CTO
# discussion), replace MIN_ROUTES with an exact-count map and assert equality:
#     EXACT_ROUTES = {"claude_code": 146, "openclaw": 149, "hermes": 173}
MIN_ROUTES = 1

# Printed by the child on success so we can parse the count off stdout
# regardless of any other import-time logging.
_SNIPPET = "import server; print('ROUTE_COUNT=' + str(len(server.app.routes)))"


@pytest.mark.parametrize("agent", AGENTS)
def test_import_and_route_parity(agent):
    env = {**os.environ, "AGENT_NAME": agent}
    proc = subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, (
        f"`import server` failed for AGENT_NAME={agent}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )

    marker = "ROUTE_COUNT="
    line = next(
        (l for l in proc.stdout.splitlines() if l.startswith(marker)), None
    )
    assert line is not None, (
        f"route count not found in output for AGENT_NAME={agent}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )

    count = int(line[len(marker):])
    assert count >= MIN_ROUTES, (
        f"AGENT_NAME={agent} registered {count} routes (expected >= {MIN_ROUTES})"
    )
