"""
Session-file I/O over the shared project layout
(`~/xo-projects/*/.xo/sessions/`).

Concerns:
- listing sessions across backends and sorting by updated time
- finding the message file for a given session id
- persisting a user-selected `directory` into the matching sessionslist.json entry

Security model
--------------
The project folder (.xo/sessions/) holds only metadata (sessionslist.json).
Chat messages are never written there. They live in each backend's own
storage, reached only through that backend's ``sessions`` capability
(``resolve_native_file`` / ``get_messages``); core never reads a backend's
message store directly and names no backend here.
"""

import json
from pathlib import Path

from services.cowork_agent.helpers import iso_now, ms_to_iso
from services.cowork_agent.project_layout import xo_projects_root


# ── Index filename resolution (new name + legacy fallback) ────────────────────


def _resolve_index_path(sessions_dir: Path) -> Path | None:
    """Return the first existing index file, preferring sessionslist.json."""
    for fname in ("sessionslist.json", "sessions.json"):
        p = sessions_dir / fname
        if p.exists():
            return p
    return None


# ── Adapter sessions-capability resolution ────────────────────────────────────


def _sessions_capability(agent: str):
    """Load an adapter's ``sessions`` capability module, or None.

    The capability exposes a uniform surface across every backend — the
    listing hooks (``USES_PROJECT_SESSIONS`` / ``enrich_project_session`` /
    ``resolve_native_file`` / ``list_native_sessions``) plus the read hooks
    (``owns_session`` / ``get_messages`` / ``set_session_directory``). Core
    forwards through here instead of branching on the backend name.
    """
    if not agent:
        return None
    from services.cowork_agent.adapters.loader import try_load_capability
    return try_load_capability("sessions", agent=agent)


# ── Session listing ───────────────────────────────────────────────────────────


def load_all_sessions() -> list[dict]:
    """Scan sessions and build SessionResponse objects, filtered by active backend.

    Two scan roots are considered, both resolved through the active backend's
    ``sessions`` capability (never by naming a backend here):
    - ``~/xo-projects/<id>/.xo/sessions/`` — project-tied sessions, scanned
      only when the active backend sets ``USES_PROJECT_SESSIONS``.
    - the backend's own native store — supplied by
      ``list_native_sessions()`` (e.g. a per-agent on-disk dir or a state db).

    Only sessions belonging to the active backend (``AGENT_NAME`` env) are
    returned: the other backends' stores aren't touched at all, so their
    sessions never leak into the sidebar. ``AGENT_NAME`` decides which world
    we're in; the other backends stay invisible.

    De-duplicated via ``sessionId`` so a session that is both project-tee'd
    and natively present surfaces only once (project-tied wins).
    """
    from services.xo_manifest import resolve_agent_name
    active_backend = resolve_agent_name()

    sessions = []
    seen_ids: set[str] = set()

    def _ingest_project_sessions_dir(sessions_dir: Path, agent_name: str, project_dir: Path) -> None:
        idx_path = _resolve_index_path(sessions_dir)
        if not idx_path:
            return
        try:
            with open(idx_path, encoding="utf-8") as f:
                index_data = json.load(f)
        except Exception:
            return

        for key, meta in index_data.items():
            session_id = meta.get("sessionId", "")
            if not session_id or session_id in seen_ids:
                continue
            seen_ids.add(session_id)

            updated_at = meta.get("updatedAt")
            time_updated = ms_to_iso(updated_at) if updated_at else iso_now()
            time_created = time_updated
            title = "Untitled Session"

            directory = meta.get("directory", "")

            # Enrich title / time_created / effective_agent from the session's
            # OWN backend (the tag in the index), via its sessions capability —
            # no backend is named here.
            backend = meta.get("backend", "")
            bmod = _sessions_capability(backend)
            enrich = getattr(bmod, "enrich_project_session", None) if bmod else None
            if enrich:
                tc, tt, effective_agent = enrich(meta, key, agent_name)
                if tc:
                    time_created = tc
                if tt:
                    title = tt
            else:
                effective_agent = agent_name

            sessions.append({
                "id": session_id,
                "project_id": None,
                "parent_id": None,
                "slug": None,
                "agent": effective_agent,
                "directory": directory or str(project_dir),
                "title": title,
                "version": 1,
                "summary_additions": 0,
                "summary_deletions": 0,
                "summary_files": 0,
                "summary_diffs": [],
                "is_pinned": False,
                "permission": {},
                "time_created": time_created,
                "time_updated": time_updated,
                "time_compacting": None,
                "time_archived": None,
            })

    # Resolve the ACTIVE backend's sessions capability once. It decides whether
    # the xo-projects scan applies and supplies any native (non-project)
    # sessions — no backend is named here.
    active_mod = _sessions_capability(active_backend)

    # Project-tied scan: only when the active backend tees into xo-projects.
    # The per-session enrichment inside still routes by each row's OWN backend
    # tag, so a project dir holding mixed-backend sessions resolves correctly.
    if getattr(active_mod, "USES_PROJECT_SESSIONS", False):
        projects_root = xo_projects_root()
        if projects_root.exists():
            for agent_dir in sorted(projects_root.iterdir()):
                if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                    continue
                _ingest_project_sessions_dir(agent_dir / ".xo" / "sessions", agent_dir.name, agent_dir)

    # Native (non-project) sessions from the active backend's own store
    # (a per-agent on-disk dir, a state db, etc.); backends without one return
    # an empty list. De-duplicated by id against the project-tied rows so the
    # other backends stay invisible when they aren't active.
    lister = getattr(active_mod, "list_native_sessions", None) if active_mod else None
    if lister:
        for row in lister():
            sid = row.get("id")
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            sessions.append(row)

    sessions.sort(key=lambda s: s["time_updated"], reverse=True)
    return sessions


