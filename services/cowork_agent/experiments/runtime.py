"""One-click XO project experiments backed by a replaceable sandbox provider.

The first provider is intentionally local-development only: it creates an
Agents API self-hosted session, safely snapshots one XO project plus the current
xo-cowork-api checkout into a Docker container, starts sandbox Space and
``codex exec-server``, and keeps the boot-verified session available for
follow-up turns. A Coder provider can replace it without changing the Space UI.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx


ExperimentStatus = Literal[
    "starting",
    "ready",
    "stopping",
    "stopped",
    "failed",
    "cleanup_failed",
]
ACTIVE_STATUSES = {"starting", "ready", "stopping", "cleanup_failed"}
STOPPABLE_STATUSES = {"starting", "ready", "cleanup_failed"}
MAX_OUTPUT_CHARS = 16_000
MAX_MESSAGE_CHARS = 20_000
MAX_TRANSCRIPT_MESSAGES = 40
DEFAULT_IMAGE = "xo-experiment-sandbox:latest"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_API_BASE = "https://api.openai.com/v1/agents"
DEFAULT_MAX_ACTIVE = 2
SPACE_PORT = 5002
SANDBOX_PROJECTS_ROOT = "/workspace/xo-projects"
SANDBOX_API_ROOT = "/workspace/xo-cowork-api"
SANDBOX_RUNTIME_ROLE = "sandbox"

BOOTSTRAP_SCRIPT = r"""
set -eu
project_dir="$XO_PROJECTS_ROOT/$XO_PROJECT_ID"
rm -rf "$XO_PROJECTS_ROOT" "$XO_COWORK_API_ROOT"
mkdir -p "$project_dir" "$XO_COWORK_API_ROOT"
(cd /xo-sources/project && tar -cf - .) | (cd "$project_dir" && tar --no-same-owner -xf -)
(cd /xo-sources/xo-cowork-api && tar -cf - .) | (cd "$XO_COWORK_API_ROOT" && tar --no-same-owner -xf -)

cd "$XO_COWORK_API_ROOT"
python -m uvicorn server:app \
  --host 0.0.0.0 \
  --port "$SPACE_PORT" \
  --lifespan off \
  >/tmp/xo-space.log 2>&1 &

cd "$project_dir"
exec codex exec-server \
  --remote "$AGENTS_API_REMOTE" \
  --environment-id "$AGENT_ENVIRONMENT_ID"
