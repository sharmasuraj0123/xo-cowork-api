"""The chokepoint guard — no file may hand-build an xo tier path.

``services/cowork_agent/project_layout.py`` owns the synced-vs-runtime tier
decision. ``sessions/`` is machine-local runtime state and lives at
``~/.xo/<pid>/sessions/``, *outside* every project tree; ``<project>/.xo/`` holds
only the synced contract. Every reader and writer is supposed to resolve through
``project_layout.sessions_dir(name)`` so that decision is made in exactly one
place.

**Why this file exists.** That rule was broken four times, in two adapters, and
each break was silent — code that hand-builds ``<project>/.xo/sessions`` reads a
location nothing writes any more, so the symptom is an empty list, not a crash::

    services/cowork_agent/adapters/codex/adapter.py        find_session_key_for_session_id
    services/cowork_agent/adapters/codex/sessions.py       _persist_session_directory
    services/cowork_agent/adapters/antigravity/adapter.py  find_session_key_for_session_id
    services/cowork_agent/adapters/antigravity/sessions.py _persist_session_directory

All four arrived the same way: an adapter was forked, and the fork copied its
parent's hardcoded paths. A convention cannot survive that. A test can.

**Why AST and not grep.** ``docs/sync-compatible-readiness.md`` finding **B4**
records that the grep this replaces returned 9 hits, *all* of them comments and
docstrings — the invariant held and the tool lied. A grep-shaped guard that cries
wolf nine times gets ignored, which is worse than no guard. So the detector
parses each file and looks only at expressions that can actually build a path,
with docstrings stripped (:func:`_docstring_constant_ids`). Comments never reach
the AST at all.

**The allowlist is data with reasons** (:data:`ALLOWLIST`), scoped per segment: a
file allowed to say ``"sessions"`` because it addresses its agent's own native
store is still guarded against ``".xo"``. :func:`test_allowlist_is_not_stale`
asserts every declared entry still produces the hit it claims to excuse, so the
list can only ever shrink.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The task's core trees. ``server.py`` is not scanned: it wires routers and holds
# no path math (verified — it contains none of these shapes).
SCANNED_TREES = ("services", "routers")

# The two directory names that carry a tier meaning in this codebase. ``.xo`` is
# unambiguous — it is *only* ever the xo contract/runtime directory. ``sessions``
# is deliberately included even though several agents also have a native store by
# that name, because the four real breaks were exactly a fork copying a
# ``sessions`` join it did not understand.
TIER_SEGMENTS = frozenset({".xo", "sessions"})

# The composite form, for string literals that embed the whole thing rather than
# joining segment by segment.
EMBEDDED_TIER_PATHS = (".xo/sessions", ".xo\\sessions")


# ── The detector ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hit:
    """One expression that hand-builds a tier path segment."""

    rel_path: str
    line: int
    segment: str
    shape: str
    source_line: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return (
            f"{self.rel_path}:{self.line}\n"
            f"      segment {self.segment!r} ({self.shape})\n"
            f"      | {self.source_line}"
        )


@dataclass(frozen=True)
class Allowance:
    """Permission for one file to name *some* tier segments, and why.

    Scoped per segment on purpose. ``openclaw/store.py`` legitimately joins
    ``"sessions"`` onto ``~/.openclaw/agents/<id>/``; that must not also buy it
    the right to join ``".xo"``, which would be the tier bypass this file exists
    to prevent.
    """

    segments: frozenset[str]
    reason: str


def _docstring_constant_ids(tree: ast.Module) -> set[int]:
    """``id()`` of every string Constant that is documentation, not code.

    Two passes, because they catch different things:

    * ``ast.get_docstring`` on Module/Class/Function — the canonical docstring
      slot, and the only one ``ast`` itself recognises.
    * every bare ``Expr(Constant(str))`` anywhere — the "second paragraph" string
      that follows a real docstring, and the string-literal-as-comment idiom.
      This is what makes the B4 false positives (a ``.xo/sessions`` mention in
      prose) invisible to the scan.
    """
    doc_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            if ast.get_docstring(node, clean=False) is not None:
                doc_ids.add(id(node.body[0].value))  # type: ignore[attr-defined]
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                doc_ids.add(id(node.value))
    return doc_ids


def _dotted_name(node: ast.AST) -> str | None:
    """Render ``os.path.join`` from its Attribute/Name chain, else ``None``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


