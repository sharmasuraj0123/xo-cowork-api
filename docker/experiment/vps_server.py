"""Standalone supervisor for an XO Experiment container.

The Agents API does not call this HTTP service. ``codex exec-server`` still
connects outbound to the Agents API; this service is the small, private control
plane that makes the container behave like a disposable VPS:

* copy the two pre-filtered source trees into a writable workspace;
* supervise sandbox Space and ``codex exec-server`` as child processes;
* expose non-secret liveness/readiness information; and
* provide a bearer-authenticated, bounded root command endpoint.

The service intentionally runs as root inside the container. The outer
container boundary (no Docker socket, no unrelated host mounts) remains the
security boundary.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
import os
import re
import shutil
import signal
import stat
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field


LOGGER = logging.getLogger("xo.experiment.vps")
SERVER_VERSION = "1"
DEFAULT_CONTROL_PORT = 8787
DEFAULT_SPACE_PORT = 5002
MAX_COMMAND_CHARS = 20_000
MAX_COMMAND_OUTPUT_BYTES = 20_000
MAX_BOOTSTRAP_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXEC_CONCURRENCY = 8
MIN_CONTROL_TOKEN_CHARS = 24
PERMISSION_PROFILES = {"unrestricted", "hardened"}

_SECRET_NAME = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)
_BEARER_VALUE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|token|secret|password|credential)\s*[=:]\s*)[^\s]+"
)
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,254}")

_BLOCKED_SOURCE_NAMES = {
    ".ssh",
    ".aws",
    ".docker",
    ".gnupg",
    ".kube",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".envrc",
    "mcp-tokens.json",
    "credentials.json",
    "secrets.env",
    "secrets.json",
    "secrets.toml",
    "secrets.yaml",
    "secrets.yml",
    "id_rsa",
    "id_ed25519",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _read_control_token() -> str:
    token_file = os.getenv("EXPERIMENT_CONTROL_TOKEN_FILE", "").strip()
    if token_file:
        path = Path(token_file)
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError("Experiment control token file could not be read") from error
    else:
        token = os.getenv("EXPERIMENT_CONTROL_TOKEN", "").strip()
    if len(token) < MIN_CONTROL_TOKEN_CHARS:
        raise RuntimeError(
            f"EXPERIMENT_CONTROL_TOKEN must be at least {MIN_CONTROL_TOKEN_CHARS} characters"
        )
    return token


def _safe_remote(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise RuntimeError("AGENTS_API_REMOTE is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RuntimeError("AGENTS_API_REMOTE is invalid")
    return value.rstrip("/")


def _validate_project_id(value: str) -> str:
    if (
        not _PROJECT_ID.fullmatch(value)
        or value in {".", ".."}
        or value.startswith(".")
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise RuntimeError("XO_PROJECT_ID is invalid")
    return value


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _require_descendant(path: Path, root: Path, label: str) -> None:
    resolved_path = _resolved(path)
    resolved_root = _resolved(root)
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        raise RuntimeError(f"{label} must be below the workspace root")


@dataclass(frozen=True)
class Settings:
    control_token: str
    project_id: str
    environment_id: str
    agents_api_remote: str
    source_root: Path = Path("/xo-sources")
    workspace_root: Path = Path("/workspace")
    projects_root: Path = Path("/workspace/xo-projects")
    cowork_api_root: Path = Path("/workspace/xo-cowork-api")
    codex_home: Path = Path("/codex-home")
    log_root: Path = Path("/tmp/xo-experiment-supervisor")
    control_port: int = DEFAULT_CONTROL_PORT
    space_port: int = DEFAULT_SPACE_PORT
    permission_profile: str = "unrestricted"
    max_command_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES
    max_bootstrap_bytes: int = MAX_BOOTSTRAP_BYTES
    max_exec_concurrency: int = MAX_EXEC_CONCURRENCY
    shutdown_grace_seconds: float = 5.0
    space_command: tuple[str, ...] | None = None
    codex_command: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _validate_project_id(self.project_id)
        if len(self.control_token) < MIN_CONTROL_TOKEN_CHARS:
            raise RuntimeError("Experiment control token is too short")
        if not self.environment_id.strip():
            raise RuntimeError("AGENT_ENVIRONMENT_ID is required")
        if self.permission_profile not in PERMISSION_PROFILES:
            raise RuntimeError("Experiment permission profile is invalid")
        if not 1 <= self.control_port <= 65_535 or not 1 <= self.space_port <= 65_535:
            raise RuntimeError("Supervisor ports are invalid")
        _require_descendant(self.projects_root, self.workspace_root, "XO_PROJECTS_ROOT")
        _require_descendant(self.cowork_api_root, self.workspace_root, "XO_COWORK_API_ROOT")
        _require_descendant(self.codex_home, Path("/"), "CODEX_HOME")

    @property
    def project_directory(self) -> Path:
        return self.projects_root / self.project_id

    @property
    def resolved_space_command(self) -> tuple[str, ...]:
        if self.space_command is not None:
            return self.space_command
        return (
            sys.executable,
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(self.space_port),
            "--lifespan",
            "off",
        )

    @property
    def resolved_codex_command(self) -> tuple[str, ...]:
        if self.codex_command is not None:
            return self.codex_command
        sandbox_mode = (
            "danger-full-access"
            if self.permission_profile == "unrestricted"
            else "workspace-write"
        )
        return (
            "codex",
            "exec-server",
            "--strict-config",
            "-c",
            f'sandbox_mode="{sandbox_mode}"',
            "-c",
            'approval_policy="never"',
            "--enable",
            "shell_tool",
            "--enable",
            "unified_exec",
            "--remote",
            self.agents_api_remote,
            "--environment-id",
            self.environment_id,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        project_id = _validate_project_id(os.getenv("XO_PROJECT_ID", "").strip())
        environment_id = os.getenv("AGENT_ENVIRONMENT_ID", "").strip()
        if not environment_id:
            raise RuntimeError("AGENT_ENVIRONMENT_ID is required")
        permission_profile = os.getenv(
            "EXPERIMENT_PERMISSION_PROFILE", "unrestricted"
        ).strip().lower()
        return cls(
            control_token=_read_control_token(),
            project_id=project_id,
            environment_id=environment_id,
            agents_api_remote=_safe_remote(
                os.getenv(
                    "AGENTS_API_REMOTE", "https://api.openai.com/v1/agents/api"
                ).strip()
            ),
            source_root=Path(os.getenv("EXPERIMENT_SOURCE_ROOT", "/xo-sources")),
            workspace_root=Path(os.getenv("EXPERIMENT_WORKSPACE_ROOT", "/workspace")),
            projects_root=Path(os.getenv("XO_PROJECTS_ROOT", "/workspace/xo-projects")),
            cowork_api_root=Path(
                os.getenv("XO_COWORK_API_ROOT", "/workspace/xo-cowork-api")
            ),
            codex_home=Path(os.getenv("CODEX_HOME", "/codex-home")),
            log_root=Path(
                os.getenv("EXPERIMENT_SUPERVISOR_LOG_ROOT", "/tmp/xo-experiment-supervisor")
            ),
            control_port=_env_int(
                "EXPERIMENT_CONTROL_PORT", DEFAULT_CONTROL_PORT, 1, 65_535
            ),
            space_port=_env_int("SPACE_PORT", DEFAULT_SPACE_PORT, 1, 65_535),
            permission_profile=permission_profile,
            max_command_output_bytes=_env_int(
                "EXPERIMENT_COMMAND_OUTPUT_BYTES",
                MAX_COMMAND_OUTPUT_BYTES,
                1_024,
                10 * 1024 * 1024,
            ),
            max_bootstrap_bytes=_env_int(
                "EXPERIMENT_BOOTSTRAP_BYTES",
                MAX_BOOTSTRAP_BYTES,
                1_024,
                20 * 1024 * 1024 * 1024,
            ),
            max_exec_concurrency=_env_int(
                "EXPERIMENT_EXEC_CONCURRENCY", MAX_EXEC_CONCURRENCY, 1, 64
            ),
        )


class Redactor:
    def __init__(self, values: set[str]) -> None:
        self._values = tuple(sorted((value for value in values if len(value) >= 6), key=len, reverse=True))

    @classmethod
    def from_environment(cls, control_token: str) -> "Redactor":
        values = {control_token}
        for name, value in os.environ.items():
            if value and _SECRET_NAME.search(name):
                values.add(value)
        return cls(values)

    def __call__(self, value: object) -> str:
        text = str(value)
        for secret in self._values:
            text = text.replace(secret, "[REDACTED]")
        text = _BEARER_VALUE.sub(r"\1[REDACTED]", text)
        text = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", text)
        return _OPENAI_KEY.sub("[REDACTED]", text)


def _blocked_source_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in _BLOCKED_SOURCE_NAMES
        or lowered == ".env"
        or lowered.startswith(".env.")
        or lowered.endswith((".pem", ".p12", ".pfx", ".key"))
    )


def _inspect_source_tree(root: Path, byte_budget: int) -> int:
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("Staged source tree is missing or unsafe")
    total = 0
    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in [*dirs, *files]:
            path = directory_path / name
            if _blocked_source_name(name):
                raise RuntimeError("Staged source tree contains a blocked credential path")
            try:
                mode = path.lstat().st_mode
            except OSError as error:
                raise RuntimeError("Staged source tree could not be inspected") from error
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise RuntimeError("Staged source tree contains a link or special file")
            if stat.S_ISREG(mode):
                total += path.stat().st_size
                if total > byte_budget:
                    raise RuntimeError("Staged source trees exceed the bootstrap size limit")
    return total


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def bootstrap_sources(settings: Settings) -> Path:
    """Copy both mounted, pre-filtered source trees into the writable workspace."""

    workspace = _resolved(settings.workspace_root)
    projects_root = _resolved(settings.projects_root)
    cowork_root = _resolved(settings.cowork_api_root)
    _require_descendant(projects_root, workspace, "XO_PROJECTS_ROOT")
    _require_descendant(cowork_root, workspace, "XO_COWORK_API_ROOT")

    project_source = settings.source_root / "project"
    cowork_source = settings.source_root / "xo-cowork-api"
    project_size = _inspect_source_tree(project_source, settings.max_bootstrap_bytes)
    _inspect_source_tree(cowork_source, settings.max_bootstrap_bytes - project_size)

    workspace.mkdir(parents=True, exist_ok=True)
    temporary = workspace / f".xo-bootstrap-{uuid.uuid4().hex}"
    temporary.mkdir(mode=0o700)
    try:
        shutil.copytree(project_source, temporary / "project")
        shutil.copytree(cowork_source, temporary / "xo-cowork-api")

        _remove_path(projects_root)
        projects_root.mkdir(parents=True, exist_ok=True)
        project_destination = projects_root / settings.project_id
        (temporary / "project").replace(project_destination)

        _remove_path(cowork_root)
        cowork_root.parent.mkdir(parents=True, exist_ok=True)
        (temporary / "xo-cowork-api").replace(cowork_root)

        marker = workspace / ".xo-experiment-bootstrap.json"
        marker.write_text(
            json.dumps(
                {
                    "version": SERVER_VERSION,
                    "project_id": settings.project_id,
                    "completed_at": _now(),
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        marker.chmod(0o600)
        # Preserve the configured path spelling. On macOS, for example, /var
        # resolves through /private/var; callers should receive the same
        # container-visible path they configured rather than a host alias.
        return settings.project_directory
    except BaseException:
        _remove_path(projects_root / settings.project_id)
        _remove_path(cowork_root)
        raise
    finally:
        _remove_path(temporary)


@dataclass
class ManagedChild:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    log_root: Path
    process: asyncio.subprocess.Process | None = None
    started_at: str | None = None
    _log_file: BinaryIO | None = field(default=None, repr=False)

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        self.log_root.mkdir(parents=True, exist_ok=True)
        self.log_root.chmod(0o700)
        descriptor = os.open(
            self.log_root / f"{self.name}.log",
            os.O_CREAT | os.O_WRONLY | os.O_APPEND,
            0o600,
        )
        self._log_file = os.fdopen(descriptor, "ab", buffering=0)
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=str(self.cwd),
                env=self.env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=self._log_file,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except BaseException:
            self._log_file.close()
            self._log_file = None
            raise
        self.started_at = _now()

    async def stop(self, grace_seconds: float) -> None:
        process = self.process
        if process is None:
            self._close_log()
            return
        if process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=grace_seconds)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with suppress(Exception):
                    await process.wait()
        else:
            with suppress(Exception):
                await process.wait()
        self._close_log()

    def snapshot(self) -> dict[str, Any]:
        process = self.process
        return {
            "name": self.name,
            "running": bool(process is not None and process.returncode is None),
            "pid": process.pid if process is not None else None,
            "exit_code": process.returncode if process is not None else None,
            "started_at": self.started_at,
        }

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None


class ExecRequest(BaseModel):
    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS)
    cwd: str | None = Field(default=None, max_length=4_096)
    timeout_seconds: int = Field(default=300, ge=1, le=600)


class ExecResponse(BaseModel):
    exit_code: int
    output: str
    cwd: str
    timed_out: bool
    truncated: bool
    duration_ms: int


async def _collect_tail(
    stream: asyncio.StreamReader | None, limit: int
) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    output = bytearray()
    total = 0
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            break
        total += len(chunk)
        output.extend(chunk)
        if len(output) > limit:
            del output[: len(output) - limit]
    return bytes(output), total > limit


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


class VpsSupervisor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redact = Redactor.from_environment(settings.control_token)
        self.phase = "created"
        self.bootstrapped = False
        self.started_at: str | None = None
        self.error: str | None = None
        self.children: dict[str, ManagedChild] = {}
        self._lock = asyncio.Lock()
        self._exec_slots = asyncio.Semaphore(settings.max_exec_concurrency)

    def _base_child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # The control credential protects the private HTTP plane; neither Space,
        # Codex nor user commands need it in their child environment.
        env.pop("EXPERIMENT_CONTROL_TOKEN", None)
        env.pop("EXPERIMENT_CONTROL_TOKEN_FILE", None)
        env["HOME"] = env.get("HOME", "/home/sandbox")
        env["CODEX_HOME"] = str(self.settings.codex_home)
        return env

    def _space_env(self) -> dict[str, str]:
        env = self._base_child_env()
        env.pop("CODEX_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
        env["XO_PROJECT_ID"] = self.settings.project_id
        env["XO_PROJECTS_ROOT"] = str(self.settings.projects_root)
        env["XO_COWORK_API_ROOT"] = str(self.settings.cowork_api_root)
        env["AI_WORKSPACE_ROOT"] = str(self.settings.project_directory)
        env["SPACE_PORT"] = str(self.settings.space_port)
        env["STAGE"] = "local"
        env["RELAY_ENABLED"] = "false"
        env["STARTUP_WARMUP_ENABLED"] = "false"
        env["EXPERIMENT_RUNTIME_ROLE"] = "sandbox"
        env["EXPERIMENT_PERMISSION_PROFILE"] = self.settings.permission_profile
        return env

    async def start(self) -> None:
        async with self._lock:
            if self.phase not in {"created", "stopped"}:
                return
            self.phase = "bootstrapping"
            self.started_at = _now()
            self.error = None
            try:
                await asyncio.to_thread(bootstrap_sources, self.settings)
                self.settings.codex_home.mkdir(parents=True, exist_ok=True)
                self.bootstrapped = True
                self.phase = "starting_children"

                self.children = {
                    "space": ManagedChild(
                        name="space",
                        argv=self.settings.resolved_space_command,
                        cwd=self.settings.cowork_api_root,
                        env=self._space_env(),
                        log_root=self.settings.log_root,
                    ),
                    "codex": ManagedChild(
                        name="codex",
                        argv=self.settings.resolved_codex_command,
                        cwd=self.settings.project_directory,
                        env=self._base_child_env(),
                        log_root=self.settings.log_root,
                    ),
                }
                await self.children["space"].start()
                await self.children["codex"].start()
                # Catch immediate command/configuration failures while leaving
                # the HTTP dashboard alive for a non-secret diagnosis.
                await asyncio.sleep(0.05)
                failed = [
                    child.name
                    for child in self.children.values()
                    if child.process is None or child.process.returncode is not None
                ]
                if failed:
                    raise RuntimeError(f"Managed child exited during startup: {', '.join(failed)}")
                self.phase = "running"
            except asyncio.CancelledError:
                self.phase = "stopping"
                await self._stop_children()
                self.phase = "stopped"
                raise
            except Exception as error:
                self.error = self.redact(error)
                self.phase = "failed"
                await self._stop_children()

    async def close(self) -> None:
        async with self._lock:
            if self.phase == "stopped":
                return
            self.phase = "stopping"
            await self._stop_children()
            self.phase = "stopped"

    async def _stop_children(self) -> None:
        for name in ("codex", "space"):
            child = self.children.get(name)
            if child is not None:
                with suppress(Exception):
                    await child.stop(self.settings.shutdown_grace_seconds)

    def is_ready(self) -> bool:
        return bool(
            self.phase == "running"
            and self.bootstrapped
            and self.settings.project_directory.is_dir()
            and self.settings.cowork_api_root.is_dir()
            and os.access(self.settings.project_directory, os.W_OK)
            and self.children
            and all(
                child.process is not None and child.process.returncode is None
                for child in self.children.values()
            )
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "service": "xo-experiment-vps",
            "version": SERVER_VERSION,
            "phase": self.phase,
            "ready": self.is_ready(),
            "bootstrapped": self.bootstrapped,
            "project_id": self.settings.project_id,
            "permission_profile": self.settings.permission_profile,
            "workspace_directory": str(self.settings.project_directory),
            "started_at": self.started_at,
            "error": self.redact(self.error) if self.error else None,
            "processes": [child.snapshot() for child in self.children.values()],
        }

    async def execute(self, request: ExecRequest) -> ExecResponse:
        command = request.command.strip()
        if not command:
            raise ValueError("Command is empty")
        cwd = Path(request.cwd) if request.cwd else self.settings.project_directory
        if not cwd.is_absolute() or "\x00" in str(cwd) or not cwd.is_dir():
            raise ValueError("Command working directory is invalid")

        started = time.monotonic()
        async with self._exec_slots:
            process = await asyncio.create_subprocess_exec(
                "sh",
                "-lc",
                command,
                cwd=str(cwd),
                env=self._base_child_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            collector = asyncio.create_task(
                _collect_tail(process.stdout, self.settings.max_command_output_bytes)
            )
            timed_out = False
            try:
                await asyncio.wait_for(process.wait(), timeout=request.timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                _kill_process_group(process)
                with suppress(Exception):
                    await process.wait()
            except asyncio.CancelledError:
                _kill_process_group(process)
                with suppress(Exception):
                    await process.wait()
                with suppress(Exception):
                    await collector
                raise
            output_bytes, truncated = await collector

        output = output_bytes.decode("utf-8", errors="replace")
        return ExecResponse(
            exit_code=124 if timed_out else int(process.returncode or 0),
            output=self.redact(output),
            cwd=str(cwd),
            timed_out=timed_out,
            truncated=truncated,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        )


def _dashboard(snapshot: dict[str, Any]) -> str:
    process_rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(str(process.get("name", ""))),
            "running" if process.get("running") else "stopped",
            html.escape(str(process.get("pid") or "—")),
            html.escape(str(process.get("exit_code") if process.get("exit_code") is not None else "—")),
        )
        for process in snapshot.get("processes", [])
    ) or '<tr><td colspan="4">No managed children</td></tr>'
    error = snapshot.get("error")
    error_html = (
        f'<p class="error">{html.escape(str(error))}</p>' if error else ""
    )
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>XO Experiment VPS</title><style>
body{{font:15px/1.5 ui-sans-serif,system-ui;background:#111318;color:#e8e5dc;max-width:860px;margin:48px auto;padding:0 24px}}
h1{{font-size:28px}}code{{color:#a8d94f}}table{{border-collapse:collapse;width:100%;margin-top:24px}}th,td{{padding:9px 12px;border:1px solid #333;text-align:left}}.ok{{color:#a8d94f}}.bad,.error{{color:#e79a84}}
</style></head><body>
<h1>XO Experiment VPS</h1><p>Status: <b class="{}">{}</b></p>
<p>Project: <code>{}</code><br>Workspace: <code>{}</code></p>{}
<table><thead><tr><th>Process</th><th>State</th><th>PID</th><th>Exit</th></tr></thead><tbody>{}</tbody></table>
<p>The command API is private and requires a bearer credential.</p></body></html>""".format(
        "ok" if snapshot.get("ready") else "bad",
        "ready" if snapshot.get("ready") else html.escape(str(snapshot.get("phase", "unknown"))),
        html.escape(str(snapshot.get("project_id", ""))),
        html.escape(str(snapshot.get("workspace_directory", ""))),
        error_html,
        process_rows,
    )


