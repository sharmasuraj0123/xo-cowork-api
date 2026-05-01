"""
Microsoft OneDrive connector via rclone (rclone backend type `onedrive`,
option 41 in `rclone config`'s backend list).

Mirrors the architecture of `gdrive_rclone.py`. Only the deltas that matter:

  1. Subprocess: `rclone authorize --auth-no-open-browser onedrive`
  2. Auth URL is hosted on Microsoft (login.microsoftonline.com / login.live.com),
     not Google.
  3. A working onedrive remote needs more fields than gdrive does. After
     capturing the OAuth token from `rclone authorize`, we call Microsoft
     Graph (`GET /v1.0/me/drive`) to discover `drive_id` and `drive_type`,
     then write all five required fields to rclone.conf:
         [name]
         type       = onedrive
         region     = global
         token      = {json}
         drive_id   = <from graph>
         drive_type = personal | business | documentLibrary

The rclone daemon (`rclone rcd`) and `rclone.conf` are shared with the gdrive
connector — there is no separate daemon for OneDrive. The cross-connector
OAuth lock (`rclone_oauth_lock`) guarantees that only ONE OAuth flow runs at
a time across all connectors that share rclone's :53682 callback.
"""

import asyncio
import json
import logging
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

import httpx

from .gdrive_rclone import (
    OAUTH_TIMEOUT,
    RCLONE_CONFIG_PATH,
    SESSION_TTL,
    _NAME_RE,
    _PipeReader,
    _deliver_code_to_rclone,
    _rc_post,
    _resolve_oauth_url,
    ensure_rclone_running,
    rclone_available,
)
from .rclone_oauth_lock import (
    cancel_all_active_oauth,
    has_active_oauth,
    register_sessions,
)

log = logging.getLogger(__name__)

# Re-export so route module can import a single name space.
__all__ = [
    "ensure_rclone_running",
    "rclone_available",
    "list_onedrive_remotes",
    "validate_remote_name",
    "create_remote_session",
    "cancel_session",
    "delete_remote",
    "get_session",
]


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------

SessionStatus = Literal["pending", "awaiting_oauth", "completed", "failed", "cancelled"]


@dataclass
class OneDriveSession:
    session_id: str
    remote_name: str
    status: SessionStatus = "pending"
    auth_url: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    oauth_started_at: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    verification_input: str | None = None
    needs_manual_code: bool = True
    oauth_state: str | None = None


_sessions: dict[str, OneDriveSession] = {}

# Share the OAuth-port lock with gdrive (and any other rclone-OAuth connector).
register_sessions(lambda: _sessions.values(), lambda sid: cancel_session(sid))


def get_session(session_id: str) -> OneDriveSession | None:
    return _sessions.get(session_id)


def _expire_sessions() -> None:
    now = time.time()
    for sid in [k for k, v in _sessions.items() if now - v.created_at > SESSION_TTL]:
        s = _sessions.pop(sid)
        if s.task and not s.task.done():
            s.task.cancel()


# ---------------------------------------------------------------------------
# Remote listing
# ---------------------------------------------------------------------------

async def list_onedrive_remotes() -> list[dict]:
    try:
        data = await _rc_post("/config/listremotes")
        names: list[str] = data.get("remotes") or []
    except Exception as exc:
        raise RuntimeError(f"Could not reach rclone: {exc}") from exc

    remotes = []
    for name in names:
        try:
            cfg = await _rc_post("/config/get", {"name": name})
            if cfg.get("type") == "onedrive":
                remotes.append({
                    "name": name,
                    "type": "onedrive",
                    "drive_type": cfg.get("drive_type", ""),
                    "region": cfg.get("region", "global"),
                    "complete": bool(cfg.get("token")) and bool(cfg.get("drive_id")),
                })
        except Exception:
            pass
    return remotes


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

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

_AUTH_URL_RE = re.compile(
    r"https?://\S+(?:auth\?state|login\.microsoftonline\.com|login\.live\.com|oauth2/.+/authorize)\S*"
)


def _extract_auth_url(line: str) -> str | None:
    m = _AUTH_URL_RE.search(line)
    return m.group(0) if m else None


