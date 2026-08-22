"""One XO root, one project identity.

Two invariants the Space tabs depend on:

1. **The directory name is the project id.** ``project_layout`` resolves
   every path as ``xo_projects_root() / name``, so a stale ``name`` inside
   ``.xo/project.json`` (left behind by a folder rename) must not become the
   id — it would send readers to the wrong folder and collapse two folders
   into one node. This is what made the Timeline plot one project while
   Files listed five: the renamed project's git history was read from a
   different, git-less folder.
2. **The XO root the Setup tab saves is the root the process boots with.**
   ``server.py`` reads ``<state root>/roots.env`` at startup, under the same
   precedence install.sh uses: shell/container env > roots.env > .env.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout

# Imported at module scope on purpose: importing server runs its dotenv
# bootstrap, which mutates os.environ from the developer's real .env and
# ~/.quirq/roots.env. Doing that lazily inside a test would undo the very
# environment the test just set up, and the suite would pass or fail
# depending on whether some earlier test had already imported it.
import server  # noqa: E402


def _project(root: Path, folder: str, meta: dict) -> None:
    xo = root / folder / ".xo"
    xo.mkdir(parents=True)
    (xo / "project.json").write_text(json.dumps(meta), encoding="utf-8")


class ProjectIdentityTests(unittest.TestCase):
    def test_directory_name_wins_over_a_stale_metadata_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # "writings" was created as "site" and renamed on disk later.
            _project(root, "writings", {"name": "site", "created_at": "2026-07-25"})
            _project(root, "site", {"name": "site", "created_at": "2026-08-05"})
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}, clear=False):
                items = project_layout.list_projects()

        names = sorted(item["name"] for item in items)
        self.assertEqual(names, ["site", "writings"])
        # Descriptive fields survive; the id and the label do not lie.
        display = {item["name"]: item["display_name"] for item in items}
        self.assertEqual(display["writings"], "writings")
        self.assertEqual(display["site"], "site")

    def test_a_deliberate_display_name_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _project(
                root,
                "writings",
                {"name": "site", "display_name": "The Quirq Papers"},
            )
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}, clear=False):
                items = project_layout.list_projects()

        self.assertEqual(items[0]["name"], "writings")
        self.assertEqual(items[0]["display_name"], "The Quirq Papers")

    def test_ids_resolve_back_to_their_own_directory(self) -> None:
        """The id list is only useful if project_dir() round-trips it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _project(root, "writings", {"name": "site"})
            (root / "writings" / "marker.md").write_text("x", encoding="utf-8")
            with patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(root)}, clear=False):
                item = project_layout.list_projects()[0]
                resolved = project_layout.project_dir(item["name"])
                self.assertTrue((resolved / "marker.md").is_file())


class StorageRootBootTests(unittest.TestCase):
    """server.py's roots.env loader — the seam that makes the Setup tab's XO
    root take effect for every tab after a restart."""

    def _load(self, state_root: Path, shell_keys: frozenset[str]):
        with patch.object(server, "_shell_env_keys", shell_keys):
            with patch.dict(
                os.environ,
                {"QUIRQ_STATE_ROOT": str(state_root)},
                clear=False,
            ):
                server._load_storage_roots()
                return os.environ.get("XO_PROJECTS_ROOT", "")

    def test_saved_root_is_applied_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_root.mkdir()
            saved = Path(tmp) / "saved-projects"
            (state_root / "roots.env").write_text(
                f"# managed\nXO_PROJECTS_ROOT={saved}\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("XO_PROJECTS_ROOT", None)
                self.assertEqual(self._load(state_root, frozenset()), str(saved))

    def test_shell_environment_outranks_the_saved_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_root.mkdir()
            saved = Path(tmp) / "saved-projects"
            exported = Path(tmp) / "exported-projects"
            (state_root / "roots.env").write_text(
                f"XO_PROJECTS_ROOT={saved}\n", encoding="utf-8"
            )
            with patch.dict(
                os.environ,
                {"XO_PROJECTS_ROOT": str(exported)},
                clear=False,
            ):
                applied = self._load(
                    state_root,
                    frozenset({"XO_PROJECTS_ROOT"}),
                )
            self.assertEqual(applied, str(exported))

    def test_a_missing_file_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_root.mkdir()
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("XO_PROJECTS_ROOT", None)
                self.assertEqual(self._load(state_root, frozenset()), "")


if __name__ == "__main__":
    unittest.main()
