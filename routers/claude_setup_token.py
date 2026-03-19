"""
Claude Code CLI setup-token flow: run `claude setup-token` and stream output;
support pasted OAuth code when redirect fails (e.g. user on different machine).

On Unix, the CLI is spawned with a pseudo-terminal (PTY) so Ink can enable raw mode
on stdin (piped stdin from the API is not a TTY and would error). Set
CLAUDE_SETUP_TOKEN_USE_PTY=0 to force the legacy pipe mode (usually broken for Ink).

On success, persists CLAUDE_CODE_OAUTH_TOKEN to .env and sets it in process env.
"""

import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional, AsyncGenerator
from urllib.parse import parse_qs, urlparse

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


# OAuth token in CLI output: full token is ~108 chars; CLI may wrap at 80 chars so we merge continuation lines
_OAUTH_TOKEN_PATTERN = re.compile(r"sk-ant-(?:oat01|api03|api04)-\S+")
_MIN_FULL_TOKEN_LEN = 100  # tokens are ~108 chars; if we get less, expect a continuation line
_CONTINUATION_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")  # next line is rest of token (no spaces)


def _strip_ansi(line: str) -> str:
    """Remove ANSI escape sequences from CLI output."""
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Zm]?", "", line)


def _extract_oauth_token(line: str) -> Optional[str]:
    """If line contains a Claude OAuth token, return it; else None."""
    plain = _strip_ansi(line).strip()
    match = _OAUTH_TOKEN_PATTERN.search(plain)
    return match.group(0) if match else None


def _is_continuation_line(line: str) -> bool:
    """True if line looks like the rest of a wrapped token (no spaces, token chars only)."""
    plain = _strip_ansi(line).strip()
    return bool(plain and not plain.startswith("sk-ant-") and _CONTINUATION_PATTERN.fullmatch(plain))


def _merge_and_extract_token(partial: str, continuation: str) -> Optional[str]:
    """Merge partial token line + continuation line and return full token if valid."""
    combined = (partial + continuation).strip()
    match = _OAUTH_TOKEN_PATTERN.search(combined)
    return match.group(0) if match and len(match.group(0)) >= _MIN_FULL_TOKEN_LEN else None


def _default_env_path() -> str:
    """Project root .env (same repo as this router), so persist works regardless of cwd."""
    return os.getenv("DOTENV_PATH") or str(Path(__file__).resolve().parent.parent / ".env")