""".strip()


class ExperimentError(RuntimeError):
    """Base error surfaced through a stable, secret-free API message."""


class ExperimentUnavailable(ExperimentError):
    """The configured experiment provider cannot launch work."""


class ExperimentCapacityExceeded(ExperimentError):
    """The local provider has reached its configured concurrent limit."""


class ExperimentNotFound(ExperimentError):
    """No experiment record exists for the requested id."""


class ExperimentNotReady(ExperimentError):
    """The experiment cannot accept a turn in its current state."""


class ExperimentTurnBusy(ExperimentError):
    """A turn is already active for this experiment."""


@dataclass
class ExperimentMessage:
    id: str
    role: Literal["user", "assistant"]
    text: str
    status: Literal["complete", "streaming", "failed"] = "complete"
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())

    def snapshot(self) -> dict[str, str]:
        return {
            "id": self.id,
            "role": self.role,
            "text": self.text,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class ExperimentRecord:
    id: str
    project_id: str
    provider: str = "local_docker"
    model: str = DEFAULT_MODEL
    status: ExperimentStatus = "starting"
    stage: str = "queued"
    output: str = ""
    error: str | None = None
    session_id: str | None = None
    environment_id: str | None = None
    sandbox_id: str | None = None
    failed_stage: str | None = None
    space_url: str | None = None
    workspace_directory: str | None = None
    turn_status: Literal["idle", "running", "failed"] = "idle"
    turn_error: str | None = None
    messages: list[ExperimentMessage] = field(default_factory=list)
    expires_at: str | None = None
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    turn_task: asyncio.Task[None] | None = field(default=None, repr=False)
    expiry_task: asyncio.Task[None] | None = field(default=None, repr=False)
    snapshot_dir: Path | None = field(default=None, repr=False)
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def touch(self) -> None:
        self.updated_at = _now()

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "stage": self.stage,
            "failed_stage": self.failed_stage,
            "output": self.output,
            "error": self.error,
            "agent_session_id": self.session_id,
            "sandbox_id": self.sandbox_id,
            "space_url": self.space_url,
            "workspace_directory": self.workspace_directory,
            "turn_status": self.turn_status,
            "turn_error": self.turn_error,
            "messages": [message.snapshot() for message in self.messages],
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "can_stop": self.status in STOPPABLE_STATUSES,
            "can_message": self.status == "ready" and self.turn_status != "running",
        }


class ExperimentProvider(Protocol):
    name: str

    async def availability(self) -> dict[str, Any]: ...

    async def launch(self, record: ExperimentRecord, source_dir: Path) -> None: ...

    async def interact(
        self,
        record: ExperimentRecord,
        prompt: str,
        response: ExperimentMessage,
    ) -> None: ...

    async def stop(self, record: ExperimentRecord) -> list[str]: ...


class LocalDockerExperimentProvider:
    """Developer provider using the verified Agents API Docker executor flow."""

    name = "local_docker"

    async def reconcile(self) -> list[str]:
        """Remove resources left by an unclean previous local server exit."""
        result = await _run_process(
            [
                "docker",
                "ps",
                "--all",
                "--filter",
                "label=xo.experiment.managed=true",
                "--format",
                "{{.Names}}\t{{.Label \"xo.experiment.session_id\"}}",
            ],
            timeout=15,
        )
        if result.returncode != 0:
            return []
        warnings: list[str] = []
        for line in result.stdout.splitlines():
            container_name, _, session_id = line.partition("\t")
            if not container_name.startswith("xo-experiment-"):
                continue
            removed = await _run_process(["docker", "rm", "-f", container_name], timeout=20)
            if removed.returncode != 0 and "No such" not in removed.output:
                warnings.append("Stale Docker sandbox cleanup failed")
            if session_id and _api_key():
                try:
                    await _delete_agent_session(session_id)
                except Exception:
                    warnings.append("Stale Agents API session cleanup failed")
        return warnings

    async def availability(self) -> dict[str, Any]:
        if _is_sandbox_runtime():
            return {
                "name": self.name,
                "label": "Managed Experiment sandbox",
                "ready": False,
                "issues": [
                    "Nested experiment launches are disabled inside a managed sandbox"
                ],
                "production": False,
                "context": "sandbox",
                "launch_allowed": False,
                "manager_url": _parent_space_url(),
            }

        issues: list[str] = []
        api_key = _api_key()
        sdk_available = importlib.util.find_spec("agent_api_sdk") is not None
        if not api_key:
            issues.append("OPENAI_API_KEY is not configured")
        if not sdk_available:
            issues.append("agent-api-sdk is not installed")
        if shutil.which("docker") is None:
            issues.append("Docker CLI is not installed")
        else:
            daemon = await _run_process(["docker", "info"], timeout=10)
            if daemon.returncode != 0:
                issues.append("Docker daemon is unavailable")
            else:
                try:
                    image = _docker_image()
                except ExperimentUnavailable as error:
                    issues.append(str(error))
                else:
                    found = await _run_process(["docker", "image", "inspect", image], timeout=10)
                    if found.returncode != 0:
                        issues.append(f"Docker image {image} is not available")
        sdk = None
        if sdk_available:
            try:
                sdk = importlib.import_module("agent_api_sdk")
            except Exception:
                issues.append("agent-api-sdk could not be imported")
        if api_key and sdk is not None:
            kwargs: dict[str, Any] = {"api_key": api_key}
            api_base = os.getenv("AGENT_API_BASE_URL", DEFAULT_API_BASE).rstrip("/")
            if api_base != DEFAULT_API_BASE:
                kwargs["base_url"] = api_base
            try:
                async with asyncio.timeout(15):
                    async with sdk.AgentAPISDK(**kwargs) as client:
                        await client.sessions.list(limit=1, order="desc")
            except asyncio.TimeoutError:
                issues.append("Agents API access check timed out")
            except sdk.AgentAPIError as error:
                issues.append(f"Agents API access denied (HTTP {error.status_code})")
            except Exception:
                issues.append("Agents API access check failed")
        return {
            "name": self.name,
            "label": "Local Docker · Agents API",
            "ready": not issues,
            "issues": issues,
            "production": False,
            "context": "host",
            "launch_allowed": not issues,
            "manager_url": None,
        }

    async def launch(self, record: ExperimentRecord, source_dir: Path) -> None:
        sdk = importlib.import_module("agent_api_sdk")
        api_key = _api_key()
        if not api_key:  # manager preflight covers this; keeps type narrowing explicit.
            raise ExperimentUnavailable("OPENAI_API_KEY is not configured")

        record.model = os.getenv("AGENT_API_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        record.workspace_directory = _sandbox_project_directory(record.project_id)
        record.stage = "creating_session"
        record.touch()

        api_base = os.getenv("AGENT_API_BASE_URL", DEFAULT_API_BASE).rstrip("/")

        async with sdk.AgentAPISDK(**_agent_client_kwargs(api_key)) as client:
            session = await _create_agent_session(client, record)
            record.stage = "cloning_project"
            record.touch()

            staged_sources = await _prepare_experiment_sources(record, source_dir)
            record.sandbox_id = _container_name(record.id)
            host_port = await _start_docker_executor(
                source_root=staged_sources,
                api_key=api_key,
                environment_id=record.environment_id or "",
                api_remote=f"{api_base}/api",
                experiment_id=record.id,
                session_id=record.session_id or "",
                container_name=record.sandbox_id,
                project_id=record.project_id,
                parent_space_url=_parent_space_url(),
            )
            record.stage = "starting_space"
            record.touch()
            await _wait_for_sandbox_space(host_port, record.project_id)
            record.stage = "connecting_agent"
            record.touch()

            await _run_boot_turn(session, record)
            final = await _retrieve_terminal_session(session)
            if final.status != "idle":
                raise ExperimentError(f"Agent session ended with status {final.status}")

        running = await _run_process(
            ["docker", "inspect", "--format", "{{.State.Running}}", record.sandbox_id],
            timeout=10,
        )
        if running.returncode != 0 or running.stdout.strip() != "true":
            raise ExperimentError("Sandbox stopped before the agent became ready")

        await _remove_source_snapshot(record)
        record.space_url = _sandbox_space_url(host_port)
        record.status = "ready"
        record.stage = "ready"
        record.touch()

    async def interact(
        self,
        record: ExperimentRecord,
        prompt: str,
        response: ExperimentMessage,
    ) -> None:
        if not record.session_id or not record.sandbox_id:
            raise ExperimentNotReady("Experiment agent is not connected")
        await _ensure_container_running(record.sandbox_id)

        sdk = importlib.import_module("agent_api_sdk")
        api_key = _api_key()
        if not api_key:
            raise ExperimentUnavailable("OPENAI_API_KEY is not configured")

        async with sdk.AgentAPISDK(**_agent_client_kwargs(api_key)) as client:
            session = await client.sessions.retrieve(record.session_id)
            if session.status != "idle":
                raise ExperimentTurnBusy(f"Agent session is {session.status}")
            saw_delta = False
            try:
                async with asyncio.timeout(float(os.getenv("EXPERIMENT_TURN_TIMEOUT", "600"))):
                    async with aclosing(session.stream(input=prompt)) as events:
                        async for event in events:
                            event_type = getattr(event, "type", "")
                            delta = getattr(event, "output_text_delta", None)
                            text = getattr(event, "output_text", None)
                            if isinstance(delta, str):
                                saw_delta = True
                                _append_message_output(response, delta)
                                record.touch()
                            elif isinstance(text, str) and not saw_delta and not response.text:
                                _append_message_output(response, text)
                                record.touch()
                            if event_type in {
                                "session.environment.failed",
                                "session.failed",
                                "session.turn.cancelled",
                                "session.turn.failed",
                            }:
                                raise ExperimentError(_event_failure(event))
            except asyncio.CancelledError:
                with suppress(Exception):
                    await session.cancel()
                raise

            final = await _retrieve_terminal_session(session)
            if final.status != "idle":
                raise ExperimentError(f"Agent session ended with status {final.status}")
            response.status = "complete"
            response.updated_at = _now()
            record.touch()

    async def stop(self, record: ExperimentRecord) -> list[str]:
        async with record.cleanup_lock:
            warnings: list[str] = []
            record.space_url = None
            if record.sandbox_id:
                warning = await _remove_owned_container(record)
                if warning:
                    warnings.append(warning)

            try:
                await _remove_source_snapshot(record)
            except Exception:
                warnings.append("Staged project cleanup failed")

            if record.session_id:
                try:
                    await _delete_agent_session(record.session_id)
                except Exception:
                    warnings.append("Agents API session cleanup failed")
                else:
                    record.session_id = None
                    record.environment_id = None
            record.touch()
            return warnings


class ExperimentManager:
    def __init__(self, provider: ExperimentProvider | None = None) -> None:
        self.provider = provider or LocalDockerExperimentProvider()
        self._records: dict[str, ExperimentRecord] = {}
        self._lock = asyncio.Lock()

    async def options(self) -> dict[str, Any]:
        return {"provider": await self.provider.availability()}

    async def reconcile(self) -> list[str]:
        reconcile = getattr(self.provider, "reconcile", None)
        if reconcile is None:
            return []
        return await reconcile()

    async def list(self) -> list[dict[str, Any]]:
        async with self._lock:
            records = sorted(self._records.values(), key=lambda row: row.created_at, reverse=True)
            return [record.snapshot() for record in records[:50]]

    async def get(self, experiment_id: str) -> dict[str, Any]:
        async with self._lock:
            record = self._records.get(experiment_id)
            if record is None:
                raise ExperimentNotFound("Experiment not found")
            return record.snapshot()

    async def start_turn(self, experiment_id: str, prompt: str) -> dict[str, Any]:
        text = prompt.strip()
        if not text:
            raise ExperimentError("Message cannot be empty")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ExperimentError(f"Message exceeds {MAX_MESSAGE_CHARS} characters")

        async with self._lock:
            record = self._records.get(experiment_id)
            if record is None:
                raise ExperimentNotFound("Experiment not found")
            if record.status != "ready" or not record.session_id or not record.sandbox_id:
                raise ExperimentNotReady("Experiment is not ready for messages")
            if record.turn_task is not None and not record.turn_task.done():
                raise ExperimentTurnBusy("An agent turn is already running")

            if record.expiry_task is not None:
                record.expiry_task.cancel()
                record.expiry_task = None
            record.expires_at = None
            record.turn_status = "running"
            record.turn_error = None
            record.messages.extend(
                [
                    ExperimentMessage(
                        id=f"msg_{uuid.uuid4().hex[:16]}",
                        role="user",
                        text=_redact(text, _api_key()),
                    ),
                    ExperimentMessage(
                        id=f"msg_{uuid.uuid4().hex[:16]}",
                        role="assistant",
                        text="",
                        status="streaming",
                    ),
                ]
            )
            _prune_messages(record)
            response = record.messages[-1]
            record.turn_task = asyncio.create_task(self._run_turn(record, text, response))
            record.touch()
            return record.snapshot()

    async def start(self, project_id: str, source_dir: Path) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            existing = self._active_record_for(project_id)
            if existing is not None:
                return existing.snapshot(), True

        availability = await self.provider.availability()
        if not availability["ready"]:
            raise ExperimentUnavailable("; ".join(availability["issues"]))

        async with self._lock:
            existing = self._active_record_for(project_id)
            if existing is not None:
                return existing.snapshot(), True
            active_count = sum(1 for row in self._records.values() if self._holds_resources(row))
            if active_count >= _max_active_experiments():
                raise ExperimentCapacityExceeded(
                    f"Experiment capacity reached ({_max_active_experiments()} active)"
                )
            self._prune_terminal_records()
            record = ExperimentRecord(
                id=f"exp_{uuid.uuid4().hex[:16]}",
                project_id=project_id,
                provider=self.provider.name,
            )
            self._records[record.id] = record
            record.task = asyncio.create_task(self._launch(record, source_dir))
            return record.snapshot(), False

    async def stop(self, experiment_id: str) -> dict[str, Any]:
        async with self._lock:
            record = self._records.get(experiment_id)
            if record is None:
                raise ExperimentNotFound("Experiment not found")
            if record.status == "stopped":
                return record.snapshot()
            record.status = "stopping"
            record.stage = "stopping"
            record.expires_at = None
            record.touch()
            task = record.task
            turn_task = record.turn_task
            expiry_task = record.expiry_task
            record.expiry_task = None

        if expiry_task is not None and expiry_task is not asyncio.current_task():
            expiry_task.cancel()
            with suppress(asyncio.CancelledError):
                await expiry_task
        if turn_task is not None and turn_task is not asyncio.current_task() and not turn_task.done():
            turn_task.cancel()
            with suppress(asyncio.CancelledError):
                await turn_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        try:
            warnings = await self.provider.stop(record)
        except Exception:
            warnings = ["Experiment cleanup failed"]

        if warnings:
            record.status = "cleanup_failed"
            record.stage = "cleanup_failed"
            suffix = "; ".join(warnings)
            record.error = f"{record.error}; {suffix}" if record.error else suffix
        else:
            record.status = "stopped"
            record.stage = "stopped"
        record.touch()
        return record.snapshot()

    async def _run_turn(
        self,
        record: ExperimentRecord,
        prompt: str,
        response: ExperimentMessage,
    ) -> None:
        try:
            interact = getattr(self.provider, "interact", None)
            if interact is None:
                raise ExperimentUnavailable("Experiment provider does not support interaction")
            await interact(record, prompt, response)
            record.turn_status = "idle"
            record.turn_error = None
        except asyncio.CancelledError:
            response.status = "failed"
            response.updated_at = _now()
            record.turn_status = "idle"
            record.turn_error = None
            record.touch()
            raise
        except Exception as error:
            response.status = "failed"
            response.updated_at = _now()
            record.turn_status = "failed"
            record.turn_error = _public_error(error)
        finally:
            record.turn_task = None
            if record.status == "ready":
                ttl = _experiment_ttl_seconds()
                record.expires_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=ttl)
                ).isoformat().replace("+00:00", "Z")
                record.expiry_task = asyncio.create_task(self._expire(record, ttl))
            record.touch()

    async def _launch(self, record: ExperimentRecord, source_dir: Path) -> None:
        try:
            await self.provider.launch(record, source_dir)
            if record.status == "ready":
                ttl = _experiment_ttl_seconds()
                record.expires_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=ttl)
                ).isoformat().replace("+00:00", "Z")
                record.expiry_task = asyncio.create_task(self._expire(record, ttl))
                record.touch()
        except asyncio.CancelledError:
            record.status = "stopping"
            record.stage = "stopping"
            record.touch()
            raise
        except Exception as error:
            record.failed_stage = record.stage
            record.status = "stopping"
            record.stage = "cleaning_up"
            record.error = _public_error(error)
            record.touch()
            try:
                warnings = await self.provider.stop(record)
            except Exception:
                warnings = ["Experiment cleanup failed"]
            if warnings:
                suffix = "; ".join(warnings)
                record.error = f"{record.error}; {suffix}" if record.error else suffix
                record.status = "cleanup_failed"
                record.stage = "cleanup_failed"
            else:
                record.status = "failed"
                record.stage = "failed"
            record.touch()
        finally:
            record.task = None

    async def _expire(self, record: ExperimentRecord, ttl: int) -> None:
        try:
            await asyncio.sleep(ttl)
            if record.status == "ready":
                record.error = f"Stopped automatically after {ttl} seconds"
                await self.stop(record.id)
        except asyncio.CancelledError:
            raise
        finally:
            if record.expiry_task is asyncio.current_task():
                record.expiry_task = None

    async def close(self) -> None:
        """Release every resource still owned by this process on shutdown."""
        async with self._lock:
            ids = [row.id for row in self._records.values() if self._holds_resources(row)]
        if ids:
            await asyncio.gather(*(self.stop(experiment_id) for experiment_id in ids))

    def _active_record_for(self, project_id: str) -> ExperimentRecord | None:
        return next(
            (
                row
                for row in self._records.values()
                if row.project_id == project_id and self._holds_resources(row)
            ),
            None,
        )

    @staticmethod
    def _holds_resources(record: ExperimentRecord) -> bool:
        return bool(
            record.status in ACTIVE_STATUSES
            or (record.task is not None and not record.task.done())
            or (record.turn_task is not None and not record.turn_task.done())
            or (record.expiry_task is not None and not record.expiry_task.done())
            or record.sandbox_id
            or record.session_id
            or record.snapshot_dir
        )

    def _prune_terminal_records(self) -> None:
        terminal = sorted(
            (row for row in self._records.values() if not self._holds_resources(row)),
            key=lambda row: row.created_at,
        )
        while len(self._records) >= 50 and terminal:
            self._records.pop(terminal.pop(0).id, None)


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str

    @property
    def output(self) -> str:
        return self.stdout


async def _run_process(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float,
) -> ProcessResult:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as error:
        return ProcessResult(127, type(error).__name__)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.communicate()
        return ProcessResult(124, "command timed out")
    except asyncio.CancelledError:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()
        raise
    return ProcessResult(process.returncode or 0, stdout.decode("utf-8", errors="replace"))


async def _create_agent_session(client: Any, record: ExperimentRecord) -> Any:
    """Capture the remote id even when Stop arrives during session creation."""

    async def create() -> Any:
        async with asyncio.timeout(float(os.getenv("EXPERIMENT_CREATE_TIMEOUT", "45"))):
            return await client.sessions.create(
                agent={
                    "model": record.model,
                    "instructions": (
                        f"Operate only inside {record.workspace_directory}. Read project instructions "
                        "before acting, never inspect process credentials or /xo-sources, keep the "
                        "project unchanged during the boot check, and answer concisely."
                    ),
                },
                environment={
                    "type": "self_hosted",
                    "workspace_directory": record.workspace_directory,
                },
            )

    task = asyncio.create_task(create())
    try:
        session = await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            session = await task
        except Exception:
            pass
        else:
            _remember_agent_session(record, session)
        raise
    _remember_agent_session(record, session)
    return session


def _remember_agent_session(record: ExperimentRecord, session: Any) -> None:
    record.session_id = str(session.id)
    environment_id = getattr(session.info.environment, "environment_id", None)
    if not environment_id:
        raise ExperimentError("Agents API did not return an environment id")
    record.environment_id = str(environment_id)
    record.touch()


async def _prepare_source_snapshot(record: ExperimentRecord, source_dir: Path) -> Path:
    """Compatibility helper that stages one filtered project copy."""
    snapshot_root = Path(tempfile.mkdtemp(prefix=f"xo-experiment-{record.id[-8:]}-"))
    snapshot_root.chmod(0o755)
    record.snapshot_dir = snapshot_root
    destination = snapshot_root / "project"
    await _stage_sanitized_source(source_dir, destination)
    return destination


async def _prepare_experiment_sources(record: ExperimentRecord, source_dir: Path) -> Path:
    """Stage the selected project and a filtered xo-cowork-api checkout."""
    snapshot_root = Path(tempfile.mkdtemp(prefix=f"xo-experiment-{record.id[-8:]}-"))
    snapshot_root.chmod(0o755)
    record.snapshot_dir = snapshot_root
    try:
        await _stage_sanitized_source(source_dir, snapshot_root / "project")
        record.stage = "cloning_cowork_api"
        record.touch()
        await _stage_sanitized_source(_cowork_api_root(), snapshot_root / "xo-cowork-api")
    except BaseException:
        with suppress(Exception):
            await asyncio.to_thread(shutil.rmtree, snapshot_root)
        record.snapshot_dir = None
        raise
    return snapshot_root


async def _stage_sanitized_source(source_dir: Path, destination: Path) -> None:
    """Make a Git-capable copy without remotes, credentials, links, or secrets."""

    # A local clone preserves normal Git behavior without exposing untracked
    # dotenv files. Overlaying the sanitized working tree also includes the
    # user's current modified/untracked work. Deleted tracked paths are pruned.
    cloned = False
    if (source_dir / ".git").exists() and shutil.which("git"):
        result = await _run_process(
            [
                "git",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--",
                str(source_dir),
                str(destination),
            ],
            timeout=float(os.getenv("EXPERIMENT_SOURCE_TIMEOUT", "120")),
        )
        cloned = result.returncode == 0
        if not cloned:
            await asyncio.to_thread(shutil.rmtree, destination, True)
        else:
            remotes = await _run_process(["git", "-C", str(destination), "remote"], timeout=10)
            if remotes.returncode == 0:
                for remote in remotes.stdout.splitlines():
                    if re.fullmatch(r"[A-Za-z0-9._-]+", remote):
                        await _run_process(
                            ["git", "-C", str(destination), "remote", "remove", remote],
                            timeout=10,
                        )
            await _run_process(
                ["git", "-C", str(destination), "config", "--local", "--unset-all", "credential.helper"],
                timeout=10,
            )

    await _copy_tree_cancellation_safe(source_dir, destination, keep_git=cloned)


async def _copy_tree_cancellation_safe(source: Path, destination: Path, *, keep_git: bool) -> None:
    task = asyncio.create_task(
        asyncio.to_thread(_copy_sanitized_tree, source, destination, keep_git=keep_git)
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await task
        raise


def _copy_sanitized_tree(source: Path, destination: Path, *, keep_git: bool) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if keep_git:
        _prune_deleted_worktree_paths(source, destination)
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        symlinks=True,
        ignore=_snapshot_ignore,
    )
    _scrub_snapshot(destination, keep_git=keep_git)
    _normalize_snapshot_permissions(destination)


def _prune_deleted_worktree_paths(source: Path, destination: Path) -> None:
    for root, dirs, files in os.walk(destination, topdown=False, followlinks=False):
        root_path = Path(root)
        if ".git" in root_path.relative_to(destination).parts:
            continue
        for name in files:
            relative = (root_path / name).relative_to(destination)
            if not (source / relative).exists() and not (source / relative).is_symlink():
                (root_path / name).unlink(missing_ok=True)
        for name in dirs:
            path = root_path / name
            relative = path.relative_to(destination)
            if name == ".git" or _sensitive_snapshot_name(name):
                continue
            if not (source / relative).exists() and not (source / relative).is_symlink():
                shutil.rmtree(path, ignore_errors=True)


def _snapshot_ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        path = Path(directory) / name
        if _sensitive_snapshot_name(name):
            ignored.add(name)
            continue
        try:
            mode = path.lstat().st_mode
        except OSError:
            ignored.add(name)
            continue
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            ignored.add(name)
    return ignored


def _sensitive_snapshot_name(name: str) -> bool:
    lowered = name.lower()
    return bool(
        lowered in {
            ".git",
            ".ssh",
            ".aws",
            ".docker",
            ".gnupg",
            ".kube",
            ".npmrc",
            ".pypirc",
            ".netrc",
            ".envrc",
            "node_modules",
            "venv",
            ".venv",
            "__pycache__",
            "dist",
            "build",
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
        or lowered == ".env"
        or lowered.startswith(".env.")
        or lowered.endswith((".pem", ".p12", ".pfx", ".key"))
    )


def _scrub_snapshot(destination: Path, *, keep_git: bool) -> None:
    for root, dirs, files in os.walk(destination, topdown=True, followlinks=False):
        root_path = Path(root)
        kept_dirs: list[str] = []
        for name in dirs:
            path = root_path / name
            if keep_git and root_path == destination and name == ".git" and not path.is_symlink():
                continue  # preserve Git metadata, but never scrub inside it
            elif path.is_symlink() or _sensitive_snapshot_name(name):
                if path.is_symlink():
                    path.unlink(missing_ok=True)
                else:
                    shutil.rmtree(path, ignore_errors=True)
            else:
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            path = root_path / name
            try:
                mode = path.lstat().st_mode
            except OSError:
                continue
            if _sensitive_snapshot_name(name) or not stat.S_ISREG(mode):
                path.unlink(missing_ok=True)


def _normalize_snapshot_permissions(destination: Path) -> None:
    """Make the filtered bind source readable by the unprivileged container user."""
    for root, dirs, files in os.walk(destination, followlinks=False):
        root_path = Path(root)
        root_path.chmod(0o755)
        for name in dirs:
            path = root_path / name
            if not path.is_symlink():
                with suppress(OSError):
                    path.chmod(0o755)
        for name in files:
            path = root_path / name
            if path.is_symlink():
                continue
            with suppress(OSError):
                mode = path.stat().st_mode
                path.chmod(0o755 if mode & stat.S_IXUSR else 0o644)


async def _remove_source_snapshot(record: ExperimentRecord) -> None:
    snapshot = record.snapshot_dir
    if snapshot is None:
        return
    try:
        await asyncio.to_thread(shutil.rmtree, snapshot)
    except FileNotFoundError:
        pass
    record.snapshot_dir = None
    record.touch()


def _container_name(experiment_id: str) -> str:
    return f"xo-experiment-{experiment_id.removeprefix('exp_')[:12]}"


async def _remove_owned_container(record: ExperimentRecord) -> str | None:
    container_name = record.sandbox_id
    if not container_name:
        return None
    ownership = await _run_process(
        [
            "docker",
            "inspect",
            "--format",
            "{{ index .Config.Labels \"xo.experiment.id\" }}",
            container_name,
        ],
        timeout=10,
    )
    if ownership.returncode != 0:
        if "No such" in ownership.output or "not found" in ownership.output.lower():
            record.sandbox_id = None
            return None
        return "Docker sandbox ownership check failed"
    if ownership.stdout.strip() != record.id:
        return "Docker sandbox ownership check failed"
    removed = await _run_process(["docker", "rm", "-f", container_name], timeout=20)
    if removed.returncode != 0 and "No such" not in removed.output:
        return "Docker sandbox cleanup failed"
    record.sandbox_id = None
    record.touch()
    return None


async def _start_docker_executor(
    *,
    source_root: Path,
    api_key: str,
    environment_id: str,
    api_remote: str,
    experiment_id: str,
    session_id: str,
    container_name: str,
    project_id: str,
    parent_space_url: str,
) -> int:
    command = [
        "docker",
        "run",
        "--detach",
        "--name",
        container_name,
        "--label",
        f"xo.experiment.id={experiment_id}",
        "--label",
        "xo.experiment.managed=true",
        "--label",
        f"xo.experiment.session_id={session_id}",
        "--init",
        "--security-opt",
        "no-new-privileges",
        "--cap-drop",
        "ALL",
        "--read-only",
        "--user",
        "10001:10001",
        "--pids-limit",
        os.getenv("EXPERIMENT_PIDS_LIMIT", "512"),
        "--memory",
        os.getenv("EXPERIMENT_MEMORY_LIMIT", "4g"),
        "--cpus",
        os.getenv("EXPERIMENT_CPU_LIMIT", "2"),
        "--tmpfs",
        f"/workspace:rw,nosuid,nodev,uid=10001,gid=10001,mode=1770,size={_tmpfs_size('EXPERIMENT_WORKSPACE_SIZE', '2g')}",
        "--tmpfs",
        f"/codex-home:rw,nosuid,nodev,uid=10001,gid=10001,mode=0700,size={_tmpfs_size('EXPERIMENT_CODEX_HOME_SIZE', '512m')}",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,uid=10001,gid=10001,mode=1770,size={_tmpfs_size('EXPERIMENT_TMP_SIZE', '512m')}",
        "--publish",
        f"127.0.0.1::{SPACE_PORT}",
        "--volume",
        f"{source_root}:/xo-sources:ro",
        "-e",
        "CODEX_API_KEY",
        "-e",
        "AGENT_ENVIRONMENT_ID",
        "-e",
        "AGENTS_API_REMOTE",
        "-e",
        "XO_PROJECT_ID",
        "-e",
        "XO_PROJECTS_ROOT",
        "-e",
        "XO_COWORK_API_ROOT",
        "-e",
        "AI_WORKSPACE_ROOT",
        "-e",
        "SPACE_PORT",
        "-e",
        "STAGE",
        "-e",
        "RELAY_ENABLED",
        "-e",
        "STARTUP_WARMUP_ENABLED",
        "-e",
        "EXPERIMENT_RUNTIME_ROLE",
        "-e",
        "EXPERIMENT_PARENT_SPACE_URL",
        _docker_image(),
        "sh",
        "-lc",
        BOOTSTRAP_SCRIPT,
    ]
    env = os.environ.copy()
    env["CODEX_API_KEY"] = api_key
    env["AGENT_ENVIRONMENT_ID"] = environment_id
    env["AGENTS_API_REMOTE"] = api_remote
    env["XO_PROJECT_ID"] = project_id
    env["XO_PROJECTS_ROOT"] = SANDBOX_PROJECTS_ROOT
    env["XO_COWORK_API_ROOT"] = SANDBOX_API_ROOT
    env["AI_WORKSPACE_ROOT"] = _sandbox_project_directory(project_id)
    env["SPACE_PORT"] = str(SPACE_PORT)
    env["STAGE"] = "local"
    env["RELAY_ENABLED"] = "false"
    env["STARTUP_WARMUP_ENABLED"] = "false"
    env["EXPERIMENT_RUNTIME_ROLE"] = SANDBOX_RUNTIME_ROLE
    env["EXPERIMENT_PARENT_SPACE_URL"] = parent_space_url
    result = await _run_process(command, env=env, timeout=60)
    if result.returncode != 0:
        detail = _redact(result.output[-500:], api_key).strip()
        raise ExperimentError(f"Docker sandbox failed to start{': ' + detail if detail else ''}")

    port_result = await _run_process(
        ["docker", "port", container_name, f"{SPACE_PORT}/tcp"],
        timeout=10,
    )
    match = re.search(r"127\.0\.0\.1:(\d+)\s*$", port_result.stdout)
    if port_result.returncode != 0 or match is None:
        raise ExperimentError("Sandbox Space port could not be discovered")
    port = int(match.group(1))
    if not 1 <= port <= 65_535:
        raise ExperimentError("Sandbox Space returned an invalid port")
    return port


async def _wait_for_sandbox_space(host_port: int, project_id: str) -> None:
    base_url = f"http://127.0.0.1:{host_port}"
    deadline = asyncio.get_running_loop().time() + float(
        os.getenv("EXPERIMENT_SPACE_TIMEOUT", "90")
    )
    last_error = "Space did not respond"
    async with httpx.AsyncClient(timeout=3, follow_redirects=True) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                health = await client.get(f"{base_url}/health")
                space = await client.get(f"{base_url}/space/")
                projects = await client.get(f"{base_url}/api/xo-projects")
                payload = projects.json() if projects.is_success else {}
                items = payload.get("items", []) if isinstance(payload, dict) else []
                ids = [item.get("id") for item in items if isinstance(item, dict)]
                if health.is_success and space.is_success and ids == [project_id]:
                    return
                last_error = "Sandbox Space project inventory was not ready"
            except (httpx.HTTPError, json.JSONDecodeError, ValueError) as error:
                last_error = type(error).__name__
            await asyncio.sleep(0.5)
    raise ExperimentError(f"Sandbox Space failed its readiness check ({last_error})")


async def _ensure_container_running(container_name: str) -> None:
    running = await _run_process(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
        timeout=10,
    )
    if running.returncode != 0 or running.stdout.strip() != "true":
        raise ExperimentNotReady("Sandbox is no longer running")


async def _run_boot_turn(session: Any, record: ExperimentRecord) -> None:
    prompt = (
        "Perform a read-only boot check for this XO project. Read AGENTS.md if present, "
        "list the workspace root, do not modify any file, and reply with a one-sentence "
        f"READY summary for project {record.project_id}."
    )
    record.stage = "booting_agent"
    record.touch()
    async with asyncio.timeout(float(os.getenv("EXPERIMENT_BOOT_TIMEOUT", "300"))):
        async with aclosing(session.stream(input=prompt)) as events:
            async for event in events:
                event_type = getattr(event, "type", "")
                if event_type == "session.environment.connected":
                    record.stage = "booting_agent"
                    record.touch()
                delta = getattr(event, "output_text_delta", None)
                text = getattr(event, "output_text", None)
                if isinstance(delta, str):
                    _append_output(record, delta)
                elif isinstance(text, str) and not record.output:
                    _append_output(record, text)
                if event_type in {
                    "session.environment.failed",
                    "session.failed",
                    "session.turn.cancelled",
                    "session.turn.failed",
                }:
                    raise ExperimentError(_event_failure(event))


async def _retrieve_terminal_session(session: Any) -> Any:
    deadline = asyncio.get_running_loop().time() + 10
    async with asyncio.timeout(12):
        snapshot = await session.retrieve()
    while snapshot.status == "in_progress" and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.25)
        async with asyncio.timeout(12):
            snapshot = await session.retrieve()
    return snapshot


async def _delete_agent_session(session_id: str) -> None:
    sdk = importlib.import_module("agent_api_sdk")
    api_key = _api_key()
    if not api_key:
        raise ExperimentUnavailable("OPENAI_API_KEY is not configured")
    kwargs: dict[str, Any] = {"api_key": api_key}
    api_base = os.getenv("AGENT_API_BASE_URL", DEFAULT_API_BASE).rstrip("/")
    if api_base != DEFAULT_API_BASE:
        kwargs["base_url"] = api_base
    async with sdk.AgentAPISDK(**kwargs) as client:
        try:
            async with asyncio.timeout(float(os.getenv("EXPERIMENT_DELETE_TIMEOUT", "20"))):
                await client.sessions.delete(session_id)
        except sdk.AgentAPIError as error:
            if error.status_code != 404:
                raise


def _append_output(record: ExperimentRecord, text: str) -> None:
    record.output = _redact((record.output + text)[-MAX_OUTPUT_CHARS:], _api_key())
    record.touch()


def _append_message_output(message: ExperimentMessage, text: str) -> None:
    message.text = _redact((message.text + text)[-MAX_MESSAGE_CHARS:], _api_key())
    message.updated_at = _now()


def _prune_messages(record: ExperimentRecord) -> None:
    if len(record.messages) > MAX_TRANSCRIPT_MESSAGES:
        del record.messages[: len(record.messages) - MAX_TRANSCRIPT_MESSAGES]


def _event_failure(event: Any) -> str:
    event_type = getattr(event, "type", "Agent API failure")
    environment = getattr(event, "environment", None)
    error = getattr(environment, "error", None)
    message = getattr(error, "message", None)
    code = getattr(error, "code", None)
    if message:
        return f"{event_type} ({code}): {message}" if code else f"{event_type}: {message}"
    session_error = getattr(event, "error", None)
    return f"{event_type}: {session_error}" if session_error else event_type


def _public_error(error: Exception) -> str:
    if isinstance(error, asyncio.TimeoutError):
        return "Experiment timed out while waiting for the agent"
    message = str(error).strip() or type(error).__name__
    return _redact(message, _api_key())[:1_000]


def _redact(value: str, secret: str | None) -> str:
    redacted = value.replace(secret, "<redacted>") if secret else value
    redacted = re.sub(
        r"(?i)((?:OPENAI|CODEX)_API_KEY\s*[:=]\s*)\S+",
        r"\1<redacted>",
        redacted,
    )
    redacted = re.sub(r"(?i)(Authorization:\s*Bearer\s+)\S+", r"\1<redacted>", redacted)
    return re.sub(r"\b(?:sk|sess)-[A-Za-z0-9._-]{4,}", "<redacted>", redacted)


def _agent_client_kwargs(api_key: str) -> dict[str, str]:
    kwargs = {"api_key": api_key}
    api_base = os.getenv("AGENT_API_BASE_URL", DEFAULT_API_BASE).rstrip("/")
    if api_base != DEFAULT_API_BASE:
        kwargs["base_url"] = api_base
    return kwargs


def _cowork_api_root() -> Path:
    root = Path(__file__).resolve().parents[3]
    if not (root / "server.py").is_file() or not (root / "space_ui").is_dir():
        raise ExperimentUnavailable("xo-cowork-api source checkout is unavailable")
    return root


def _sandbox_project_directory(project_id: str) -> str:
    return f"{SANDBOX_PROJECTS_ROOT}/{project_id}"


def _sandbox_space_url(host_port: int) -> str:
    template = os.getenv(
        "EXPERIMENT_SPACE_URL_TEMPLATE",
        "http://127.0.0.1:{port}/space/#/projects",
    ).strip()
    if "{port}" not in template:
        raise ExperimentUnavailable("EXPERIMENT_SPACE_URL_TEMPLATE must contain {port}")
    try:
        url = template.format(port=host_port)
        parsed = urlsplit(url)
        parsed_port = parsed.port
    except (KeyError, ValueError) as error:
        raise ExperimentUnavailable("EXPERIMENT_SPACE_URL_TEMPLATE is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed_port is None
    ):
        raise ExperimentUnavailable("EXPERIMENT_SPACE_URL_TEMPLATE is invalid")
    return url


def _tmpfs_size(env_name: str, default: str) -> str:
    value = os.getenv(env_name, default).strip().lower() or default
    if not re.fullmatch(r"[1-9][0-9]*[kmgt]?", value):
        raise ExperimentUnavailable(f"{env_name} is invalid")
    return value


def _api_key() -> str | None:
    value = os.getenv("OPENAI_API_KEY", "").strip()
    return value or None


def _is_sandbox_runtime() -> bool:
    role = os.getenv("EXPERIMENT_RUNTIME_ROLE", "").strip().lower()
    if role:
        return role == SANDBOX_RUNTIME_ROLE
    # Backward-compatible detection for sandboxes launched before the explicit
    # role marker existed. Both values are set only on the managed child.
    return (
        os.getenv("XO_COWORK_API_ROOT", "").strip() == SANDBOX_API_ROOT
        and bool(os.getenv("AGENTS_API_REMOTE", "").strip())
    )


def _parent_space_url() -> str:
    value = os.getenv("EXPERIMENT_PARENT_SPACE_URL", "").strip()
    if not value:
        port_text = os.getenv("PORT", "5002").strip() or "5002"
        try:
            port = int(port_text)
        except ValueError as error:
            raise ExperimentUnavailable("PORT is invalid") from error
        if not 1 <= port <= 65_535:
            raise ExperimentUnavailable("PORT is invalid")
        value = f"http://127.0.0.1:{port}/space/#/experiment"
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError as error:
        raise ExperimentUnavailable("EXPERIMENT_PARENT_SPACE_URL is invalid") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (parsed_port is not None and not 1 <= parsed_port <= 65_535)
    ):
        raise ExperimentUnavailable("EXPERIMENT_PARENT_SPACE_URL is invalid")
    return value


def _docker_image() -> str:
    image = os.getenv("EXPERIMENT_DOCKER_IMAGE", DEFAULT_IMAGE).strip() or DEFAULT_IMAGE
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]*", image):
        raise ExperimentUnavailable("EXPERIMENT_DOCKER_IMAGE is invalid")
    return image


def _max_active_experiments() -> int:
    try:
        return max(1, min(10, int(os.getenv("EXPERIMENT_MAX_ACTIVE", DEFAULT_MAX_ACTIVE))))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ACTIVE


def _experiment_ttl_seconds() -> int:
    try:
        return max(60, min(86_400, int(os.getenv("EXPERIMENT_TTL_SECONDS", "3600"))))
    except (TypeError, ValueError):
        return 3600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


experiment_manager = ExperimentManager()
