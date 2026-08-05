"""``project_layout`` — the one place the synced-vs-runtime tier decision is made.

Every path in the system funnels through this module (``project_layout.py:5-7``).
That makes it the chokepoint the tier split depends on, and it makes three
distinct classes of regression possible here:

* **Tier**: a helper starts resolving runtime state back into the project tree.
  ``sessions_dir`` is the one the design doc rates highest-risk — it is the path
  that actually moved, and four adapter call sites had to be rewritten to stop
  hand-building ``<project>/.xo/sessions``.
* **Identity**: ``runtime_key`` picks the wrong key. Runtime is keyed by
  ``project.json:pid``, with a folder-name fallback for the pre-mint window. Get
  the fallback conditions wrong and a project's telemetry silently relocates.
* **Safety**: ``project.json`` is in the SYNCED tier, so its ``pid`` arrives from
  other machines and from snapshot restores — untrusted input joined straight
  into a path that runtime writers ``mkdir(parents=True)``. Fix R1 sanitises and
  clamps it; these tests pin that shut.

Nothing here starts the app or the watcher — these are pure path/JSON helpers.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

# Safe at module scope: pytest imports ``conftest.py`` (and therefore runs its
# Layer 1 environment rewrite) before it imports any test module in this
# directory, so the home-bound constants this drags in are already sandboxed.
from services.cowork_agent import project_layout
from services.cowork_agent.helpers import normalize_agent_id


# Runtime keys that must never reach the filesystem as-is. The first three are
# the ones that actually escape (``Path("~/.xo") / "/etc/evil"`` is just
# ``/etc/evil``); the rest are the shapes that would land somewhere unintended.
HOSTILE_PIDS = [
    pytest.param("/etc/evil", id="absolute-path"),
    pytest.param("../../escape", id="traversal"),
    pytest.param("a/b", id="separator"),
    pytest.param("..\\..\\escape", id="windows-traversal"),
    pytest.param("..", id="parent-ref"),
    pytest.param(".", id="self-ref"),
    pytest.param("ok\x00bad", id="null-byte"),
    pytest.param("x" * 65, id="over-length"),
]


def _write_meta(project_id: str, doc) -> Path:
    """Overwrite a project's ``.xo/project.json`` with ``doc`` (verbatim if str)."""
    path = project_layout.project_metadata_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        doc if isinstance(doc, str) else json.dumps(doc, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """``{relative posix path: content}`` for every file under ``root``."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ── The split itself ─────────────────────────────────────────────────────────


def test_runtime_root_is_outside_the_projects_root(xo_roots) -> None:
    """The runtime home is not inside any project tree. This is all of 7b5044f.

    ``xo_runtime_root()`` (``project_layout.py:111``) is a sibling of, never a
    descendant of, ``xo_projects_root()``. Everything else in the tier split is
    downstream of this one fact: it is what makes "runtime state cannot be
    committed, tarred or synced" a property of the filesystem rather than of a
    ``.gitignore`` someone has to remember to write.
    """
    projects = project_layout.xo_projects_root()
    runtime = project_layout.xo_runtime_root()

    assert projects != runtime
    with pytest.raises(ValueError):
        runtime.relative_to(projects)
    # And the default shape, sanity-checked against the fixture's own dirs.
    assert (projects, runtime) == (xo_roots.projects, xo_roots.runtime)


def test_path_helpers_compose_the_documented_shapes(xo_roots) -> None:
    """The path algebra in the module header (``project_layout.py:9-24``) holds.

    Synced tier hangs off ``<projects>/<name>/.xo/``; runtime tier hangs off
    ``<runtime>/<key>/``; the workspace tier is ``<runtime>/workspace/`` — the
    address it moved to from ``~/xo-projects/.xo/``.
    """
    key = "0f9c2a11-1111-4111-8111-000000000001"

    assert project_layout.project_dir("demo") == xo_roots.projects / "demo"
    assert project_layout.xo_dir("demo") == xo_roots.projects / "demo" / ".xo"
    assert (
        project_layout.project_metadata_path("demo")
        == xo_roots.projects / "demo" / ".xo" / "project.json"
    )

    assert project_layout.runtime_dir(key) == xo_roots.runtime / key
    assert project_layout.runtime_sessions_dir(key) == xo_roots.runtime / key / "sessions"
    assert project_layout.workspace_runtime_dir() == xo_roots.runtime / "workspace"

    # Folder names are normalised on the way in, so callers may pass user input.
    assert project_layout.project_dir("My Project!") == xo_roots.projects / "my-project"


def test_sessions_dir_resolves_into_the_runtime_tier(scaffolded_project) -> None:
    """``sessions_dir(name)`` lands in the runtime home, never in the project.

    The highest-risk chokepoint in the design (``project_layout.py:258-267``).
    ``sessions/`` used to live at ``<project>/.xo/sessions/``; four adapter call
    sites hand-built that path and had to be rewritten (codex/antigravity
    ``adapter.py`` + ``sessions.py``). Any code that rebuilds it by hand now
    *reads a location nothing writes*, which fails silently — an empty session
    list rather than an error.

    Asserted both ways: the resolved path is under ``~/.xo/<pid>/``, and writing
    through the helper leaves no ``sessions`` anywhere in the project tree.
    """
    proj = scaffolded_project("demo")
    sessions = project_layout.sessions_dir(proj.name)

    assert sessions == proj.runtime / "sessions"
    assert sessions == project_layout.runtime_sessions_dir(proj.pid)
    assert sessions.is_relative_to(project_layout.xo_runtime_root())
    assert not sessions.is_relative_to(proj.dir)

    # Write the payload an adapter writes, then prove the project tree is clean.
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "sessionslist.json").write_text("{}", encoding="utf-8")

    assert not (proj.xo / "sessions").exists()
    assert [p for p in proj.dir.rglob("sessions")] == []


# ── runtime_key: which key the runtime store is addressed by ─────────────────


def test_runtime_key_uses_the_pid_once_minted(scaffolded_project) -> None:
    """A filled ``project.json`` keys the runtime store by its ``pid``.

    ``runtime_key`` (``project_layout.py:205``) is the only reader of
    ``project.json`` in the path layer, and its answer decides where every
    runtime artefact for the project lives.
    """
    pid = "3f1c9b2e-7a44-4d1e-9c8b-0a1b2c3d4e5f"
    proj = scaffolded_project("demo", pid=pid)

    assert project_layout.runtime_key(proj.name) == pid
    assert project_layout.project_runtime_dir(proj.name) == (
        project_layout.xo_runtime_root() / pid
    )
    # The folder name is emphatically NOT the key once a pid exists — renaming
    # the folder must not orphan the runtime store.
    assert project_layout.runtime_key(proj.name) != proj.name


@pytest.mark.parametrize(
    "doc, why",
    [
        pytest.param(
            {"schema": 1, "_template": True, "pid": "unfilled-pid-0001"},
            "the template is still unfilled, so its pid is not an identity yet",
            id="template-flag-set",
        ),
        pytest.param(
            {"schema": 1, "pid": None},
            "no pid has been minted",
            id="null-pid",
        ),
        pytest.param(None, "there is no project.json at all", id="no-metadata"),
        pytest.param("{ not json", "project.json is malformed", id="malformed-json"),
        pytest.param('["not", "an", "object"]', "project.json is not an object",
                     id="non-object-json"),
    ],
)
def test_runtime_key_falls_back_to_the_folder_name(
    scaffolded_project, doc, why: str
) -> None:
    """Without a usable pid, the runtime key is the normalised folder name.

    The fallback is transitional and deliberate (``project_layout.py:205-219``):
    a brand-new project has no pid until the watcher's first identity fill, and
    runtime is machine-local so a short-lived folder-name key is harmless. What
    would *not* be harmless is raising — the watcher would never get far enough
    to mint the pid that ends the window.

    Each case here is a different way the pid can be unusable.
    """
    proj = scaffolded_project("demo", pid=None)
    if doc is None:
        project_layout.project_metadata_path(proj.name).unlink()
    else:
        _write_meta(proj.name, doc)

    assert project_layout.runtime_key(proj.name) == normalize_agent_id(proj.name), why
    assert project_layout.project_runtime_dir(proj.name) == (
        project_layout.xo_runtime_root() / "demo"
    )


# ── R1: the pid is untrusted input ───────────────────────────────────────────


@pytest.mark.parametrize("pid", HOSTILE_PIDS)
def test_hostile_pid_in_project_json_cannot_escape_the_runtime_root(
    scaffolded_project, xo_roots, pid: str
) -> None:
    """A corrupt/hostile ``pid`` is contained, not obeyed (fix R1).

    ``project.json`` is SYNCED: it travels between machines and a restore drops
    a snapshot's copy in wholesale, so ``pid`` is untrusted. Joined naively it is
    an attacker-chosen **write** — the runtime sinks ``mkdir(parents=True)``
    before writing, and ``Path("~/.xo") / "/etc/evil"`` is simply ``/etc/evil``.

    ``runtime_key`` (``project_layout.py:220-234``) therefore validates the pid
    and falls back to the folder name when it fails: wrong-but-contained beats
    redirecting every runtime write out of the runtime home. Contrast with
    :func:`test_runtime_dir_refuses_an_unsafe_key_outright` — the low-level
    helper raises; only the ``project.json``-reading layer falls back, because
    that is the layer that has a safe answer available.
    """
    proj = scaffolded_project("demo", pid=None)
    _write_meta(proj.name, {"schema": 1, "pid": pid, "name": proj.name})

    key = project_layout.runtime_key(proj.name)
    assert key == normalize_agent_id(proj.name)

    for resolved in (
        project_layout.project_runtime_dir(proj.name),
        project_layout.project_runtime_sessions_dir(proj.name),
        project_layout.sessions_dir(proj.name),
    ):
        assert resolved.is_relative_to(xo_roots.runtime), (
            f"pid {pid!r} resolved to {resolved}, outside the runtime home "
            f"{xo_roots.runtime}"
        )

    # Nothing was created at the address the hostile pid was aiming for.
    assert not Path("/etc/evil").exists()
    assert not (xo_roots.projects.parent.parent / "escape").exists()


@pytest.mark.parametrize("pid", HOSTILE_PIDS)
def test_runtime_dir_refuses_an_unsafe_key_outright(pid: str) -> None:
    """``runtime_dir`` raises rather than resolve a key outside the runtime home.

    The low-level helper has no safe fallback to offer — it was handed a key, not
    a project — so it fails closed (``project_layout.py:158-185``). Both of its
    raw-pid callers (``visualizer/migrate.py``, ``visualizer/registry.py`` via
    ``build_registry``) are exception-guarded, so one poisoned project is skipped
    loudly instead of relocating its data.
    """
    with pytest.raises(ValueError):
        project_layout.runtime_dir(pid)


def test_runtime_dir_refuses_a_key_symlinked_out_of_the_runtime_home(
    xo_roots,
) -> None:
    """A charset-clean key whose directory symlinks outside is still refused.

    The character allowlist cannot see a symlink, so ``runtime_dir`` also
    resolves and clamps with ``relative_to``. Without the clamp, an attacker (or
    a well-meaning "move telemetry to the big disk" symlink) turns every runtime
    write into a write outside the runtime home.
    """
    outside = xo_roots.runtime.parent / "outside-target"
    outside.mkdir()
    (xo_roots.runtime / "symlinked").symlink_to(outside)

    with pytest.raises(ValueError):
        project_layout.runtime_dir("symlinked")


def test_a_normal_uuid_pid_resolves_unchanged(xo_roots) -> None:
    """Sanitising must not perturb the shape every real project actually has.

    Pids are minted as UUIDs (``sinks/project_json.py:80``); the guard rails
    above are worthless if they also mangle the happy path.
    """
    pid = str(uuid.uuid4())
    assert project_layout.runtime_dir(pid) == xo_roots.runtime / pid
    assert project_layout.runtime_sessions_dir(pid) == xo_roots.runtime / pid / "sessions"


# ── Scaffolding ──────────────────────────────────────────────────────────────


def test_scaffold_writes_only_the_synced_contract(xo_roots) -> None:
    """A fresh project's ``.xo/`` holds the synced contract and nothing else.

    ``scaffold_project`` (``project_layout.py:277-307``) deliberately does *not*
    seed ``sessions/``: the session index is machine-local runtime state created
    lazily by the adapters under ``~/.xo/<pid>/sessions/``. Seeding it here would
    put runtime back inside the shareable tree at creation time — before any
    watcher or adapter has run.
    """
    project_layout.scaffold_project("demo")

    xo = project_layout.xo_dir("demo")
    assert {p.name for p in xo.iterdir()} == {
        "project.json",
        "peers.json",
        "todos.json",
    }
    assert [p for p in project_layout.project_dir("demo").rglob("sessions")] == []
    # The runtime tier is not pre-created either — it appears on first write.
    assert not (xo_roots.runtime / "demo").exists()

    # The rest of the template did land, so the assertion above is about tiering
    # rather than about scaffolding having quietly done nothing.
    assert (project_layout.project_dir("demo") / "AGENTS.md").is_file()


def test_scaffold_is_idempotent_and_never_clobbers() -> None:
    """Re-scaffolding fills gaps only: existing bytes are never overwritten.

    Idempotence is load-bearing — ``scaffold_project`` is the "create or repair"
    entry point and is called on projects that already exist. A clobbering
    scaffold would destroy a user's edited ``AGENTS.md`` and, worse, reset the
    ``pid`` that addresses the whole runtime store.
    """
    project_layout.scaffold_project("demo", display_name="Acme", description="widgets")

    pdir = project_layout.project_dir("demo")
    (pdir / "AGENTS.md").write_text("# my own rules\n", encoding="utf-8")
    (pdir / ".xo" / "todos.json").write_text('{"mine": true}', encoding="utf-8")
    _write_meta("demo", {"schema": 1, "pid": "keep-me", "name": "demo",
                         "created_at": "1999-01-01T00:00:00Z",
                         "display_name": "Acme", "description": "widgets"})

    # Punch a gap so the second call has real work to do — otherwise "nothing
    # changed" could just as well mean "scaffold returned early".
    (pdir / "PLAN.md").unlink()
    before = _tree_bytes(pdir)

    meta = project_layout.scaffold_project("demo")

    after = _tree_bytes(pdir)
    assert set(after) - set(before) == {"PLAN.md"}, "the missing file was restored"
    assert {k: v for k, v in after.items() if k != "PLAN.md"} == before, (
        "re-scaffolding rewrote existing files"
    )
    assert meta["pid"] == "keep-me"
    assert project_layout.runtime_key("demo") == "keep-me"


# ── Listing / traversal safety ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "relative_path",
    ["..", "../..", "../../etc", "memory/../..", "a/./b", ".", "/etc", "\\etc",
     "bad\x00path"],
)
def test_list_project_tree_rejects_traversal(scaffolded_project, relative_path: str) -> None:
    """``list_project_tree`` refuses any path that could leave the project.

    It is reached from the file-browser BFF endpoints with a caller-supplied
    ``relative_path``, so it is a directory-listing oracle for the whole disk if
    it gets this wrong. It raises ``ValueError`` (a *rejection*) rather than
    returning ``None`` (which the caller reads as "no such directory"), so the
    router can answer 400 rather than 404 (``project_layout.py:422-457``).
    """
    proj = scaffolded_project("demo")
    with pytest.raises(ValueError):
        project_layout.list_project_tree(proj.name, relative_path)


def test_list_project_tree_lists_a_legitimate_subdirectory(scaffolded_project) -> None:
    """The happy path still works — the traversal guard is not a blanket refusal."""
    proj = scaffolded_project("demo")

    root = project_layout.list_project_tree(proj.name)
    assert root is not None
    assert "memory" in {d["name"] for d in root["dirs"]}
    assert root["parent_relative_path"] is None

    nested = project_layout.list_project_tree(proj.name, "memory")
    assert nested is not None
    assert nested["relative_path"] == "memory"
    assert nested["parent_relative_path"] == ""
    # Entries are addressed relative to the project root, not to the listed dir.
    for entry in nested["dirs"] + nested["files"]:
        assert entry["relative_path"].startswith("memory/")

    assert project_layout.list_project_tree("no-such-project") is None
