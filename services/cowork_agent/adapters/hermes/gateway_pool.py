"""
Per-profile hermes gateway pool.

Hermes's ``hermes gateway`` process inherits one active profile at startup
(``hermes_cli.profiles.get_active_profile_name()``), so a single gateway can
only ever write to ONE profile's state.db. To support multiple hermes profiles
concurrently in xo-cowork we spawn one gateway per profile, each with its own
``HERMES_HOME`` and ``API_SERVER_PORT``.

Layout:
- The "default" profile keeps using the gateway managed by ``hermes.sh`` on
  ``HERMES_API_URL`` (port 8642). We do not touch it.
- Custom profiles (aria, research, swe, …) get a dedicated gateway managed
  by this module on ports 8643+.
- A JSON registry at ``~/.hermes/.gateway-pool.json`` tracks the mapping so
  ports are stable across cowork-api restarts and we can recover orphaned
  processes by PID.

Spawn env:
- ``HERMES_HOME`` points at ``~/.hermes/profiles/<name>`` so the gateway loads
  that profile's sessions/memories/skills.
- ``API_SERVER_PORT`` overrides the bind port.
- ``API_SERVER_KEY`` is loaded from ``~/.hermes/.env`` because hermes only
  enables ``api_server`` when that key is present in env.
- Messaging-platform tokens (TELEGRAM_*, SLACK_*, WHATSAPP_*, DISCORD_*) get
  conditional handling — each profile is allowed its OWN bot, but duplicate
  tokens across profiles get stripped from the later-spawning profile so two
  gateways never race for the same Slack/Telegram socket. See
  ``_build_spawn_env`` for the precedence rules.

Lifecycle:
- ``ensure_gateway(profile)`` is the only public entry point. Idempotent
  get-or-start; returns the base URL.
- Death is detected lazily (next ``ensure_gateway`` call) — we don't run a
  background reaper.
- ``stop_gateway(profile)`` and ``stop_all()`` are exposed for tests and a
  future admin route.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

from services.cowork_agent.agent_registry import get_agent

log = logging.getLogger(__name__)

_HERMES = get_agent("hermes")
_HERMES_HOME: Path = _HERMES.home_dir
_PROFILES_DIR: Path = _HERMES.agents_dir
_HERMES_BIN: str = _HERMES.binary

# The registry lives under hermes home so it survives cowork-api restarts.
_POOL_FILE: Path = _HERMES_HOME / ".gateway-pool.json"
_LOG_DIR: Path = Path("/tmp")

# Port allocation starts after the canonical default gateway port (8642).
_PORT_BASE = 8643
_PORT_MAX = 8742  # cap so we don't sprawl unbounded

# Profile name accepted as the implicit "use the existing default gateway"
# alias. We never spawn a gateway for this one — hermes.sh owns it.
_DEFAULT_PROFILE = "default"

# Wait window for a freshly spawned gateway to start listening.
_BIND_WAIT_TIMEOUT = 20.0
_BIND_POLL_INTERVAL = 0.25

# Env-var prefixes that bind messaging-platform sockets. Each unique token
# value can only be live on one gateway process at a time (Slack Socket Mode,
# Telegram getUpdates, etc. open a long-lived stream). When the same token
# appears on two profiles, the later spawn gets it stripped so they don't
# race; profiles with DISTINCT tokens each bind their own bot.
_CHANNEL_ENV_PREFIXES = (
    "TELEGRAM_",
    "SLACK_",
    "WHATSAPP_",
    "DISCORD_",
    "FEISHU_",
    "SMS_",
    "DINGTALK_",
)


def _is_channel_var(key: str) -> bool:
    return any(key.startswith(p) for p in _CHANNEL_ENV_PREFIXES)

_PROFILE_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_lock = threading.Lock()


# ── Registry I/O ─────────────────────────────────────────────────────────────


def _load_pool() -> dict[str, dict]:
    if not _POOL_FILE.exists():
        return {}
    try:
        raw = json.loads(_POOL_FILE.read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pool(pool: dict[str, dict]) -> None:
    _POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    _POOL_FILE.write_text(json.dumps(pool, indent=2, sort_keys=True))


# ── Health helpers ───────────────────────────────────────────────────────────


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError, TypeError):
        return False


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _wait_for_listen(port: int, timeout: float = _BIND_WAIT_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_listening(port):
            return True
        time.sleep(_BIND_POLL_INTERVAL)
    return False


# ── Port allocation ──────────────────────────────────────────────────────────


def _allocate_port(profile: str, pool: dict[str, dict]) -> int:
    """Pick a stable port for ``profile``. Reuses the registry's existing
    assignment if any; otherwise grabs the next free slot in ``[_PORT_BASE,
    _PORT_MAX]`` not already taken by another profile or by an unrelated
    listener.
    """
    if profile in pool and "port" in pool[profile]:
        return int(pool[profile]["port"])
    taken_by_pool = {int(entry.get("port", 0)) for entry in pool.values() if entry.get("port")}
    for port in range(_PORT_BASE, _PORT_MAX + 1):
        if port in taken_by_pool:
            continue
        if _port_listening(port):
            # Something else owns it — skip; let the user discover the
            # collision rather than fighting an unknown process.
            continue
        return port
    raise RuntimeError(
        f"hermes gateway pool exhausted (no free port in {_PORT_BASE}-{_PORT_MAX})"
    )


# ── Spawning ─────────────────────────────────────────────────────────────────


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser — same shape as openclaw_env, but only the bits
    the hermes gateway cares about. Lines like ``# comment`` and blanks are
    skipped; ``KEY=value`` pairs are split on the first ``=``.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


def _live_channel_claims(exclude: str) -> dict[tuple[str, str], str]:
    """Map ``(channel_var, value) → owning profile`` for tokens already
    claimed by another live gateway.

    Default's gateway is hermes.sh-managed and considered always-live, so its
    ``~/.hermes/.env`` channel tokens are always claimants. Non-default
    profiles count only when their pool entry has a live PID — a profile that
    listed a token but isn't running doesn't block us.

    Used by ``_build_spawn_env`` to strip duplicates without preventing
    profiles with distinct tokens from binding their own bots.
    """
    out: dict[tuple[str, str], str] = {}

    if exclude != _DEFAULT_PROFILE:
        for k, v in _parse_env_file(_HERMES_HOME / ".env").items():
            if v and _is_channel_var(k):
                out[(k, v)] = _DEFAULT_PROFILE

    for pname, entry in _load_pool().items():
        if pname == exclude or not isinstance(entry, dict):
            continue
        pid = int(entry.get("pid") or 0)
        if not pid or not _is_alive(pid):
            continue
        for k, v in _parse_env_file(_PROFILES_DIR / pname / ".env").items():
            if v and _is_channel_var(k):
                out.setdefault((k, v), pname)

    return out


def _build_spawn_env(profile_home: Path, port: int) -> dict[str, str]:
    """Compose the subprocess env for ``hermes gateway run``.

    Layering (each step overrides the previous on conflict):

    1. ``os.environ`` — cowork-api's running env. We strip every channel
       prefix up front so default's tokens (which cowork-api.sh loaded into
       its env at startup) don't silently leak into a non-default gateway.
    2. ``~/.hermes/.env`` non-channel keys — API_SERVER_KEY etc. that every
       gateway needs. Channel keys from default's .env are NOT layered here;
       step 3 handles claims explicitly.
    3. ``<profile_home>/.env`` — the profile's own env, including any
       channel tokens it declares. The profile is the authoritative source
       for its own .env.
    4. Channel-collision strip — for each channel var in the assembled env,
       check if another live gateway has already claimed that exact value.
       If yes, strip from this spawn (the earlier-bound owner wins) and log
       a warning. This is what lets two profiles each have their own Slack
       workspace while preventing them from racing on a shared token.
    """
    profile_name = profile_home.name
    env: dict[str, str] = dict(os.environ)

    for key in [k for k in env if _is_channel_var(k)]:
        env.pop(key, None)

    for k, v in _parse_env_file(_HERMES_HOME / ".env").items():
        if _is_channel_var(k):
            continue
        env[k] = v

    for k, v in _parse_env_file(profile_home / ".env").items():
        env[k] = v

    claims = _live_channel_claims(exclude=profile_name)
    for var in [k for k in env if _is_channel_var(k)]:
        val = env.get(var) or ""
        owner = claims.get((var, val))
        if owner and val:
            log.warning(
                "hermes pool: stripping %s on profile=%s — same value already claimed by profile=%s",
                var, profile_name, owner,
            )
            env.pop(var, None)

    env["HERMES_HOME"] = str(profile_home)
    env["API_SERVER_PORT"] = str(port)
    # Belt-and-braces: ensure api_server is enabled even if API_SERVER_KEY
    # got stripped or never existed in this env. Without the key the gateway
    # will start but reject every authenticated request — fine to surface as
    # 401 rather than silently bind nothing.
    env.setdefault("API_SERVER_ENABLED", "true")
    return env


def _spawn(profile: str, profile_home: Path, port: int) -> int:
    """Spawn ``hermes gateway run`` for the given profile/port. Returns PID."""
    log_path = _LOG_DIR / f"hermes-gateway-{profile}.log"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Append-mode so restarts don't clobber prior crash traces.
    log_fd = log_path.open("a")
    try:
        proc = subprocess.Popen(
            [_HERMES_BIN, "gateway", "run"],
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=_build_spawn_env(profile_home, port),
            cwd=str(profile_home),
            close_fds=True,
            start_new_session=True,
        )
    finally:
        # Subprocess inherits a dup of the FD; ours can close.
        log_fd.close()
    return proc.pid


# ── Public API ───────────────────────────────────────────────────────────────


def default_gateway_url() -> str:
    """URL of the hermes.sh-managed default-profile gateway (the one we
    never touch)."""
    return _HERMES.api_url.replace("/v1/chat/completions", "")


def ensure_gateway(profile: str | None) -> str:
    """Return the base URL of a gateway for ``profile``, starting it if needed.

    ``None``/``""``/``"default"`` always resolves to the hermes.sh-managed
    gateway — we never spawn that one ourselves. For custom profiles we
    look up the registry, validate the running PID + port, and spawn a new
    process if either check fails.

    The returned URL is the **base** (e.g. ``http://127.0.0.1:8643``) — the
    caller appends ``/v1/chat/completions`` etc.
    """
    if not profile or profile == _DEFAULT_PROFILE:
        return default_gateway_url()

    if not _PROFILE_RE.match(profile):
        raise ValueError(f"invalid hermes profile name: {profile!r}")

    profile_home = _PROFILES_DIR / profile
    if not profile_home.is_dir():
        raise FileNotFoundError(
            f"hermes profile {profile!r} not found at {profile_home}"
        )

    with _lock:
        pool = _load_pool()
        entry = pool.get(profile)

        # Healthy entry: PID alive AND port listening. Bail early.
        if (
            isinstance(entry, dict)
            and _is_alive(int(entry.get("pid") or 0))
            and _port_listening(int(entry.get("port") or 0))
        ):
            return f"http://127.0.0.1:{int(entry['port'])}"

        # Stale or missing — clean up and re-spawn. Keep the original port
        # assignment if it's still ours so the user's UI bookmarks survive
        # a gateway crash.
        port = _allocate_port(profile, pool)
        if entry and isinstance(entry, dict):
            old_pid = int(entry.get("pid") or 0)
            if old_pid and _is_alive(old_pid):
                # Process exists but isn't listening on its port — kill it
                # before binding a fresh one to avoid leaking zombies.
                try:
                    os.kill(old_pid, 15)  # SIGTERM
                except ProcessLookupError:
                    pass
                time.sleep(0.2)
                if _is_alive(old_pid):
                    try:
                        os.kill(old_pid, 9)  # SIGKILL
                    except ProcessLookupError:
                        pass

        log.info("hermes pool: spawning gateway for profile=%s on port=%d", profile, port)
        pid = _spawn(profile, profile_home, port)

        if not _wait_for_listen(port):
            # Roll back the registry write — the spawn flopped, don't lie to
            # callers about a port that nobody's serving.
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
            raise RuntimeError(
                f"hermes gateway for profile {profile!r} did not bind on "
                f":{port} within {_BIND_WAIT_TIMEOUT:.0f}s — see "
                f"/tmp/hermes-gateway-{profile}.log"
            )

        pool[profile] = {
            "profile": profile,
            "port": port,
            "pid": pid,
            "started_at": time.time(),
        }
        _save_pool(pool)
        return f"http://127.0.0.1:{port}"


def stop_gateway(profile: str) -> bool:
    """Stop the pooled gateway for ``profile``. No-op for default profile.

    Returns True iff a process was actually signaled.
    """
    if not profile or profile == _DEFAULT_PROFILE:
        return False

    with _lock:
        pool = _load_pool()
        entry = pool.pop(profile, None)
        _save_pool(pool)
        if not isinstance(entry, dict):
            return False
        pid = int(entry.get("pid") or 0)
        if not pid or not _is_alive(pid):
            return False
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            return False
        # Give it a moment to drain.
        for _ in range(20):
            if not _is_alive(pid):
                return True
            time.sleep(0.1)
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        return True


def stop_all() -> int:
    """Stop every pooled gateway. Returns the number of processes killed.
    Intended for cowork-api shutdown hooks and tests.
    """
    pool = _load_pool()
    count = 0
    for profile in list(pool.keys()):
        if stop_gateway(profile):
            count += 1
    return count


def list_pool() -> list[dict]:
    """Snapshot of the registry, augmented with liveness info. Each entry:
    ``{profile, port, pid, started_at, alive, listening}``.
    """
    pool = _load_pool()
    out: list[dict] = []
    for profile, entry in sorted(pool.items()):
        if not isinstance(entry, dict):
            continue
        pid = int(entry.get("pid") or 0)
        port = int(entry.get("port") or 0)
        out.append({
            "profile": profile,
            "port": port,
            "pid": pid,
            "started_at": entry.get("started_at"),
            "alive": _is_alive(pid),
            "listening": _port_listening(port),
        })
    return out
