"""
Claude Code CLI interactive-login flow: run `claude auth login` and stream output;
support pasting the login code when the loopback redirect can't reach the CLI
(common over SSH / in containers / on a different machine).

Unlike the setup-token flow, this does NOT scrape a token out of the terminal
stream. `claude auth login` persists its own credential to
``~/.claude/.credentials.json`` (a self-refreshing OAuth login with a refresh
token), which the claude_code adapter already consumes natively via
``_has_usable_native_login()``. So "success" here just means: the CLI exited 0
and that credentials file is present and parseable — no regex, no .env capture.

On Unix, the CLI is spawned with a pseudo-terminal (PTY) so Ink can enable raw
mode on stdin (piped stdin from the API is not a TTY and would error). Set
CLAUDE_AUTH_LOGIN_USE_PTY=0 to force the legacy pipe mode (usually broken for Ink).
"""

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, AsyncGenerator
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

# PTY lets Ink/Claude Code use raw mode on stdin (piped stdin is not a TTY and throws).
try:
    import fcntl
    import struct
    import termios

    import pty as _pty

    _HAS_PTY = hasattr(_pty, "openpty") and sys.platform != "win32"
except ImportError:
    _HAS_PTY = False

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


# Strip CSI, OSC (incl. the OSC-8 hyperlink the CLI wraps the OAuth URL in),
# nF-class and two-char escapes. The nF strip (ESC + intermediate 0x20-0x2F +
# final 0x30-0x7E) catches the charset-reset ESC ( B that the 2-char regex
# misses; without it the escape leaks into the authorize link and corrupts it.
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_NF_RE = re.compile(r"\x1b[\x20-\x2f]+[\x30-\x7e]")
_ANSI_2CHAR_RE = re.compile(r"\x1b[@-Z\\-_]")


def _strip_ansi(line: str) -> str:
    """Remove ANSI/OSC escape sequences (and stray BEL) from CLI output."""
    line = _ANSI_OSC_RE.sub("", line)
    line = _ANSI_CSI_RE.sub("", line)
    line = _ANSI_NF_RE.sub("", line)
    line = _ANSI_2CHAR_RE.sub("", line)
    return line.replace("\x07", "")


_AUTH_URL_RE = re.compile(r"https://claude\.ai/oauth/authorize\?[^\s\"'<>\x00-\x1f]+")


def _extract_auth_url(text: str) -> Optional[str]:
    """Return a clean, param-deduplicated OAuth authorize URL from `text`, or None.

    Redraws can repeat the URL (and each query param) several times; we keep the
    first value of each param so the rebuilt link is the canonical one.
    """
    match = _AUTH_URL_RE.search(text)
    if not match:
        return None
    raw = match.group(0)
    second = raw.find("https://", len("https://"))  # drop a concatenated redraw
    if second != -1:
        raw = raw[:second]
    split = urlsplit(raw)
    deduped: dict[str, str] = {}
    for key, value in parse_qsl(split.query, keep_blank_values=True):
        value = "".join(ch for ch in value if ord(ch) >= 0x20)  # drop leftover garbage
        if key and key not in deduped:
            deduped[key] = value
    if not deduped:
        return None
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(deduped), ""))


# The CLI prints something like "Paste code here if prompted" when the loopback
# redirect can't reach it; surfacing this lets the frontend reveal a paste box.
_PASTE_PROMPT_RE = re.compile(r"paste\s+code", re.IGNORECASE)


def _claude_config_dir() -> Path:
    """Where `claude auth login` writes its credentials: $CLAUDE_CONFIG_DIR or ~/.claude."""
    return Path(os.path.expanduser(os.getenv("CLAUDE_CONFIG_DIR") or "~/.claude"))


def _read_native_login_summary() -> Optional[dict]:
    """Return a non-secret summary of the native login the CLI wrote, or None.

    Mirrors the shape the claude_code adapter parses (``claudeAiOauth`` with
    refreshToken / accessToken / expiresAt). Never returns token material.
    """
    home = _claude_config_dir()
    for name in (".credentials.json", "credentials.json"):
        path = home / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        if not isinstance(oauth, dict):
            oauth = data if isinstance(data, dict) else {}
        has_access = bool(oauth.get("accessToken"))
        if not (has_access or oauth.get("refreshToken")):
            continue
        return {
            "present": True,
            "path": str(path),
            "has_refresh_token": bool(oauth.get("refreshToken")),
            "expires_at": oauth.get("expiresAt"),
            "subscription_type": oauth.get("subscriptionType"),
        }
    return None


