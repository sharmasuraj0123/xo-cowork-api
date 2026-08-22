from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.adapters.claude_code import visualizer_source
from services.cowork_agent.visualizer import project_index
from services.cowork_agent.visualizer.ingest.jsonl_tail import OffsetStore


class SessionMountTests(unittest.TestCase):
    def test_host_workspace_cwd_maps_to_container_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            container_root = Path(tmp) / "container-projects"
            container_root.mkdir()
            with (
                patch.object(
                    project_index,
                    "xo_projects_root",
                    return_value=container_root,
                ),
                patch.dict(
                    os.environ,
                    {"QUIRQ_HOST_PROJECTS_ROOT": "/Users/example/xo-projects"},
                ),
            ):
                project_id = project_index.project_id_for_cwd(
                    "/Users/example/xo-projects/demo/src"
                )
        self.assertEqual(project_id, "demo")

    def test_claude_discovery_checks_host_encoded_project_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native_projects = root / ".claude" / "projects"
            container_projects = root / "container-projects"
            host_projects = Path("/Users/example/xo-projects")
            encoded = str(host_projects / "demo").replace("/", "-")
            log_dir = native_projects / encoded
            log_dir.mkdir(parents=True)
            jsonl = log_dir / "session.jsonl"
            jsonl.write_text("{}\n", encoding="utf-8")

            source = visualizer_source.Source(
                offsets=OffsetStore(root / "offsets.json")
            )
            with (
                patch.object(
                    visualizer_source,
                    "_CLAUDE_PROJECTS_DIR",
                    native_projects,
                ),
                patch.object(
                    visualizer_source,
                    "xo_projects_root",
                    return_value=container_projects,
                ),
                patch.object(
                    visualizer_source,
                    "list_project_ids",
                    return_value=["demo"],
                ),
                patch.object(
                    visualizer_source,
                    "iter_sessionslist_rows",
                    return_value=[],
                ),
                patch.dict(
                    os.environ,
                    {"QUIRQ_HOST_PROJECTS_ROOT": str(host_projects)},
                ),
            ):
                discovered = list(source._discover_jsonls())

        self.assertEqual(discovered, [("demo", jsonl)])


if __name__ == "__main__":
    unittest.main()
