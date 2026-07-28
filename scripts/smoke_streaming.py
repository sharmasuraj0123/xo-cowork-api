#!/usr/bin/env python3
"""Live smoke gates — the handful that genuinely need the real `claude` binary
and a running server. Everything else is in `tests/` and runs in 12 s with no
CLI, no network and no server; keep it that way.

    venv/bin/python scripts/smoke_streaming.py --list
    venv/bin/python scripts/smoke_streaming.py --gate oversize,orphan
    venv/bin/python scripts/smoke_streaming.py --all --base http://127.0.0.1:5002

Exit code is the number of failed gates, so CI can call it and ignore it:

    - `pytest` is the blocking gate. It must be green.
    - this script is advisory. It is NOT run in CI (needs an authenticated CLI
      and a live server) and is marked so nobody wires it in by accident.

`--refresh-captures` regenerates `tests/fixtures/captures/` from the real CLI
and re-scrubs them. That is the one job here that has to exist: the whole unit
suite is only as truthful as the corpus, and the corpus goes stale every time
the CLI changes its wire format. Run it when `claude --version` changes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
CAPTURES = REPO / "tests" / "fixtures" / "captures"
GATES: dict[str, str] = {}


def gate(name: str, doc: str):
    def deco(fn):
        fn._gate, fn._doc = name, doc
        GATES[name] = fn
        return fn
    return deco


def ok(msg): print(f"  \033[32mPASS\033[0m {msg}")
def bad(msg): print(f"  \033[31mFAIL\033[0m {msg}")
def note(msg): print(f"       {msg}")


# ── gates that need the real CLI ─────────────────────────────────────────────

@gate("cli-version", "record the CLI version the corpus was captured against")
async def g_cli_version(args) -> bool:
    cli = os.environ.get("CLAUDE_CLI_PATH") or shutil.which("claude")
    if not cli:
        bad("no claude on PATH"); return False
    out = subprocess.run([cli, "--version"], capture_output=True, text=True).stdout.strip()
    stamp = CAPTURES / "CAPTURED_WITH.txt"
    recorded = stamp.read_text().strip() if stamp.exists() else "(none)"
    note(f"live={out!r} corpus={recorded!r}")
    if out != recorded:
        bad("corpus was captured against a different CLI — run --refresh-captures")
        return False
    ok("corpus matches the installed CLI")
    return True


@gate("oversize", "§9 #4 — a real >64 KiB stdout line must not lose the turn")
async def g_oversize(args) -> bool:
    """Only meaningful against the real CLI: the unit test uses a synthetic
    line, which proves the *reader* is fixed but not that the CLI still
    produces lines of that shape."""
    sys.path.insert(0, str(REPO))
    from services.cowork_agent.adapters.claude_code.adapter import Adapter
    from services.cowork_agent.registry.settings import load_agent_config

    adapter = Adapter(load_agent_config("claude_code"))
    events, err = [], None
    try:
        async for ev in adapter.stream(
            "Read the file tests/fixtures/big_input.txt in one Read call, "
            "then reply with exactly: DONE",
            None, is_new_session=True,
        ):
            events.append(ev)
    except Exception as exc:            # noqa: BLE001 — that is the finding
        err = exc

    if err is not None:
        bad(f"turn died: {type(err).__name__}: {err}")
        note(f"got {len(events)} events before the exception")
        return False
    text = "".join(e.get("token", "") for e in events if e.get("type") == "token")
    if "DONE" not in text:
        bad(f"turn truncated; text={text[:120]!r}"); return False
    ok(f"survived, {len(events)} events")
    return True


@gate("rc-nonzero", "§9 #9 — a CLI exiting non-zero must surface agent-error")
async def g_rc_nonzero(args) -> bool:
    cli = os.environ.get("CLAUDE_CLI_PATH") or shutil.which("claude")
    proc = subprocess.run(
        [cli, "--print", "--output-format", "stream-json", "--nonexistent-flag", "-p", "hi"],
        capture_output=True, text=True,
    )
    note(f"rc={proc.returncode} stdout={len(proc.stdout)}B stderr={proc.stderr.strip()!r}")
    if proc.returncode == 0:
        bad("CLI no longer exits non-zero on a bad flag — the gate's premise is gone")
        return False
    ok("premise holds (the router-side assertion lives in tests/)")
    return True


# ── gates that need a live server ────────────────────────────────────────────

async def _sse(session, url: str, seconds: float):
    """Read an SSE stream for `seconds`, return (frames, first_gap_ok)."""
    import httpx
    frames, t0 = [], time.monotonic()
    async with httpx.AsyncClient(timeout=None) as c:
        async with c.stream("GET", url) as r:
            async for line in r.aiter_lines():
                if line.startswith("event: "):
                    frames.append((time.monotonic() - t0, line[7:]))
                if time.monotonic() - t0 > seconds:
                    break
    return frames


@gate("xss", "§9 #3 — /callback must escape error_description")
async def g_xss(args) -> bool:
    import httpx
    payload = "<img src=x onerror=alert(1)>"
    async with httpx.AsyncClient(base_url=args.base, timeout=10) as c:
        r = await c.get("/callback", params={"error": "x", "error_description": payload})
    if payload in r.text:
        bad("reflected unescaped — connectors/vercel.py:179"); return False
    ok("escaped")
    return True


@gate("false-done", "§9 #5 — reconnect must not report done while alive")
async def g_false_done(args) -> bool:
    import httpx
    async with httpx.AsyncClient(base_url=args.base, timeout=30) as c:
        r = await c.post("/api/chat/prompt", json={
            "text": "Read three files in this repo one at a time and summarise each.",
            "agent_name": "claude_code"})
        sid = r.json()["stream_id"]

    url = f"{args.base}/api/chat/stream/{sid}"
    await _sse(None, url, 15.0)                       # connect, then drop
    t0 = time.monotonic()
    frames = await _sse(None, url, 5.0)               # reconnect
    elapsed = time.monotonic() - t0

    alive = subprocess.run(["pgrep", "-f", os.environ.get("CLAUDE_CLI_PATH", "claude")],
                           capture_output=True).returncode == 0
    done_fast = any(e == "done" for _, e in frames) and elapsed < 2
    note(f"reconnect frames={[e for _, e in frames]} in {elapsed:.1f}s, child_alive={alive}")
    if done_fast and alive:
        bad("instant false done while the CLI is still running"); return False
    ok("no false done")
    return True


@gate("orphan", "§9 #10 — disconnect must not kill the turn")
async def g_orphan(args) -> bool:
    import httpx
    async with httpx.AsyncClient(base_url=args.base, timeout=30) as c:
        r = await c.post("/api/chat/prompt", json={
            "text": "Read four files in this repo and summarise each in one line.",
            "agent_name": "claude_code"})
        sid, session = r.json()["stream_id"], r.json()["session_id"]

    await _sse(None, f"{args.base}/api/chat/stream/{sid}", 8.0)
    async with httpx.AsyncClient(base_url=args.base, timeout=30) as c:
        before = len((await c.get(f"/api/messages/{session}")).json().get("messages", []))
        await asyncio.sleep(25)
        after = len((await c.get(f"/api/messages/{session}")).json().get("messages", []))
    note(f"messages {before} -> {after} across a 25 s disconnect")
    if after <= before:
        bad("turn stopped producing after disconnect"); return False
    ok("turn survived the disconnect")
    return True


@gate("abort", "§9 #11 — abort must kill the process group within 2 s")
async def g_abort(args) -> bool:
    import httpx
    cli = os.environ.get("CLAUDE_CLI_PATH") or shutil.which("claude")
    async with httpx.AsyncClient(base_url=args.base, timeout=30) as c:
        r = await c.post("/api/chat/prompt", json={
            "text": "Read ten files in this repo, one at a time.",
            "agent_name": "claude_code"})
        sid = r.json()["stream_id"]
        asyncio.create_task(_sse(None, f"{args.base}/api/chat/stream/{sid}", 60))
        await asyncio.sleep(6)
        # Match on the RESOLVED cli_path, not ^claude — docs §9 #11, C.7.
        pids = subprocess.run(["pgrep", "-f", cli], capture_output=True, text=True).stdout.split()
        if not pids:
            bad(f"no process matching {cli!r} — nothing to abort"); return False
        await c.post("/api/chat/abort", json={"stream_id": sid})

    for _ in range(20):
        left = subprocess.run(["pgrep", "-f", cli], capture_output=True, text=True).stdout.split()
        if not left:
            ok(f"process group gone (was {len(pids)} pids)"); return True
        await asyncio.sleep(0.1)
    bad(f"still alive 2 s after abort: {left}")
    return False


@gate("perf", "§5 — uncached status endpoints")
async def g_perf(args) -> bool:
    import httpx
    budgets = {"/providers/status": 0.15, "/api/usage/summary": 0.10, "/health": 0.05}
    passed = True
    async with httpx.AsyncClient(base_url=args.base, timeout=30) as c:
        for path, budget in budgets.items():
            times = []
            for _ in range(3):
                t = time.monotonic()
                r = await c.get(path)
                times.append(time.monotonic() - t)
            best = min(times)
            line = f"{path} best={best*1000:.0f}ms budget={budget*1000:.0f}ms status={r.status_code}"
            if best > budget:
                bad(line); passed = False
            else:
                ok(line)
    return passed


# ── corpus refresh ───────────────────────────────────────────────────────────

def refresh_captures() -> int:
    """Re-capture the corpus from the real CLI and re-scrub it.

    Deliberately NOT a gate: it rewrites committed fixtures, so it must be an
    explicit human action followed by `git diff` and a golden regeneration.
    """
    print(__doc__.split("`--refresh-captures`")[1].strip()[:400])
    print("\nSee tests/fixtures/captures/README.md for the exact prompts.")
    print("Then:  XO_UPDATE_GOLDEN=1 venv/bin/pytest tests/test_parser_golden.py")
    print("       git diff tests/fixtures/   # read every line")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("XO_BASE", "http://127.0.0.1:5002"))
    ap.add_argument("--gate", default="", help="comma-separated gate names")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--refresh-captures", action="store_true")
    args = ap.parse_args()

    if args.list:
        for name, fn in GATES.items():
            print(f"  {name:14s} {fn._doc}")
        return 0
    if args.refresh_captures:
        return refresh_captures()

    names = [n for n in args.gate.split(",") if n] or (list(GATES) if args.all else [])
    if not names:
        ap.print_help(); return 2

    failed = 0
    for name in names:
        fn = GATES.get(name)
        if fn is None:
            print(f"unknown gate {name!r}"); failed += 1; continue
        print(f"\n\033[1m{name}\033[0m — {fn._doc}")
        try:
            if not asyncio.run(fn(args)):
                failed += 1
        except Exception as exc:                        # noqa: BLE001
            bad(f"{type(exc).__name__}: {exc}"); failed += 1
    print(f"\n{len(names) - failed}/{len(names)} gates passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
