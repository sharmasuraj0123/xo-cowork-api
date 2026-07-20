from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "docker" / "experiment" / "vps_server.py"
SPEC = importlib.util.spec_from_file_location("xo_experiment_vps_server", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
vps = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vps
SPEC.loader.exec_module(vps)


class VpsFixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sources = self.root / "sources"
        self.workspace = self.root / "workspace"
        self.project_source = self.sources / "project"
        self.cowork_source = self.sources / "xo-cowork-api"
        self.project_source.mkdir(parents=True)
        self.cowork_source.mkdir(parents=True)
        (self.project_source / "README.md").write_text("demo project\n", encoding="utf-8")
        (self.cowork_source / "server.py").write_text("# sandbox Space\n", encoding="utf-8")
        sleeper = (
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        )
        self.settings = vps.Settings(
            control_token="control-token-for-focused-tests-123456",
            project_id="demo",
            environment_id="env_test",
            agents_api_remote="https://api.openai.com/v1/agents/api",
            source_root=self.sources,
            workspace_root=self.workspace,
            projects_root=self.workspace / "xo-projects",
            cowork_api_root=self.workspace / "xo-cowork-api",
            codex_home=self.workspace / "codex-home",
            log_root=self.workspace / "supervisor-logs",
            shutdown_grace_seconds=1,
            space_command=sleeper,
            codex_command=sleeper,
        )

    def close(self) -> None:
        self.temp.cleanup()


class ExperimentVpsBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = VpsFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_bootstrap_copies_both_filtered_trees_to_writable_workspace(self) -> None:
        destination = vps.bootstrap_sources(self.fixture.settings)

        self.assertEqual(destination, self.fixture.settings.project_directory)
        self.assertEqual((destination / "README.md").read_text(), "demo project\n")
        self.assertTrue((self.fixture.settings.cowork_api_root / "server.py").is_file())
        self.assertTrue(os.access(destination, os.W_OK))
        marker = self.fixture.workspace / ".xo-experiment-bootstrap.json"
        self.assertIn('"project_id":"demo"', marker.read_text())

    def test_bootstrap_rejects_credentials_links_and_special_paths(self) -> None:
        (self.fixture.project_source / ".env").write_text("API_KEY=secret")
        with self.assertRaisesRegex(RuntimeError, "blocked credential"):
            vps.bootstrap_sources(self.fixture.settings)

        (self.fixture.project_source / ".env").unlink()
        (self.fixture.project_source / "escape").symlink_to(Path("/tmp"))
        with self.assertRaisesRegex(RuntimeError, "link or special"):
            vps.bootstrap_sources(self.fixture.settings)

    def test_default_codex_command_is_unrestricted_and_uses_self_hosted_id(self) -> None:
        settings = self.fixture.settings
        unrestricted = vps.Settings(
            **{
                **settings.__dict__,
                "codex_command": None,
            }
        )
        command = unrestricted.resolved_codex_command

        self.assertIn('sandbox_mode="danger-full-access"', command)
        self.assertIn('approval_policy="never"', command)
        self.assertEqual(command[-1], "env_test")

    def test_hardened_profile_keeps_workspace_write_codex_mode(self) -> None:
        settings = self.fixture.settings
        hardened = vps.Settings(
            **{
                **settings.__dict__,
                "permission_profile": "hardened",
                "codex_command": None,
            }
        )

        self.assertIn('sandbox_mode="workspace-write"', hardened.resolved_codex_command)


class ExperimentVpsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = VpsFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.fixture.settings.control_token}"}

    def test_public_status_is_read_only_and_exec_requires_bearer(self) -> None:
        supervisor = vps.VpsSupervisor(self.fixture.settings)
        app = vps.create_app(self.fixture.settings, supervisor)
        with TestClient(app) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            ready = client.get("/readyz")
            self.assertEqual(ready.status_code, 200)
            self.assertTrue(ready.json()["ready"])
            dashboard = client.get("/")
            self.assertEqual(dashboard.status_code, 200)
            self.assertNotIn(self.fixture.settings.control_token, dashboard.text)
            self.assertEqual(client.post("/").status_code, 405)

            denied = client.post("/v1/exec", json={"command": "id"})
            self.assertEqual(denied.status_code, 401)

    def test_exec_is_bounded_runs_in_workspace_and_redacts_secrets(self) -> None:
        secret = "sk-vps-secret-value-123456789"
        with patch.dict(os.environ, {"CODEX_API_KEY": secret}):
            supervisor = vps.VpsSupervisor(self.fixture.settings)
            app = vps.create_app(self.fixture.settings, supervisor)
            with TestClient(app) as client:
                response = client.post(
                    "/v1/exec",
                    headers=self._headers(),
                    json={
                        "command": 'pwd; printf "\\n%s" "$CODEX_API_KEY"',
                        "timeout_seconds": 5,
                    },
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["exit_code"], 0)
        self.assertIn(str(self.fixture.settings.project_directory), payload["output"])
        self.assertIn("[REDACTED]", payload["output"])
        self.assertNotIn(secret, payload["output"])

    def test_invalid_request_does_not_echo_command_input(self) -> None:
        supervisor = vps.VpsSupervisor(self.fixture.settings)
        app = vps.create_app(self.fixture.settings, supervisor)
        sensitive_command = "sk-invalid-command-value-123456789"
        with TestClient(app) as client:
            response = client.post(
                "/v1/exec",
                headers=self._headers(),
                json={"command": sensitive_command, "timeout_seconds": 0},
            )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(sensitive_command, response.text)

    def test_timeout_kills_command_process_group(self) -> None:
        supervisor = vps.VpsSupervisor(self.fixture.settings)
        app = vps.create_app(self.fixture.settings, supervisor)
        with TestClient(app) as client:
            response = client.post(
                "/v1/exec",
                headers=self._headers(),
                json={"command": "sleep 30", "timeout_seconds": 1},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["timed_out"])
        self.assertEqual(response.json()["exit_code"], 124)


class ExperimentVpsLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = VpsFixture()

    async def asyncTearDown(self) -> None:
        self.fixture.close()

    async def test_supervisor_gracefully_reaps_managed_children(self) -> None:
        supervisor = vps.VpsSupervisor(self.fixture.settings)
        await supervisor.start()
        processes = [child.process for child in supervisor.children.values()]

        self.assertTrue(supervisor.is_ready())
        self.assertTrue(all(process is not None and process.returncode is None for process in processes))
        await supervisor.close()

        self.assertEqual(supervisor.phase, "stopped")
        self.assertTrue(all(process is not None and process.returncode is not None for process in processes))


class ExperimentVpsImageContractTests(unittest.TestCase):
    def test_image_runs_supervisor_as_root_and_exposes_only_known_service_ports(self) -> None:
        dockerfile = (REPO_ROOT / "docker" / "experiment" / "Dockerfile").read_text()

        self.assertIn("USER root", dockerfile)
        self.assertIn("EXPOSE 8787 5002 3000", dockerfile)
        self.assertIn("/opt/xo-experiment/vps_server.py", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)


if __name__ == "__main__":
    unittest.main()
