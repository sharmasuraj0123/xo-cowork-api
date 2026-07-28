#!/usr/bin/env python3
"""A stand-in for the `claude` binary. Point `CLAUDE_CLI_PATH` at this file.

It ignores every CLI flag it is given (so `_build_cmd`'s real argv works
unchanged) and is driven entirely by environment variables:

    FAKE_CAPTURE     path to a capture .jsonl to replay          (default: builtin)
    FAKE_LINE_DELAY  seconds between stdout lines                (default: 0.05)
    FAKE_OVERSIZE    emit one tool_result line of N bytes        (default: off)
    FAKE_EXIT_CODE   exit with this code after the last line     (default: 0)
    FAKE_STDERR      write this to stderr before exiting         (default: "")
    FAKE_GRANDCHILD  spawn `sleep 300` before streaming          (default: off)
    FAKE_STUBBORN    grandchild chain whose MIDDLE member SIG_IGNs
                     SIGTERM — the group survives a SIGTERM-only
                     "kill" unless the caller escalates            (default: off)
    FAKE_ESCAPEE     spawn `sleep 300` with start_new_session=1,
                     i.e. OUTSIDE our process group, still holding
                     the inherited stdout — killpg cannot reach it
                     and stdout therefore never reaches EOF        (default: off)
    FAKE_HANG        seconds to idle instead of exiting (a wedged
                     CLI)                                          (default: 0)
    FAKE_HANG_AFTER  with FAKE_HANG: emit only this many lines
                     first, so the turn is genuinely INCOMPLETE
                     (no `result` record)                          (default: all)
    FAKE_PIDFILE     write our pid (and the grandchild's) here
    FAKE_PROGRESS    append a line to this file per stdout line  — the
                     test-suite stand-in for "the transcript keeps growing"

Why a real subprocess rather than a mock: gates #10 and #11 are *about* process
lifetime. `proc.kill()` not reaching a reparented grandchild (§4.4) and
`start_new_session=True` + `os.killpg` being required are facts about the OS,
not about Python objects. A mock cannot be wrong in the way production was.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

BUILTIN = [
    {"type": "system", "subtype": "init", "session_id": "fake-native-session-0001",
     "cwd": "/workspace", "tools": []},
    {"type": "assistant", "message": {"role": "assistant",
     "content": [{"type": "text", "text": "working"}]},
     "parent_tool_use_id": None},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_fake01", "name": "Read", "input": {}}]},
     "parent_tool_use_id": None},
    {"type": "user", "message": {"role": "user", "content": [
        {"tool_use_id": "toolu_fake01", "type": "tool_result", "content": "ok"}]},
     "parent_tool_use_id": None},
    {"type": "assistant", "message": {"role": "assistant",
     "content": [{"type": "text", "text": " done"}]}, "parent_tool_use_id": None},
    {"type": "result", "subtype": "success", "is_error": False, "result": "working done",
     "session_id": "fake-native-session-0001", "usage": {"input_tokens": 1,
     "output_tokens": 2}, "model": "fake-model"},
]


def main() -> int:
    delay = float(os.environ.get("FAKE_LINE_DELAY", "0.05"))
    pidfile = os.environ.get("FAKE_PIDFILE")
    progress = os.environ.get("FAKE_PROGRESS")

    if pidfile:
        pathlib.Path(pidfile).write_text(f"self {os.getpid()}\n")

    child = None
    if os.environ.get("FAKE_GRANDCHILD"):
        # Reproduces §4.4: a grandchild that reparents and survives proc.kill().
        child = subprocess.Popen(["sleep", "300"])
        if pidfile:
            with open(pidfile, "a") as fh:
                fh.write(f"grandchild {child.pid}\n")

    if os.environ.get("FAKE_STUBBORN"):
        # A depth-3 tree whose MIDDLE member ignores SIGTERM, the shape a real
        # tool subprocess has (node children install SIGTERM handlers, bash
        # background jobs and MCP servers outlive their parent). The leader —
        # this process — does NOT ignore it, which is the whole point: a caller
        # that escalates on "did the LEADER exit" declares success in ~10 ms and
        # never sends the SIGKILL these two need.
        stubborn = subprocess.Popen([
            sys.executable, "-c",
            "import signal,subprocess,sys,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "p=subprocess.Popen(['sleep','300']);"
            "open(sys.argv[1],'a').write('stubborn_child %d\\n' % p.pid);"
            "time.sleep(300)",
            pidfile or os.devnull,
        ])
        if pidfile:
            with open(pidfile, "a") as fh:
                fh.write(f"stubborn {stubborn.pid}\n")

    if os.environ.get("FAKE_ESCAPEE"):
        # A tool that daemonises — `claude` running a Bash step that starts a dev
        # server or an MCP server. `start_new_session` puts it outside our
        # process group, so no killpg can reach it, and it keeps the INHERITED
        # stdout write end open, so the adapter's read loop never sees EOF.
        escapee = subprocess.Popen(["sleep", "300"], start_new_session=True)
        if pidfile:
            with open(pidfile, "a") as fh:
                fh.write(f"escapee {escapee.pid}\n")

    capture = os.environ.get("FAKE_CAPTURE")
    if capture:
        lines = pathlib.Path(capture).read_text().splitlines()
    else:
        lines = [json.dumps(e, separators=(",", ":")) for e in BUILTIN]

    oversize = int(os.environ.get("FAKE_OVERSIZE", "0"))
    if oversize:
        big = {"type": "user", "message": {"role": "user", "content": [
            {"tool_use_id": "toolu_big", "type": "tool_result", "content": "x" * oversize}]},
            "parent_tool_use_id": None}
        lines.insert(max(1, len(lines) // 2), json.dumps(big, separators=(",", ":")))

    hang = float(os.environ.get("FAKE_HANG", "0"))
    # Stop after this many lines and idle forever. The default (all lines) still
    # emits the `result` record, i.e. a COMPLETE turn — a wall clock that fires
    # on that is bounding a lingering process, not truncating an answer. To test
    # the truncated case the CLI has to wedge mid-turn, which is what this is.
    hang_after = int(os.environ.get("FAKE_HANG_AFTER", "0")) or len(lines)

    for index, line in enumerate(lines):
        if hang and index >= hang_after:
            break
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if progress:
            with open(progress, "a") as fh:
                fh.write(f"{time.time():.3f}\n")
        time.sleep(delay)

    if os.environ.get("FAKE_STDERR"):
        sys.stderr.write(os.environ["FAKE_STDERR"])
        sys.stderr.flush()

    if hang:
        # A wedged CLI — the case the wall clock exists for. Without this the
        # fake always reaches EOF on its own and no timeout test is honest.
        time.sleep(hang)
    return int(os.environ.get("FAKE_EXIT_CODE", "0"))


if __name__ == "__main__":
    raise SystemExit(main())