# ── Message file lookup ───────────────────────────────────────────────────────


def find_session_file(session_id: str) -> Path | None:
    """Find the JSONL messages file for a session.

    Resolves the native message file through the owning backend's ``sessions``
    capability (``resolve_native_file``): a project-tied session is matched by
    its sessionslist.json metadata, then handed to its backend; a non-project
    session is resolved by id against each backend's native store.
    """
    # xo-projects: check sessionslist.json for metadata to find native file.
    projects_root = xo_projects_root()
    if projects_root.exists():
        for agent_dir in projects_root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            idx_path = _resolve_index_path(agent_dir / ".xo" / "sessions")
            if not idx_path:
                continue
            try:
                index = json.loads(idx_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for meta in index.values():
                if not isinstance(meta, dict) or meta.get("sessionId") != session_id:
                    continue
                # Resolve the native message file via the session's OWN backend
                # capability. No backend is named here.
                bmod = _sessions_capability(meta.get("backend", ""))
                resolver = getattr(bmod, "resolve_native_file", None) if bmod else None
                if resolver:
                    path = resolver(meta, session_id)
                    if path:
                        return path

    # Native (non-project) sessions: ask each adapter to resolve the file by id
    # alone (used when no project was selected at chat time) — generic, each
    # backend resolves from its own native store or returns None.
    from services.cowork_agent.registry.adapter_registry import list_adapters

    for name in list_adapters():
        bmod = _sessions_capability(name)
        resolver = getattr(bmod, "resolve_native_file", None) if bmod else None
        if resolver:
            path = resolver({}, session_id)
            if path:
                return path

    return None


def find_session_backend(session_id: str) -> str | None:
    """Return the adapter name that owns session_id, or None."""
    # xo-projects: read backend tag directly from sessionslist.json.
    projects_root = xo_projects_root()
    if projects_root.exists():
        for agent_dir in projects_root.iterdir():
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            idx_path = _resolve_index_path(agent_dir / ".xo" / "sessions")
            if not idx_path:
                continue
            try:
                index = json.loads(idx_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for meta in index.values():
                if isinstance(meta, dict) and meta.get("sessionId") == session_id:
                    tag = meta.get("backend", "")
                    if tag:
                        return tag

    # Not project-tagged: ask each adapter whether it owns this session via
    # its sessions capability (each backend scans its own native store).
    # No backend is named here.
    from services.cowork_agent.registry.adapter_registry import list_adapters
    from services.cowork_agent.adapters.loader import try_load_capability

    for name in list_adapters():
        mod = try_load_capability("sessions", agent=name)
        owns = getattr(mod, "owns_session", None) if mod else None
        if owns is not None and owns(session_id):
            return name

    return None
