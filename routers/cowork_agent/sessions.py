"""
Session + message routes.

Covers the `/api/sessions/*` CRUD surface and `/api/messages/{id}`. Route
order matters here: `/api/sessions/search` must register before
`/api/sessions/{session_id}` so the literal path isn't swallowed by the
path-parameter route.
"""

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.cowork_agent.helpers import parse_jsonl
from services.cowork_agent.messages import convert_messages
from services.cowork_agent.sessions_io import (
    find_session_file,
    load_all_sessions,
    update_session_directory,
    update_claude_session_directory,
)

router = APIRouter()


# ── Read side (GET) — order matters so /search beats /{session_id} ──────────


@router.get("/api/sessions")
def list_sessions(limit: int = 50, offset: int = 0):
    all_sessions = load_all_sessions()
    return all_sessions[offset : offset + limit]


@router.get("/api/sessions/search")
def search_sessions(q: str = "", limit: int = 20, offset: int = 0):
    all_sessions = load_all_sessions()
    q_lower = q.lower()
    results = []
    for s in all_sessions:
        if q_lower in (s.get("title") or "").lower():
            results.append({"session": s, "snippet": None})
    return results[offset : offset + limit]


@router.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    all_sessions = load_all_sessions()
    for s in all_sessions:
        if s["id"] == session_id:
            return s
    return JSONResponse(status_code=404, content={"detail": "Session not found"})


@router.get("/api/messages/{session_id}")
def get_messages(session_id: str, limit: int = 50, offset: int = -1):
    path = find_session_file(session_id)
    if not path:
        return {"total": 0, "offset": 0, "messages": []}

    records = parse_jsonl(path)
    all_messages = convert_messages(session_id, records)
    total = len(all_messages)

    if offset == -1:
        start = max(0, total - limit)
    else:
        start = offset

    page = all_messages[start : start + limit]

    return {
        "total": total,
        "offset": start,
        "messages": page,
    }


# ── Write side ───────────────────────────────────────────────────────────────


@router.post("/api/sessions")
async def create_session(request: Request):
    # Frontend may call this but we create sessions via chat/prompt instead
    return {"id": str(uuid.uuid4()), "title": "New Chat"}


@router.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    directory = body.get("directory")
    if directory is None:
        return {"ok": True}

    directory = str(directory).strip()
    if not directory:
        return JSONResponse(status_code=400, content={"detail": "directory must be a non-empty string"})

    updated = update_session_directory(session_id, directory)
    if not updated:
        updated = update_claude_session_directory(session_id, directory)
    if not updated:
        return JSONResponse(status_code=404, content={"detail": "Session not found"})

    return {"ok": True, "session_id": session_id, "directory": directory}


@router.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    return {"ok": True}


# ── Per-session read-only extras ─────────────────────────────────────────────


@router.get("/api/sessions/{session_id}/todos")
def session_todos(session_id: str):
    return {"todos": []}


@router.get("/api/sessions/{session_id}/files")
def session_files(session_id: str):
    return {"files": []}