def _resolve_claude_cli_path() -> str:
    """Resolve Claude CLI path from env or PATH (avoids circular import from server)."""
    path = (os.getenv("CLAUDE_CLI_PATH") or "claude").strip()
    if not os.path.isabs(path):
        found = shutil.which(path)
        if found:
            return found
    return path


CLAUDE_AUTH_LOGIN_TIMEOUT_SECONDS = int(os.getenv("CLAUDE_AUTH_LOGIN_TIMEOUT", "300"))

_login_lock = asyncio.Lock()
_login_process: Optional[asyncio.subprocess.Process] = None
_login_stdin: Optional[asyncio.StreamWriter] = None
# When using a PTY, stdin is not a StreamWriter; we write paste payloads here.
_login_pty_master: Optional[int] = None
_login_session_id: Optional[str] = None
_login_ready: Optional[asyncio.Event] = None


def _pty_wanted() -> bool:
    flag = os.getenv("CLAUDE_AUTH_LOGIN_USE_PTY", "1").strip().lower()
    return flag not in ("0", "false", "no", "off") and _HAS_PTY


def _set_pty_winsize(slave_fd: int, rows: int = 24, cols: int = 1000) -> None:
    """Best-effort terminal size for the child. A wide width stops Ink from
    wrapping the long OAuth URL across lines (which corrupts capture)."""
    if not _HAS_PTY:
        return
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


_BRACKETED_PASTE_START = b"\x1b[200~"
_BRACKETED_PASTE_END = b"\x1b[201~"


async def _write_login_stdin(text_bytes: bytes) -> None:
    """Write the user-pasted login code to the running `auth login` CLI (pipe or PTY).

    The CLI enables bracketed paste mode (ESC[?2004h), so we wrap the content in
    ESC[200~ ... ESC[201~ for Ink's usePaste hook, then send Enter (\\r) to submit.
    """
    global _login_stdin, _login_pty_master
    if _login_pty_master is not None:
        paste_payload = _BRACKETED_PASTE_START + text_bytes + _BRACKETED_PASTE_END
        written = await asyncio.to_thread(os.write, _login_pty_master, paste_payload)
        print(f"[auth-login] PTY os.write returned {written}")
        await asyncio.sleep(0.15)
        await asyncio.to_thread(os.write, _login_pty_master, b"\r")
        return
    if _login_stdin is not None:
        paste_payload = _BRACKETED_PASTE_START + text_bytes + _BRACKETED_PASTE_END + b"\r"
        _login_stdin.write(paste_payload)
        await _login_stdin.drain()
        return
    print("[auth-login] ERROR: no stdin channel available")
    raise RuntimeError("No auth-login stdin channel (internal error)")


def _close_login_pty_master() -> None:
    global _login_pty_master
    if _login_pty_master is not None:
        fd = _login_pty_master
        _login_pty_master = None
        try:
            os.close(fd)
        except OSError:
            pass


router = APIRouter(prefix="/claude", tags=["claude-auth-login"])


class ClaudeAuthLoginCallbackBody(BaseModel):
    """Body for pasting the login code when the loopback redirect doesn't reach the CLI."""
    code: str  # Paste the full string from the browser, e.g. "code#state"
    session_id: str  # Must match the session_id emitted by the auth-login SSE stream


