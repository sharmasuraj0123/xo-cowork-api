"""Hermetic test foundation — three layers of filesystem isolation.

This repo's code reaches for the user's home directory in two very different
ways, and only one of them is patchable from a fixture:

1. **Per-call env reads.** ``project_layout.xo_projects_root()`` re-reads
   ``XO_PROJECTS_ROOT`` and ``xo_runtime_root()`` re-reads ``XO_RUNTIME_ROOT``
   on *every* call. A ``monkeypatch.setenv`` in a fixture reaches these fine.

2. **Import-time bindings.** A second, larger class of paths freezes
   ``Path.home()`` into a module constant (or into a def-time default argument)
   the first time the module is imported. Once that has happened no fixture can
   ever move it — the value is already a concrete absolute path. The known set
   at the time of writing::

       services/cowork_agent/visualizer/flock.py            _LOCKS_ROOT
       services/cowork_agent/visualizer/ingest/jsonl_tail.py DEFAULT_OFFSETS_PATH
                                                            (also frozen into
                                                            OffsetStore.__init__'s
                                                            default argument)
       services/cowork_agent/xo_cowork_state.py             STATE_DIR / STATE_FILE
       services/cowork_agent/registry/settings.py           CLAUDE_COWORK_DIR
       services/cowork_agent/registry/agent_env.py          ENV_FILE
       services/cowork_agent/registry/agent_registry.py     _MANIFESTS (module
                                                            cache; every manifest
                                                            path is expanduser'd
                                                            once and reused)
       services/cowork_agent/adapters/openclaw/paths.py     OPENCLAW_DIR, AGENTS_DIR,
                                                            OPENCLAW_JSON,
                                                            DEFAULT_OPENCLAW_WORKSPACE
       services/cowork_agent/adapters/antigravity/paths.py  AGY_HOME + 5 derived
       services/cowork_agent/adapters/claude_code/
           visualizer_source.py                             _CLAUDE_PROJECTS_DIR,
                                                            _CLAUDE_SESSIONS_DIR
           remote_control.py                                _CLAUDE_HOME, _GLOBAL_CONFIG
       services/cowork_agent/adapters/openclaw/
           visualizer_source.py                             _OPENCLAW_AGENTS_DIR
       services/cowork_agent/adapters/hermes/
           visualizer_source.py                             _OFFSETS_FILE

   The only lever that reaches all of them is ``HOME`` itself, and it has to be
   rewritten **before the first import**. That is what Layer 1 below does, at
   conftest module-import time — conftest is imported before any test module,
   so this runs before anything drags the app in.

So isolation is three layers:

* **Layer 1 (module scope, below):** rewrite ``HOME`` + the XDG vars + both XO
  roots + every agent-home escape hatch to a session tempdir, and put the repo
  root on ``sys.path``. Runs once, at import, before the app exists.
* **Layer 2 (:func:`_sandbox_roots`, autouse):** re-point both XO roots at this
  test's ``tmp_path`` so tests never share on-disk state with each other.
* **Layer 3 (:func:`_no_real_home_writes`, autouse):** an escape detector. Stat
  the *real* home's XO/agent directories before and after every test and fail on
  any change. Layers 1 and 2 are a claim; this is the proof.

Helper modules live in ``tests/fakes.py`` and are imported as a top-level
module (``from fakes import ...``) — ``tests/`` has no ``__init__.py``, so
pytest's default prepend import mode puts this directory on ``sys.path``.
"""

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1 — import-time sandbox. MUST stay at the very top of this file.
#
# Nothing above this block may import the application (directly or transitively):
# the constants listed in the module docstring bind Path.home() the instant their
# module is first imported, and no fixture can move them afterwards.
# ─────────────────────────────────────────────────────────────────────────────

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# The REAL home, captured before the rewrite below — Layer 3 needs it to know
# which directories must stay untouched. Resolved via expanduser (not
# Path.home()) purely for symmetry with how the app resolves its own paths.
REAL_HOME = Path(os.path.expanduser("~")).resolve()

