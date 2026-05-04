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
import base64
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

_OPENCLAW_DEFAULT_PRIMARY_MODEL = "openai-codex/gpt-5.4"


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


def _decode_jwt_payload(jwt: str) -> Optional[dict]:
    """Decode a JWT's payload segment. No signature verification — caller trusts source."""
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return None
        seg = parts[1]
        pad = "=" * (-len(seg) % 4)
        decoded = base64.urlsafe_b64decode(seg + pad)
        payload = json.loads(decoded)
        return payload if isinstance(payload, dict) else None
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _read_codex_credentials() -> Optional[dict]:
    """
    After login succeeds the CLI writes credentials to a known location.
    Try common paths and return a dict if found:
        {"token": str,
         "email": Optional[str],
         "refresh": Optional[str],
         "expires_ms": Optional[int]}
    Email/refresh/expires_ms are only populated when reading the codex CLI's
    chatgpt-mode auth.json (which carries an id_token JWT and refresh_token).
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
            token: Optional[str] = None
            email: Optional[str] = None
            refresh: Optional[str] = None
            expires_ms: Optional[int] = None
            if isinstance(data, dict):
                # codex CLI ChatGPT/device-auth mode: {"tokens": {"access_token": ..., "id_token": ..., "refresh_token": ..., "account_id": ...}, ...}
                tokens_obj = data.get("tokens")
                if isinstance(tokens_obj, dict):
                    token = tokens_obj.get("access_token") or tokens_obj.get("id_token")
                    rt = tokens_obj.get("refresh_token")
                    if isinstance(rt, str) and rt:
                        refresh = rt
                    id_token = tokens_obj.get("id_token")
                    if isinstance(id_token, str):
                        payload = _decode_jwt_payload(id_token)
                        if isinstance(payload, dict):
                            claim = payload.get("email")
                            if isinstance(claim, str) and claim:
                                email = claim
                            exp = payload.get("exp")
                            if isinstance(exp, (int, float)):
                                expires_ms = int(exp) * 1000
                # codex CLI API-key mode: {"auth_mode": "...", "OPENAI_API_KEY": "sk-..."}
                if not token:
                    api_key = data.get("OPENAI_API_KEY")
                    if isinstance(api_key, str):
                        token = api_key
                # legacy flat format: {"access_token": "..."}
                if not token:
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
                print(f"[codex-setup] credentials read from {path}")
                return {
                    "token": token,
                    "email": email,
                    "refresh": refresh,
                    "expires_ms": expires_ms,
                }
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return None


def _openclaw_config_path() -> str:
    return str(Path.home() / ".openclaw" / "openclaw.json")


def _upsert_openclaw_config(email: str) -> None:
    """
    Update ~/.openclaw/openclaw.json post-login:
      1. auth.profiles["openai-codex:<email>"] = {provider, mode, email}
      2. agents.defaults.model.primary = _OPENCLAW_DEFAULT_PRIMARY_MODEL (always overwrite)

    Best-effort: missing/malformed file is logged and skipped, never raised.
    Atomic write via temp file + os.replace so a crash mid-write cannot corrupt the file.
    """
    path = _openclaw_config_path()
    if not os.path.isfile(path):
        print(f"[codex-setup] openclaw.json not found at {path}, skipping config upsert")
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[codex-setup] failed to read {path} ({e}); skipping config upsert")
        return
    if not isinstance(data, dict):
        print(f"[codex-setup] {path} is not a JSON object; skipping config upsert")
        return

    auth = data.get("auth")
    if not isinstance(auth, dict):
        auth = {}
        data["auth"] = auth
    profiles = auth.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
        auth["profiles"] = profiles
    profile_key = f"openai-codex:{email}"
    profiles[profile_key] = {
        "provider": "openai-codex",
        "mode": "oauth",
        "email": email,
    }

    agents = data.get("agents")
    if not isinstance(agents, dict):
        agents = {}
        data["agents"] = agents
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        defaults = {}
        agents["defaults"] = defaults
    model = defaults.get("model")
    if not isinstance(model, dict):
        model = {}
        defaults["model"] = model
    model["primary"] = _OPENCLAW_DEFAULT_PRIMARY_MODEL

    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
        print(
            f"[codex-setup] openclaw.json updated: profile={profile_key}, "
            f"primary={_OPENCLAW_DEFAULT_PRIMARY_MODEL}"
        )
    except OSError as e:
        print(f"[codex-setup] failed to write {path} ({e}); skipping config upsert")
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _agent_auth_profiles_path() -> str:
    return str(Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json")


def _upsert_agent_auth_profile(
    email: str,
    access: str,
    refresh: str,
    expires_ms: int,
) -> None:
    """
    Upsert the email-keyed openai-codex oauth entry into the main agent's
    auth-profiles.json — the credential store openclaw consults at runtime.

    Schema matches the existing email-keyed entries:
        profiles["openai-codex:<email>"] = {
            "type": "oauth", "provider": "openai-codex",
            "access": <access_token>, "refresh": <refresh_token>,
            "expires": <ms epoch from JWT exp>, "email": <email>
        }

    Creates the file (and parent dirs) with `{"version": 1, "profiles": {...}}`
    if it doesn't exist. A malformed existing file is logged and skipped to
    avoid clobbering hand-edits in flight. Always overwrites an existing entry
    for this email so re-login refreshes tokens.
    """
    path = _agent_auth_profiles_path()
    created = False
    if not os.path.isfile(path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError as e:
            print(f"[codex-setup] failed to create dir for {path} ({e}); skipping agent profile upsert")
            return
        data: dict = {"version": 1, "profiles": {}}
        created = True
    else:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[codex-setup] failed to read {path} ({e}); skipping agent profile upsert")
            return
        if not isinstance(data, dict):
            print(f"[codex-setup] {path} is not a JSON object; skipping agent profile upsert")
            return

    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
        data["profiles"] = profiles
    profile_key = f"openai-codex:{email}"
    profiles[profile_key] = {
        "type": "oauth",
        "provider": "openai-codex",
        "access": access,
        "refresh": refresh,
        "expires": expires_ms,
        "email": email,
    }

    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
        action = "created" if created else "updated"
        print(f"[codex-setup] agent auth-profiles.json {action}: profile={profile_key}")
    except OSError as e:
        print(f"[codex-setup] failed to write {path} ({e}); skipping agent profile upsert")
        try:
            os.remove(tmp_path)
        except OSError:
            pass


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
                        creds = _read_codex_credentials()
                        if creds and creds.get("token"):
                            token = creds["token"]
                            for key in _TOKEN_ENV_KEYS:
                                os.environ[key] = token
                            _persist_token_to_env_files(token)
                            print(f"[codex-setup] token persisted (len={len(token)})")
                            email = creds.get("email")
                            if email:
                                _upsert_openclaw_config(email)
                                refresh = creds.get("refresh")
                                expires_ms = creds.get("expires_ms")
                                if refresh and expires_ms:
                                    _upsert_agent_auth_profile(email, token, refresh, expires_ms)
                                else:
                                    print(
                                        "[codex-setup] missing refresh/expires; "
                                        "skipping agent auth-profiles.json upsert"
                                    )
                            else:
                                print("[codex-setup] no email claim found, skipping openclaw.json upsert")
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
