"""Import gate + route parity — docs §9 #1, without any hardcoded count.

The numbers in the docs have been wrong three times over:

    CLAUDE.md:47      144 / 147 / 171           (claude_code / openclaw / hermes)
    DEVELOPING.md:184 146 / 149 / 173 / 148     (+ antigravity)
    measured 2026-07-27  claude_code 151, openclaw 151, hermes 175,
                         antigravity 150, codex 148

They disagree with each other and with reality, and they will be wrong again the
next time anyone adds a route. A magic number is the wrong assertion: what we
actually care about is

  (a) every agent still *imports* — the real value of the old gate;
  (b) the route set is what we last agreed it should be — a reviewable snapshot,
      not a number;
  (c) the de-leak invariant holds: an agent must not carry another agent's
      routes.

(c) is the one that never goes stale, so it is the one that should gate CI.

Regenerate (b) deliberately, in the same commit as the route change:

    XO_UPDATE_SNAPSHOT=1 venv/bin/pytest tests/test_route_parity.py
    git diff tests/fixtures/routes/
"""
from __future__ import annotations

import functools
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOTS = pathlib.Path(__file__).resolve().parent / "fixtures" / "routes"
UPDATE = os.environ.get("XO_UPDATE_SNAPSHOT") == "1"

PROBE = (
    "import json, server; "
    "print('@@', json.dumps(sorted(server.app.openapi()['paths'])))"
)


def discover_agents() -> list[str]:
    """No hardcoded agent list either — adapters are auto-discovered
    (`registry/adapter_registry.py`), so the gate discovers them the same way."""
    sys.path.insert(0, str(REPO_ROOT))
    from services.cowork_agent.registry.adapter_registry import list_adapters

    return list_adapters()


AGENTS = discover_agents()


@functools.lru_cache(maxsize=None)
def routes_for(agent: str) -> tuple[str, ...]:
    """Import `server` under AGENT_NAME=<agent> in a clean interpreter.

    A subprocess per agent is not an optimisation problem: routers register on
    the module-level `app` at import time, so a second `import server` in the
    same process would measure a union, not a per-agent set. Cost is ~1 s each.
    """
    env = {**os.environ, "AGENT_NAME": agent, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"AGENT_NAME={agent} failed to import server:\n{proc.stderr[-3000:]}"
    )
    marker = [l for l in proc.stdout.splitlines() if l.startswith("@@")]
    assert marker, f"probe produced no output for {agent}:\n{proc.stdout[-2000:]}"
    return tuple(json.loads(marker[-1][2:]))


@pytest.fixture(scope="session")
def route_sets() -> dict[str, set[str]]:
    return {agent: set(routes_for(agent)) for agent in AGENTS}


# ── (a) the import gate ──────────────────────────────────────────────────────

@pytest.mark.parametrize("agent", AGENTS)
def test_agent_imports(agent):
    """`AGENT_NAME=<a> venv/bin/python -c "import server"` for every discovered
    agent. This is the gate that has actually caught things."""
    assert routes_for(agent), f"{agent} registered no routes at all"


# ── (b) the snapshot ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("agent", AGENTS)
def test_route_snapshot(agent):
    actual = list(routes_for(agent))
    path = SNAPSHOTS / f"{agent}.txt"
    if UPDATE:
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(actual) + "\n")
        pytest.skip(f"snapshot updated for {agent} ({len(actual)} paths)")
    assert path.exists(), (
        f"no route snapshot for discovered agent {agent!r}. If this agent is "
        f"new, run XO_UPDATE_SNAPSHOT=1 pytest and commit tests/fixtures/routes/."
    )
    expected = path.read_text().split()
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    assert not (added or removed), (
        f"{agent} route set changed ({len(expected)} -> {len(actual)}).\n"
        f"  added:   {added}\n  removed: {removed}\n"
        f"If intended: XO_UPDATE_SNAPSHOT=1 pytest tests/test_route_parity.py"
    )


# ── (c) the invariant that never goes stale ──────────────────────────────────

