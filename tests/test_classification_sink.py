"""Classification sink: persists a project's environment classification into
.xo/project.json without disturbing identity or the manual category override.

Runs against a temp workspace via XO_PROJECTS_ROOT (project_layout resolves
the root at call time), so the real workspace's watcher-owned files are
never touched.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _scaffold(root: Path, pid: str, *, manual_category: str | None = None) -> Path:
    proj = root / pid
    (proj / ".xo").mkdir(parents=True)
    (proj / "src").mkdir()
    (proj / "README.md").write_text("# Test project\n\nA thing.\n")
    (proj / "package.json").write_text('{"name": "t", "scripts": {"dev": "x"}}\n')
    (proj / "src" / "app.js").write_text("console.log(1)\n")
    (proj / "src" / "util.js").write_text("console.log(2)\n")
    identity = {
        "schema": 1, "pid": "00000000-0000-4000-8000-000000000001",
        "name": pid, "owner_user_id": "local",
        "created_at": "2026-01-01T00:00:00Z",
    }
    if manual_category:
        identity["category"] = manual_category
    (proj / ".xo" / "project.json").write_text(json.dumps(identity))
    return proj


class ClassificationSinkTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".xo").mkdir()
        self.env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.root)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _apply(self, pid: str, **kw) -> bool:
        from services.cowork_agent.visualizer.sinks import classification
        return classification.apply(self.root / pid / ".xo", pid, **kw)

    def test_writes_block_and_preserves_identity(self):
        _scaffold(self.root, "proj-a")
        self.assertTrue(self._apply("proj-a", dirty=True))
        doc = json.loads((self.root / "proj-a" / ".xo" / "project.json").read_text())
        block = doc["classification"]
        for key in ("computed_at", "category", "ptype"):
            self.assertIn(key, block)
        self.assertEqual(block["category"], "app")   # package.json manifest
        self.assertEqual(block["ptype"], "app")
        self.assertIn("output", block["xotype_counts"])
        self.assertIn("inbox", block["xotype_counts"])   # README.md
        self.assertIn("system", block["xotype_counts"])  # package.json
        # identity untouched
        self.assertEqual(doc["pid"], "00000000-0000-4000-8000-000000000001")
        self.assertEqual(doc["name"], "proj-a")
        self.assertEqual(doc["schema"], 1)

    def test_manual_category_never_touched(self):
        _scaffold(self.root, "proj-b", manual_category="customer")
        self.assertTrue(self._apply("proj-b", dirty=True))
        doc = json.loads((self.root / "proj-b" / ".xo" / "project.json").read_text())
        self.assertEqual(doc["category"], "customer")          # manual wins, untouched
        self.assertEqual(doc["classification"]["category"], "customer")  # override respected

    def test_throttle_honors_computed_at(self):
        _scaffold(self.root, "proj-c")
        self.assertTrue(self._apply("proj-c", dirty=True))
        # Fresh block: an immediate re-apply is a no-op even when dirty.
        self.assertFalse(self._apply("proj-c", dirty=True))
        # Age the block past the dirty threshold: recomputes.
        pj = self.root / "proj-c" / ".xo" / "project.json"
        doc = json.loads(pj.read_text())
        doc["classification"]["computed_at"] = "2026-01-01T00:00:00Z"
        pj.write_text(json.dumps(doc))
        self.assertTrue(self._apply("proj-c", dirty=True))

    def test_only_project_json_changes(self):
        proj = _scaffold(self.root, "proj-d")
        before = {p: p.stat().st_mtime_ns for p in proj.rglob("*") if p.is_file()}
        self.assertTrue(self._apply("proj-d", dirty=True))
        after = {p: p.stat().st_mtime_ns for p in proj.rglob("*") if p.is_file()}
        changed = [str(p.relative_to(proj)) for p in after
                   if before.get(p) is not None and before[p] != after[p]]
        self.assertEqual(changed, [".xo/project.json"])
        self.assertEqual(set(after) - set(before), set())  # no new files


if __name__ == "__main__":
    unittest.main()
