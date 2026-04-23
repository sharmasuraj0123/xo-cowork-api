"""
Subprocess command-runner utility.

A small wrapper over `asyncio.create_subprocess_exec` (and a sync sibling
over `subprocess.run`) so new code that needs to shell out — to the agent
CLI or anything else — has a single, consistent entry point with:

* timeout enforcement (kills the process instead of hanging forever),
* captured stdout+stderr (merged, so call sites can log one stream),
* optional append-to-log-file for background provisioning flows,
* structured `CommandResult` return type (no bare ints floating around).

Designed to pair with `services.cowork_agent.agent_registry.AgentManifest.command`,
which renders templated argvs from the manifest — those argvs go directly
into `run` / `run_sync` here.

Examples
--------
    # Async (inside a route handler or background task):
    from utils.commands import run
    from services.cowork_agent.agent_registry import get_default_agent

    agent = get_default_agent()
    argv = agent.command("models_set", model="anthropic/claude-opus-4.6")
    result = await run(argv, cwd=agent.cwd, timeout=agent.cli_timeout_seconds)
    if not result.ok:
        log.warning("cli failed: %s", result.output)

    # Sync (scripts, startup checks):
    from utils.commands import run_sync
    result = run_sync(["git", "rev-parse", "HEAD"])
    print(result.output.strip())

    # With a log file (background provisioning style):
    await run(argv, cwd=agent.cwd, log_path=agent.provisioning_log,
              log_label=f"provisioning: {provider_id}")

Security
--------
These helpers ONLY use `create_subprocess_exec` / `subprocess.run` with a
list argv — never `shell=True`. Do not add a `shell=True` path; callers
should pre-render argvs (e.g. via manifest command templates) so user
input never reaches a shell interpreter.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one subprocess run.

    `returncode` is -1 when the process was killed by the runner (timeout,
    binary-not-found, or another local exception) — check `ok` rather
    than testing for 0 directly when you want "finished cleanly".
    """

    argv: list[str]
    returncode: int
    output: str  # stdout + stderr, merged
    duration_seconds: float
    timed_out: bool = False
    binary_missing: bool = False
    exception: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.binary_missing


def _render_log_entry(ts: str, label: str, argv: Sequence[str], result: CommandResult) -> str:
    header = f"\n=== {ts} {label} ===\n" if label else f"\n=== {ts} ===\n"
    cmdline = " ".join(repr(a) if " " in a else a for a in argv)
    rc = (
        "timeout" if result.timed_out
        else "missing-binary" if result.binary_missing
        else "exception" if result.exception is not None
        else str(result.returncode)
    )
    tail = result.output
    if tail and not tail.endswith("\n"):
        tail += "\n"
    return f"{header}$ {cmdline}\n{tail}[exit {rc}]\n"


def _write_log(log_path: Path, entry: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(entry)


async def run(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    log_path: str | Path | None = None,
    log_label: str = "",
) -> CommandResult:
    """Run a command asynchronously and return a `CommandResult`.

    Parameters
    ----------
    argv:       command + args as a list — never a string (no shell).
    cwd:        working directory for the child process.
    timeout:    seconds before the runner kills the process. `None` = no timeout.
    env:        environment overrides; unset → inherit parent.
    log_path:   if provided, append a formatted log entry after the run.
    log_label:  prefix for the log entry (e.g. "provisioning: anthropic").
    """
    if not argv:
        raise ValueError("argv must be non-empty")

    argv_list = [str(a) for a in argv]
    ts = datetime.now(timezone.utc).isoformat()
    started = asyncio.get_event_loop().time()

    result: CommandResult
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv_list,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"{argv_list[0]} not found in PATH",
            duration_seconds=0.0,
            binary_missing=True,
        )
        if log_path is not None:
            _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
        return result
    except Exception as e:  # noqa: BLE001 — surface as CommandResult, never raise
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"[exception] {e}",
            duration_seconds=0.0,
            exception=str(e),
        )
        if log_path is not None:
            _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
        return result

    try:
        if timeout is not None:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        else:
            stdout, _ = await proc.communicate()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"[timed out after {timeout}s]",
            duration_seconds=asyncio.get_event_loop().time() - started,
            timed_out=True,
        )
        if log_path is not None:
            _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
        return result

    duration = asyncio.get_event_loop().time() - started
    result = CommandResult(
        argv=argv_list,
        returncode=proc.returncode if proc.returncode is not None else -1,
        output=stdout.decode(errors="replace"),
        duration_seconds=duration,
    )
    if log_path is not None:
        _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
    return result


def run_sync(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    log_path: str | Path | None = None,
    log_label: str = "",
) -> CommandResult:
    """Synchronous sibling of `run` — for scripts, startup probes, or tests.

    Do NOT call this from inside an async handler — it will block the
    event loop. Use `run` there.
    """
    if not argv:
        raise ValueError("argv must be non-empty")

    argv_list = [str(a) for a in argv]
    ts = datetime.now(timezone.utc).isoformat()
    import time
    started = time.monotonic()

    try:
        completed = subprocess.run(
            argv_list,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"{argv_list[0]} not found in PATH",
            duration_seconds=0.0,
            binary_missing=True,
        )
        if log_path is not None:
            _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
        return result
    except subprocess.TimeoutExpired as e:
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=(e.stdout or "") + (e.stderr or "") + f"\n[timed out after {timeout}s]",
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )
        if log_path is not None:
            _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
        return result
    except Exception as e:  # noqa: BLE001
        result = CommandResult(
            argv=argv_list,
            returncode=-1,
            output=f"[exception] {e}",
            duration_seconds=time.monotonic() - started,
            exception=str(e),
        )
        if log_path is not None:
            _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
        return result

    merged = (completed.stdout or "") + (completed.stderr or "")
    result = CommandResult(
        argv=argv_list,
        returncode=completed.returncode,
        output=merged,
        duration_seconds=time.monotonic() - started,
    )
    if log_path is not None:
        _write_log(Path(log_path), _render_log_entry(ts, log_label, argv_list, result))
    return result


async def run_chain(
    argvs: Sequence[Sequence[str]],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    log_path: str | Path | None = None,
    log_label: str = "",
    abort_on_failure: bool = True,
) -> list[CommandResult]:
    """Run a sequence of commands, optionally aborting on the first failure.

    Matches the provider/channel provisioning pattern — batch first, then
    post-commands — so those call sites can migrate to this helper later
    without reshaping their control flow.
    """
    results: list[CommandResult] = []
    for argv in argvs:
        result = await run(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            log_path=log_path,
            log_label=log_label,
        )
        results.append(result)
        if not result.ok and abort_on_failure:
            if log_path is not None:
                _write_log(
                    Path(log_path),
                    f"[chain aborted{' for ' + log_label if log_label else ''} at: {' '.join(argv)}]\n",
                )
            break
    return results
