"""Git-backed self-update (Setup tab · 04 Version).

Exercises the real git flows against throwaway repos: a local "origin" plays
the remote, a clone plays the installed checkout, and REPO_ROOT is pointed at
the clone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from services.cowork_agent import self_update


def _git_env(home: str) -> dict:
    git_bin = shutil.which("git") or "/usr/bin/git"
    return {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": home,
        "PATH": os.path.dirname(git_bin) + ":/usr/bin:/bin:/usr/local/bin",
    }


class SelfUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not available")
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.origin = tmp / "origin"
        self.work = tmp / "work"
        self.env = _git_env(self._tmp.name)
        self.origin.mkdir()
        self._run("init", "-q", cwd=self.origin)
        (self.origin / "requirements.txt").write_text("a==1\n", encoding="utf-8")
        (self.origin / "code.py").write_text("x=1\n", encoding="utf-8")
        self._run("add", "-A", cwd=self.origin)
        self._run("commit", "-q", "-m", "first", cwd=self.origin)
        self._run("clone", "-q", str(self.origin), str(self.work), cwd=tmp)
        self._orig_root = self_update.REPO_ROOT
        self_update.REPO_ROOT = self.work

    def tearDown(self) -> None:
        self_update.REPO_ROOT = self._orig_root
        self._tmp.cleanup()

    def _run(self, *args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True,
                       capture_output=True, env=self.env)

    def _commit_upstream(self, message: str, requirements: bool = False) -> None:
        target = "requirements.txt" if requirements else "code.py"
        path = self.origin / target
        path.write_text(path.read_text(encoding="utf-8") + "#\n", encoding="utf-8")
        self._run("add", "-A", cwd=self.origin)
        self._run("commit", "-q", "-m", message, cwd=self.origin)

    def test_up_to_date_status_and_noop_apply(self) -> None:
        status = self_update.check_update_status()
        self.assertTrue(status["supported"])
        self.assertTrue(status["fetch_ok"])
        self.assertTrue(status["up_to_date"])
        self.assertEqual(status["behind"], 0)
        self.assertFalse(status["dirty"])
        result = self_update.apply_update()
        self.assertFalse(result["updated"])
        self.assertEqual(result["reason"], "up_to_date")

    def test_behind_remote_then_fast_forward(self) -> None:
        self._commit_upstream("bump requirements", requirements=True)
        status = self_update.check_update_status()
        self.assertFalse(status["up_to_date"])
        self.assertEqual(status["behind"], 1)
        self.assertNotEqual(status["latest"]["sha"], status["current"]["sha"])
        result = self_update.apply_update()
        self.assertTrue(result["updated"])
        self.assertEqual(result["commits"], 1)
        self.assertTrue(result["requirements_changed"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(result["to"]["sha"], status["latest"]["sha"])
        self.assertTrue(self_update.check_update_status()["up_to_date"])

    def test_dirty_tree_refuses(self) -> None:
        self._commit_upstream("newer")
        (self.work / "local-edit.txt").write_text("wip", encoding="utf-8")
        result = self_update.apply_update()
        self.assertFalse(result["updated"])
        self.assertEqual(result["reason"], "dirty_tree")

    def test_diverged_refuses(self) -> None:
        self._commit_upstream("remote work")
        (self.work / "code.py").write_text("x=2\n", encoding="utf-8")
        self._run("add", "-A", cwd=self.work)
        self._run("commit", "-q", "-m", "local work", cwd=self.work)
        result = self_update.apply_update()
        self.assertFalse(result["updated"])
        self.assertEqual(result["reason"], "diverged")

    def test_non_checkout_is_unsupported(self) -> None:
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        self_update.REPO_ROOT = plain
        status = self_update.check_update_status()
        self.assertFalse(status["supported"])
        self.assertEqual(status["reason"], "not_a_git_checkout")
        result = self_update.apply_update()
        self.assertFalse(result["updated"])
        self.assertEqual(result["reason"], "not_a_git_checkout")


if __name__ == "__main__":
    unittest.main()