# One throwaway home for the whole session. Layer 2 narrows the two XO roots
# further, per test; everything else (agent homes, ~/.xo-cowork, offsets, locks)
# lands here and is torn down at interpreter exit.
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="xo-cowork-tests-home-")).resolve()
atexit.register(shutil.rmtree, _SESSION_HOME, True)  # True == ignore_errors

# ``server.py`` calls ``load_dotenv()``, which defaults to ``override=False`` —
# so anything already in os.environ wins over the repo's .env. That is what makes
# this block authoritative rather than advisory, and it matters: ``.env.example``
# ships a real ``XO_PROJECTS_ROOT=/home/coder/xo-projects``, so a developer who
# copied it to .env would otherwise have the suite writing into their live
# projects tree. Setting the value here is what stops that.
_SANDBOX_ENV = {
    # The master lever: every ``Path.home()`` / ``expanduser("~")`` in the tree,
    # including the ones frozen at import time, resolves through this.
    "HOME": str(_SESSION_HOME),
    # routers/auth/codex_setup.py reads both XDG vars directly when probing for
    # a codex auth.json, and falls back to ~/.local/share and ~/.config.
    "XDG_CONFIG_HOME": str(_SESSION_HOME / ".config"),
    "XDG_DATA_HOME": str(_SESSION_HOME / ".local" / "share"),
    # The two per-call roots (re-read on every call; Layer 2 narrows them again).
    "XO_PROJECTS_ROOT": str(_SESSION_HOME / "xo-projects"),
    "XO_RUNTIME_ROOT": str(_SESSION_HOME / ".xo"),
    # Agent-home escape hatches. HOME already covers their defaults; these are
    # set so that an env var inherited from the developer's shell (a real
    # CODEX_HOME, say) cannot punch back out of the sandbox.
    "CLAUDE_COWORK_ROOT": str(_SESSION_HOME / "claude-cowork"),
    "CLAUDE_PROJECTS_DIR": str(_SESSION_HOME / ".claude" / "projects"),
    "CODEX_HOME": str(_SESSION_HOME / ".codex"),
    "OPENCLAW_HOME": str(_SESSION_HOME / ".openclaw"),
    "OPENCLAW_AGENTS_DIR": str(_SESSION_HOME / ".openclaw" / "agents"),
    "AI_WORKSPACE_ROOT": str(_SESSION_HOME),
    "XO_RC_DIR": str(_SESSION_HOME),
    "XO_JSON_PATH": str(_SESSION_HOME / "xo-projects" / ".xo" / "xo.json"),
    # Not home-bound, but repo- or machine-bound writes we still want contained:
    # the usage-sync watermark defaults into the repo's data/ dir, and
    # codex_setup writes credentials into routers/.env unless DOTENV_PATH says
    # otherwise.
    "USAGE_SYNC_STATE_FILE": str(_SESSION_HOME / "usage_sync_state.json"),
    "DOTENV_PATH": str(_SESSION_HOME / "dotenv" / ".env"),
    # Read-only sqlite probe, but ~/.argus is the default — keep it in-sandbox.
    "ARGUS_DB": str(_SESSION_HOME / ".argus" / "argus.db"),
}
os.environ.update(_SANDBOX_ENV)

# NOT overridden on purpose:
#   SPACE_DIR  — defaults to the repo's own space_ui/, not to home. Pointing it
#                at the sandbox would break the /space static mount for no gain.
#   AGENT_NAME — left to .env / the caller. The tick suite reaches the Watcher
#                through the __new__ seam (see fakes.make_watcher) and therefore
#                never resolves an agent at all; the parity matrix sets
#                AGENT_NAME explicitly in each subprocess.

# pytest's prepend import mode puts ``tests/`` on sys.path (no __init__.py here),
# but not the repo root — and ``venv/bin/pytest`` does not add the cwd either.
# Without this, ``import server`` / ``import services...`` fails.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ─────────────────────────────────────────────────────────────────────────────
# End of Layer 1. Application imports are safe from here on — but keep them
# lazy (inside fixtures) so a future import-sorting pass cannot hoist one above
# the block and silently break the isolation.
# ─────────────────────────────────────────────────────────────────────────────

import json  # noqa: E402
import uuid  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Iterator  # noqa: E402