async def _resolve_default_drive(token_json: str) -> tuple[str, str]:
    """
    Call Microsoft Graph /me/drive with the freshly captured OAuth token to
    discover the user's default drive id + type. rclone needs both fields in
    rclone.conf for a fully working onedrive remote.
    """
    try:
        token_obj = json.loads(token_json)
    except Exception as exc:
        raise RuntimeError(f"Could not parse OAuth token JSON: {exc}") from exc

    access_token = token_obj.get("access_token")
    if not access_token:
        raise RuntimeError("OAuth token JSON had no access_token field.")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://graph.microsoft.com/v1.0/me/drive",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if not resp.is_success:
            raise RuntimeError(
                f"Microsoft Graph /me/drive failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()

    drive_id = body.get("id") or ""
    drive_type = body.get("driveType") or "personal"
    if not drive_id:
        raise RuntimeError("Microsoft Graph /me/drive response had no drive id.")
    return drive_id, drive_type


async def _run_oauth_flow(session: OneDriveSession) -> None:
    """
    Mirrors gdrive_rclone._run_oauth_flow but for OneDrive — manual paste:
      1. Spawn `rclone authorize --auth-no-open-browser onedrive`
      2. Read local /auth URL from stderr → resolve Microsoft URL via 307
      3. User signs in at Microsoft, pastes redirect URL into the UI
      4. Bridge delivers code locally to rclone's callback server
      5. Capture token JSON from stdout
      6. Resolve drive_id + drive_type via Microsoft Graph
      7. Append a complete rclone.conf section
      8. Verify the remote shows up in /config/listremotes
    """
    name = session.remote_name
    proc: subprocess.Popen | None = None

    try:
        log.info("OneDrive %s: spawning rclone authorize", session.session_id)
        proc = subprocess.Popen(
            ["rclone", "authorize", "--auth-no-open-browser", "onedrive",
             f"--config={RCLONE_CONFIG_PATH}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        stderr_reader = _PipeReader(proc.stderr, "stderr")
        stdout_reader = _PipeReader(proc.stdout, "stdout")

        # ── Wait for the local auth URL on stderr ────────────────────────
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

        # Resolve the actual Microsoft URL via the local /auth → 307 Location.
        try:
            ms_url, state = await _resolve_oauth_url(local_auth_url)
        except Exception as exc:
            session.status = "failed"
            session.error = f"Could not resolve Microsoft auth URL: {exc}"
            proc.kill()
            return

        session.auth_url = ms_url
        session.oauth_state = state
        session.oauth_started_at = time.time()
        session.status = "awaiting_oauth"
        log.info(
            "OneDrive %s: Microsoft auth URL ready (state=%s…)",
            session.session_id, state[:8],
        )

        # ── Wait for paste → deliver code locally → rclone exits ─────────
        log.info("OneDrive %s: waiting for user to paste redirect URL...", session.session_id)
        complete_deadline = time.time() + OAUTH_TIMEOUT
        delivered = False
        while time.time() < complete_deadline:
            if session.status == "cancelled":
                proc.kill()
                return
            if proc.poll() is not None:
                break
            if not delivered and session.verification_input:
                code = session.verification_input
                try:
                    await _deliver_code_to_rclone(state, code)
                    delivered = True
                    log.info("OneDrive %s: delivered code to rclone callback", session.session_id)
                except Exception as exc:
                    log.warning("OneDrive %s: delivery failed: %s", session.session_id, exc)
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

        await asyncio.sleep(0.5)
        exit_code = proc.returncode
        log.info(
            "OneDrive %s: rclone authorize exited with code %s",
            session.session_id, exit_code,
        )
        if exit_code != 0:
            session.status = "failed"
            session.error = (
                f"rclone authorize failed (exit code {exit_code}).\n"
                + "\n".join(stderr_reader.lines[-5:])
            )
            return

        # ── Extract token JSON from stdout ───────────────────────────────
        token_json: str | None = None
        for line in stdout_reader.lines:
            if "access_token" in line:
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

        log.info("OneDrive %s: captured token (%d chars)", session.session_id, len(token_json))

        # ── Resolve drive_id + drive_type via Microsoft Graph ────────────
        try:
            drive_id, drive_type = await _resolve_default_drive(token_json)
        except Exception as exc:
            session.status = "failed"
            session.error = f"Could not look up your OneDrive: {exc}"
            return

        log.info(
            "OneDrive %s: resolved drive_id=%s drive_type=%s",
            session.session_id, drive_id[:16] + "…", drive_type,
        )

        # ── Write remote section directly to rclone.conf ─────────────────
        # See gdrive_rclone for why we write the INI directly instead of
        # going through /config/create (port 53682 conflicts).
        config_section = (
            f"\n[{name}]\n"
            f"type = onedrive\n"
            f"region = global\n"
            f"token = {token_json}\n"
            f"drive_id = {drive_id}\n"
            f"drive_type = {drive_type}\n"
        )
        try:
            with open(RCLONE_CONFIG_PATH, "a", encoding="utf-8") as f:
                f.write(config_section)
            log.info(
                "OneDrive %s: wrote remote '%s' to %s",
                session.session_id, name, RCLONE_CONFIG_PATH,
            )
        except Exception as exc:
            session.status = "failed"
            session.error = f"Could not write rclone.conf: {exc}"
            return

        # ── Verify ───────────────────────────────────────────────────────
        for _ in range(10):
            if session.status == "cancelled":
                return
            await asyncio.sleep(1)
            try:
                remotes = await list_onedrive_remotes()
                found = next((r for r in remotes if r["name"] == name), None)
                if found and found.get("complete"):
                    session.status = "completed"
                    log.info("OneDrive %s: remote '%s' ready ✓", session.session_id, name)
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
        log.exception("OneDrive OAuth error in session %s", session.session_id)
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

async def create_remote_session(name: str, force: bool = False) -> OneDriveSession:
    _expire_sessions()
    if force:
        await cancel_all_active_oauth()
    if has_active_oauth():
        raise RuntimeError("Another connection is being set up. Please finish or cancel it first.")

    session_id = str(uuid.uuid4())
    session = OneDriveSession(session_id=session_id, remote_name=name)
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
