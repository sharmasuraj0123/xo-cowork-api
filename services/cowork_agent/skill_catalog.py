"""
On-demand skill install catalog.

Backs ``GET /api/skills/catalog`` and ``POST /api/skills/install``. The catalog
file (``config/skills/catalog.json``) is the server-side source of truth
mapping a skill name to one or more shell commands; clients only ever send a
name, and command text is never returned to them (entries may embed tokens or
host paths). Distinct from ``skill_installer.py``, which copies repo-bundled
skills at startup.

Catalog entry shape (one of ``command``/``commands`` is required):

    name             required, unique
    description      optional
    command          single shell command string
    commands         non-empty list of shell command strings, run
                     sequentially, stopping at the first failure
    timeout_seconds  optional, default 300 — applies per command
    cwd              optional working directory for every command

A missing or malformed catalog degrades to an empty catalog with a printed
warning; invalid entries are skipped, valid ones kept — matches the non-fatal
bootstrap pattern of ``skill_installer.py``.
"""

import asyncio
import contextlib
import json
from pathlib import Path

from services.cowork_agent.registry import agent_registry

_REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = _REPO_ROOT / "config" / "skills" / "catalog.json"

DEFAULT_TIMEOUT_SECONDS = 300
_OUTPUT_CAP = 10_000  # chars kept per stream per step

# One lock per skill name, guarding against concurrent installs of the same
# skill. In-process only — like chat_state.py, this does not coordinate across
# multiple workers.
_locks: dict[str, asyncio.Lock] = {}


class UnknownSkillError(KeyError):
    """Requested skill name is not in the catalog."""


class InstallInProgressError(RuntimeError):
    """An install for this skill name is already running in this process."""


def load_catalog() -> dict[str, dict]:
    """Read and validate the catalog file. Returns ``{name: entry}``."""
    try:
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"⚠️ skill catalog unreadable ({CATALOG_PATH}): {exc}")
        return {}

    entries = raw.get("skills") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        print(f"⚠️ skill catalog malformed ({CATALOG_PATH}): expected {{\"skills\": [...]}}")
        return {}

    catalog: dict[str, dict] = {}
    for entry in entries:
        normalized = _normalize(entry)
        if normalized is None:
            label = entry.get("name", "<unnamed>") if isinstance(entry, dict) else "<not an object>"
            print(f"⚠️ skill catalog: skipping invalid entry {label!r} in {CATALOG_PATH}")
            continue
        catalog[normalized["name"]] = normalized
    return catalog


async def install(name: str) -> dict:
    """Run the catalogued commands for ``name`` sequentially, fail-fast.

    Raises ``UnknownSkillError`` for names not in the catalog and
    ``InstallInProgressError`` if this process is already installing ``name``.
    A failing or timed-out command is not an exception — it is reported in the
    returned result (top-level ``ok`` is True only if every step succeeded).
    """
    entry = load_catalog().get(name)
    if entry is None:
        raise UnknownSkillError(name)

    lock = _lock_for(name)
    if lock.locked():
        raise InstallInProgressError(name)

    async with lock:
        steps: list[dict] = []
        ok = True
        for index, command in enumerate(entry["commands"]):
            try:
                rendered = _expand_placeholders(command)
            except Exception as exc:
                steps.append(_step_result(index, ok=False, exit_code=None, stdout="",
                                          stderr=f"placeholder expansion failed: {exc}",
                                          duration=0.0, timed_out=False))
                ok = False
                break
            step = await _run_step(index, rendered, entry["timeout_seconds"], entry["cwd"])
            steps.append(step)
            if not step["ok"]:
                ok = False
                break
        steps_total = len(entry["commands"])
        return {
            "name": name,
            "ok": ok,
            "summary": _summarize(name, ok, steps, steps_total),
            "steps": steps,
            "steps_total": steps_total,
            "steps_run": len(steps),
        }


def _summarize(name: str, ok: bool, steps: list[dict], steps_total: int) -> str:
    """Build a human-facing one-line summary the frontend can show as-is."""
    plural = "s" if steps_total != 1 else ""
    if ok:
        return f"Installed {name!r} ({steps_total} step{plural})."

    failed = steps[-1] if steps else None
    at = f"step {len(steps)} of {steps_total}"
    if failed is None:
        return f"Install of {name!r} failed."
    if failed["timed_out"]:
        return f"Install of {name!r} timed out at {at}."
    if failed["exit_code"] is None:
        # Command never started (bad cwd, missing binary, placeholder error).
        return f"Install of {name!r} failed at {at}: command could not start."
    return f"Install of {name!r} failed at {at} (exit code {failed['exit_code']})."


def _expand_placeholders(command: str) -> str:
    """Substitute known ``{token}`` placeholders from the active agent.

    Supported tokens (resolved from the active agent's manifest, so the
    catalog never names a backend):

        {skills_dir}  — ``<home_dir>/skills`` for the active agent
        {home_dir}    — the active agent's home directory
        {agent_name}  — the active agent's name

    Unknown ``{...}`` sequences are left verbatim so a literal brace in a
    command survives. Tokens are only resolved lazily if the command actually
    contains one, so placeholder-free commands never touch the registry.
    """
    if "{" not in command:
        return command

    agent = agent_registry.get_active_agent()
    tokens = {
        "skills_dir": str(agent.home_dir / "skills"),
        "home_dir": str(agent.home_dir),
        "agent_name": agent.name,
    }

    class _Passthrough(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    return command.format_map(_Passthrough(tokens))


def _lock_for(name: str) -> asyncio.Lock:
    return _locks.setdefault(name, asyncio.Lock())


def _normalize(entry) -> dict | None:
    """Validate one raw catalog entry; None means invalid (skip it)."""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    command, commands = entry.get("command"), entry.get("commands")
    if isinstance(command, str) and command.strip() and commands is None:
        resolved = [command]
    elif (
        command is None
        and isinstance(commands, list)
        and commands
        and all(isinstance(c, str) and c.strip() for c in commands)
    ):
        resolved = list(commands)
    else:
        return None

    timeout = entry.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        return None
    cwd = entry.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return None

    return {
        "name": name.strip(),
        "description": entry.get("description") or "",
        "commands": resolved,
        "timeout_seconds": timeout,
        "cwd": cwd,
    }


async def _run_step(index: int, command: str, timeout_seconds: float, cwd: str | None) -> dict:
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except Exception as exc:
        return _step_result(index, ok=False, exit_code=None, stdout="",
                            stderr=f"failed to start command: {exc}",
                            duration=loop.time() - started, timed_out=False)

    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        stdout_b, stderr_b = b"", b""

    exit_code = proc.returncode
    return _step_result(
        index,
        ok=(not timed_out and exit_code == 0),
        exit_code=exit_code,
        stdout=stdout_b.decode(errors="replace")[:_OUTPUT_CAP],
        stderr=stderr_b.decode(errors="replace")[:_OUTPUT_CAP],
        duration=loop.time() - started,
        timed_out=timed_out,
    )


def _step_result(index, *, ok, exit_code, stdout, stderr, duration, timed_out) -> dict:
    return {
        "index": index,
        "ok": ok,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": round(duration, 3),
        "timed_out": timed_out,
    }
