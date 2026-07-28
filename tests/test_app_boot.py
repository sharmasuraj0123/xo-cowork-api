"""The suite's own guardrail: prove the app can be tested without booting it.

`server.lifespan` runs `_run_agent_setup()` and `_install_shared_deps()` — two
synchronous `subprocess.run` calls measured at ~23 s combined (docs §5) that
reinstall rclone/gh/gnupg into /usr/bin — then starts a filesystem watcher
(12 root scans/s), a usage-sync scheduler and a warmup HTTP request.

If any of that ever starts running under pytest, the suite goes from ~10 s to
minutes and mutates the machine. Someone will then delete the suite. So it gets
an explicit test.
"""
from __future__ import annotations

import subprocess

import httpx
import pytest


async def test_lifespan_never_runs_under_asgi_transport(app, monkeypatch):
    """`httpx.ASGITransport` does not emit the ASGI `lifespan` scope at all, so
    startup/shutdown handlers never fire. This asserts it rather than trusting
    it — and it is why the suite must NOT use `with TestClient(app)`, which
    *does* run the lifespan."""
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or None)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/health")

    assert r.status_code == 200
    assert calls == [], f"lifespan ran during a test and shelled out: {calls}"


async def test_json_endpoints_are_reachable(client):
    """Smoke: the ASGI plumbing actually serves the app."""
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json().get("status")


def test_suite_never_spawns_the_real_cli():
    """`conftest._isolate_env` points CLAUDE_CLI_PATH at a non-existent file.
    A test that accidentally reaches the real binary would burn tokens and take
    minutes; this makes it fail in milliseconds instead."""
    import os

    assert not os.path.exists(os.environ["CLAUDE_CLI_PATH"]), (
        "CLAUDE_CLI_PATH points at a real binary during tests"
    )


@pytest.mark.parametrize("path", ["/api/chat/prompt"])
async def test_prompt_rejects_empty_text(client, path):
    r = await client.post(path, json={"text": "   "})
    assert r.status_code == 400
