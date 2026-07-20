from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from routers.cowork_agent.bff.experiments import _resolve_project_source
from services.cowork_agent.experiments import runtime


class FakeProvider:
    name = "fake_sandbox"

    def __init__(self, *, ready: bool = True, block: bool = False) -> None:
        self.ready = ready
        self.block = block
        self.launch_calls = 0
        self.stop_calls = 0
        self.started = asyncio.Event()
        self.turn_started = asyncio.Event()
        self.block_turn = False

    async def availability(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ready": self.ready,
            "issues": [] if self.ready else ["not ready"],
        }

    async def launch(self, record: runtime.ExperimentRecord, _source: Path) -> None:
        self.launch_calls += 1
        record.session_id = "sess_fake"
        record.sandbox_id = "sandbox_fake"
        self.started.set()
        if self.block:
            await asyncio.Future()
        record.status = "ready"
        record.stage = "ready"
        record.touch()

    async def stop(self, record: runtime.ExperimentRecord) -> list[str]:
        self.stop_calls += 1
        record.session_id = None
        record.environment_id = None
        record.sandbox_id = None
        record.snapshot_dir = None
        return []

    async def interact(
        self,
        record: runtime.ExperimentRecord,
        _prompt: str,
        response: runtime.ExperimentMessage,
    ) -> None:
        self.turn_started.set()
        if self.block_turn:
            await asyncio.Future()
        runtime._append_message_output(response, "sandbox response")
        response.status = "complete"
        record.touch()


class FailingCleanupProvider(FakeProvider):
    async def stop(self, record: runtime.ExperimentRecord) -> list[str]:
        self.stop_calls += 1
        return ["sandbox cleanup failed"]


class FailingLaunchProvider(FakeProvider):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    async def launch(self, record: runtime.ExperimentRecord, _source: Path) -> None:
        self.launch_calls += 1
        record.session_id = "sess_fake"
        record.stage = "creating_session"
        raise runtime.ExperimentError(self.message)


class ExperimentManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_provider_is_rejected_before_scheduling(self) -> None:
        manager = runtime.ExperimentManager(FakeProvider(ready=False))

        with self.assertRaises(runtime.ExperimentUnavailable):
            await manager.start("demo", Path("/tmp/demo"))

        self.assertEqual(manager._records, {})

    async def test_provider_name_and_duplicate_start_are_stable(self) -> None:
        provider = FakeProvider()
        manager = runtime.ExperimentManager(provider)
        first, reused = await manager.start("demo", Path("/tmp/demo"))
        record = manager._records[first["id"]]
        assert record.task is not None
        await record.task

        second, second_reused = await manager.start("demo", Path("/tmp/demo"))

        self.assertFalse(reused)
        self.assertTrue(second_reused)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["provider"], provider.name)
        self.assertIsNotNone(second["expires_at"])
        self.assertEqual(provider.launch_calls, 1)
        stopped = await manager.stop(second["id"])
        self.assertIsNone(stopped["expires_at"])

    async def test_concurrent_starts_create_one_launch(self) -> None:
        provider = FakeProvider()
        manager = runtime.ExperimentManager(provider)

        left, right = await asyncio.gather(
            manager.start("demo", Path("/tmp/demo")),
            manager.start("demo", Path("/tmp/demo")),
        )
        record = next(iter(manager._records.values()))
        task = record.task
        if task is not None:
            await task

        self.assertEqual(left[0]["id"], right[0]["id"])
        self.assertEqual(sorted([left[1], right[1]]), [False, True])
        self.assertEqual(provider.launch_calls, 1)
        await manager.close()

    async def test_stop_during_launch_cleans_once(self) -> None:
        provider = FakeProvider(block=True)
        manager = runtime.ExperimentManager(provider)
        snapshot, _ = await manager.start("demo", Path("/tmp/demo"))
        await provider.started.wait()

        stopped = await manager.stop(snapshot["id"])

        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(stopped["can_stop"])
        self.assertEqual(provider.stop_calls, 1)
        self.assertIsNone(stopped["sandbox_id"])
        self.assertIsNone(stopped["agent_session_id"])

    async def test_cleanup_failure_retains_retryable_state(self) -> None:
        provider = FailingCleanupProvider(block=True)
        manager = runtime.ExperimentManager(provider)
        snapshot, _ = await manager.start("demo", Path("/tmp/demo"))
        await provider.started.wait()

        stopped = await manager.stop(snapshot["id"])

        self.assertEqual(stopped["status"], "cleanup_failed")
        self.assertTrue(stopped["can_stop"])
        self.assertEqual(stopped["sandbox_id"], "sandbox_fake")
        self.assertEqual(stopped["agent_session_id"], "sess_fake")

    async def test_launch_failure_redacts_secrets_and_cleans(self) -> None:
        secret = "sk-test-super-secret"
        provider = FailingLaunchProvider(
            f"OPENAI_API_KEY={secret} Authorization: Bearer another-secret"
        )
        manager = runtime.ExperimentManager(provider)
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            snapshot, _ = await manager.start("demo", Path("/tmp/demo"))
            record = manager._records[snapshot["id"]]
            assert record.task is not None
            await record.task

        final = await manager.get(snapshot["id"])
        self.assertEqual(final["status"], "failed")
        self.assertNotIn(secret, final["error"] or "")
        self.assertNotIn("another-secret", final["error"] or "")
        self.assertEqual(final["failed_stage"], "creating_session")
        self.assertEqual(provider.stop_calls, 1)

    async def test_follow_up_turn_updates_transcript_and_renews_expiry(self) -> None:
        provider = FakeProvider()
        manager = runtime.ExperimentManager(provider)
        snapshot, _ = await manager.start("demo", Path("/tmp/demo"))
        record = manager._records[snapshot["id"]]
        assert record.task is not None
        await record.task

        started = await manager.start_turn(snapshot["id"], "inspect the project")
        self.assertEqual(started["turn_status"], "running")
        self.assertEqual(started["messages"][0]["role"], "user")
        assert record.turn_task is not None
        await record.turn_task

        final = await manager.get(snapshot["id"])
        self.assertEqual(final["turn_status"], "idle")
        self.assertEqual(final["messages"][-1]["text"], "sandbox response")
        self.assertTrue(final["can_message"])
        self.assertIsNotNone(final["expires_at"])
        await manager.close()

    async def test_concurrent_turn_is_rejected_and_stop_cancels_it(self) -> None:
        provider = FakeProvider()
        provider.block_turn = True
        manager = runtime.ExperimentManager(provider)
        snapshot, _ = await manager.start("demo", Path("/tmp/demo"))
        record = manager._records[snapshot["id"]]
        assert record.task is not None
        await record.task
        await manager.start_turn(snapshot["id"], "long task")
        await provider.turn_started.wait()

        with self.assertRaises(runtime.ExperimentTurnBusy):
            await manager.start_turn(snapshot["id"], "duplicate")

        stopped = await manager.stop(snapshot["id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertIsNone(record.turn_task)


class ExperimentAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_sandbox_reports_context_instead_of_host_prerequisites(self) -> None:
        provider = runtime.SelfHostedVPSExperimentProvider()
        process = AsyncMock(side_effect=AssertionError("sandbox preflight ran a host command"))
        with patch.dict(
            os.environ,
            {
                "EXPERIMENT_RUNTIME_ROLE": "sandbox",
                "EXPERIMENT_PARENT_SPACE_URL": "http://127.0.0.1:5002/space/#/chat",
            },
        ), patch.object(runtime, "_run_process", process):
            result = await provider.availability()

        self.assertFalse(result["ready"])
        self.assertFalse(result["launch_allowed"])
        self.assertEqual(result["context"], "sandbox")
        self.assertEqual(
            result["manager_url"],
            "http://127.0.0.1:5002/space/#/chat",
        )
        self.assertNotIn("OPENAI_API_KEY is not configured", result["issues"])
        self.assertNotIn("agent-api-sdk is not installed", result["issues"])
        self.assertNotIn("Docker CLI is not installed", result["issues"])
        process.assert_not_awaited()

    async def test_legacy_sandbox_environment_is_detected(self) -> None:
        provider = runtime.SelfHostedVPSExperimentProvider()
        with patch.dict(
            os.environ,
            {
                "EXPERIMENT_RUNTIME_ROLE": "",
                "XO_COWORK_API_ROOT": runtime.SANDBOX_API_ROOT,
                "AGENTS_API_REMOTE": "https://api.openai.com/v1/agents/api",
            },
        ):
            result = await provider.availability()

        self.assertEqual(result["context"], "sandbox")

    def test_parent_space_url_is_validated(self) -> None:
        with patch.dict(os.environ, {"PORT": "5111"}):
            os.environ.pop("EXPERIMENT_PARENT_SPACE_URL", None)
            self.assertEqual(
                runtime._parent_space_url(),
                "http://127.0.0.1:5111/space/#/chat",
            )
        for value in ["javascript:alert(1)", "http://user:pass@host/space/"]:
            with self.subTest(value=value), patch.dict(
                os.environ, {"EXPERIMENT_PARENT_SPACE_URL": value}
            ):
                with self.assertRaises(runtime.ExperimentUnavailable):
                    runtime._parent_space_url()


class ExperimentSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_is_current_recursive_and_secret_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as outside:
            root = Path(root_text)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('current')\n")
            (root / ".env").write_text("OPENAI_API_KEY=secret\n")
            (root / "src" / ".env.local").write_text("TOKEN=secret\n")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "dep.js").write_text("large")
            (root / ".next" / "cache").mkdir(parents=True)
            (root / ".next" / "cache" / "bundle.js").write_text("generated")
            (root / ".next-old-11611").mkdir()
            (root / ".next-old-11611" / "bundle.js").write_text("generated")
            (root / "secrets.toml").write_text("secret=true")
            external = Path(outside) / "outside.txt"
            external.write_text("must not be copied")
            (root / "outside-link").symlink_to(external)
            record = runtime.ExperimentRecord(id="exp_snapshot", project_id="demo")

            staged = await runtime._prepare_source_snapshot(record, root)

            self.assertEqual((staged / "src" / "app.py").read_text(), "print('current')\n")
            self.assertFalse((staged / ".env").exists())
            self.assertFalse((staged / "src" / ".env.local").exists())
            self.assertFalse((staged / "node_modules").exists())
            self.assertFalse((staged / ".next").exists())
            self.assertFalse((staged / ".next-old-11611").exists())
            self.assertFalse((staged / "secrets.toml").exists())
            self.assertFalse((staged / "outside-link").exists())
            await runtime._remove_source_snapshot(record)
            self.assertFalse(staged.parent.exists())

    async def test_snapshot_rejects_special_files_and_stages_both_sources(self) -> None:
        with tempfile.TemporaryDirectory() as project_text, tempfile.TemporaryDirectory() as api_text:
            project = Path(project_text)
            api = Path(api_text)
            (project / "AGENTS.md").write_text("project")
            (project / ".envrc").write_text("secret")
            os.mkfifo(project / "unsafe-fifo")
            (api / "server.py").write_text("app = object()")
            (api / "secrets.py").write_text("SAFE_SOURCE = True")
            (api / "space_ui").mkdir()
            (api / "space_ui" / "index.html").write_text("space")
            (api / ".env").write_text("OPENAI_API_KEY=secret")
            record = runtime.ExperimentRecord(id="exp_bundle", project_id="demo")

            with patch.object(runtime, "_cowork_api_root", return_value=api):
                staged = await runtime._prepare_experiment_sources(record, project)

            self.assertEqual((staged / "project" / "AGENTS.md").read_text(), "project")
            self.assertEqual((staged / "xo-cowork-api" / "server.py").read_text(), "app = object()")
            self.assertTrue((staged / "xo-cowork-api" / "secrets.py").is_file())
            self.assertFalse((staged / "project" / ".envrc").exists())
            self.assertFalse((staged / "project" / "unsafe-fifo").exists())
            self.assertFalse((staged / "xo-cowork-api" / ".env").exists())
            await runtime._remove_source_snapshot(record)

    async def test_git_snapshot_keeps_only_the_current_revision(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Experiment Test"],
                check=True,
            )
            for revision in range(3):
                (root / "revision.txt").write_text(str(revision))
                subprocess.run(["git", "-C", str(root), "add", "revision.txt"], check=True)
                subprocess.run(
                    ["git", "-C", str(root), "commit", "--quiet", "-m", str(revision)],
                    check=True,
                )
            record = runtime.ExperimentRecord(id="exp_git", project_id="demo")

            staged = await runtime._prepare_source_snapshot(record, root)

            count = subprocess.run(
                ["git", "-C", str(staged), "rev-list", "--count", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(count.stdout.strip(), "1")
            self.assertEqual((staged / "revision.txt").read_text(), "2")
            await runtime._remove_source_snapshot(record)

    async def test_source_bundle_reserves_workspace_headroom(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            payload = root / "payload.bin"
            with payload.open("wb") as stream:
                stream.truncate(900)

            with patch.dict(os.environ, {"EXPERIMENT_WORKSPACE_SIZE": "1k"}):
                with self.assertRaisesRegex(runtime.ExperimentError, "source bundle is too large"):
                    await runtime._validate_source_bundle(root)


class ExperimentSelfHostedVPSTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_session_uses_selected_sandbox_project_directory(self) -> None:
        class Environment:
            environment_id = "env_selected"

        class Info:
            environment = Environment()

        class Session:
            id = "sess_selected"
            info = Info()

        client = type("Client", (), {})()
        client.sessions = type("Sessions", (), {})()
        client.sessions.create = AsyncMock(return_value=Session())
        record = runtime.ExperimentRecord(id="exp_selected", project_id="demo root")
        record.workspace_directory = runtime._sandbox_project_directory(record.project_id)

        await runtime._create_agent_session(client, record)

        agent = client.sessions.create.await_args.kwargs["agent"]
        environment = client.sessions.create.await_args.kwargs["environment"]
        self.assertTrue(agent["code_mode"])
        self.assertEqual(agent["tools"][0]["name"], runtime.VPS_EXEC_TOOL_NAME)
        self.assertIn(runtime.VPS_EXEC_TOOL_NAME, agent["instructions"])
        self.assertEqual(environment["type"], "self_hosted")
        self.assertEqual(
            environment["workspace_directory"],
            "/workspace/xo-projects/demo root",
        )

    async def test_process_cancellation_kills_and_reaps_child(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.returncode = None
                self.killed = False
                self.communicate_calls = 0

            async def communicate(self):
                self.communicate_calls += 1
                if self.communicate_calls == 1:
                    await asyncio.Future()
                return b"", None

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

        process = FakeProcess()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(runtime._run_process(["safe-command"], timeout=60))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(process.killed)
        self.assertEqual(process.communicate_calls, 2)

    async def test_unrestricted_docker_command_uses_root_and_writable_rootfs(self) -> None:
        secret = "sk-command-secret"
        process = AsyncMock(
            side_effect=[
                runtime.ProcessResult(0, "container-id\n"),
                runtime.ProcessResult(0, "127.0.0.1:49173\n"),
                runtime.ProcessResult(0, "127.0.0.1:49174\n"),
                runtime.ProcessResult(0, "127.0.0.1:49175\n"),
            ]
        )
        staged = Path("/tmp/staged project")
        with patch.object(runtime, "_run_process", process):
            ports = await runtime._start_docker_vps(
                source_root=staged,
                api_key=secret,
                environment_id="env_123",
                api_remote="https://api.openai.com/v1/agents/api",
                experiment_id="exp_123",
                session_id="sess_123",
                container_name="xo-experiment-123",
                project_id="demo",
                parent_space_url="http://127.0.0.1:5002/space/#/chat",
                vps_token="vps-secret",
            )

        command = process.await_args_list[0].args[0]
        env = process.await_args_list[0].kwargs["env"]
        self.assertEqual(ports, (49173, 49174, 49175))
        self.assertNotIn(secret, " ".join(command))
        self.assertEqual(env["CODEX_API_KEY"], secret)
        self.assertEqual(env["XO_PROJECTS_ROOT"], "/workspace/xo-projects")
        self.assertEqual(env["AI_WORKSPACE_ROOT"], "/workspace/xo-projects/demo")
        self.assertEqual(env["EXPERIMENT_RUNTIME_ROLE"], "sandbox")
        self.assertEqual(env["EXPERIMENT_PERMISSION_PROFILE"], "unrestricted")
        self.assertEqual(env["EXPERIMENT_CONTROL_TOKEN"], "vps-secret")
        self.assertEqual(env["EXPERIMENT_CONTROL_PORT"], "8787")
        self.assertEqual(
            env["EXPERIMENT_PARENT_SPACE_URL"],
            "http://127.0.0.1:5002/space/#/chat",
        )
        self.assertIn(f"{staged}:/xo-sources:ro", command)
        self.assertIn("0:0", command)
        self.assertNotIn("no-new-privileges", command)
        self.assertNotIn("--read-only", command)
        self.assertNotIn("--cap-drop", command)
        self.assertIn("127.0.0.1::8787", command)
        self.assertIn("127.0.0.1::5002", command)
        self.assertIn("127.0.0.1::3000", command)
        self.assertEqual(command[-1], runtime._docker_image())

    async def test_hardened_profile_retains_outer_container_restrictions(self) -> None:
        process = AsyncMock(
            side_effect=[
                runtime.ProcessResult(0, "container-id\n"),
                runtime.ProcessResult(0, "127.0.0.1:49173\n"),
                runtime.ProcessResult(0, "127.0.0.1:49174\n"),
                runtime.ProcessResult(0, "127.0.0.1:49175\n"),
            ]
        )
        with patch.dict(os.environ, {"EXPERIMENT_PERMISSION_PROFILE": "hardened"}), patch.object(
            runtime,
            "_run_process",
            process,
        ):
            await runtime._start_docker_vps(
                source_root=Path("/tmp/staged"),
                api_key="sk-test",
                environment_id="env_123",
                api_remote="https://api.openai.com/v1/agents/api",
                experiment_id="exp_123",
                session_id="sess_123",
                container_name="xo-experiment-123",
                project_id="demo",
                parent_space_url="http://127.0.0.1:5002/space/#/chat",
                vps_token="vps-secret",
            )

        command = process.await_args_list[0].args[0]
        env = process.await_args_list[0].kwargs["env"]
        self.assertEqual(env["EXPERIMENT_PERMISSION_PROFILE"], "hardened")
        self.assertIn("no-new-privileges", command)
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop", command)
        self.assertIn("10001:10001", command)

    async def test_vps_exec_uses_authenticated_control_server_and_redacts_output(self) -> None:
        secret = "sk-sandbox-command-secret"
        process = AsyncMock(return_value=runtime.ProcessResult(0, "true\n"))
        record = runtime.ExperimentRecord(id="exp_123", project_id="demo")
        record.sandbox_id = "xo-experiment-123"
        record.workspace_directory = "/workspace/xo-projects/demo"
        record.vps_port = 49173
        record.vps_token = "control-secret"

        class Response:
            is_success = True
            status_code = 200

            @staticmethod
            def json():
                return {
                    "exit_code": 0,
                    "output": f"uid=0(root) {secret}\n",
                    "cwd": record.workspace_directory,
                    "truncated": False,
                }

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                self.url = url
                self.kwargs = kwargs
                return Response()

        client = Client()
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}), patch.object(
            runtime,
            "_run_process",
            process,
        ), patch.object(runtime.httpx, "AsyncClient", return_value=client):
            result = await runtime._run_vps_command(
                record,
                {"command": "id", "timeout_seconds": 12},
            )

        self.assertEqual(result["exit_code"], 0)
        self.assertIn("uid=0(root)", result["output"])
        self.assertNotIn(secret, result["output"])
        self.assertEqual(client.url, "http://127.0.0.1:49173/v1/exec")
        self.assertEqual(client.kwargs["headers"]["Authorization"], "Bearer control-secret")
        self.assertEqual(client.kwargs["json"]["command"], "id")

    async def test_boot_turn_uses_sdk_owned_vps_handler_and_proof(self) -> None:
        record = runtime.ExperimentRecord(id="exp_123", project_id="demo")
        record.sandbox_id = "xo-experiment-123"
        record.workspace_directory = "/workspace/xo-projects/demo"

        class Session:
            def stream(self, *, input, tool_handlers):
                async def events():
                    self.handlers = tool_handlers
                    await tool_handlers[runtime.VPS_EXEC_TOOL_NAME](
                        {"command": "pwd", "cwd": record.workspace_directory}
                    )
                    yield SimpleNamespace(type="session.turn.output_text.done", output_text="READY")

                self.input = input
                return events()

        with patch.object(
            runtime,
            "_run_vps_command",
            AsyncMock(
                return_value={
                    "exit_code": 0,
                    "output": "ok",
                    "cwd": record.workspace_directory,
                    "truncated": False,
                }
            ),
        ):
            session = Session()
            await runtime._run_boot_turn(session, record)

        self.assertIn(runtime.VPS_EXEC_TOOL_NAME, session.input)
        self.assertEqual(list(session.handlers), [runtime.VPS_EXEC_TOOL_NAME])
        self.assertIn("READY", record.output)

    async def test_boot_turn_rejects_text_only_readiness(self) -> None:
        record = runtime.ExperimentRecord(id="exp_123", project_id="demo")
        record.sandbox_id = "xo-experiment-123"
        record.workspace_directory = "/workspace/xo-projects/demo"

        class Session:
            def stream(self, *, input, tool_handlers):
                async def events():
                    self.handlers = tool_handlers
                    yield SimpleNamespace(type="session.turn.output_text.done", output_text="READY")

                return events()

        with patch.object(
            runtime,
            "_run_vps_command",
            AsyncMock(
                return_value={
                    "exit_code": 0,
                    "output": "ok",
                    "cwd": record.workspace_directory,
                    "truncated": False,
                }
            ),
        ):
            with self.assertRaisesRegex(runtime.ExperimentError, "did not prove VPS command"):
                await runtime._run_boot_turn(Session(), record)

    def test_permission_profile_defaults_to_unrestricted_and_rejects_unknown(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXPERIMENT_PERMISSION_PROFILE", None)
            self.assertEqual(runtime._permission_profile(), "unrestricted")
        with patch.dict(os.environ, {"EXPERIMENT_PERMISSION_PROFILE": "host-root"}):
            with self.assertRaises(runtime.ExperimentUnavailable):
                runtime._permission_profile()

    async def test_container_diagnostic_surfaces_exit_logs_and_redacts_keys(self) -> None:
        secret = "sk-diagnostic-secret"
        process = AsyncMock(
            side_effect=[
                runtime.ProcessResult(0, "exited\t2\t\n"),
                runtime.ProcessResult(
                    0,
                    f"OPENAI_API_KEY={secret}\n"
                    "tar: Cannot write: No space left on device\n"
                    "tar: Exiting with failure status\n",
                ),
            ]
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}), patch.object(
            runtime,
            "_run_process",
            process,
        ):
            detail = await runtime._sandbox_container_diagnostic("sandbox-test")

        assert detail is not None
        self.assertIn("exit code 2", detail)
        self.assertIn("No space left on device", detail)
        self.assertNotIn(secret, detail)

    async def test_running_container_has_no_failure_diagnostic(self) -> None:
        process = AsyncMock(return_value=runtime.ProcessResult(0, "running\t0\t\n"))
        with patch.object(runtime, "_run_process", process):
            detail = await runtime._sandbox_container_diagnostic("sandbox-test")

        self.assertIsNone(detail)
        process.assert_awaited_once()

    def test_invalid_image_cannot_become_a_docker_flag(self) -> None:
        with patch.dict(os.environ, {"EXPERIMENT_DOCKER_IMAGE": "--privileged"}):
            with self.assertRaises(runtime.ExperimentUnavailable):
                runtime._docker_image()

    def test_space_url_template_requires_a_safe_http_url_and_port_placeholder(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXPERIMENT_SPACE_URL_TEMPLATE", None)
            self.assertEqual(
                runtime._sandbox_space_url(49173),
                "http://127.0.0.1:49173/space/#/projects",
            )
        for template in [
            "http://127.0.0.1:5002/space/",
            "javascript:alert(1)?port={port}",
            "http://user:pass@host:{port}/",
        ]:
            with self.subTest(template=template), patch.dict(
                os.environ, {"EXPERIMENT_SPACE_URL_TEMPLATE": template}
            ):
                with self.assertRaises(runtime.ExperimentUnavailable):
                    runtime._sandbox_space_url(49173)

    def test_app_url_uses_its_dynamic_published_port(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXPERIMENT_APP_URL_TEMPLATE", None)
            self.assertEqual(runtime._sandbox_app_url(49174), "http://127.0.0.1:49174/")
        with patch.dict(
            os.environ,
            {"EXPERIMENT_APP_URL_TEMPLATE": "https://sandbox.example.test:{port}/"},
        ):
            self.assertEqual(
                runtime._sandbox_app_url(49174),
                "https://sandbox.example.test:49174/",
            )

    def test_vps_url_uses_its_dynamic_published_port(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXPERIMENT_VPS_URL_TEMPLATE", None)
            self.assertEqual(runtime._vps_url(49173), "http://127.0.0.1:49173/")

    def test_output_is_bounded_and_key_redacted(self) -> None:
        record = runtime.ExperimentRecord(id="exp_output", project_id="demo")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-output-secret"}):
            runtime._append_output(record, "sk-output-secret\n" + "x" * 20_000)

        self.assertLessEqual(len(record.output), runtime.MAX_OUTPUT_CHARS)
        self.assertNotIn("sk-output-secret", record.output)


class ExperimentRouteTests(unittest.TestCase):
    def test_project_resolution_rejects_aliases_and_malformed_ids(self) -> None:
        with tempfile.TemporaryDirectory() as root_text, tempfile.TemporaryDirectory() as outside:
            root = Path(root_text)
            project = root / "real project"
            project.mkdir()
            (root / "alias").symlink_to(project, target_is_directory=True)
            (root / "outside").symlink_to(Path(outside), target_is_directory=True)

            with patch(
                "routers.cowork_agent.bff.experiments.xo_projects_root",
                return_value=root,
            ):
                self.assertEqual(_resolve_project_source("real project"), project.resolve())
                for project_id in [".", "..", ".hidden", "../escape", "a/b", "a\\b", "bad\nname"]:
                    with self.subTest(project_id=project_id):
                        with self.assertRaises(HTTPException):
                            _resolve_project_source(project_id)
                for project_id in ["alias", "outside", "missing"]:
                    with self.subTest(project_id=project_id):
                        with self.assertRaises(HTTPException) as raised:
                            _resolve_project_source(project_id)
                        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