# ``join`` callables that treat their arguments as path segments. A bare ``join``
# name covers ``from os.path import join``; ``"/".join(...)`` is an Attribute on a
# Constant and so renders to None here, which is what we want — it is a string
# operation, not a path build.
_PATH_JOINS = frozenset(
    {"os.path.join", "path.join", "posixpath.join", "ntpath.join", "join"}
)


def _tier_segments_in_literal(value: str) -> set[str]:
    """Tier segments appearing as whole *path components* of ``value``.

    Split with both separators so ``"sessions/sessionslist.json"`` and
    ``".xo\\sessions"`` are both decomposed. Matching whole components (rather
    than substrings) is what keeps route strings like ``/api/sessions/{id}``
    from registering — they are not built into filesystem paths.
    """
    parts = set(PurePosixPath(value).parts) | set(PureWindowsPath(value).parts)
    return {p for p in parts if p in TIER_SEGMENTS}


def scan_source(source: str, rel_path: str) -> list[Hit]:
    """Return every hand-built tier path expression in ``source``.

    Four shapes, all of which can produce a filesystem path:

    1. ``pathlib`` division — ``base / ".xo" / "sessions"``. This is the shape of
       all four historical breaks.
    2. A string literal embedding the composite path (``".xo/sessions"``).
    3. ``os.path.join(base, ".xo", "sessions")``.
    4. ``Path("sessions/sessionslist.json")`` — the same join expressed as a
       relative ``Path`` constant. Not one of the three shapes the audit named,
       but an obvious equivalent hole, and it has a live instance today.
    """
    tree = ast.parse(source, filename=rel_path)
    doc_ids = _docstring_constant_ids(tree)
    lines = source.splitlines()
    hits: list[Hit] = []

    def add(node: ast.AST, segment: str, shape: str) -> None:
        line = getattr(node, "lineno", 0)
        text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        hits.append(
            Hit(
                rel_path=rel_path,
                line=line,
                segment=segment,
                shape=shape,
                source_line=text,
            )
        )

    def literal_args(call: ast.Call):
        """Constant string args of a call, unwrapping a single list/tuple arg."""
        for arg in call.args:
            if isinstance(arg, (ast.List, ast.Tuple)):
                yield from (
                    e
                    for e in arg.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                )
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield arg

    for node in ast.walk(tree):
        # 1. Path-division chains.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            for side in (node.left, node.right):
                if (
                    isinstance(side, ast.Constant)
                    and isinstance(side.value, str)
                    and id(side) not in doc_ids
                    and side.value in TIER_SEGMENTS
                ):
                    add(side, side.value, "path-division chain")

        # 2. String literals embedding the composite tier path.
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in doc_ids and any(
                embedded in node.value for embedded in EMBEDDED_TIER_PATHS
            ):
                add(node, ".xo/sessions", "embedded path literal")

        # 3 & 4. Calls that turn their string arguments into path segments.
        elif isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name in _PATH_JOINS:
                shape = "os.path.join"
            elif name is not None and name.split(".")[-1] == "Path":
                shape = "Path() literal"
            else:
                continue
            for arg in literal_args(node):
                if id(arg) in doc_ids:
                    continue
                for segment in sorted(_tier_segments_in_literal(arg.value)):
                    add(arg, segment, shape)

    return sorted(hits, key=lambda h: (h.rel_path, h.line, h.segment))


def _python_files() -> list[Path]:
    return sorted(
        p
        for tree in SCANNED_TREES
        for p in (REPO_ROOT / tree).rglob("*.py")
        if "__pycache__" not in p.parts
    )


def scan_repo() -> list[Hit]:
    """Every hand-built tier path expression under :data:`SCANNED_TREES`."""
    hits: list[Hit] = []
    for path in _python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        hits.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    return hits


# ── The allowlist ────────────────────────────────────────────────────────────
#
# Reasons that apply to several files are named once so the entries stay
# readable and a whole class can be re-argued in one place.

