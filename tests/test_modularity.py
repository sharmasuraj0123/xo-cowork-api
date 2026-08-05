"""The modularity invariant — core code may not name a specific agent.

``DEVELOPING.md`` §6 states the rule and then says the AST guard for it "is kept
as local dev tooling, not committed", leaving the invariant to be "upheld in
review". This file is that guard, committed.

**The rule.** This API is a broker. The active backend is resolved from
``AGENT_NAME`` through one seam — ``adapters/loader.py`` — and agent-specific
code lives in exactly three trees::

    services/cowork_agent/adapters/<name>/
    config/agents/<name>/
    config/models/<name>/

Everything else is core, and no core file may name an agent in code.

**Why it is discovered, not listed.** The forbidden names come from
``adapter_registry.list_adapters()``, which globs ``adapters/*/adapter.py``. So
dropping in a sixth adapter automatically (a) makes its own tree exempt and
(b) makes its name forbidden everywhere else — the guard tightens itself, with
no edit here. A hardcoded list would go stale the first time someone forgot.

**Why AST and not grep.** Two reasons, both live in this tree today. Prose is
not code: ``providers_status_lib.py``'s module docstring names three agents
while describing the env cascade, and that is correct documentation. And the
match is **case-sensitive**, so ``CLAUDE_CODE_OAUTH_TOKEN`` — which appears in
core at ``routers/auth/claude_setup_token.py:617`` — is not a violation:
``claude_code`` is the agent, ``CLAUDE_CODE`` is an Anthropic env var, and a
case-insensitive scan cannot tell them apart.

:data:`ALLOWLIST` carries the sanctioned exceptions **with reasons**, and
:func:`test_allowlist_is_not_stale` makes it a ratchet that can only shrink.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Core is "everything that is not an agent-owned tree". Scanning config/ matters:
# config/__init__.py and config/models/__init__.py sit *above* the owned
# subdirectories and are core, so a shortcut like "skip all of config/" would
# blind the guard to them.
SCANNED_TREES = ("services", "routers", "config")
SCANNED_FILES = ("server.py",)

# The three agent-owned trees, as prefixes awaiting an agent name.
OWNED_TREE_PREFIXES = (
    "services/cowork_agent/adapters",
    "config/agents",
    "config/models",
)


def agent_names() -> tuple[str, ...]:
    """The agent names to forbid, discovered from the adapters on disk.

    Imported lazily: ``adapter_registry`` pulls in the adapters package, and
    conftest's Layer 1 must have rewritten ``HOME`` first.
    """
    from services.cowork_agent.registry.adapter_registry import list_adapters

    return tuple(list_adapters())


# ── The detector ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hit:
    """One code token in a core file that names a specific agent."""

    rel_path: str
    line: int
    agent: str
    kind: str
    token: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        shown = self.token if len(self.token) <= 72 else self.token[:69] + "..."
        return f"{self.rel_path}:{self.line}  names {self.agent!r} in {self.kind} {shown!r}"


@dataclass(frozen=True)
class Allowance:
    """Permission for one core file to name agents, and why."""

    reason: str


def is_agent_owned(rel_path: str, names: Iterable[str]) -> bool:
    """True iff ``rel_path`` lives in one of the three agent-owned trees.

    Note the trailing ``/<name>/``: ``adapters/loader.py`` and
    ``adapters/base.py`` are core (they are the seam, not an agent), so only
    ``adapters/<discovered-name>/...`` is exempt.
    """
    return any(
        rel_path.startswith(f"{prefix}/{name}/")
        for prefix in OWNED_TREE_PREFIXES
        for name in names
    )


def _docstring_constant_ids(tree: ast.Module) -> set[int]:
    """``id()`` of every string Constant that is documentation, not code.

    ``ast.get_docstring`` covers the canonical Module/Class/Function slot; the
    bare ``Expr(Constant(str))`` sweep additionally covers trailing prose
    paragraphs and the string-literal-as-comment idiom. Comments proper never
    reach the AST.
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