import pytest  # noqa: E402


# ── Layer 2 — per-test roots ─────────────────────────────────────────────────


@dataclass(frozen=True)
class XoRoots:
    """The two tier roots for one test.

    ``projects`` is the SYNCED tier (``<projects>/<name>/.xo/``), ``runtime`` is
    the machine-local tier (``<runtime>/<pid>/``). They are deliberately separate
    trees: an artefact appearing under ``projects`` that belongs under ``runtime``
    is exactly the tier-split regression the suite exists to catch.
    """

    projects: Path
    runtime: Path


@pytest.fixture
def xo_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> XoRoots:
    """Point ``XO_PROJECTS_ROOT`` / ``XO_RUNTIME_ROOT`` at this test's tmp_path.

    Both are re-read from the environment on every call in
    ``services/cowork_agent/project_layout.py``, so setting them here is enough —
    no module reloading required. Requested implicitly by every test through the
    autouse :func:`_sandbox_roots`; request it by name when you need the paths.
    """
    projects = tmp_path / "xo-projects"
    runtime = tmp_path / ".xo"
    projects.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XO_PROJECTS_ROOT", str(projects))
    monkeypatch.setenv("XO_RUNTIME_ROOT", str(runtime))
    return XoRoots(projects=projects, runtime=runtime)


@pytest.fixture(autouse=True)
def _sandbox_roots(_no_real_home_writes, xo_roots: XoRoots) -> XoRoots:
    """Autouse wrapper so no test can forget the per-test roots.

    Fixtures are cached per test, so this and an explicit ``xo_roots`` request
    resolve to the same instance.

    Depends on :func:`_no_real_home_writes` purely for **ordering**: naming it
    first makes the escape detector the outermost fixture, so its "before"
    snapshot is taken before anything else runs and its "after" check happens
    after every other teardown. Without the dependency the order would fall out
    of declaration order in this file, which is too easy to break by moving a
    function.
    """
    return xo_roots


# ── Layer 3 — escape detector ────────────────────────────────────────────────

# Directories in the REAL home that this suite must never create or modify.
# Names only — resolved against REAL_HOME (captured before Layer 1 rewrote HOME).
_PROTECTED_REAL_PATHS = tuple(
    REAL_HOME / name
    for name in (
        ".xo",            # runtime tier home
        "xo-projects",    # synced tier home
        ".xo-cowork",     # watcher offsets + flock sentinels
        ".claude",        # claude_code agent home
        ".openclaw",      # openclaw agent home
        ".hermes",        # hermes agent home
        ".codex",         # codex agent home
        ".gemini",        # antigravity agent home (~/.gemini/antigravity-cli)
        "claude-cowork",  # CLAUDE_COWORK_ROOT default
    )
)


def _snapshot_protected() -> dict[Path, tuple[bool, int | None]]:
    """``{path: (exists, st_mtime_ns)}`` for every protected real-home path.

    A directory's mtime changes when an entry is added, removed or renamed
    inside it — which is precisely the "a test wrote here" signal. A path that
    does not exist snapshots as ``(False, None)``, so *creating* it is caught
    too (that is the most likely escape: a helper that mkdirs on read).
    """
    out: dict[Path, tuple[bool, int | None]] = {}
    for path in _PROTECTED_REAL_PATHS:
        try:
            out[path] = (True, path.stat().st_mtime_ns)
        except OSError:
            out[path] = (False, None)
    return out


@pytest.fixture(autouse=True)
def _no_real_home_writes() -> Iterator[None]:
    """Fail any test that touches the real home.

    Layers 1 and 2 are supposed to make this impossible. Do not trust them —
    assert it. Several helpers in this tree (``xo_projects_root``,
    ``xo_runtime_root``, the flock sentinel dir) ``mkdir`` on *read*, so a single
    missed patch turns an innocent-looking read into a write in the developer's
    live tree.
    """
    before = _snapshot_protected()
    yield
    after = _snapshot_protected()

    # ``.get`` rather than ``[]``: a test may legitimately have extended
    # _PROTECTED_REAL_PATHS, in which case the key is absent from ``before`` and
    # a missing/present difference is exactly what we want reported.
    escaped = [
        f"  {path}: {before.get(path)} -> {after.get(path)}"
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    ]
    if escaped:
        raise AssertionError(
            "test escaped the sandbox and touched the real home "
            "(entries are (exists, st_mtime_ns) before -> after):\n"
            + "\n".join(escaped)
            + "\n\nEither the code under test binds a home path at import time "
            "(add it to the Layer 1 env block in tests/conftest.py, or patch the "
            "module constant directly), or something outside pytest wrote to "
            "these paths while the test ran."
        )


