"""The 5-agent import gate and route-surface parity matrix.

This file replaces the hand-run validation step in ``DEVELOPING.md:184-188``.
Two things are pinned per agent:

1. **The import gate.** ``import server`` must succeed under every value of
   ``AGENT_NAME``. This is the whole point of the broker architecture: a
   missing capability makes a router return its empty/501 shape, never crash
   the process (``adapters/loader.py``, CLAUDE.md §"Agent-modular
   architecture"). A child that exits non-zero fails the test.

2. **The route surface.** Both the unique-path count and the OpenAPI path
   count, for all five adapters. Per-agent counts differ *by design* — the
   route de-leak means non-hermes agents don't carry ``/api/channels/hermes/*``
   — so a single shared number cannot express the invariant; the matrix can.

**Why a subprocess per agent is mandatory.** ``AGENT_NAME`` is resolved exactly
once per interpreter and then frozen in three separate places:

* ``registry/agent_registry.py:173-174`` caches the parsed manifests in module
  globals, and ``_expand()`` expanduser's every path in them exactly once;
* ``registry/agent_env.py:28`` binds ``ENV_FILE = get_active_agent().env_file``
  at import time;
* ``server`` itself, and every adapter module it pulled in, stays in
  ``sys.modules``.

So an in-process ``@pytest.mark.parametrize`` over the five agents does not test
five agents — it tests whichever one was imported first, five times, and passes.
That failure mode is silent, which is why the seam is a fresh interpreter and
why this module carries the ``subprocess`` marker.

**Why the counting form matters** — see
:func:`test_the_naive_route_count_cannot_be_the_gate`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.subprocess

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Measured baseline for this HEAD. (unique paths, openapi paths) per agent.
# These are a contract, not an observation: a diff that moves any of them has
# added or removed a route, and must say so in its description.
_EXPECTED: dict[str, tuple[int, int]] = {
    "claude_code": (159, 151),
    "openclaw": (159, 151),
    "hermes": (183, 175),
    "codex": (156, 148),
    "antigravity": (158, 150),
}

# The child prints exactly one marked line. Everything else on stdout (a stray
# print from an imported module, a warning banner) is ignored, so the probe
# cannot be broken by unrelated output.
_MARKER = "__ROUTE_PROBE__ "

# `@MARKER@` is substituted below rather than f-stringed: the source contains
# dict literals, so `.format()` / f-strings would have to escape every brace.
_PROBE = r"""
import json

import server


def walk(routes):
    # Starlette 1.x wraps an `include_router`ed sub-router in an
    # `_IncludedRouter` proxy that has NO `.path` attribute -- the real routes
    # hang off its `.original_router`. Recursing through that is the only way to
    # see the whole surface.
    for r in routes:
        original = getattr(r, "original_router", None)
        if original is not None:
            yield from walk(original.routes)
        elif hasattr(r, "path"):
            yield r


