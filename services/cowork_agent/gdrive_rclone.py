"""
Google Drive connector via rclone (CLI mode — no daemon, no port).

Architecture:
  1. Run `rclone authorize --auth-no-open-browser drive` as a subprocess.
     - Captures the Google auth URL from stderr.
     - Blocks until the OAuth callback is received on localhost:53682.
  2. If port 53682 is occupied, we start our OWN tiny HTTP server on a
     free port, capture the auth code ourselves, then deliver it to rclone's
     waiting process via HTTP GET to localhost:53682.
  3. rclone authorize prints the token JSON to stdout.
  4. We write the remote section directly into rclone.conf (no API call) so
     the next `rclone listremotes --config <path>` picks it up immediately.

All read/list/delete operations invoke `rclone` as a subprocess via
`_rclone_cli()` — no `rclone rcd` daemon is started.
"""

import asyncio
import json
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
from typing import Any, AsyncIterator, Literal

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

async def _rclone_cli(*args: str, timeout: int = 30) -> str:
    """
    Run an `rclone` CLI command and return stdout. Raises RuntimeError on non-zero
    exit. Always passes --config so commands target our project's rclone.conf.
    """
    full_args = ("rclone",) + args + ("--config", RCLONE_CONFIG_PATH)
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("rclone binary not found in PATH") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"rclone {args[0] if args else ''} timed out after {timeout}s")

    if proc.returncode != 0:
        err_text = (stderr or stdout).decode(errors="replace").strip()
        raise RuntimeError(f"rclone error: {err_text or f'exit code {proc.returncode}'}")
    return stdout.decode(errors="replace")


async def _rclone_cli_stdin_stream(
    *args: str,
    chunk_iter: AsyncIterator[bytes],
) -> None:
    """
    Spawn `rclone` and stream `chunk_iter` to its stdin. Raises RuntimeError on
    non-zero exit (with stderr tail). Always passes --config.

    No subprocess timeout — the natural backstop is the caller's HTTP request.
    On any error or early return, kills the subprocess in a finally block to
    avoid zombie rclone processes.
    """
    full_args = ("rclone",) + args + ("--config", RCLONE_CONFIG_PATH)
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("rclone binary not found in PATH") from exc

    assert proc.stdin is not None and proc.stderr is not None

    # Bounded stderr ring (last 64 KiB) for diagnostics on failure.
    stderr_buf = bytearray()
    _STDERR_MAX = 64 * 1024

    async def drain_stderr() -> None:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            stderr_buf.extend(chunk)
            if len(stderr_buf) > _STDERR_MAX:
                del stderr_buf[: len(stderr_buf) - _STDERR_MAX]

    client_disconnected = False

    async def feed_stdin() -> None:
        nonlocal client_disconnected
        try:
            async for chunk in chunk_iter:
                if not chunk:
                    continue
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # rclone closed stdin (e.g. exited early on validation failure).
            # Swallow — the non-zero return code surfaces the real error.
            pass
        except Exception as exc:
            # Starlette raises ClientDisconnect when the browser/proxy drops
            # the upload mid-stream. Re-raise as a clean RuntimeError so the
            # caller's exception handler maps it to a useful HTTP error.
            if exc.__class__.__name__ == "ClientDisconnect":
                client_disconnected = True
                return
            raise
        finally:
            try:
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    stderr_task = asyncio.create_task(drain_stderr())
    try:
        await feed_stdin()
        rc = await proc.wait()
        await stderr_task
        if client_disconnected:
            raise RuntimeError(
                "Upload connection dropped before all bytes arrived. "
                "If you're behind the Next.js dev proxy, configure "
                "NEXT_PUBLIC_XO_COWORK_API_URL to bypass it."
            )
        if rc != 0:
            tail = bytes(stderr_buf).decode(errors="replace").strip()
            raise RuntimeError(tail or f"rclone exit {rc}")
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        if not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass


async def _rc_post(endpoint: str, body: dict | None = None, timeout: int = 15) -> dict:
    """
    Compatibility shim: maps the legacy rclone rc HTTP endpoints used by this
    project to local `rclone` CLI invocations, so we don't need a daemon
    listening on a port. Return shapes match the original rc JSON responses.
    """
    body = body or {}
    ep = endpoint.lstrip("/")

    if ep == "rc/noop":
        await _rclone_cli("version", timeout=timeout)
        return {}

    if ep == "config/listremotes":
        out = await _rclone_cli("listremotes", timeout=timeout)
        names = [line.rstrip(":").strip() for line in out.splitlines() if line.strip()]
        return {"remotes": names}

    if ep == "config/get":
        name = body.get("name", "")
        out = await _rclone_cli("config", "dump", timeout=timeout)
        try:
            dump = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Could not parse rclone config dump: {exc}") from exc
        return dump.get(name, {})

    if ep == "config/delete":
        name = body.get("name", "")
        if not name:
            raise RuntimeError("config/delete requires a 'name'")
        await _rclone_cli("config", "delete", name, timeout=timeout)
        return {}

    raise RuntimeError(f"Unsupported rclone CLI shim endpoint: {endpoint}")