_OWNS_THE_DECISION = (
    "project_layout.py IS the chokepoint — the one file allowed to know the "
    "on-disk layout. Every other tier path must resolve through its helpers."
)

_XO_JSON_HOLDOUT = (
    "~/xo-projects/.xo/xo.json is the frontend manifest, not project or runtime "
    "state. It is a legitimately different file that did NOT move in the tier "
    "split, and these three sites read AND write the same place, so there is no "
    "split-brain. If it is ever reclassified as workspace runtime it needs "
    "project_layout.workspace_runtime_dir() plus a migration."
)

_SYNCED_AGENT_JSON = (
    "<project>/.xo/agent.json is part of the SYNCED contract "
    "({project,todos,peers,agent}.json), so it belongs in the project tree. It "
    "is not a runtime-tier bypass. Note this excuses '.xo' only: a 'sessions' "
    "join in this file would still fail."
)

_NATIVE_SESSION_STORE = (
    "Addresses the AGENT'S OWN native session store (~/.openclaw/agents/<id>/"
    "sessions, $CODEX_HOME/sessions, ~/.claude/sessions, <hermes profile>/"
    "sessions) — a different filesystem entirely, which the tier split never "
    "touched. Excuses 'sessions' only."
)

_WORKSPACE_TIER = (
    "Joins 'sessions' onto project_layout.workspace_runtime_dir(), i.e. it is "
    "already inside the runtime tier and got there through the chokepoint. The "
    "workspace tier has no per-project pid, so it has no sessions_dir() helper "
    "to use instead."
)

ALLOWLIST: dict[str, Allowance] = {
    # ── The owner ──
    "services/cowork_agent/project_layout.py": Allowance(
        frozenset({".xo", "sessions"}), _OWNS_THE_DECISION
    ),
    # ── xo.json: the frontend manifest, which did not move ──
    "services/xo_manifest.py": Allowance(frozenset({".xo"}), _XO_JSON_HOLDOUT),
    "services/cowork_agent/providers_status_lib.py": Allowance(
        frozenset({".xo"}), _XO_JSON_HOLDOUT
    ),
    "services/cowork_agent/adapters/antigravity/providers_status.py": Allowance(
        frozenset({".xo"}), _XO_JSON_HOLDOUT
    ),
    # ── agent.json: synced contract, so the project tree is correct ──
    "services/cowork_agent/adapters/claude_code/agents.py": Allowance(
        frozenset({".xo"}), _SYNCED_AGENT_JSON
    ),
    "services/cowork_agent/adapters/codex/agents.py": Allowance(
        frozenset({".xo"}), _SYNCED_AGENT_JSON
    ),
    "services/cowork_agent/adapters/antigravity/agents.py": Allowance(
        frozenset({".xo"}), _SYNCED_AGENT_JSON
    ),
    # ── Each agent's own native store, which is not an xo tier at all ──
    "services/cowork_agent/adapters/claude_code/visualizer_source.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/codex/paths.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/hermes/agents.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/agents.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/direct_stream.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/sessions.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/store.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/transcript.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/usage.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/visualizer_source.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    # ── Workspace tier: already through the chokepoint ──
    "services/cowork_agent/visualizer/workspace/sessions_augment.py": Allowance(
        frozenset({"sessions"}), _WORKSPACE_TIER
    ),
    "services/cowork_agent/visualizer/workspace/sessionslist.py": Allowance(
        frozenset({"sessions"}), _WORKSPACE_TIER
    ),
    # ── One-offs ──
    "services/cowork_agent/visualizer/migrate.py": Allowance(
        frozenset({"sessions"}),
        "The migration is the one thing that MUST know the pre-split location: "
        "it reads <project>/.xo/sessions in order to move it into the runtime "
        "tier. Deleting this join would strand every pre-split install.",
    ),
    "services/cowork_agent/visualizer/sinks/sessions_augment.py": Allowance(
        frozenset({"sessions"}),
        "Path('sessions/...') here is a RELATIVE sub-path joined onto a tier "
        "root the caller supplies (watcher.py passes the runtime dir), so the "
        "tier decision is still made upstream by project_layout.",
    ),
}


