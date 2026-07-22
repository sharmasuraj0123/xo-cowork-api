"""claude_code Remote Control lifecycle.

Start / stop / inspect a local ``claude remote-control`` server so the
workspace's Claude Code session can be driven from the Claude mobile app or
claude.ai/code. The session runs *here* (this workspace's filesystem); the
phone/browser is only a window into it.

Design notes (see docs/superpowers/specs/2026-07-22-claude-remote-control-design.md):
  * cwd is the projects root; one workspace-level session (one button).
  * Auth: native login only. RC rejects CLAUDE_CODE_OAUTH_TOKEN / API keys, so we
    always strip the three token vars and require ~/.claude/.credentials.json.
  * Two non-interactive gates (trust + enable dialog) are pre-seeded here and at boot.
  * Detached process tracked by a PID file; outlives API restarts; stop() kills the group.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from services.cowork_agent.adapters.cli_status import resolve_binary
from services.cowork_agent.project_layout import xo_projects_root

PID_FILE = Path("/tmp/xo-rc.pid")
LOG_FILE = Path("/tmp/xo-rc.log")

# Auth vars Remote Control can never use — always dropped so the CLI falls back
# to the native ~/.claude/.credentials.json login.
_TOKEN_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")

_CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
_GLOBAL_CONFIG = Path(os.path.expanduser("~/.claude.json"))

_SESSION_URL_RE = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9]+")


# ── process helpers ──────────────────────────────────────────────────────────

def _read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` is a live process. Reaps it if it's our own zombie child
    (crash-exit while we were the parent); tolerates non-child pids, so a session
    started by a previous API instance and re-adopted by init still reads as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False  # was a zombie of ours — now reaped
    except ChildProcessError:
        pass  # not our child (e.g. after an API restart) — but it is alive
    except OSError:
        pass
    return True


def _running_pid() -> Optional[int]:
    pid = _read_pid()
    if pid is not None and _pid_alive(pid):
        return pid
    return None


def _session_url() -> Optional[str]:
    try:
        text = LOG_FILE.read_text(errors="ignore")
    except OSError:
        return None
    matches = _SESSION_URL_RE.findall(text)
    return matches[-1] if matches else None


# ── auth / gate helpers ──────────────────────────────────────────────────────

def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in _TOKEN_VARS:
        env.pop(key, None)
    return env


def native_login_present() -> bool:
    """True when a native claude.ai OAuth session exists — the only credential
    Remote Control accepts. Written by the 'Connect Claude' (`claude auth login`)
    flow, self-refreshing thereafter."""
    for name in (".credentials.json", "credentials.json"):
        p = _CLAUDE_HOME / name
        try:
            if p.is_file() and p.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def ensure_gates_seeded() -> None:
    """Idempotently pre-clear the trust + enable dialogs in ~/.claude.json.

    Skips the write when both are already set (the steady state after
    ``setup.sh`` seeds them at boot), so we don't race the CLI, which rewrites
    this file on exit. Only writes when something is missing, atomically.
    """
    projects_root = str(xo_projects_root())
    try:
        data = json.loads(_GLOBAL_CONFIG.read_text()) if _GLOBAL_CONFIG.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    projects = data.get("projects")
    proj_entry = projects.get(projects_root) if isinstance(projects, dict) else None
    trust_ok = isinstance(proj_entry, dict) and proj_entry.get("hasTrustDialogAccepted") is True
    dialog_ok = data.get("remoteDialogSeen") is True
    if trust_ok and dialog_ok:
        return  # already seeded — no write, no race

    data["remoteDialogSeen"] = True
    if not isinstance(data.get("projects"), dict):
        data["projects"] = {}
    entry = data["projects"].get(projects_root)
    if not isinstance(entry, dict):
        entry = {}
    entry["hasTrustDialogAccepted"] = True
    data["projects"][projects_root] = entry

    tmp = tempfile.NamedTemporaryFile(
        "w", dir=str(_GLOBAL_CONFIG.parent), delete=False, suffix=".xo-rc.tmp"
    )
    try:
        json.dump(data, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, _GLOBAL_CONFIG)
    except OSError:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


# ── public API (start / stop / status) ───────────────────────────────────────

def _default_name() -> str:
    return (os.getenv("XO_WORKSPACE_NAME", "") or "").strip() or socket.gethostname()


def status() -> dict[str, Any]:
    login = native_login_present()
    pid = _running_pid()
    if pid is None:
        return {"running": False, "login_present": login, "session_url": None}
    return {
        "running": True,
        "login_present": login,
        "pid": pid,
        "name": _default_name(),
        "session_url": _session_url(),
        "projects_root": str(xo_projects_root()),
    }


def start(name: Optional[str] = None) -> dict[str, Any]:
    """Launch the Remote Control server (idempotent). Returns the status dict, or
    ``{ok: False, error}`` when the native login is missing."""
    if _running_pid() is not None:
        return {"ok": True, "already_running": True, **status()}

    if not native_login_present():
        return {
            "ok": False,
            "running": False,
            "login_present": False,
            "error": "no_native_login",
            "detail": (
                "Remote Control needs a native claude.ai login "
                "(~/.claude/.credentials.json). Run the Connect Claude flow first."
            ),
        }

    ensure_gates_seeded()

    binary = resolve_binary("CLAUDE_CLI_PATH", "claude")
    label = (name or "").strip() or _default_name()
    # NB: no `-c`/`--continue`. It errors out ("No recent session found in this
    # directory") whenever there's no prior RC session — i.e. on every first Start
    # — so each Start creates a fresh session instead.
    cmd = [binary, "remote-control", "--name", label]

    logf = open(LOG_FILE, "ab")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(xo_projects_root()),
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own session/group → detached, killpg-able
        )
    finally:
        logf.close()

    PID_FILE.write_text(str(proc.pid))
    return {"ok": True, "already_running": False, **status()}


def stop() -> dict[str, Any]:
    """Stop the Remote Control server. Kills the whole process group so no child
    session survives. Idempotent."""
    pid = _read_pid()
    if pid is None:
        return {"ok": True, "running": False}

    if _pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        # brief grace, then hard kill if still up
        for _ in range(10):
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        if _pid_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass

    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return {"ok": True, "running": False}