def create_app(
    settings: Settings | None = None,
    supervisor: VpsSupervisor | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_supervisor = supervisor or VpsSupervisor(resolved_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await resolved_supervisor.start()
        try:
            yield
        finally:
            await resolved_supervisor.close()

    app = FastAPI(
        title="XO Experiment VPS",
        version=SERVER_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.supervisor = resolved_supervisor

    def require_bearer(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {resolved_settings.control_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default 422 body echoes the rejected input. A command may
        # itself contain a credential, so return only a stable generic error.
        return JSONResponse(status_code=422, content={"detail": "invalid request"})

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return _dashboard(resolved_supervisor.snapshot())

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "xo-experiment-vps"}

    @app.get("/readyz")
    async def readiness() -> JSONResponse:
        snapshot = resolved_supervisor.snapshot()
        return JSONResponse(
            status_code=200 if snapshot["ready"] else 503,
            content=snapshot,
        )

    @app.get("/status")
    async def service_status() -> dict[str, Any]:
        return resolved_supervisor.snapshot()

    @app.post(
        "/v1/exec",
        response_model=ExecResponse,
        dependencies=[Depends(require_bearer)],
    )
    async def execute(request: ExecRequest) -> ExecResponse:
        try:
            return await resolved_supervisor.execute(request)
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid command request") from None
        except Exception as error:
            LOGGER.error("Experiment command failed: %s", resolved_supervisor.redact(error))
            raise HTTPException(status_code=500, detail="command execution failed") from None

    return app


def main() -> None:
    logging.basicConfig(
        level=os.getenv("EXPERIMENT_SUPERVISOR_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = Settings.from_env()
    except Exception as error:
        # Settings construction has no Redactor yet; its errors intentionally
        # contain only variable names and stable validation text, never values.
        raise SystemExit(f"XO Experiment VPS configuration failed: {error}") from None
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.control_port,
        access_log=True,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