def code_tokens(tree: ast.Module) -> Iterator[tuple[int, str, str]]:
    """Yield ``(line, kind, token)`` for every identifier-or-literal that is code.

    Deliberately *not* a walk over every string in the file. Only these can
    couple core to an agent:

    * ``Constant`` strings that are not documentation — dispatch keys,
      ``AGENT_NAME`` comparisons, URL prefixes.
    * ``Name.id`` / ``Attribute.attr`` — a symbol whose name is an agent.
    * import module paths, imported names and ``as`` aliases — a core→adapter
      import is the sharpest violation there is.
    * ``def`` / ``class`` names, which are the definition side of the ``Name``
      case (a helper only ever called via ``getattr`` would otherwise hide).
    """
    doc_ids = _docstring_constant_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in doc_ids:
                yield node.lineno, "string", node.value
        elif isinstance(node, ast.Name):
            yield node.lineno, "name", node.id
        elif isinstance(node, ast.Attribute):
            yield node.lineno, "attribute", node.attr
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, "import", alias.name
                if alias.asname:
                    yield node.lineno, "import alias", alias.asname
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.lineno, "import", node.module
            for alias in node.names:
                yield node.lineno, "imported name", alias.name
                if alias.asname:
                    yield node.lineno, "import alias", alias.asname
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.lineno, "definition", node.name


def scan_source(source: str, rel_path: str, names: Sequence[str]) -> list[Hit]:
    """Every agent-naming code token in ``source``.

    Matching is **substring and case-sensitive**. Substring because
    ``codex_setup_router`` and ``services.cowork_agent.adapters.hermes.paths``
    both name an agent without equalling one. Case-sensitive because
    ``CLAUDE_CODE_OAUTH_TOKEN`` and ``CODEX_HOME`` are third-party env vars that
    core is entitled to name — they are not this repo's agent identifiers, which
    are lowercase everywhere.
    """
    tree = ast.parse(source, filename=rel_path)
    seen: set[Hit] = set()
    for line, kind, token in code_tokens(tree):
        for name in names:
            if name in token:
                seen.add(
                    Hit(rel_path=rel_path, line=line, agent=name, kind=kind, token=token)
                )
    return sorted(seen, key=lambda h: (h.rel_path, h.line, h.agent, h.token))


def core_files() -> list[str]:
    """Repo-relative paths of every core Python file."""
    names = agent_names()
    paths = [
        p
        for tree in SCANNED_TREES
        for p in (REPO_ROOT / tree).rglob("*.py")
        if "__pycache__" not in p.parts
    ]
    paths.extend(REPO_ROOT / f for f in SCANNED_FILES)
    rels = sorted(p.relative_to(REPO_ROOT).as_posix() for p in paths)
    return [rel for rel in rels if not is_agent_owned(rel, names)]


def scan_core() -> list[Hit]:
    """Every agent-naming code token across all core files."""
    names = agent_names()
    hits: list[Hit] = []
    for rel in core_files():
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        hits.extend(scan_source(source, rel, names))
    return hits


# ── The allowlist ────────────────────────────────────────────────────────────
#
# DEVELOPING.md §6 blesses four frozen exceptions; the fifth (server.py) is the
# Plane A wiring, which §6's prose covers indirectly by exempting
# `config.models.*`. That exemption is expressed here as a per-file allowance
# rather than a blanket rule on import paths — deliberately stricter, so that a
# *new* core file importing a Plane A model client still fails.

