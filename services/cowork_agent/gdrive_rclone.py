"""
Google Drive connector via rclone.

Architecture (correct for rclone v1.57+):
  1. Run `rclone authorize --auth-no-open-browser drive` as a subprocess.
     - Captures the Google auth URL from stderr.
     - Blocks until the OAuth callback is received on localhost:53682.
  2. If port 53682 is occupied, we start our OWN tiny HTTP server on a
     free port, capture the auth code ourselves, then deliver it to rclone's
     waiting process via HTTP GET to localhost:53682.
  3. rclone authorize prints the token JSON to stdout.
  4. We write the remote config directly via /config/create + the token,
     WITHOUT going through the interactive state machine (which blocks).
"""

import asyncio
import logging
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from .rclone_oauth_lock import (
    cancel_all_active_oauth,
    has_active_oauth,
    register_sessions,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RCLONE_RC_URL  = os.getenv("RCLONE_RCD_URL",  "http://127.0.0.1:5572")
RCLONE_RC_USER = os.getenv("RCLONE_RCD_USER", "")
RCLONE_RC_PASS = os.getenv("RCLONE_RCD_PASS", "")

# rclone config file — stored inside the project directory
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RCLONE_CONFIG_PATH = os.getenv(
    "RCLONE_CONFIG",
    os.path.join(_PROJECT_ROOT, "rclone.conf"),
)

# rclone's OAuth callback port — hardcoded by Google's OAuth client registration.
# Port 53682 is embedded in rclone's bundled Google OAuth credentials.
RCLONE_OAUTH_PORT = int(os.getenv("RCLONE_OAUTH_PORT", "53682"))

SESSION_TTL    = 600   # 10 min
OAUTH_TIMEOUT  = 300   # 5 min

# ---------------------------------------------------------------------------
# Port helpers
# ---------------------------------------------------------------------------

def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------

SessionStatus = Literal["pending", "awaiting_oauth", "completed", "failed", "cancelled"]


@dataclass
class GDriveSession:
    session_id: str
    remote_name: str
    status: SessionStatus = "pending"
    auth_url: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    oauth_started_at: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    # Set by POST /sessions/{id}/submit when user pastes the redirect URL
    verification_input: str | None = None
    # Always True now — manual paste is the only supported path. The user's
    # browser cannot reach the workspace's :53682 unless an IDE port-forward
    # is active, so we don't rely on an automatic browser-side callback.
    needs_manual_code: bool = True
    # rclone's OAuth state token, captured from the local /auth URL and
    # replayed back when delivering the code to rclone's local callback.
    oauth_state: str | None = None


_sessions: dict[str, GDriveSession] = {}

# Register with the cross-connector OAuth lock — onedrive_rclone (or any
# other rclone-backed connector) shares port 53682, so only ONE OAuth flow
# can be active at a time across all of them.
register_sessions(lambda: _sessions.values(), lambda sid: cancel_session(sid))


def get_session(session_id: str) -> GDriveSession | None:
    return _sessions.get(session_id)


def _expire_sessions() -> None:
    now = time.time()
    for sid in [k for k, v in _sessions.items() if now - v.created_at > SESSION_TTL]:
        s = _sessions.pop(sid)
        if s.task and not s.task.done():
            s.task.cancel()


# ---------------------------------------------------------------------------
# rclone RC helpers
# ---------------------------------------------------------------------------

def _rc_auth() -> tuple[str | None, str | None]:
    return (RCLONE_RC_USER or None), (RCLONE_RC_PASS or None)


async def _rc_post(endpoint: str, body: dict | None = None, timeout: int = 15) -> dict:
    u, p = _rc_auth()
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{RCLONE_RC_URL}/{endpoint.lstrip('/')}",
            json=body or {},
            auth=(u, p) if u else None,
        )
        if not resp.is_success:
            try:
                msg = resp.json().get("error", resp.text)
            except Exception:
                msg = resp.text or f"HTTP {resp.status_code}"
            raise RuntimeError(f"rclone error: {msg}")
        return resp.json()


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------