def _format(hits: list[Hit]) -> str:
    return "\n".join(f"  - {hit}" for hit in hits)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_no_unallowlisted_tier_paths() -> None:
    """No file under services/ or routers/ may hand-build an xo tier path.

    Pins the chokepoint: ``project_layout.sessions_dir(name)`` (project_layout.py:258)
    is the only sanctioned way to reach a project's sessions directory, and
    ``project_layout.xo_dir(name)`` (:254) the only way to reach its synced
    ``.xo/``. Guards against the four historical breaks listed in the module
    docstring — codex/antigravity ``adapter.py`` and ``sessions.py``, all of
    which hand-built ``<project>/.xo/sessions`` and so read a directory nothing
    writes any more.
    """
    offenders = [
        hit
        for hit in scan_repo()
        if hit.segment not in ALLOWLIST.get(hit.rel_path, Allowance(frozenset(), "")).segments
    ]
    assert not offenders, (
        f"{len(offenders)} expression(s) hand-build an xo tier path segment:\n"
        + _format(offenders)
        + "\n\nThe tier decision belongs to services/cowork_agent/project_layout.py "
        "and nowhere else:\n"
        "  * a project's sessions dir  -> project_layout.sessions_dir(project_name)\n"
        "  * a project's synced .xo/   -> project_layout.xo_dir(project_name)\n"
        "  * per-project runtime       -> project_layout.project_runtime_dir(name)\n"
        "  * workspace runtime         -> project_layout.workspace_runtime_dir()\n\n"
        "sessions/ is machine-local and lives at ~/.xo/<pid>/sessions/, OUTSIDE "
        "the project tree — code that builds <project>/.xo/sessions reads a "
        "location nothing writes, which fails silently as an empty result.\n\n"
        "If this really is your agent's OWN native store (e.g. ~/.openclaw/"
        "agents/<id>/sessions) and not an xo tier path, add an ALLOWLIST entry "
        "in this file naming the segment and saying why."
    )


@pytest.mark.parametrize("rel_path", sorted(ALLOWLIST))
def test_allowlist_is_not_stale(rel_path: str) -> None:
    """Every allowlisted (file, segment) must still produce a hit.

    Makes :data:`ALLOWLIST` a ratchet: once a file stops hand-building a segment
    the entry has to be deleted, so the list can only shrink. Without this an
    exception outlives the code that needed it and silently re-opens the hole
    for whoever edits that file next.
    """
    path = REPO_ROOT / rel_path
    assert path.is_file(), (
        f"ALLOWLIST names {rel_path}, which no longer exists. Delete the entry."
    )

    allowance = ALLOWLIST[rel_path]
    found = {
        hit.segment
        for hit in scan_source(path.read_text(encoding="utf-8"), rel_path)
    }
    unused = sorted(allowance.segments - found)
    assert not unused, (
        f"{rel_path} no longer hand-builds {unused} — the ALLOWLIST entry is "
        f"stale. Narrow or delete it.\nRecorded reason: {allowance.reason}"
    )


def test_detector_flags_a_hand_built_sessions_path() -> None:
    """The detector bites on the exact expression that broke four times.

    Phase A already fixed the real sites, so a green run of
    :func:`test_no_unallowlisted_tier_paths` proves nothing on its own — the
    guard could be scanning nothing at all and still pass. This feeds it the
    pre-fix source of ``codex/adapter.py:find_session_key_for_session_id`` and
    asserts it is caught, without reverting anything.
    """
    source = (
        "def find_session_key_for_session_id(session_id):\n"
        "    for entry in xo_projects_root().iterdir():\n"
        '        sessions_base = entry / ".xo" / "sessions"\n'
        '        index = sessions_base / "sessionslist.json"\n'
        "    return None\n"
    )
    hits = scan_source(source, "fake/codex/adapter.py")
    assert {hit.segment for hit in hits} == {".xo", "sessions"}
    assert all(hit.line == 3 for hit in hits)


def test_detector_ignores_the_chokepoint_helper() -> None:
    """The sanctioned form produces no hits, so the fix is not itself a failure.

    ``sessions_dir(entry.name)`` is what the four broken sites were rewritten
    to. If the detector flagged it, the only way to a green suite would be to
    allowlist every correct call site — which would invert the guard.
    """
    source = (
        "from services.cowork_agent.project_layout import sessions_dir\n"
        "\n"
        "def find_session_key_for_session_id(session_id):\n"
        "    for entry in xo_projects_root().iterdir():\n"
        "        sessions_base = sessions_dir(entry.name)\n"
        '        index = sessions_base / "sessionslist.json"\n'
        "    return None\n"
    )
    assert scan_source(source, "fake/codex/adapter.py") == []