# ── Shared project scaffolding ───────────────────────────────────────────────


@dataclass(frozen=True)
class ScaffoldedProject:
    """A project on disk, with both of its tier directories pre-resolved.

    ``xo`` is the SYNCED contract dir (``project.json``, ``todos.json``,
    ``peers.json``, ``agent.json``); ``runtime`` and ``sessions`` are the
    machine-local tier, keyed by ``pid`` and living outside the project tree.
    """

    name: str
    pid: str
    dir: Path
    xo: Path
    runtime: Path
    sessions: Path


@pytest.fixture
def scaffolded_project(xo_roots: XoRoots):
    """Factory: build a real project tree under this test's projects root.

    Uses the production scaffolder (``project_layout.scaffold_project``) so the
    tree matches what the API actually creates — bundled template files, a
    ``.xo/`` holding only the synced contract, and no ``sessions/`` anywhere in
    the project tree.

    The template ships ``project.json`` with ``_template: true`` and a null
    ``pid``; production mints the pid on the watcher's first identity fill. The
    factory mints it up front by default so a test knows where the runtime tier
    will land without having to run a tick first. Pass ``pid=None`` to keep the
    unfilled template shape (for tests that exercise ``fill_identity`` itself);
    the returned ``runtime``/``sessions`` paths then reflect the folder-name
    fallback that ``project_layout.runtime_key`` applies in that window.

        proj = scaffolded_project("demo")
        assert proj.sessions.parent == proj.runtime
    """

    # Imported here, not at module scope: Layer 1 must have rewritten the
    # environment before project_layout is first imported, and a lazy import
    # makes that ordering impossible to break by moving lines around.
    from services.cowork_agent import project_layout
    from services.cowork_agent.helpers import normalize_agent_id

    def _make(
        name: str = "demo",
        *,
        pid: str | None = "",
        display_name: str | None = None,
        description: str | None = None,
    ) -> ScaffoldedProject:
        # The folder name is the project id everywhere in project_layout, and it
        # is NOT the same as the metadata's "name" field: the template ships
        # `"name": null` and `_upsert_metadata` only fills keys that are *absent*,
        # so a freshly scaffolded project.json still has a null name until the
        # watcher's identity fill runs. Derive the id the way the layer does.
        project_id = normalize_agent_id(name)
        project_layout.scaffold_project(
            project_id, display_name=display_name, description=description
        )
        xo = project_layout.xo_dir(project_id)

        if pid is not None:
            # "" (the default) means "mint a fresh uuid"; an explicit string is
            # used as-is; None means "leave the template unfilled".
            # This mirrors what `sinks/project_json.fill_identity` does on the
            # watcher's first sight of the project, so a test that needs a
            # settled pid does not have to run a tick to get one.
            minted = pid or str(uuid.uuid4())
            meta_path = project_layout.project_metadata_path(project_id)
            current = json.loads(meta_path.read_text(encoding="utf-8"))
            current.pop("_template", None)
            current["pid"] = minted
            current["schema"] = current.get("schema") or 1
            current["name"] = current.get("name") or project_id
            current["owner_user_id"] = current.get("owner_user_id") or "local"
            meta_path.write_text(
                json.dumps(current, indent=2) + "\n", encoding="utf-8"
            )

        return ScaffoldedProject(
            name=project_id,
            pid=project_layout.runtime_key(project_id),
            dir=project_layout.project_dir(project_id),
            xo=xo,
            runtime=project_layout.project_runtime_dir(project_id),
            sessions=project_layout.sessions_dir(project_id),
        )

    return _make
