"""
Connect Antigravity — agent-owned OAuth login flow (the ``routes`` capability).

Mounted **only when ``AGENT_NAME=antigravity``** (via ``_active_agent_routes`` in
``routers/cowork_agent/__init__.py``), so the agent literal stays inside the
adapter tree and the modularity invariant holds — this is why the flow lives here
rather than in ``routers/auth/`` like ``claude_setup_token.py``.

Mechanism (mirrors Connect Claude, adapted to agy):
  * agy has **no ``auth login`` subcommand**; a logged-out ``agy -p`` prints
    ``Authentication required. Please visit the URL to log in:`` followed by a
    Google OAuth URL, then waits and reads a pasted authorization code from stdin
    (verified live). So the login *trigger* is a throwaway ``agy -p`` under a PTY.
  * The authoritative "connected" check is the **token file** (agy has no auth
    CLI): ``adapters/antigravity/auth.has_usable_login()``. A background poller
    flips the flow to success the instant the token lands — so we never wait for
    (or pay for) the throwaway prompt to run; we kill agy as soon as auth writes
    the token.

Flow:
  POST /connect/antigravity (SSE)
    1. If already logged in → emit ``done`` immediately (idempotent).
    2. Spawn ``agy -p … --dangerously-skip-permissions`` under a PTY.
    3. Extract the OAuth URL from its output → emit ``auth_url``.
  POST /connect/antigravity/callback
    4. Write the pasted code (+CR) to the PTY master.
    5. When ``has_usable_login()`` flips true (or agy exits 0) → emit ``done``,
       kill agy, clear session. On agy error output / non-zero exit → ``error``.

Event shapes match Connect Claude (``session`` / ``auth_url`` / ``stdout`` /
``done`` / ``error``) so the frontend Connect component is reusable with a
different endpoint. On Unix a PTY is used; set ANTIGRAVITY_CONNECT_USE_PTY=0 to
force pipe mode (login prompts generally need the PTY).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from typing import AsyncGenerator, Optional
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.cowork_agent.adapters.antigravity.auth import has_usable_login

try:
    import pty as _pty
    import fcntl
    import struct
    import termios

    _HAS_PTY = hasattr(_pty, "openpty") and sys.platform != "win32"
except ImportError:
    _HAS_PTY = False


router = APIRouter(tags=["antigravity-connect"])


# ── ANSI stripping + URL extraction ───────────────────────────────────────────

_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_2CHAR_RE = re.compile(r"\x1b[@-Z\\-_]")
_ANSI_NF_RE = re.compile(r"\x1b[\x20-\x2f]+[\x30-\x7e]")


def _strip_ansi(line: str) -> str:
    line = _ANSI_OSC_RE.sub("", line)
    line = _ANSI_CSI_RE.sub("", line)
    line = _ANSI_NF_RE.sub("", line)
    line = _ANSI_2CHAR_RE.sub("", line)
    return line.replace("\x07", "")


# agy prints the OAuth URL on its own (indented) line after
# "Please visit the URL to log in:". Capture the Google OAuth authorize URL
# (host-flexible: any https URL bearing the oauth2 authorize path), with a
# redirect_uri-bearing fallback.
_AUTH_URL_RE = re.compile(r"https://[^\s\"'<>\x00-\x1f]*/o/oauth2/[^\s\"'<>\x00-\x1f]+")
_AUTH_URL_FALLBACK_RE = re.compile(r"https://[^\s\"'<>\x00-\x1f]+redirect_uri=[^\s\"'<>\x00-\x1f]+")


def _extract_auth_url(text: str) -> Optional[str]:
    m = _AUTH_URL_RE.search(text) or _AUTH_URL_FALLBACK_RE.search(text)
    if not m:
        return None
    raw = m.group(0)
    # Drop a concatenated redraw if the URL appears twice back-to-back.
    second = raw.find("https://", len("https://"))
    if second != -1:
        raw = raw[:second]
    return raw


def _normalize_code(raw_value: str) -> str:
    """Normalize a pasted value to the authorization code agy expects.

    Accepts the raw code, a full redirect URL, or a ``code=…&state=…`` string.
    agy already holds the PKCE verifier/state for the session, so we hand it the
    bare ``code`` when we can extract one; otherwise pass the value through."""
    value = (raw_value or "").strip()
    if not value:
        return value
    if value.startswith("http://") or value.startswith("https://"):
        params = parse_qs(urlparse(value).query)
        code = (params.get("code") or [None])[0]
        return code or value
    if "code=" in value:
        params = parse_qs(value)
        code = (params.get("code") or [None])[0]
        if code:
            return code
    return value


def _resolve_agy_path() -> str:
    path = (os.getenv("AGY_CLI_PATH") or "agy").strip()
    if not os.path.isabs(path):
        return shutil.which(path) or path
    return path


# The deployed Connect dialog detects login success only by scraping this exact
# phrase from a stdout line (its `isAuthSuccess`). agy prints no such line, so we
# emit a compat one alongside our own `done` event to flip the UI to "Connected".
_SUCCESS_COMPAT_LINE = "Authentication token created successfully"

CONNECT_TIMEOUT_SECONDS = int(os.getenv("ANTIGRAVITY_CONNECT_TIMEOUT", "180"))
SSE_HEARTBEAT_SECONDS = int(os.getenv("ANTIGRAVITY_CONNECT_HEARTBEAT", "15"))
_TRIGGER_MODEL = "Gemini 3.5 Flash (Low)"


def _pty_wanted() -> bool:
    flag = os.getenv("ANTIGRAVITY_CONNECT_USE_PTY", "1").strip().lower()
    return flag not in ("0", "false", "no", "off") and _HAS_PTY


def _set_pty_winsize(slave_fd: int, rows: int = 24, cols: int = 1000) -> None:
    """Wide width so the long OAuth URL never wraps (wrapping corrupts extraction)."""
    if not _HAS_PTY:
        return
    try:
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _disable_pty_echo(slave_fd: int) -> None:
    """Suppress terminal echo so the pasted code never lands in logs/SSE."""
    if not _HAS_PTY:
        return
    try:
        attrs = termios.tcgetattr(slave_fd)
        attrs[3] = attrs[3] & ~termios.ECHO
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    except OSError:
        pass


# ── Single in-flight session state ────────────────────────────────────────────

_lock = asyncio.Lock()
_process: Optional[asyncio.subprocess.Process] = None
_pty_master: Optional[int] = None
_session_id: Optional[str] = None
_ready: Optional[asyncio.Event] = None
_queue: Optional[asyncio.Queue] = None
_auth_url: Optional[str] = None
_started_at: Optional[float] = None
_last_completed_session_id: Optional[str] = None
_last_completed_ok: bool = False


def _deliver(session_id: str, item: tuple) -> None:
    if _session_id == session_id and _queue is not None:
        _queue.put_nowait(item)


def _close_pty_master() -> None:
    global _pty_master
    if _pty_master is not None:
        fd = _pty_master
        _pty_master = None
        try:
            os.close(fd)
        except OSError:
            pass


async def _write_code_to_pty(text_bytes: bytes) -> None:
    if _pty_master is None:
        raise RuntimeError("No PTY channel for the login process")
    await asyncio.to_thread(os.write, _pty_master, text_bytes + b"\r")


# ── Callback ──────────────────────────────────────────────────────────────────

class AntigravityConnectCallbackBody(BaseModel):
    code: str          # the authorization code pasted from the browser
    session_id: str    # must match the SSE stream's session_id


@router.post("/connect/antigravity/callback")
async def antigravity_connect_callback(body: AntigravityConnectCallbackBody):
    """Send the pasted authorization code to the running ``agy`` login process."""
    global _process, _session_id, _ready

    if _process is None or _session_id is None:
        if body.session_id == _last_completed_session_id and _last_completed_ok:
            return {"ok": True, "message": "Login already completed", "session_id": body.session_id}
        raise HTTPException(
            status_code=409,
            detail="No antigravity login session active. Start one with POST /connect/antigravity first.",
        )
    if body.session_id != _session_id:
        raise HTTPException(
            status_code=409,
            detail=f"Session mismatch (active '{_session_id}', got '{body.session_id}'). Start a new session.",
        )

    ready_event = _ready
    if ready_event is not None and not ready_event.is_set():
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Login process did not become ready in time.")

    async with _lock:
        if _process is None or _pty_master is None:
            raise HTTPException(status_code=409, detail="Login session is no longer active.")
        if body.session_id != _session_id:
            raise HTTPException(status_code=409, detail="Session changed while waiting. Start a new session.")
        if _process.returncode is not None:
            raise HTTPException(status_code=409, detail="Login process already finished. Start a new session.")
        try:
            normalized = _normalize_code(body.code)
            await _write_code_to_pty(normalized.encode("utf-8"))
            return {"ok": True, "message": "Code sent", "session_id": _session_id}
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            raise HTTPException(status_code=410, detail=f"Login process input closed: {e}")


# ── SSE login stream ──────────────────────────────────────────────────────────

@router.post("/connect/antigravity")
async def antigravity_connect():
    """Drive an ``agy`` login and stream its progress via SSE.

    agy prints an OAuth URL; the frontend shows it, the user authorizes and pastes
    the code back to POST /connect/antigravity/callback. On success agy writes its
    self-refreshing token file; we persist nothing and detect success from it."""
    global _process, _pty_master, _session_id, _ready, _queue, _auth_url, _started_at

    agy_path = _resolve_agy_path()

    async def generate() -> AsyncGenerator[str, None]:
        global _process, _pty_master, _session_id, _ready, _queue, _auth_url, _started_at
        global _last_completed_session_id, _last_completed_ok

        session_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        ready_event = asyncio.Event()
        process: Optional[asyncio.subprocess.Process] = None
        auth_url_sent = False

        # Idempotent short-circuit: already logged in → nothing to do.
        if has_usable_login():
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
            yield f"data: {json.dumps({'type': 'stdout', 'line': _SUCCESS_COMPAT_LINE})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'returncode': 0})}\n\n"
            return

        def clear_session() -> None:
            async def _clear():
                global _process, _pty_master, _session_id, _ready, _queue, _auth_url, _started_at
                async with _lock:
                    if _process is process:
                        _process = None
                        _session_id = None
                        _ready = None
                        _queue = None
                        _auth_url = None
                        _started_at = None
                        _close_pty_master()
            asyncio.create_task(_clear())

        def _pty_reader_thread(master_fd: int, loop: asyncio.AbstractEventLoop) -> None:
            """Blocking PTY reader on a plain thread (immune to SSE close)."""
            buf = b""
            try:
                while True:
                    chunk = os.read(master_fd, 65536)
                    if not chunk:
                        break
                    if not ready_event.is_set():
                        ready_event.set()
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        text = raw.decode("utf-8", errors="replace").rstrip("\r")
                        loop.call_soon_threadsafe(_deliver, session_id, ("stdout", text))
            except OSError:
                pass
            if buf:
                text = buf.decode("utf-8", errors="replace").rstrip("\r")
                loop.call_soon_threadsafe(_deliver, session_id, ("stdout", text))

        async def login_watcher() -> None:
            """The authoritative agy success signal: poll the token file. The
            instant login writes it, kill agy (skip the throwaway prompt) and
            report success."""
            while True:
                await asyncio.sleep(1.0)
                if process is None:
                    return
                if has_usable_login():
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    _deliver(session_id, ("login_ok", 0))
                    return
                if process.returncode is not None:
                    return

        async def wait_done(proc: asyncio.subprocess.Process) -> None:
            global _last_completed_session_id, _last_completed_ok
            returncode = await proc.wait()
            ok = returncode == 0 or has_usable_login()
            _last_completed_session_id = session_id
            _last_completed_ok = ok
            _deliver(session_id, ("exit", 0 if ok else (returncode or 1)))
            clear_session()

        try:
            async with _lock:
                # If a login is already in flight, reject a second start (keep it simple —
                # unlike claude we don't multiplex; the UI opens one dialog).
                if _process is not None and _process.returncode is None:
                    yield f"data: {json.dumps({'type': 'error', 'error': 'An antigravity login is already in progress.'})}\n\n"
                    return
                if _process is not None:
                    _close_pty_master()

                env = os.environ.copy()
                env.setdefault("TERM", "xterm-256color")
                env.setdefault("AGY_CLI_DISABLE_AUTO_UPDATE", "1")
                # Don't let agy try to open a browser on the SERVER — the user
                # opens the URL on their own machine (OOB paste-code flow).
                env["BROWSER"] = "true"
                env["DISPLAY"] = ""

                use_pty = _pty_wanted()
                if not use_pty:
                    yield f"data: {json.dumps({'type': 'error', 'error': 'PTY unavailable; antigravity login needs a terminal.'})}\n\n"
                    return

                master_fd, slave_fd = _pty.openpty()
                try:
                    _set_pty_winsize(slave_fd)
                    _disable_pty_echo(slave_fd)
                    process = await asyncio.create_subprocess_exec(
                        agy_path, "-p", "hello",
                        "--model", _TRIGGER_MODEL,
                        "--dangerously-skip-permissions",
                        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, env=env,
                    )
                except BaseException:
                    for fd in (slave_fd, master_fd):
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    raise
                os.close(slave_fd)
                _process = process
                _pty_master = master_fd
                _session_id = session_id
                _ready = ready_event
                _queue = queue
                _auth_url = None
                _started_at = time.monotonic()
                threading.Thread(
                    target=_pty_reader_thread,
                    args=(master_fd, asyncio.get_running_loop()),
                    daemon=True, name=f"agy-connect-{session_id[:8]}",
                ).start()
                asyncio.create_task(wait_done(process))
                asyncio.create_task(login_watcher())

            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
            deadline = (_started_at or time.monotonic()) + CONNECT_TIMEOUT_SECONDS

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    if time.monotonic() >= deadline:
                        if process and process.returncode is None:
                            process.kill()
                        clear_session()
                        yield f"data: {json.dumps({'type': 'error', 'error': 'Antigravity login timed out'})}\n\n"
                        break
                    yield ": heartbeat\n\n"
                    continue

                kind, value = item

                if kind in ("login_ok", "exit"):
                    ok = has_usable_login() or (kind == "login_ok")
                    if kind == "exit" and value != 0 and not ok:
                        clear_session()
                        yield f"data: {json.dumps({'type': 'error', 'error': 'Antigravity login failed. Please try again.'})}\n\n"
                        break
                    if ok:
                        clear_session()
                        yield f"data: {json.dumps({'type': 'stdout', 'line': _SUCCESS_COMPAT_LINE})}\n\n"
                        yield f"data: {json.dumps({'type': 'done', 'returncode': 0})}\n\n"
                        break
                    # exit==0 but no token yet — keep waiting briefly for the watcher.
                    continue

                # kind == "stdout": forward escape-free text; emit the URL once.
                clean = _strip_ansi(value)
                stripped = clean.strip()
                low = stripped.lower()
                if ("authentication failed" in low or "sign-in failed" in low
                        or "invalid" in low and "code" in low):
                    # Retryable errors (bad code) keep the session alive; the line
                    # still streams below so the UI can show it.
                    pass
                if not auth_url_sent:
                    url = _extract_auth_url(clean)
                    if url:
                        _auth_url = url
                        auth_url_sent = True
                        yield f"data: {json.dumps({'type': 'auth_url', 'url': url})}\n\n"
                        continue
                if stripped:
                    yield f"data: {json.dumps({'type': 'stdout', 'line': clean})}\n\n"

        except FileNotFoundError:
            clear_session()
            yield f"data: {json.dumps({'type': 'error', 'error': f'agy CLI not found: {agy_path}'})}\n\n"
        except Exception as e:
            clear_session()
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            if process is not None and process.returncode is not None:
                clear_session()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