def _normalize_callback_code(raw_value: str) -> str:
    """
    Accept multiple callback formats and normalize to the `code#state` format
    that Claude Code expects in its interactive prompt.
    """
    value = (raw_value or "").strip()
    if not value:
        return value

    # Full redirect URL -> extract query params
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        params = parse_qs(parsed.query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        if code and state:
            return f"{code}#{state}"
        if code:
            return code
        return value

    # querystring payload -> convert to code#state
    if "code=" in value and "state=" in value:
        params = parse_qs(value)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        if code and state:
            return f"{code}#{state}"
        if code:
            return code
    return value


@router.post("/auth-login/callback")
async def claude_auth_login_callback(body: ClaudeAuthLoginCallbackBody):
    """
    Send the pasted login code to the running `claude auth login` process.

    When the user authorizes in the browser and the loopback redirect can't reach
    the CLI (SSH / container / different machine), Anthropic shows a code to paste.
    The frontend sends it here and we write it to the CLI's stdin so login completes.

    Requires `session_id` from the SSE stream to target the correct session.
    """
    global _login_process, _login_stdin, _login_pty_master, _login_session_id, _login_ready

    print(f"[auth-login] callback received (session_id={body.session_id})")

    if _login_process is None or _login_session_id is None:
        raise HTTPException(
            status_code=409,
            detail="No auth-login session active. Start one with POST /claude/auth-login first.",
        )

    if body.session_id != _login_session_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Session mismatch. The active session is '{_login_session_id}' "
                f"but callback provided '{body.session_id}'. "
                "Start a new auth-login session and use the session_id from its SSE stream."
            ),
        )

    ready_event = _login_ready
    if ready_event is not None and not ready_event.is_set():
        print("[auth-login] callback waiting for CLI readiness (raw mode)...")
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail="CLI process did not become ready in time. It may have failed to start.",
            )

    async with _login_lock:
        no_input = _login_stdin is None and _login_pty_master is None
        if no_input or _login_process is None:
            raise HTTPException(
                status_code=409,
                detail="Auth-login session is no longer active (it may have been cleaned up).",
            )
        if body.session_id != _login_session_id:
            raise HTTPException(
                status_code=409,
                detail="Session changed while waiting. Please start a new auth-login session.",
            )
        if _login_process.returncode is not None:
            _login_process = None
            _login_stdin = None
            _login_session_id = None
            _login_ready = None
            _close_login_pty_master()
            raise HTTPException(
                status_code=409,
                detail="Auth-login process already finished. Start a new session if needed.",
            )
        try:
            normalized = _normalize_callback_code(body.code)
            print(f"[auth-login] callback normalized (raw_len={len((body.code or '').strip())} normalized_len={len(normalized)})")
            await _write_login_stdin(normalized.encode("utf-8"))
            print("[auth-login] callback payload sent successfully")
            await asyncio.sleep(1.0)
            return {"ok": True, "message": "Code sent to CLI", "session_id": _login_session_id}
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            _login_process = None
            _login_stdin = None
            _login_session_id = None
            _login_ready = None
            _close_login_pty_master()
            print(f"[auth-login] callback write FAILED: {e}")
            raise HTTPException(status_code=410, detail=f"Process stdin closed: {e}")