print("@MARKER@" + json.dumps({
    "unique": len({r.path for r in walk(server.app.routes)}),
    "openapi": len(server.app.openapi()["paths"]),
    # The documented-but-broken form, measured here so the test below can
    # assert on it without a second interpreter launch.
    "naive": len({r.path for r in server.app.routes if hasattr(r, "path")}),
}))
""".replace("@MARKER@", _MARKER)


def _child_env(agent: str, home: Path) -> dict[str, str]:
    """Environment for one probe: this agent, in a home of its own.

    conftest's Layer 1 already re-pointed ``HOME`` and roughly a dozen derived
    variables (``XO_PROJECTS_ROOT``, ``CODEX_HOME``, ``ARGUS_DB``, …) at a
    session tempdir, so the child inherits a scrubbed environment rather than
    the developer's. Re-homing it is a single generic rewrite: any inherited
    value that lives under the parent's ``HOME`` is re-anchored under this
    probe's ``home``. That keeps the sandbox list in exactly one place
    (``tests/conftest.py``) instead of drifting between two files.

    It matters that this is airtight: ``xo_projects_root`` / ``xo_runtime_root``
    ``mkdir`` on *read* (``project_layout.py:87-120``), so a child with an
    unscrubbed home creates directories in the developer's real one.
    """
    parent_home = os.environ.get("HOME", "")
    env = {
        key: (str(home) + value[len(parent_home):]
              if parent_home and value.startswith(parent_home)
              else value)
        for key, value in os.environ.items()
    }
    # Belt and braces: set the four that actually decide the test outcome
    # explicitly, so the test never silently depends on the rewrite above.
    env["HOME"] = str(home)
    env["XO_PROJECTS_ROOT"] = str(home / "xo-projects")
    env["XO_RUNTIME_ROOT"] = str(home / ".xo")
    env["AGENT_NAME"] = agent
    # `python -c` puts cwd on sys.path, but say it out loud rather than relying
    # on an interpreter default that has changed before.
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return env


def _run_probe(agent: str, home: Path) -> dict[str, int]:
    """Import ``server`` under ``AGENT_NAME=agent`` and measure its routes."""
    home.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=_REPO_ROOT,
        env=_child_env(agent, home),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"`import server` failed under AGENT_NAME={agent} "
            f"(exit {proc.returncode}). The broker must boot for every agent; a "
            f"missing capability is supposed to degrade to an empty/501 route, "
            f"not to an ImportError.\n"
            f"--- stderr ---\n{proc.stderr[-4000:]}\n"
            f"--- stdout ---\n{proc.stdout[-2000:]}"
        )
    for line in proc.stdout.splitlines():
        if line.startswith(_MARKER):
            return json.loads(line[len(_MARKER):])
    pytest.fail(
        f"probe for AGENT_NAME={agent} exited 0 but printed no result line.\n"
        f"--- stdout ---\n{proc.stdout[-2000:]}"
    )


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    """Memoised probe runner — one interpreter per agent for the whole module.

    Module-scoped purely so the two tests below share the five launches rather
    than doubling them; each agent still gets its own fresh interpreter and its
    own home, which is the isolation that matters.
    """
    cache: dict[str, dict[str, int]] = {}

    def _probe(agent: str) -> dict[str, int]:
        if agent not in cache:
            cache[agent] = _run_probe(
                agent, Path(tmp_path_factory.mktemp(f"probe-{agent}"))
            )
        return cache[agent]

    return _probe


@pytest.mark.parametrize("agent", sorted(_EXPECTED))
def test_server_imports_and_route_surface_is_unchanged(agent, probe):
    """``import server`` succeeds for this agent and serves exactly N paths.

    Pins the import gate plus the route-surface baseline recorded in
    ``DEVELOPING.md:184-188`` (whose numbers predate codex/antigravity — the
    table in this module is the current one).

    A move in ``unique`` with ``openapi`` steady means a route was added or
    removed outside the OpenAPI schema (a mount, a non-schema route); a move in
    both means the public API changed. Either is a real change and belongs in
    the diff description — update the table deliberately, never to make the
    test green.
    """
    unique, openapi = probe(agent)["unique"], probe(agent)["openapi"]
    assert (unique, openapi) == _EXPECTED[agent], (
        f"{agent}: route surface moved — got unique={unique} openapi={openapi}, "
        f"expected {_EXPECTED[agent]}"
    )


def test_the_naive_route_count_cannot_be_the_gate(probe):
    """The documented ``{r.path for r in app.routes}`` form fails OPEN.

    This is the reason the previous gate was worthless, and this test exists so
    nobody "simplifies" :func:`_PROBE`'s ``walk`` back into a one-line set
    comprehension.

    Under Starlette 1.x every ``include_router``ed sub-router appears in
    ``app.routes`` as an ``_IncludedRouter`` proxy with **no** ``.path``
    attribute; the real routes hang off its ``.original_router``. The naive
    comprehension therefore silently drops the entire mounted surface — the part
    that actually differs between agents — and counts only the handful of routes
    defined directly on ``app``.

    Three assertions, because "it undercounts" alone would be satisfied by an
    off-by-a-few: the naive count is not merely wrong per agent, it is
    *identical* for all five, so it cannot distinguish hermes' 183 paths from
    codex' 156. A gate that returns the same number whatever it is measuring is
    not a gate. Recorded in ``docs/watcher-new-adapters-plan.md:217-218``.

    Both comparisons are against numbers measured in the same run, never against
    :data:`_EXPECTED` — the openapi count in particular is computed by FastAPI
    itself and owes nothing to ``walk()``, so it stays a valid yardstick even if
    ``walk()`` is the thing that broke.
    """
    results = {agent: probe(agent) for agent in _EXPECTED}

    for agent, result in results.items():
        assert result["naive"] != result["unique"], (
            f"{agent}: the naive form now agrees with the recursive walk(). "
            f"Either Starlette stopped proxying included routers — in which "
            f"case simplify walk() and delete this test deliberately — or "
            f"walk() has been simplified back into the naive form and the gate "
            f"is fake again."
        )
        # ...and it is a tiny fraction of the real surface, not a near miss.
        assert result["naive"] * 2 < result["openapi"], (
            f"{agent}: naive={result['naive']} is no longer a gross undercount "
            f"of the {result['openapi']} paths FastAPI publishes; re-check the "
            f"assumption before trusting either number."
        )

    naive = {agent: result["naive"] for agent, result in results.items()}
    assert len(set(naive.values())) == 1, (
        "the naive count now varies by agent; re-check whether it still fails "
        f"open before trusting it: {naive}"
    )
