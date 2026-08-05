"""``project.json`` identity fill — a merge, never a rebuild (regression B1).

``project.json`` has more than one writer. ``project_layout._upsert_metadata``
stamps ``display_name``/``description`` at scaffold time; this sink mints the
``pid`` on the watcher's first sight of the project, typically seconds later.

The bug B1 fixed: the sink built a fresh five-key document from its own field
list and wrote it over the top. Everything it did not know about — the display
name and description the user had just typed into the create-project form — was
gone by the first tick. Nothing errored; the project simply lost its name.

That failure mode is *structural*, not a typo: any sink that rebuilds a shared
document deletes whatever the other writers put there, including fields that do
not exist yet. So the tests below pin two things, and the second is the one that
stops the bug coming back under a new name:

* the concrete fields that were lost (:func:`test_fill_preserves_scaffold_metadata`)
* that an **arbitrary unknown key** survives (:func:`test_fill_preserves_unknown_future_fields`)

``project.json`` is also the SYNCED tier's identity record *and* the key to the
runtime store (``pid`` addresses ``~/.xo/<pid>/``), so a rewrite here is not just
cosmetic data loss — it re-homes a project's entire runtime tier.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

# Safe at module scope — pytest imports conftest (Layer 1) before any test module.
from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.sinks import project_json

IDENTITY_FIELDS = {"schema", "pid", "name", "owner_user_id", "created_at"}


def _read(proj) -> dict:
    return json.loads(
        (proj.xo / "project.json").read_text(encoding="utf-8")
    )


def _write(xo: Path, doc) -> None:
    """Write ``project.json`` verbatim if ``doc`` is a str, else as JSON."""
    xo.mkdir(parents=True, exist_ok=True)
    (xo / "project.json").write_text(
        doc if isinstance(doc, str) else json.dumps(doc, indent=2) + "\n",
        encoding="utf-8",
    )


# ── The regression ───────────────────────────────────────────────────────────


def test_fill_preserves_scaffold_metadata(scaffolded_project) -> None:
    """The identity fill does not delete ``display_name`` / ``description``.

    This is B1 verbatim (``sinks/project_json.py:74``). ``scaffold_project``
    writes both fields when the user creates the project; the watcher's first
    tick used to wipe them, so a project created as "Acme Corp" was called
    "acme" from the first second onwards.

    Also pinned here: the fill is what hands the runtime tier its permanent key.
    Before it, ``runtime_key`` uses the folder-name fallback; after it, the pid.
    """
    proj = scaffolded_project(
        "acme", pid=None, display_name="Acme Corp", description="widgets"
    )
    assert _read(proj)["_template"] is True, "precondition: template not yet filled"
    assert project_layout.runtime_key(proj.name) == proj.name

    assert project_json.fill_identity(proj.xo, proj.name) is True

    doc = _read(proj)
    assert doc["display_name"] == "Acme Corp"
    assert doc["description"] == "widgets"

    # ...and the fill itself did happen: a real uuid pid, template flag cleared.
    assert uuid.UUID(doc["pid"])
    assert "_template" not in doc
    assert doc["name"] == proj.name
    assert doc["created_at"]

    # The runtime store is now addressed by the minted pid, not the folder name.
    assert project_layout.runtime_key(proj.name) == doc["pid"]


def test_fill_preserves_unknown_future_fields(scaffolded_project) -> None:
    """A key this sink has never heard of survives the fill.

    The generalisation of B1, and the assertion that actually prevents a
    recurrence: preserving ``display_name`` by adding it to a hardcoded field
    list would fix the reported bug and leave the next contract addition just as
    exposed. ``peers_schema`` here stands in for "any field a later version, or
    another writer, adds".
    """
    proj = scaffolded_project("demo", pid=None)
    current = _read(proj)
    current["peers_schema"] = 2
    current["some_future_block"] = {"nested": ["values"]}
    _write(proj.xo, current)

    assert project_json.fill_identity(proj.xo, proj.name) is True

    doc = _read(proj)
    assert doc["peers_schema"] == 2
    assert doc["some_future_block"] == {"nested": ["values"]}
    # Nothing was invented either — the sink adds exactly the identity fields.
    assert set(doc) - {"peers_schema", "some_future_block", "display_name",
                       "description"} == IDENTITY_FIELDS


def test_explicit_values_always_win(scaffolded_project) -> None:
    """A value already on disk is never overwritten — only ``_template`` is dropped.

    The fill defaults with ``or``, so it treats the template's nulls as missing
    but leaves anything real alone (``sinks/project_json.py:79-83``). ``schema``
    is in this set on purpose: a sink that reset a future ``schema: 2`` back to
    ``1`` would be the same class of bug in miniature — the identity writer
    silently downgrading a contract it does not understand.
    """
    proj = scaffolded_project("demo", pid=None)
    explicit = {
        "schema": 2,
        "_template": True,
        "pid": "already-mine",
        "name": "explicit-name",
        "owner_user_id": "someone",
        "created_at": "1999-01-01T00:00:00Z",
    }
    _write(proj.xo, explicit)

    assert project_json.fill_identity(proj.xo, proj.name) is True

    assert _read(proj) == {k: v for k, v in explicit.items() if k != "_template"}


def test_pid_survives_a_partially_filled_template(scaffolded_project) -> None:
    """A pid minted before the flag was cleared is kept, not re-minted.

    The two conditions are independent: the sink runs while ``_template`` is set
    *or* while ``pid`` is missing (``sinks/project_json.py:67``). A crash between
    minting the pid and clearing the flag leaves exactly this state, and
    re-minting would strand every runtime artefact already written under the old
    ``~/.xo/<pid>/``.
    """
    proj = scaffolded_project("demo", pid=None)
    _write(proj.xo, {"schema": 1, "_template": True, "pid": "minted-earlier",
                     "name": None, "owner_user_id": None, "created_at": None})

    assert project_json.fill_identity(proj.xo, proj.name) is True

    doc = _read(proj)
    assert doc["pid"] == "minted-earlier"
    assert "_template" not in doc
    assert project_layout.runtime_key(proj.name) == "minted-earlier"


# ── Idempotence ──────────────────────────────────────────────────────────────


def test_fill_is_idempotent_to_the_byte(scaffolded_project) -> None:
    """A second fill reports no change and writes nothing.

    The watcher calls this on **every** tick for every project
    (``visualizer/watcher.py:167``), roughly once a second. A fill that kept
    rewriting would churn the file's mtime forever — enough on its own to make a
    synced project look permanently dirty to whatever is watching it for changes.
    """
    proj = scaffolded_project("demo", pid=None)
    path = proj.xo / "project.json"

    assert project_json.fill_identity(proj.xo, proj.name) is True
    settled = path.read_bytes()
    settled_mtime = path.stat().st_mtime_ns

    assert project_json.fill_identity(proj.xo, proj.name) is False
    assert project_json.fill_identity(proj.xo, proj.name) is False
    assert path.read_bytes() == settled
    assert path.stat().st_mtime_ns == settled_mtime, "the file was rewritten"


# ── Damage tolerance ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "doc, why",
    [
        pytest.param(None, "there is no project.json", id="missing-file"),
        pytest.param("", "the file is empty", id="empty-file"),
        pytest.param("{ not json", "the JSON is malformed", id="malformed-json"),
        pytest.param('["not", "an", "object"]', "the document is not an object",
                     id="non-object-json"),
        pytest.param({}, "the document is an empty object", id="empty-object"),
    ],
)
def test_fill_recovers_from_an_unreadable_document(tmp_path: Path, doc, why: str) -> None:
    """An unusable ``project.json`` is rebuilt, not propagated as an exception.

    The sink runs inside the watcher's per-project try/except, so raising here
    would skip every *other* sink for the project (``watcher.py:126-135``) — one
    corrupt file would silently stop that project's telemetry entirely. It starts
    clean instead (``sinks/project_json.py:62-65``); there is nothing in an
    unparseable document worth preserving.
    """
    xo = tmp_path / "proj" / ".xo"
    if doc is not None:
        _write(xo, doc)

    assert project_json.fill_identity(xo, "demo") is True, why

    written = json.loads((xo / "project.json").read_text(encoding="utf-8"))
    assert set(written) == IDENTITY_FIELDS
    assert uuid.UUID(written["pid"])
    assert written["name"] == "demo"


def test_fill_creates_a_missing_xo_directory(tmp_path: Path) -> None:
    """The sink writes into a project whose ``.xo/`` does not exist yet.

    The watcher discovers bare directories under ``~/xo-projects/`` too, not just
    scaffolded ones (``visualizer/workspace_index.list_project_ids``), so the
    identity fill is sometimes the very first thing to touch a project.
    """
    xo = tmp_path / "never-scaffolded" / ".xo"
    assert not xo.exists()

    assert project_json.fill_identity(xo, "never-scaffolded") is True

    assert (xo / "project.json").is_file()
    assert json.loads((xo / "project.json").read_text(encoding="utf-8"))["name"] == (
        "never-scaffolded"
    )


def test_owner_defaults_to_local_without_an_authenticated_session(
    scaffolded_project,
) -> None:
    """With no auth state, ``owner_user_id`` is ``"local"`` — never null or absent.

    The schema requires the field, and the watcher runs long before (or entirely
    without) anyone signing in. ``_resolve_user_id`` swallows both an absent
    user id and an import failure of ``routers.auth`` and answers ``"local"``
    (``sinks/project_json.py:42-53``). This test runs in a hermetic sandbox with
    no session, which is exactly that state.
    """
    proj = scaffolded_project("demo", pid=None)

    assert project_json.fill_identity(proj.xo, proj.name) is True
    assert _read(proj)["owner_user_id"] == "local"