ALLOWLIST: dict[str, Allowance] = {
    "services/cowork_agent/registry/agent_registry.py": Allowance(
        "The one sanctioned core agent-literal: the 'openclaw' safe-boot default "
        "(agent_registry.py:198-206), so the API boots with no env configured. "
        "Named in CLAUDE.md and DEVELOPING.md §6. Deliberate — do not 'fix'."
    ),
    "services/cowork_agent/providers_status_lib.py": Allowance(
        "DEVELOPING.md §6: the /providers/status OAuth keys. 'claude_code' and "
        "'codex' here are the JSON response contract the frontend reads, not "
        "dispatch — renaming them would break the UI, and they must be emitted "
        "whichever agent is active."
    ),
    "routers/cowork_agent/legacy/openclaw_usage.py": Allowance(
        "DEVELOPING.md §6: the legacy /openclaw/usage URL alias. The name is a "
        "frozen public URL kept for backward compatibility; every route binds "
        "the same agent-agnostic handler from routers/cowork_agent/usage.py."
    ),
    "routers/auth/codex_setup.py": Allowance(
        "KNOWN DEBT — allowlisted so the guard lands green and the debt stays "
        "visible; do NOT fix it here. DEVELOPING.md §6 blesses only the "
        "'codex legacy openclaw-gateway credential writes' half. The rest is "
        "recorded in docs/sync-compatible-readiness.md: finding A2 (:788 "
        "`active_backend == \"hermes\"`, plus :379 and :444 — the repo's only "
        "two core->adapter imports) and finding A3 (:787 "
        "`os.getenv(\"AGENT_NAME\", \"openclaw\")`, a second unsanctioned "
        "safe-boot literal). Both are scheduled for Phase 4: move persistence "
        "behind a `codex_auth` capability and resolve the backend through "
        "xo_manifest.resolve_agent_name(). Narrow this entry when they land."
    ),
    "server.py": Allowance(
        "Plane A wiring. The legacy /ask_question* plane selects its client by "
        "AI_PROVIDER from config/models/<name>/, so server.py is the one file "
        "that imports those clients by name (:26 config.models.codex) and "
        "branches on the literal (:300). It also mounts the two legacy routers "
        "blessed above. Plane B dispatch in this file is agent-agnostic."
    ),
}


def _format(hits: list[Hit]) -> str:
    return "\n".join(f"  - {hit}" for hit in hits)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_no_agent_names_in_core() -> None:
    """No core file may name a specific agent in code.

    The invariant from DEVELOPING.md §6 and CLAUDE.md's "Agent-modular
    architecture" section: the active backend is resolved from ``AGENT_NAME``
    through ``services/cowork_agent/adapters/loader.py`` and nowhere else, so
    adding an agent is "drop three folders, zero core edits". Every literal in a
    core file is a place that promise is false.

    A failure here usually means one of: a core→adapter import, an
    ``AGENT_NAME == "<agent>"`` branch, or a dispatch dict keyed by agent name.
    All three have the same fix — a capability module under
    ``adapters/<name>/`` reached via ``load_capability`` / ``try_load_capability``.
    """
    offenders = [hit for hit in scan_core() if hit.rel_path not in ALLOWLIST]
    assert not offenders, (
        f"{len(offenders)} core code token(s) name a specific agent:\n"
        + _format(offenders)
        + "\n\nCore must resolve the backend from AGENT_NAME through the "
        "capability loader:\n"
        "    from services.cowork_agent.adapters.loader import try_load_capability\n"
        "    mod = try_load_capability('<capability>')   # None if the agent lacks it\n\n"
        "Agent-specific code belongs in exactly one of:\n"
        "    services/cowork_agent/adapters/<name>/\n"
        "    config/agents/<name>/\n"
        "    config/models/<name>/\n\n"
        "If this really is a sanctioned exception, add an ALLOWLIST entry in "
        "this file with the reason and a pointer to the decision that blessed it."
    )


@pytest.mark.parametrize("rel_path", sorted(ALLOWLIST))
def test_allowlist_is_not_stale(rel_path: str) -> None:
    """Every allowlisted file must still name an agent.

    Makes :data:`ALLOWLIST` a ratchet. When finding A2/A3 are fixed and
    ``routers/auth/codex_setup.py`` stops naming hermes and openclaw, this test
    fails until the entry is narrowed or deleted — so the debt cannot quietly
    outlive its excuse.
    """
    path = REPO_ROOT / rel_path
    assert path.is_file(), (
        f"ALLOWLIST names {rel_path}, which no longer exists. Delete the entry."
    )

    hits = scan_source(path.read_text(encoding="utf-8"), rel_path, agent_names())
    assert hits, (
        f"{rel_path} no longer names any agent in code — the ALLOWLIST entry is "
        f"stale, delete it.\nRecorded reason: {ALLOWLIST[rel_path].reason}"
    )