def _read_token_from_cli_credentials() -> Optional[str]:
    """
    After setup-token succeeds, the CLI may write the token to a credentials file
    instead of (or in addition to) printing it. Read from known locations.
    """
    candidates = [
        Path.home() / ".claude" / "credentials.json",
        Path.home() / ".claude" / ".credentials.json",
        Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "claude" / "credentials.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            # Nested formats seen in the wild
            token = None
            if isinstance(data, dict):
                token = (
                    data.get("claudeAiOauth", {}).get("accessToken")
                    or data.get("accessToken")
                    or (data.get("credentials", {}) or {}).get("accessToken")
                )
            if token and isinstance(token, str) and len(token) > 20:
                return token
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return None


def _persist_token_to_env_file(token: str) -> None:
    """
    Add or update CLAUDE_CODE_OAUTH_TOKEN in .env so it persists across restarts.
    Also safe for token values that contain '=' or '#' by quoting.
    """
    env_path = _default_env_path()
    if not os.path.isfile(env_path):
        with open(env_path, "w") as f:
            f.write(f'CLAUDE_CODE_OAUTH_TOKEN="{token}"\n')
        return
    lines: list[str] = []
    key = "CLAUDE_CODE_OAUTH_TOKEN"
    found = False
    with open(env_path, "r") as f:
        for raw in f:
            if raw.strip().startswith(f"{key}="):
                lines.append(f'{key}="{token}"\n')
                found = True
            else:
                lines.append(raw)
    if not found:
        lines.append(f'\n{key}="{token}"\n')
    with open(env_path, "w") as f:
        f.writelines(lines)


def _resolve_claude_cli_path() -> str:
    """Resolve Claude CLI path from env or PATH (avoids circular import from server)."""
    path = (os.getenv("CLAUDE_CLI_PATH") or "claude").strip()
    if not os.path.isabs(path):
        found = shutil.which(path)
        if found:
            return found
    return path


CLAUDE_SETUP_TOKEN_TIMEOUT_SECONDS = int(os.getenv("CLAUDE_SETUP_TOKEN_TIMEOUT", "300"))

_setup_token_lock = asyncio.Lock()
_setup_token_process: Optional[asyncio.subprocess.Process] = None
_setup_token_stdin: Optional[asyncio.StreamWriter] = None
# When using a PTY, stdin is not a StreamWriter; we write OAuth paste payloads here.
_setup_token_pty_master: Optional[int] = None


def _pty_wanted() -> bool:
    flag = os.getenv("CLAUDE_SETUP_TOKEN_USE_PTY", "1").strip().lower()
    return flag not in ("0", "false", "no", "off") and _HAS_PTY


def _set_pty_winsize(slave_fd: int, rows: int = 24, cols: int = 120) -> None:
    """Best-effort terminal size for the child (some CLIs behave better with a defined geometry)."""
    if not _HAS_PTY:
        return
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


async def _write_setup_token_stdin(data: bytes) -> None:
    """Write user-pasted OAuth code to the running setup-token CLI (pipe or PTY)."""
    global _setup_token_stdin, _setup_token_pty_master
    if _setup_token_pty_master is not None:
        print(f"[setup-token] writing callback payload to PTY master (bytes={len(data)})")
        await asyncio.to_thread(os.write, _setup_token_pty_master, data)
        return
    if _setup_token_stdin is not None:
        print(f"[setup-token] writing callback payload to PIPE stdin (bytes={len(data)})")
        _setup_token_stdin.write(data)
        await _setup_token_stdin.drain()
        return
    print("[setup-token] ERROR: no stdin channel available")
    raise RuntimeError("No setup-token stdin channel (internal error)")


def _close_setup_token_pty_master() -> None:
    global _setup_token_pty_master
    if _setup_token_pty_master is not None:
        fd = _setup_token_pty_master
        _setup_token_pty_master = None
        try:
            os.close(fd)
        except OSError:
            pass


router = APIRouter(prefix="/claude", tags=["claude-setup-token"])


class ClaudeSetupTokenCallbackBody(BaseModel):
    """Body for pasting OAuth code when redirect fails (e.g. user on different machine)."""
    code: str  # Paste the full string from the browser, e.g. "code#state"


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


@router.post("/setup-token/callback")
async def claude_setup_token_callback(body: ClaudeSetupTokenCallbackBody):
    """
    Send the pasted OAuth code to the running `claude setup-token` process.

    When the user authorizes in the browser and the redirect fails (e.g. they're on a
    different machine), Anthropic shows a "Paste this into Claude Code" page. The user
    copies that string and the frontend sends it here; we write it to the CLI's stdin
    so the flow can complete and the token is streamed back.
    """
    global _setup_token_process, _setup_token_stdin, _setup_token_pty_master
    async with _setup_token_lock:
        print("[setup-token] callback received")
        no_input = _setup_token_stdin is None and _setup_token_pty_master is None
        if no_input or _setup_token_process is None:
            print("[setup-token] callback rejected: no active session/input")
            raise HTTPException(
                status_code=409,
                detail="No setup-token session active. Start one with POST /claude/setup-token first.",
            )
        if _setup_token_process.returncode is not None:
            _setup_token_process = None
            _setup_token_stdin = None
            _close_setup_token_pty_master()
            print("[setup-token] callback rejected: process already finished")
            raise HTTPException(
                status_code=409,
                detail="Setup-token process already finished. Start a new session if needed.",
            )
        try:
            normalized = _normalize_callback_code(body.code)
            print(f"[setup-token] callback normalized (raw_len={len((body.code or '').strip())} normalized_len={len(normalized)})")
            # Send as plain keystrokes + Enter (\r).  NO bracketed paste wrapper.
            # Ink's useInput reads individual keystrokes; bracketed paste goes to
            # usePaste which is a different channel the input field may not use.
            payload = (normalized + "\r").encode("utf-8")
            print(f"[setup-token] sending plain text + Enter to CLI (bytes={len(payload)})")
            await _write_setup_token_stdin(payload)
            print("[setup-token] callback payload sent successfully")
            return {"ok": True, "message": "Code sent to CLI"}
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            _setup_token_process = None
            _setup_token_stdin = None
            _close_setup_token_pty_master()
            print(f"[setup-token] callback write FAILED: {e}")
            raise HTTPException(status_code=410, detail=f"Process stdin closed: {e}")


@router.post("/setup-token")
async def claude_setup_token():
    """
    Run `claude setup-token` and stream stdout/stderr to the client via SSE.

    The CLI prints an OAuth URL; the frontend shows it so the user can open it and
    authorize. If the redirect goes to the user's localhost (different machine),
    Anthropic shows "Paste this into Claude Code" — the user copies that string and
    the frontend sends it to POST /claude/setup-token/callback so we can complete the flow.

    Uses a PTY on Unix by default so Claude Code (Ink) does not fail with
    "Raw mode is not supported on the current process.stdin".
    """
    global _setup_token_process, _setup_token_stdin, _setup_token_pty_master
    cli_path = _resolve_claude_cli_path()
    print(f"[setup-token] session start requested (cli_path={cli_path})")

    async def generate() -> AsyncGenerator[str, None]:
        global _setup_token_process, _setup_token_stdin, _setup_token_pty_master
        queue: asyncio.Queue = asyncio.Queue()
        process: Optional[asyncio.subprocess.Process] = None
        token_buffer: Optional[str] = None

        async def read_stdout(proc: asyncio.subprocess.Process) -> None:
            if proc.stdout is None:
                return
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
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

        async def read_pty_master(master_fd: int) -> None:
            """PTY merges stdout/stderr; stream as stdout lines for the SSE client."""
            buf = b""
            try:
                while True:
                    chunk = await asyncio.to_thread(os.read, master_fd, 65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        text = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                        await queue.put(("stdout", text))
            except OSError:
                pass
            if buf:
                tail = buf.decode("utf-8", errors="replace").rstrip("\r")
                if tail:
                    await queue.put(("stdout", tail))

        async def wait_done(proc: asyncio.subprocess.Process) -> None:
            returncode = await proc.wait()
            print(f"[setup-token] process exited (returncode={returncode})")
            await queue.put(("done", returncode))

        def clear_session() -> None:
            async def _clear():
                global _setup_token_process, _setup_token_stdin, _setup_token_pty_master
                async with _setup_token_lock:
                    if _setup_token_process is process:
                        _setup_token_process = None
                        _setup_token_stdin = None
                        _close_setup_token_pty_master()
                        print("[setup-token] session cleared")

            asyncio.create_task(_clear())

        try:
            env = os.environ.copy()
            use_pty = _pty_wanted()
            print(f"[setup-token] spawning process (use_pty={use_pty})")
            if use_pty:
                master_fd, slave_fd = _pty.openpty()
                try:
                    _set_pty_winsize(slave_fd)
                    process = await asyncio.create_subprocess_exec(
                        cli_path,
                        "setup-token",
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        env=env,
                    )
                except BaseException:
                    for fd in (slave_fd, master_fd):
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    raise
                os.close(slave_fd)
                async with _setup_token_lock:
                    _setup_token_process = process
                    _setup_token_stdin = None
                    _setup_token_pty_master = master_fd
                asyncio.create_task(read_pty_master(master_fd))
                asyncio.create_task(wait_done(process))
            else:
                process = await asyncio.create_subprocess_exec(
                    cli_path,
                    "setup-token",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                    env=env,
                )
                async with _setup_token_lock:
                    _setup_token_process = process
                    _setup_token_stdin = process.stdin
                    _setup_token_pty_master = None
                asyncio.create_task(read_stdout(process))
                asyncio.create_task(read_stderr(process))
                asyncio.create_task(wait_done(process))

            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=CLAUDE_SETUP_TOKEN_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    if process and process.returncode is None:
                        print("[setup-token] timed out; killing process")
                        process.kill()
                    clear_session()
                    yield f"data: {json.dumps({'type': 'error', 'error': 'Setup timed out'})}\n\n"
                    break
                kind, value = item
                if kind == "done":
                    # If we never saw the token in stdout, try reading from CLI credentials file
                    # (CLI may write there on success without printing the token.)
                    if value == 0:
                        fallback_token = _read_token_from_cli_credentials()
                        if fallback_token:
                            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = fallback_token
                            try:
                                _persist_token_to_env_file(fallback_token)
                                print(f"[setup-token] token persisted to .env from CLI credentials (len={len(fallback_token)})")
                            except OSError as e:
                                print(f"[setup-token] .env write failed: {e}")
                    clear_session()
                    yield f"data: {json.dumps({'type': 'done', 'returncode': value})}\n\n"
                    break
                # On success, CLI may print the OAuth token (~108 chars); CLI often wraps at 80 chars
                if kind == "stdout":
                    def persist_token(t: str) -> None:
                        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = t
                        try:
                            _persist_token_to_env_file(t)
                            print(f"[setup-token] token persisted to .env from stdout (len={len(t)})")
                        except OSError as e:
                            print(f"[setup-token] .env write failed: {e}")

                    if token_buffer is not None:
                        if _is_continuation_line(value):
                            merged = _merge_and_extract_token(token_buffer, value)
                            if merged:
                                persist_token(merged)
                                token_buffer = None
                        else:
                            if token_buffer.startswith("sk-ant-") and len(token_buffer) >= _MIN_FULL_TOKEN_LEN:
                                persist_token(token_buffer)
                            token_buffer = None

                    if token_buffer is None:
                        token = _extract_oauth_token(value)
                        if token:
                            if len(token) >= _MIN_FULL_TOKEN_LEN:
                                persist_token(token)
                            else:
                                token_buffer = token
                yield f"data: {json.dumps({'type': kind, 'line': value})}\n\n"
        except FileNotFoundError:
            clear_session()
            print(f"[setup-token] CLI not found: {cli_path}")
            yield f"data: {json.dumps({'type': 'error', 'error': f'CLI not found: {cli_path}'})}\n\n"
        except Exception as e:
            clear_session()
            print(f"[setup-token] unexpected error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