@router.post("/auth-login")
async def claude_auth_login():
    """
    Run `claude auth login --claudeai` and stream stdout/stderr to the client via SSE.

    The CLI prints an OAuth URL; the frontend shows it so the user can authorize.
    If the loopback redirect reaches the CLI, login completes automatically. If the
    browser instead shows a code (SSH / container / different machine), the user
    pastes it via POST /claude/auth-login/callback to finish.

    On a clean exit the CLI has written its own ~/.claude/.credentials.json — we
    do not scrape any token; success = that credentials file is present.
    """
    global _login_process, _login_stdin, _login_pty_master
    cli_path = _resolve_claude_cli_path()
    print(f"[auth-login] session start requested (cli_path={cli_path})")

    async def generate() -> AsyncGenerator[str, None]:
        global _login_process, _login_stdin, _login_pty_master, _login_session_id, _login_ready
        queue: asyncio.Queue = asyncio.Queue()
        process: Optional[asyncio.subprocess.Process] = None
        auth_url_sent = False
        paste_prompt_sent = False
        session_id = str(uuid.uuid4())
        ready_event = asyncio.Event()

        async def read_stdout(proc: asyncio.subprocess.Process) -> None:
            if proc.stdout is None:
                return
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                if not ready_event.is_set():
                    ready_event.set()
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                await queue.put(("stdout", text))

        async def read_stderr(proc: asyncio.subprocess.Process) -> None:
            if proc.stderr is None:
                return
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                await queue.put(("stderr", text))

        def _pty_reader_thread(master_fd: int, loop: asyncio.AbstractEventLoop) -> None:
            """Read PTY master in a plain OS thread — decoupled from asyncio so it
            keeps draining even after the SSE response context closes (otherwise
            the PTY buffer fills and the CLI deadlocks on stdout write)."""
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
                        raw_line, buf = buf.split(b"\n", 1)
                        text = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                        stripped = _strip_ansi(text).strip()
                        if stripped:
                            print(f"[auth-login] PTY> {stripped[:200]}")
                        loop.call_soon_threadsafe(queue.put_nowait, ("stdout", text))
            except OSError as e:
                print(f"[auth-login] PTY read ended: {e}")
            if buf:
                tail = buf.decode("utf-8", errors="replace").rstrip("\r")
                if tail:
                    loop.call_soon_threadsafe(queue.put_nowait, ("stdout", tail))
            print("[auth-login] PTY reader thread exiting")

        async def wait_done(proc: asyncio.subprocess.Process) -> None:
            returncode = await proc.wait()
            print(f"[auth-login] process exited (returncode={returncode})")
            await queue.put(("done", returncode))
            clear_session()

        def clear_session() -> None:
            async def _clear():
                global _login_process, _login_stdin, _login_pty_master, _login_session_id, _login_ready
                async with _login_lock:
                    if _login_process is process:
                        _login_process = None
                        _login_stdin = None
                        _login_session_id = None
                        _login_ready = None
                        _close_login_pty_master()
                        print("[auth-login] session cleared")

            asyncio.create_task(_clear())

        try:
            env = os.environ.copy()
            # Pin TERM so the CLI's terminal output is deterministic regardless of
            # how the API was launched (an unset TERM changes the escape sequences
            # the CLI emits). setdefault respects an explicitly-set TERM.
            env.setdefault("TERM", "xterm-256color")
            use_pty = _pty_wanted()
            print(f"[auth-login] spawning process (use_pty={use_pty}, TERM={env.get('TERM')})")
            cmd_args = ["auth", "login", "--claudeai"]
            if use_pty:
                master_fd, slave_fd = _pty.openpty()
                try:
                    _set_pty_winsize(slave_fd)
                    process = await asyncio.create_subprocess_exec(
                        cli_path, *cmd_args,
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
                async with _login_lock:
                    _login_process = process
                    _login_stdin = None
                    _login_pty_master = master_fd
                    _login_session_id = session_id
                    _login_ready = ready_event
                _reader_thread = threading.Thread(
                    target=_pty_reader_thread,
                    args=(master_fd, asyncio.get_event_loop()),
                    daemon=True,
                    name=f"auth-login-pty-{session_id[:8]}",
                )
                _reader_thread.start()
                asyncio.create_task(wait_done(process))
            else:
                process = await asyncio.create_subprocess_exec(
                    cli_path, *cmd_args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                    env=env,
                )
                async with _login_lock:
                    _login_process = process
                    _login_stdin = process.stdin
                    _login_pty_master = None
                    _login_session_id = session_id
                    _login_ready = ready_event
                asyncio.create_task(read_stdout(process))
                asyncio.create_task(read_stderr(process))
                asyncio.create_task(wait_done(process))

            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
            print(f"[auth-login] session started (session_id={session_id})")

            _SSE_HEARTBEAT_INTERVAL = 15
            _deadline = time.monotonic() + CLAUDE_AUTH_LOGIN_TIMEOUT_SECONDS

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    if time.monotonic() >= _deadline:
                        if process and process.returncode is None:
                            print("[auth-login] timed out; killing process")
                            process.kill()
                        clear_session()
                        yield f"data: {json.dumps({'type': 'error', 'error': 'Login timed out'})}\n\n"
                        break
                    yield ": heartbeat\n\n"
                    continue
                kind, value = item
                if kind == "done":
                    # No token scraping: the CLI persists its own credentials file.
                    # Success = a clean exit AND a present, parseable native login.
                    login = _read_native_login_summary() if value == 0 else None
                    logged_in = bool(login and login.get("present"))
                    if value == 0 and not logged_in:
                        print("[auth-login] WARNING: exit 0 but no credentials file found")
                    yield f"data: {json.dumps({'type': 'success', 'logged_in': logged_in, 'login': login})}\n\n"
                    clear_session()
                    yield f"data: {json.dumps({'type': 'done', 'returncode': value})}\n\n"
                    break

                clean_line = _strip_ansi(value)
                if kind == "stdout":
                    auth_url = _extract_auth_url(clean_line)
                    if auth_url:
                        if not auth_url_sent:
                            auth_url_sent = True
                            yield f"data: {json.dumps({'type': 'auth_url', 'url': auth_url})}\n\n"
                        continue  # don't also forward the raw URL line / redraws
                    if not paste_prompt_sent and _PASTE_PROMPT_RE.search(clean_line):
                        paste_prompt_sent = True
                        yield f"data: {json.dumps({'type': 'paste_prompt'})}\n\n"
                yield f"data: {json.dumps({'type': kind, 'line': clean_line})}\n\n"
        except FileNotFoundError:
            clear_session()
            print(f"[auth-login] CLI not found: {cli_path}")
            yield f"data: {json.dumps({'type': 'error', 'error': f'CLI not found: {cli_path}'})}\n\n"
        except Exception as e:
            clear_session()
            print(f"[auth-login] unexpected error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            # Keep the process alive so the callback can still paste the code even
            # after the SSE client disconnects; cleanup happens in wait_done().
            if process is not None and process.returncode is not None:
                clear_session()
            print("[auth-login] SSE generator finished")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