# ---------------------------------------------------------------------------
# Availability checks (CLI mode — no daemon to keep alive)
# ---------------------------------------------------------------------------

async def ensure_rclone_running() -> None:
    """
    No-op in CLI mode. Logs whether the binary is reachable so startup
    surfaces missing-binary problems early instead of at first request.
    """
    try:
        await _rclone_cli("version", timeout=5)
        log.info("rclone CLI available (no daemon needed)")
    except Exception as exc:
        log.warning("rclone not available: %s", exc)


async def rclone_available() -> bool:
    try:
        await _rclone_cli("version", timeout=5)
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


async def mkdir_remote_path(name: str, path: str) -> None:
    """Create a folder on a configured remote via `rclone mkdir <name>:<path>`.

    Raises ValueError on invalid input; lets RuntimeError from the CLI bubble up
    so the router can surface it.
    """
    cleaned = path.strip().lstrip("/")
    if not cleaned:
        raise ValueError("Folder path is required.")
    segments = cleaned.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise ValueError("Invalid folder path.")
    await _rclone_cli("mkdir", f"{name}:{cleaned}", timeout=30)


async def delete_remote_folder(name: str, path: str) -> None:
    """Delete a folder on a configured remote via `rclone purge <name>:<path>`.

    `purge` removes the folder and all of its contents that rclone can see.
    Under drive.file scope, that's only files/folders rclone itself created.

    Raises ValueError on invalid input; lets RuntimeError from the CLI bubble up.
    """
    cleaned = path.strip().lstrip("/")
    if not cleaned:
        raise ValueError("Folder path is required.")
    segments = cleaned.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise ValueError("Invalid folder path.")
    await _rclone_cli("purge", f"{name}:{cleaned}", timeout=60)


def _validate_upload_filename(filename: str) -> str:
    """Validate and return the cleaned filename. Raises ValueError on bad input."""
    cleaned = filename.strip()
    if not cleaned:
        raise ValueError("Filename is required.")
    if "/" in cleaned or "\\" in cleaned:
        raise ValueError("Filename must not contain '/' or '\\'.")
    if cleaned in (".", ".."):
        raise ValueError("Invalid filename.")
    if any(ord(c) < 0x20 for c in cleaned):
        raise ValueError("Filename contains control characters.")
    return cleaned


async def upload_file_to_remote(
    name: str,
    folder_path: str,
    filename: str,
    size: int | None,
    chunk_iter: AsyncIterator[bytes],
) -> str:
    """Stream `chunk_iter` to `<name>:<folder_path>/<filename>` via `rclone rcat`.

    folder_path may be empty (root). Returns the cleaned remote-relative path.
    Raises ValueError on invalid path/filename; RuntimeError on rclone failure.
    """
    safe_name = _validate_upload_filename(filename)

    cleaned_folder = folder_path.strip().lstrip("/")
    if cleaned_folder:
        if any(seg in ("", ".", "..") for seg in cleaned_folder.split("/")):
            raise ValueError("Invalid folder path.")
        rel_path = f"{cleaned_folder}/{safe_name}"
    else:
        rel_path = safe_name

    target = f"{name}:{rel_path}"
    if size is not None:
        argv: tuple[str, ...] = ("rcat", "--size", str(size), target)
    else:
        argv = ("rcat", target)

    await _rclone_cli_stdin_stream(*argv, chunk_iter=chunk_iter)
    return rel_path


async def list_remote_folders(name: str) -> list[dict]:
    """List top-level folders on a remote via `rclone lsjson --dirs-only`.

    Returns a list of {name, modified} dicts. With drive.file scope, only
    folders rclone itself created are visible.
    """
    raw = await _rclone_cli(
        "lsjson", f"{name}:", "--dirs-only", "--max-depth", "1", timeout=30
    )
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"rclone returned non-JSON output: {exc}") from exc
    return [
        {"name": it.get("Name") or it.get("Path") or "", "modified": it.get("ModTime")}
        for it in items
        if it.get("IsDir")
    ]


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
             "--drive-scope=drive.file",
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
        #    We write the INI section directly to the config file instead of
        #    invoking `rclone config create`, because that command tries to
        #    validate/refresh the token and starts another auth webserver on
        #    port 53682 — causing port conflicts with our own listener.
        #    Subsequent CLI calls re-read rclone.conf, so the new remote
        #    is visible immediately.
        config_section = (
            f"\n[{name}]\n"
            f"type = drive\n"
            f"scope = drive.file\n"
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
