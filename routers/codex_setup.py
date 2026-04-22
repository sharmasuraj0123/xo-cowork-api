"""
OpenAI Codex CLI device-code login.

Flow:
  POST /codex/setup  -> SSE stream
    1. Check if `codex` CLI (@openai/codex) is installed.
       If not, run `npm install -g @openai/codex` and stream install progress.
    2. Run `codex login --device-auth` via PTY.
    3. Stream output — CLI shows a short alphanumeric user code and verification URL.
    4. User visits the URL and enters the code; CLI polls until auth completes.
    5. Emit {type: "done", returncode: 0} on success.

SSE event types emitted:
  {type: "installing", package: "@openai/codex"}   — npm install starting
  {type: "install_log", line: "..."}               — npm install output
  {type: "stdout", line: "..."}                    — codex login output
  {type: "stderr", line: "..."}                    — codex login stderr (pipe mode only)
  {type: "done", returncode: N}                    — CLI exited
  {type: "error", error: "..."}                    — failure

No callback endpoint needed — device auth is handled entirely by the CLI.
"""

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

try:
    import fcntl
    import struct
    import termios
    import pty as _pty

    _HAS_PTY = hasattr(_pty, "openpty") and sys.platform != "win32"
except ImportError:
    _HAS_PTY = False

from fastapi import APIRouter
from fastapi.responses import StreamingResponse


CODEX_NPM_PACKAGE = "@openai/codex"
CODEX_SETUP_TIMEOUT_SECONDS = int(os.getenv("CODEX_SETUP_TIMEOUT", "900"))
_NPM_INSTALL_TIMEOUT_SECONDS = int(os.getenv("CODEX_NPM_INSTALL_TIMEOUT", "300"))
_SSE_HEARTBEAT_INTERVAL = 15

_TOKEN_ENV_KEYS = ["OPENAI_CODEX_ACCESS_TOKEN"]


def _strip_ansi(line: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Zm]?", "", line)


def _project_env_path() -> str:
    return os.getenv("DOTENV_PATH") or str(Path(__file__).resolve().parent.parent / ".env")


def _openclaw_env_path() -> str:
    return str(Path.home() / ".openclaw" / ".env")


def _upsert_env_key(env_path: str, key: str, value: str) -> None:
    """Insert or update a single KEY="value" in a .env file. Creates file if missing."""
    if not os.path.isfile(env_path):
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w") as f:
            f.write(f'{key}="{value}"\n')
        return
    lines: list[str] = []
    found = False
    with open(env_path, "r") as f:
        for raw in f:
            if raw.strip().startswith(f"{key}="):
                lines.append(f'{key}="{value}"\n')
                found = True
            else:
                lines.append(raw)
    if not found:
        lines.append(f'\n{key}="{value}"\n')
    with open(env_path, "w") as f:
        f.writelines(lines)


def _persist_token_to_env_files(token: str) -> None:
    """Write OPENAI_CODEX_ACCESS_TOKEN to project .env and ~/.openclaw/.env."""
    for env_path in [_project_env_path(), _openclaw_env_path()]:
        for key in _TOKEN_ENV_KEYS:
            try:
                _upsert_env_key(env_path, key, token)
            except OSError as e:
                print(f"[codex-setup] failed to write {key} to {env_path}: {e}")