def test_agent_names_are_discovered_not_hardcoded() -> None:
    """The forbidden names come from the filesystem, so the guard self-tightens.

    ``adapter_registry.list_adapters()`` (adapter_registry.py:42) globs
    ``adapters/*/adapter.py``. Pinning that the guard consumes exactly that set
    is what makes "adding an agent = drop three folders, zero core edits" a
    tested property rather than a claim: the new name becomes forbidden in core
    the moment its folder lands.
    """
    adapters_dir = REPO_ROOT / "services/cowork_agent/adapters"
    on_disk = {
        p.name
        for p in adapters_dir.iterdir()
        if p.is_dir() and (p / "adapter.py").is_file()
    }
    assert set(agent_names()) == on_disk
    assert len(on_disk) >= 2, (
        "guard is scanning for fewer than two agents — either discovery broke "
        "or the adapters tree is missing; either way it would pass vacuously"
    )


def test_guard_tightens_when_an_agent_is_added() -> None:
    """A name is forbidden in core, and exempt in its own tree, purely by discovery.

    Proves the mechanism end to end on a name that does not exist yet: the same
    source is a violation when the agent is registered and invisible when it is
    not, and its own adapter tree is exempt either way. This is what
    :func:`test_no_agent_names_in_core` would do on the day a sixth adapter
    lands, without waiting for that day.
    """
    source = 'if os.getenv("AGENT_NAME") == "newagent":\n    pass\n'

    unknown = scan_source(source, "routers/cowork_agent/dispatch.py", ["openclaw"])
    assert unknown == [], "an unregistered name must not be flagged"

    registered = scan_source(source, "routers/cowork_agent/dispatch.py", ["newagent"])
    assert [h.token for h in registered] == ["newagent"]

    # ...and the same literal inside the new agent's own tree is fine.
    assert is_agent_owned(
        "services/cowork_agent/adapters/newagent/adapter.py", ["newagent"]
    )
    # The seam itself is core, not owned — it must never name an agent.
    assert not is_agent_owned("services/cowork_agent/adapters/loader.py", ["newagent"])


def test_matching_is_case_sensitive() -> None:
    """``CLAUDE_CODE_OAUTH_TOKEN`` in core is not a violation.

    Guards ``routers/auth/claude_setup_token.py:617``, a core file that names
    Anthropic's env var while remaining fully agent-agnostic. A case-insensitive
    scan flags it, and the only ways out are allowlisting a clean file or
    deleting the guard — so case-sensitivity is what keeps this test credible.
    """
    source = (
        "for key in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'):\n"
        "    env.pop(key, None)\n"
        "home = os.getenv('CODEX_HOME')\n"
    )
    assert scan_source(source, "routers/auth/x.py", ["claude_code", "codex"]) == []

    # The real file, scanned as core: it must not be a violation today.
    rel = "routers/auth/claude_setup_token.py"
    real = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert "CLAUDE_CODE_OAUTH_TOKEN" in real, (
        "expected claude_setup_token.py to still reference the Anthropic env var"
    )
    assert scan_source(real, rel, agent_names()) == []
    assert rel not in ALLOWLIST


def test_docstrings_and_comments_are_not_code() -> None:
    """Prose may name agents; only code tokens count.

    Core documentation legitimately explains per-agent behaviour — e.g.
    ``providers_status_lib.py``'s module docstring names three agents while
    describing the env cascade. Flagging that would push authors to write worse
    comments to appease a test, which is the opposite of the point.
    """
    source = (
        '"""Reads the token from openclaw .env, hermes .env or claude_code os.environ."""\n'
        "\n"
        "# codex and antigravity both use OAuth here.\n"
        "\n"
        "def resolve():\n"
        '    """Falls back to openclaw when AGENT_NAME is unset."""\n'
        '    "Trailing prose about hermes."\n'
        "    return get_active_agent()\n"
    )
    names = ["openclaw", "hermes", "claude_code", "codex", "antigravity"]
    assert scan_source(source, "services/cowork_agent/x.py", names) == []

    # And on the real file whose docstring does exactly this: the only hits must
    # be the blessed OAuth response keys in code, never the prose.
    rel = "services/cowork_agent/providers_status_lib.py"
    hits = scan_source((REPO_ROOT / rel).read_text(encoding="utf-8"), rel, agent_names())
    assert hits and all(hit.line > 100 for hit in hits), (
        "expected only the code-level OAuth keys to register, not the module "
        f"docstring's agent names: {_format(hits)}"
    )