_rclone_proc: subprocess.Popen | None = None


async def ensure_rclone_running() -> None:
    try:
        await _rc_post("/rc/noop")
        log.info("rclone rcd already running at %s", RCLONE_RC_URL)
        return
    except Exception:
        pass

    log.info("Starting rclone rcd ...")
    global _rclone_proc
    try:
        host_port = RCLONE_RC_URL.replace("http://", "").replace("https://", "")
        _rclone_proc = subprocess.Popen(
            ["rclone", "rcd", "--rc-no-auth",
             f"--rc-addr={host_port}",
             f"--config={RCLONE_CONFIG_PATH}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        log.warning("rclone not found in PATH")
        return

    for _ in range(10):
        await asyncio.sleep(0.5)
        try:
            await _rc_post("/rc/noop")
            log.info("rclone rcd started (pid=%d)", _rclone_proc.pid)
            return
        except Exception:
            pass
    log.error("rclone rcd did not start in time")


async def rclone_available() -> bool:
    try:
        await _rc_post("/rc/noop")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Remote listing
# ---------------------------------------------------------------------------

async def list_drive_remotes() -> list[dict]:
    try:
        data = await _rc_post("/config/listremotes")
        names: list[str] = data.get("remotes") or []
    except Exception as exc:
        raise RuntimeError(f"Could not reach rclone: {exc}") from exc

    remotes = []
    for name in names:
        try:
            cfg = await _rc_post("/config/get", {"name": name})
            if cfg.get("type") == "drive":
                remotes.append({
                    "name": name,
                    "type": "drive",
                    "scope": cfg.get("scope", "drive"),
                    "complete": bool(cfg.get("token")),
                })
        except Exception:
            pass
    return remotes


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


async def validate_remote_name(name: str) -> str | None:
    if not _NAME_RE.match(name):
        return "Name must be 1-32 chars: lowercase letters, digits, _ or -"
    try:
        data = await _rc_post("/config/listremotes")
        if name in (data.get("remotes") or []):
            return "A remote with this name already exists."
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

def _extract_auth_url(line: str) -> str | None:
    """Find the Google/rclone auth URL in a log line."""
    m = re.search(r"https?://\S+(?:auth\?state|accounts\.google\.com/o/oauth)\S*", line)
    return m.group(0) if m else None


class _PipeReader:
    """
    Drains a subprocess pipe in a background thread.

    This prevents the classic Windows subprocess deadlock where one pipe's
    buffer fills up because we stopped reading it, blocking the child process
    from writing to the OTHER pipe we're trying to read.
    """

    def __init__(self, pipe, name: str = "pipe"):
        self.lines: list[str] = []
        self.name = name
        self._pipe = pipe
        self._thread = threading.Thread(target=self._drain, daemon=True, name=f"rclone-{name}")
        self._thread.start()

    def _drain(self):
        try:
            for raw_line in self._pipe:
                line = raw_line.decode(errors="replace").strip()
                if line:
                    self.lines.append(line)
        except Exception:
            pass

    def join(self, timeout: float | None = None):
        self._thread.join(timeout=timeout)


async def _resolve_oauth_url(local_auth_url: str) -> tuple[str, str]:
    """
    GET rclone's local /auth?state=... endpoint (no redirect-following) and
    return (provider_url, state). rclone responds with HTTP 307 + Location
    set to the real provider OAuth URL (Google, Microsoft, ...).
    """
    parsed = urllib.parse.urlparse(local_auth_url)
    state = urllib.parse.parse_qs(parsed.query).get("state", [""])[0]
    if not state:
        raise RuntimeError(f"Could not parse state from {local_auth_url!r}")
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        resp = await client.get(local_auth_url)
        if resp.status_code not in (301, 302, 303, 307, 308):
            raise RuntimeError(
                f"rclone /auth returned HTTP {resp.status_code} (expected redirect)"
            )
        location = resp.headers.get("location")
        if not location:
            raise RuntimeError("rclone /auth response had no Location header")
    return location, state


async def _deliver_code_to_rclone(state: str, code: str) -> None:
    """Deliver the OAuth code to rclone's local callback server."""
    callback_url = (
        f"http://127.0.0.1:{RCLONE_OAUTH_PORT}/"
        f"?state={urllib.parse.quote(state, safe='')}"
        f"&code={urllib.parse.quote(code, safe='')}"
    )
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        await client.get(callback_url)


async def _run_oauth_flow(session: GDriveSession) -> None:
    """
    Complete OAuth flow using `rclone authorize` subprocess + manual paste.

    Both stdout and stderr are drained concurrently via background threads
    to prevent pipe buffer deadlocks (critical on Windows).

    Flow:
      1. Spawn: rclone authorize --auth-no-open-browser drive
      2. Poll stderr for the local URL → resolve Google URL via 307 redirect
      3. User signs in at Google; pastes the redirect URL into the UI
      4. Bridge delivers code locally to rclone's callback server
      5. Poll stdout for token JSON
      6. Write remote section directly to rclone.conf
    """
    name = session.remote_name
    proc: subprocess.Popen | None = None

    try:
        # ── 1. Spawn rclone authorize ────────────────────────────────────
        log.info("GDrive %s: spawning rclone authorize", session.session_id)
        proc = subprocess.Popen(
            ["rclone", "authorize", "--auth-no-open-browser", "drive",
             f"--config={RCLONE_CONFIG_PATH}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Start concurrent pipe readers (prevents deadlock)
        stderr_reader = _PipeReader(proc.stderr, "stderr")
        stdout_reader = _PipeReader(proc.stdout, "stdout")

        # ── 2. Wait for the local auth URL on stderr ─────────────────────
        local_auth_url: str | None = None
        url_deadline = time.time() + 15

        while time.time() < url_deadline:
            if session.status == "cancelled":
                proc.kill()
                return
            for line in stderr_reader.lines:
                url = _extract_auth_url(line)
                if url:
                    local_auth_url = url
                    break
            if local_auth_url:
                break
            await asyncio.sleep(0.3)

        if not local_auth_url:
            session.status = "failed"
            session.error = (
                "rclone authorize did not produce an auth URL.\n"
                + "\n".join(stderr_reader.lines[-5:])
            )
            proc.kill()
            return

        # Resolve the actual Google URL via the local /auth → 307 Location.
        try:
            google_url, state = await _resolve_oauth_url(local_auth_url)
        except Exception as exc:
            session.status = "failed"
            session.error = f"Could not resolve Google auth URL: {exc}"
            proc.kill()
            return

        session.auth_url = google_url
        session.oauth_state = state
        session.oauth_started_at = time.time()
        session.status = "awaiting_oauth"
        log.info(
            "GDrive %s: Google auth URL ready (state=%s…)",
            session.session_id, state[:8],
        )

        # ── 3. Wait for paste → deliver code locally → rclone exits ──────
        log.info("GDrive %s: waiting for user to paste redirect URL...", session.session_id)
        complete_deadline = time.time() + OAUTH_TIMEOUT
        delivered = False

        while time.time() < complete_deadline:
            if session.status == "cancelled":
                proc.kill()
                return
            if proc.poll() is not None:
                break  # rclone exited (success or error)
            if not delivered and session.verification_input:
                code = session.verification_input
                try:
                    await _deliver_code_to_rclone(state, code)
                    delivered = True
                    log.info("GDrive %s: delivered code to rclone callback", session.session_id)
                except Exception as exc:
                    log.warning("GDrive %s: delivery failed: %s", session.session_id, exc)
                    session.verification_input = None
            await asyncio.sleep(1)
        else:
            session.status = "failed"
            session.error = (
                "Timed out waiting for paste. Click Cancel and try again."
                if not delivered
                else "Timed out after delivering the code to rclone."
            )
            proc.kill()
            return

        # Give reader threads a moment to flush
        await asyncio.sleep(0.5)
        exit_code = proc.returncode
        log.info("GDrive %s: rclone authorize exited with code %s", session.session_id, exit_code)

        if exit_code != 0:
            session.status = "failed"
            session.error = (
                f"rclone authorize failed (exit code {exit_code}).\n"
                + "\n".join(stderr_reader.lines[-5:])
            )
            return

        # Extract token from captured stdout
        token_json: str | None = None
        for line in stdout_reader.lines:
            if "access_token" in line:
                # Strip wrapping text, keep only the JSON
                stripped = line.strip()
                if stripped.startswith("{"):
                    token_json = stripped
                break

        if not token_json:
            session.status = "failed"
            session.error = (
                "Auth succeeded but no token was found in rclone output.\n"
                "stdout lines: " + " | ".join(stdout_reader.lines[-5:])
            )
            return

        log.info("GDrive %s: captured token (%d chars)", session.session_id, len(token_json))

        # ── 5. Write remote directly to rclone.conf ──────────────────────
        #    We write the INI section directly to the config file instead
        #    of using the RC API, because config/create and config/update
        #    both try to validate/refresh the token, which starts an auth
        #    webserver on port 53682 — causing port conflicts.
        #    rclone rcd auto-detects config file changes.
        config_section = (
            f"\n[{name}]\n"
            f"type = drive\n"
            f"scope = drive\n"
            f"token = {token_json}\n"
        )
        try:
            with open(RCLONE_CONFIG_PATH, "a", encoding="utf-8") as f:
                f.write(config_section)
            log.info("GDrive %s: wrote remote '%s' to %s", session.session_id, name, RCLONE_CONFIG_PATH)
        except Exception as exc:
            session.status = "failed"
            session.error = f"Could not write rclone.conf: {exc}"
            return

        # ── 6. Verify the remote is configured ───────────────────────────
        for attempt in range(10):
            if session.status == "cancelled":
                return
            await asyncio.sleep(1)
            try:
                remotes = await list_drive_remotes()
                found = next((r for r in remotes if r["name"] == name), None)
                if found and found.get("complete"):
                    session.status = "completed"
                    log.info("GDrive %s: remote '%s' ready ✓", session.session_id, name)
                    return
            except Exception:
                pass

        session.status = "failed"
        session.error = "Remote was created but verification failed. Try refreshing."

    except asyncio.CancelledError:
        session.status = "cancelled"
        if proc and proc.poll() is None:
            proc.kill()
    except Exception as exc:
        log.exception("GDrive OAuth error in session %s", session.session_id)
        session.status = "failed"
        session.error = str(exc)
        if proc and proc.poll() is None:
            proc.kill()
        try:
            await _rc_post("/config/delete", {"name": name})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def create_remote_session(name: str, force: bool = False) -> GDriveSession:
    _expire_sessions()
    if force:
        await cancel_all_active_oauth()
    if has_active_oauth():
        raise RuntimeError("Another connection is being set up. Please finish or cancel it first.")

    session_id = str(uuid.uuid4())
    session = GDriveSession(session_id=session_id, remote_name=name)
    _sessions[session_id] = session
    session.task = asyncio.create_task(_run_oauth_flow(session))
    return session


async def cancel_session(session_id: str) -> None:
    session = _sessions.get(session_id)
    if not session:
        return
    session.status = "cancelled"
    if session.task and not session.task.done():
        session.task.cancel()
    try:
        await _rc_post("/config/delete", {"name": session.remote_name})
    except Exception:
        pass
    _sessions.pop(session_id, None)


async def delete_remote(name: str) -> None:
    await _rc_post("/config/delete", {"name": name})