def _read_token_from_codex_credentials() -> Optional[str]:
    """
    After login succeeds the CLI writes credentials to a known location.
    Try common paths and return the access token if found.
    """
    xdg_data = os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    xdg_config = os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    candidates = [
        Path(xdg_data) / "codex" / "auth.json",
        Path(xdg_config) / "codex" / "auth.json",
        Path.home() / ".codex" / "auth.json",
        Path.home() / ".config" / "codex" / "auth.json",
        # openclaw auth store (written by our own write_auth_credentials previously)
        Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            token = None
            if isinstance(data, dict):
                # flat format: {"access_token": "..."}
                token = data.get("access_token") or data.get("accessToken")
                # openclaw auth-profiles format
                if not token:
                    profiles = data.get("profiles", {})
                    for profile in profiles.values():
                        if isinstance(profile, dict):
                            token = profile.get("access") or profile.get("access_token")
                            if token:
                                break
            if token and isinstance(token, str) and len(token) > 20:
                print(f"[codex-setup] token read from {path}")
                return token
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return None


def _codex_cli_path() -> str:
    path = (os.getenv("CODEX_CLI_PATH") or "codex").strip()
    if not os.path.isabs(path):
        found = shutil.which(path)
        if found:
            return found
    return path


def _is_codex_installed() -> bool:
    return shutil.which("codex") is not None


def _pty_wanted() -> bool:
    flag = os.getenv("CODEX_LOGIN_USE_PTY", "1").strip().lower()
    return flag not in ("0", "false", "no", "off") and _HAS_PTY


def _set_pty_winsize(slave_fd: int, rows: int = 24, cols: int = 120) -> None:
    if not _HAS_PTY:
        return
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


router = APIRouter(prefix="/codex", tags=["codex-setup"])


@router.post("/setup")
async def codex_setup():
    """
    Run `codex login --device-auth` and stream its output via SSE.

    Installs the codex CLI via npm on first use. Subsequent calls skip install
    because `shutil.which("codex")` finds the already-installed binary.
    """
    async def generate() -> AsyncGenerator[str, None]:
        # ------------------------------------------------------------------ #
        # Step 1 – ensure codex CLI is installed                              #
        # ------------------------------------------------------------------ #
        if not _is_codex_installed():
            print(f"[codex-setup] codex CLI not found, installing {CODEX_NPM_PACKAGE}")
            yield f"data: {json.dumps({'type': 'installing', 'package': CODEX_NPM_PACKAGE})}\n\n"
            try:
                npm_proc = await asyncio.create_subprocess_exec(
                    "npm", "install", "-g", CODEX_NPM_PACKAGE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    async def _drain_npm():
                        while True:
                            line = await npm_proc.stdout.readline()
                            if not line:
                                break
                            text = line.decode("utf-8", errors="replace").rstrip()
                            print(f"[codex-setup] npm> {text}")
                            return text  # caller collects via async for

                    # Stream npm output line by line with overall timeout
                    deadline = time.monotonic() + _NPM_INSTALL_TIMEOUT_SECONDS
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            npm_proc.kill()
                            yield f"data: {json.dumps({'type': 'error', 'error': 'npm install timed out'})}\n\n"
                            return
                        try:
                            line = await asyncio.wait_for(
                                npm_proc.stdout.readline(),
                                timeout=min(remaining, _SSE_HEARTBEAT_INTERVAL),
                            )
                        except asyncio.TimeoutError:
                            yield ": heartbeat\n\n"
                            continue
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace").rstrip()
                        print(f"[codex-setup] npm> {text}")
                        yield f"data: {json.dumps({'type': 'install_log', 'line': text})}\n\n"

                    await npm_proc.wait()
                except Exception:
                    if npm_proc.returncode is None:
                        npm_proc.kill()
                    raise

                if npm_proc.returncode != 0:
                    yield f"data: {json.dumps({'type': 'error', 'error': f'npm install failed (exit code {npm_proc.returncode})'})}\n\n"
                    return

                if not _is_codex_installed():
                    yield f"data: {json.dumps({'type': 'error', 'error': 'codex CLI not found in PATH after npm install'})}\n\n"
                    return

                print("[codex-setup] codex CLI installed successfully")
                yield f"data: {json.dumps({'type': 'install_log', 'line': 'codex CLI installed successfully'})}\n\n"

            except FileNotFoundError:
                yield f"data: {json.dumps({'type': 'error', 'error': 'npm not found — cannot install codex CLI'})}\n\n"
                return
        else:
            print("[codex-setup] codex CLI already installed, skipping npm install")

        # ------------------------------------------------------------------ #
        # Step 2 – run codex login --device-auth via PTY                      #
        # ------------------------------------------------------------------ #
        cli_path = _codex_cli_path()
        print(f"[codex-setup] spawning: {cli_path} login --device-auth")

        queue: asyncio.Queue = asyncio.Queue()
        process: Optional[asyncio.subprocess.Process] = None
        master_fd: Optional[int] = None

        def _pty_reader_thread(fd: int, loop: asyncio.AbstractEventLoop) -> None:
            """Blocking read loop in a plain OS thread — immune to asyncio lifecycle issues."""
            buf = b""
            try:
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        text = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                        stripped = _strip_ansi(text).strip()
                        if stripped:
                            print(f"[codex-setup] PTY> {stripped[:200]}")
                        loop.call_soon_threadsafe(queue.put_nowait, ("stdout", text))
            except OSError as e:
                print(f"[codex-setup] PTY read ended: {e}")
            if buf:
                tail = buf.decode("utf-8", errors="replace").rstrip("\r")
                if tail:
                    loop.call_soon_threadsafe(queue.put_nowait, ("stdout", tail))
            print("[codex-setup] PTY reader thread exiting")

        try:
            env = os.environ.copy()
            use_pty = _pty_wanted()
            print(f"[codex-setup] spawning login (use_pty={use_pty})")

            if use_pty:
                master_fd, slave_fd = _pty.openpty()
                try:
                    _set_pty_winsize(slave_fd)
                    process = await asyncio.create_subprocess_exec(
                        cli_path, "login", "--device-auth",
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
                    master_fd = None
                    raise
                os.close(slave_fd)

                threading.Thread(
                    target=_pty_reader_thread,
                    args=(master_fd, asyncio.get_event_loop()),
                    daemon=True,
                    name="codex-pty-reader",
                ).start()

                async def _wait_done_pty():
                    rc = await process.wait()
                    print(f"[codex-setup] process exited (rc={rc})")
                    await queue.put(("done", rc))

                asyncio.create_task(_wait_done_pty())

            else:
                process = await asyncio.create_subprocess_exec(
                    cli_path, "login", "--device-auth",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    env=env,
                )

                async def _read_pipe(stream, kind: str) -> None:
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        text = line.decode("utf-8", errors="replace").rstrip("\n")
                        await queue.put((kind, text))

                asyncio.create_task(_read_pipe(process.stdout, "stdout"))
                asyncio.create_task(_read_pipe(process.stderr, "stderr"))

                async def _wait_done_pipe():
                    rc = await process.wait()
                    print(f"[codex-setup] process exited (rc={rc})")
                    await queue.put(("done", rc))

                asyncio.create_task(_wait_done_pipe())

            deadline = time.monotonic() + CODEX_SETUP_TIMEOUT_SECONDS

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    if time.monotonic() >= deadline:
                        if process and process.returncode is None:
                            print("[codex-setup] timed out; killing process")
                            process.kill()
                        yield f"data: {json.dumps({'type': 'error', 'error': 'Login timed out'})}\n\n"
                        break
                    yield ": heartbeat\n\n"
                    continue

                kind, value = item
                if kind == "done":
                    if value == 0:
                        token = _read_token_from_codex_credentials()
                        if token:
                            for key in _TOKEN_ENV_KEYS:
                                os.environ[key] = token
                            _persist_token_to_env_files(token)
                            print(f"[codex-setup] token persisted (len={len(token)})")
                        else:
                            print("[codex-setup] login succeeded but no token found in credential files")
                    yield f"data: {json.dumps({'type': 'done', 'returncode': value})}\n\n"
                    break
                yield f"data: {json.dumps({'type': kind, 'line': value})}\n\n"

        except FileNotFoundError:
            print(f"[codex-setup] CLI not found: {cli_path}")
            yield f"data: {json.dumps({'type': 'error', 'error': f'codex CLI not found: {cli_path}'})}\n\n"
        except Exception as e:
            print(f"[codex-setup] unexpected error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            print("[codex-setup] SSE generator finished")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