def test_detector_flags_join_and_literal_forms() -> None:
    """``os.path.join`` and embedded string literals are caught too.

    The same bypass expressed three other ways. Without these, moving one line
    from ``pathlib`` to ``os.path`` — or to an f-string — would silently defeat
    the guard.
    """
    source = (
        "import os\n"
        "from pathlib import Path\n"
        'joined = os.path.join(str(entry), ".xo", "sessions")\n'
        'literal = f"{entry}/.xo/sessions/sessionslist.json"\n'
        'windows = str(entry) + ".xo\\\\sessions"\n'
        'relative = Path(".xo/sessions")\n'
    )
    by_line = {}
    for hit in scan_source(source, "fake/mod.py"):
        by_line.setdefault(hit.line, set()).add(hit.segment)

    assert by_line[3] == {".xo", "sessions"}, "os.path.join form missed"
    assert by_line[4] == {".xo/sessions"}, "f-string literal form missed"
    assert by_line[5] == {".xo/sessions"}, "backslash literal form missed"
    assert by_line[6] == {".xo", "sessions", ".xo/sessions"}, "Path() form missed"


def test_docstrings_and_comments_are_not_code() -> None:
    """Prose mentioning ``.xo/sessions`` must never register.

    This is the whole reason the guard is AST-based. Finding **B4** in
    docs/sync-compatible-readiness.md records that the grep this replaces
    returned 9 hits, every one of them a comment or docstring; a guard with a
    9:0 false-positive ratio is one people learn to ignore. Several of those
    exact strings are still in the tree today (e.g.
    ``adapters/codex/sessions.py`` line 10, which documents the trap), so this
    invariant is load-bearing, not hypothetical.
    """
    source = (
        '"""Module doc mentioning <project>/.xo/sessions as the old spot."""\n'
        "\n"
        '# A comment about .xo/sessions and about / ".xo" / "sessions" chains.\n'
        "\n"
        "class Store:\n"
        '    """Reads .xo/sessions/sessionslist.json."""\n'
        "\n"
        "    def load(self):\n"
        '        """Never build <project>/.xo/sessions by hand."""\n'
        '        "A bare string used as a comment: .xo/sessions."\n'
        "        return None\n"
    )
    assert scan_source(source, "fake/prose.py") == []

    # And the real tree: the live prose mentions must be invisible too.
    documented = REPO_ROOT / "services/cowork_agent/adapters/codex/sessions.py"
    assert ".xo/sessions" in documented.read_text(encoding="utf-8"), (
        "expected codex/sessions.py to still document the pre-split location"
    )
    rel = documented.relative_to(REPO_ROOT).as_posix()
    assert scan_source(documented.read_text(encoding="utf-8"), rel) == []


def test_sessions_dir_resolves_outside_the_project_tree(
    scaffolded_project, xo_roots
) -> None:
    """The behaviour the static guard protects: sessions live in the runtime tier.

    ``project_layout.sessions_dir(name)`` (project_layout.py:258) must land under
    ``~/.xo/<pid>/`` and never inside ``~/xo-projects/<name>/``. Asserting this
    alongside the AST scan is what stops the guard from degrading into a lint
    rule about a location that no longer means anything: if the tier split were
    reverted, this fails even though every hand-built path had been removed.
    """
    proj = scaffolded_project("chokepoint-demo")

    assert proj.sessions.parent == proj.runtime
    assert proj.runtime.parent == xo_roots.runtime
    # The pre-split location the four breaks hand-built. It must NOT be where
    # the chokepoint points, and scaffolding must not create it.
    legacy = proj.dir / ".xo" / "sessions"
    assert proj.sessions != legacy
    assert not legacy.exists()
    assert not list(proj.dir.rglob("sessions")), (
        "no path named 'sessions' may exist anywhere in the project tree — "
        "runtime state must stay outside it for share-safety"
    )