# ── the ratchet ──────────────────────────────────────────────────────────────
#
# Agent-named routes that every agent already serves, as of 2026-07-27. This is
# a BASELINE, not an allowlist: the gate fails on anything NEW, and each entry
# here is a debt with a reason.
#
#   /connect/claude-code*, /connect/codex   deliberate. The auth "connect"
#       flows must be reachable whatever AGENT_NAME is, so the user can log
#       into a backend that is not currently active.
#   /openclaw/usage/*                       PRE-EXISTING MODULARITY VIOLATION.
#       Five openclaw-named routes registered globally. Same class as the one
#       docs §9 gate 2 flags at visualizer/ingest/pii_filter.py:46-52 —
#       "to resolve, not extend". Do not add to this list.
#   ("openclaw", "/api/codex/status")       openclaw/routes.py:57 — an openclaw
#       route named for codex. Rename when the frontend can be updated.
BASELINE_AGENT_NAMED_SHARED_ROUTES = {
    "/connect/claude-code",
    "/connect/claude-code/callback",
    "/connect/codex",
    "/openclaw/usage/analytics",
    "/openclaw/usage/sessions",
    "/openclaw/usage/sessions/{session_id}",
    "/openclaw/usage/summary",
    "/openclaw/usage/summary/card",
}
KNOWN_CROSS_AGENT_ROUTES = {
    ("openclaw", "/api/codex/status"),
}


def test_core_routes_present_for_every_agent(route_sets):
    """Every agent must serve the common core. A capability that silently stops
    registering (a missing `adapters/<name>/routes.py`, a swallowed ImportError
    at `loader.py:54`) shows up here as a route disappearing under one agent
    only — which no single-agent test can see."""
    core = set.intersection(*route_sets.values())
    assert len(core) > 100, f"core route set collapsed to {len(core)} paths"
    for agent, paths in route_sets.items():
        assert core <= paths, f"{agent} is missing core routes: {sorted(core - paths)}"


def test_every_non_core_route_belongs_to_exactly_one_agent(route_sets):
    """The de-leak invariant, stated without a single number and without a
    naming heuristic.

    A path is either core (served by all agents) or agent-specific (served by
    exactly one). Anything served by *some but not all* agents is a leak: it
    means a router registered under a backend that does not own it — the exact
    failure the route de-leak was built to prevent.

    Note this deliberately does NOT require the path to contain the agent's
    name. `claude_code` legitimately owns `/api/remote-control/*`, which names
    no agent; an earlier draft of this gate rejected it, which is how we learned
    the naming heuristic was the wrong invariant.
    """
    n_agents = len(route_sets)
    owners: dict[str, list[str]] = {}
    for agent, paths in route_sets.items():
        for path in paths:
            owners.setdefault(path, []).append(agent)

    leaks = {
        path: sorted(agents)
        for path, agents in owners.items()
        if 1 < len(agents) < n_agents
    }
    assert not leaks, (
        "routes served by some-but-not-all agents (route leak — a router is "
        f"registering under a backend that does not own it): {leaks}"
    )


def test_no_agent_serves_another_agents_named_routes(route_sets):
    """A path that names agent B must not be served by agent A.

    This is the one check that can catch a leak *inside* the core set — e.g. if
    `/api/config/hermes` were accidentally promoted to a shared router it would
    become "core" and the previous test would go quiet."""
    offenders = []
    for path in set().union(*route_sets.values()):
        if path in BASELINE_AGENT_NAMED_SHARED_ROUTES:
            continue
        for named in route_sets:
            if named.replace("_", "-") not in path and named not in path:
                continue
            for agent, paths in route_sets.items():
                if agent != named and path in paths:
                    if (agent, path) not in KNOWN_CROSS_AGENT_ROUTES:
                        offenders.append((agent, path, named))
    assert not offenders, (
        "agent serves a route named for another agent — a NEW modularity "
        f"violation (the pre-existing ones are in the baseline above): "
        f"{sorted(offenders)}"
    )
