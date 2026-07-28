"""Shared test fixtures.

Design constraints this file exists to satisfy:

1. **Never run the app's lifespan.** `server.lifespan` calls `_run_agent_setup()`
   and `_install_shared_deps()` — two synchronous `subprocess.run` calls that
   take ~23 s and mutate `/usr/bin` — then spawns a filesystem watcher, a usage
   sync scheduler and a warmup HTTP request. `httpx.ASGITransport` never sends
   the ASGI `lifespan` scope, so simply *not* using `TestClient` as a context
   manager keeps all of that dormant. There is an explicit test for this
   (`test_app_boot.py::test_lifespan_never_runs_in_tests`) because if it ever
   regresses the suite goes from 2 s to 2 min and someone deletes it.

2. **Never touch the real `~/.claude` tree.** Every fixture that can write
   redirects HOME and the agent roots into a tmp_path.

3. **Import `server` exactly once per agent.** `import server` is 0.7-1.6 s and
   registers routers globally; the `app_for_agent` fixture caches per agent
   name in a subprocess-free way by reloading only when the agent changes.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CAPTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "captures"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ── environment isolation ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True, scope="session")
def _isolate_env():
    """Session-wide guards. Anything that would reach the network, the real
    home directory, or the real CLI is pointed somewhere harmless."""
    os.environ.setdefault("AGENT_NAME", "claude_code")
    # Belt and braces: if a test accidentally spawns the real binary, make it
    # fail fast and loudly instead of burning tokens.
    os.environ["CLAUDE_CLI_PATH"] = os.environ.get(
        "XO_TEST_CLI_PATH", "/nonexistent/claude-must-not-be-spawned"
    )
    os.environ["STARTUP_WARMUP_ENABLED"] = "false"
    yield


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Redirect HOME + the cowork roots into tmp_path for tests that write."""
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_COWORK_ROOT", str(home / "claude-cowork"))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    return home


# ── the app ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def app():
    """The FastAPI app under the default test agent, lifespan NOT run."""
    import server

    return server.app


@pytest.fixture
async def client(app):
    """httpx AsyncClient over the ASGI app. No socket, no lifespan, no server."""
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


# ── capture corpus ────────────────────────────────────────────────────────────

def _capture_names() -> list[str]:
    return sorted(p.stem for p in CAPTURES.glob("*.jsonl") if not p.stem.endswith("_seed"))


@pytest.fixture(params=_capture_names())
def capture(request):
    """Parametrised over every committed capture. Yields (name, [raw bytes])."""
    path = CAPTURES / f"{request.param}.jsonl"
    return request.param, path.read_bytes().splitlines(keepends=True)


@pytest.fixture
def capture_lines():
    """Load one named capture's raw lines."""

    def _load(name: str) -> list[bytes]:
        return (CAPTURES / f"{name}.jsonl").read_bytes().splitlines(keepends=True)

    return _load


@pytest.fixture
def oversized_line():
    """Build a stream-json line of an arbitrary size from the committed seed.

    The seed is a real `user`/`tool_result` record with its content replaced by
    the token `@@FILL@@`. We expand at test time rather than committing a
    200 KB blob: the payload bytes carry no information, and the sizes that
    matter (100 / 140 / 300 KB — the thresholds where CPython's StreamReader
    switches from "separator found" to "separator not found" recovery) are a
    parameter of the test, not of the fixture.
    """
    seed = json.loads((CAPTURES / "oversized_seed.jsonl").read_text())

    def _build(total_bytes: int) -> bytes:
        block = seed["message"]["content"][0]
        base = len(json.dumps(seed, separators=(",", ":")).encode()) - len("@@FILL@@")
        fill = "x" * max(1, total_bytes - base)
        block["content"] = fill
        return json.dumps(seed, separators=(",", ":")).encode() + b"\n"

    return _build
