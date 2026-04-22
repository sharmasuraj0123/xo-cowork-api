"""
Workspace / filesystem endpoints under `/api/files/*`.

Covers uploads, directory listing, text & binary content reads, and
directory creation with optional xo-cowork scaffolding (WORKSPACE.md /
AGENTS.md / OBJECTIVES.md / sessions.json). All paths are clamped to the
user's home dir for safety.

#TODO --- need safect related change (dir)
"""

import hashlib
import mimetypes
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()


# ── Scaffold templates used by mkdir ─────────────────────────────────────────

_PROJECT_SCAFFOLD: dict[str, str] = {
    "WORKSPACE.md": """\
# WORKSPACE.md

## Workspace Summary
- **Name:** <project-name>
- **Owner:** <owner-or-team>
- **Last updated:** <YYYY-MM-DD>
- **Primary repository/folder:** <absolute-or-repo-relative-path>

## Mission
<!-- 1-3 lines on why this workspace exists and what success looks like. -->

## Product/Project Context
<!-- Problem statement, users, constraints, and non-goals. -->

## Architecture Snapshot
- **Frontend:** <framework/runtime>
- **Backend:** <framework/runtime>
- **Data layer:** <db/cache/queue>
- **Integrations:** <external APIs/services>

## Working Boundaries
- In-scope:
  - <what can be changed>
- Out-of-scope:
  - <what should not be changed without approval>

## Sources of Truth
- Requirements: <path or link>
- Design docs: <path or link>
- API contracts: <path or link>
- Runbooks: <path or link>

## Current Focus
- Sprint/iteration theme: <theme>
- Active objective IDs: <OBJ-1, OBJ-2>
- Risks/blockers:
  - <risk 1>
  - <risk 2>

## Handover Notes
<!-- Short operational notes future agents should know before they start. -->

---
""",
    "AGENTS.md": """\
# AGENTS.md

## Agent Operating Contract
All agents working in this workspace must:
1. Read `WORKSPACE.md` and `OBJECTIVES.md` before making edits.
2. Align every task to at least one objective ID from `OBJECTIVES.md`.
3. Keep changes inside agreed workspace boundaries.
4. Document findings, decisions, and progress in the logs below.

## Execution Rules
- Prefer small, reversible changes.
- Do not use destructive commands without explicit approval.
- Validate critical changes with available tests/checks.
- Surface assumptions and blockers early.
- Keep documentation in sync with behavior changes.

## Required Logs
### Objective Progress Log
| Date | Agent | Objective ID | Progress | Evidence/PR/Commit | Next Step |
| --- | --- | --- | --- | --- | --- |
| <YYYY-MM-DD> | <agent-name> | <OBJ-1> | <what moved> | <link-or-path> | <next action> |

### Findings Log
| Date | Agent | Area | Finding | Impact | Recommendation |
| --- | --- | --- | --- | --- | --- |
| <YYYY-MM-DD> | <agent-name> | <component> | <observation> | <high/med/low> | <proposal> |

### Decision Log
| Date | Decision | Rationale | Owner | Review Date |
| --- | --- | --- | --- | --- |
| <YYYY-MM-DD> | <decision summary> | <why> | <owner> | <date> |

## Reporting Format (end of task)
- Objective alignment: `<OBJ-ids>`
- What changed: `<files and behavior>`
- Validation: `<tests/checks run>`
- Risks/unknowns: `<open items>`
- Follow-up: `<next suggested step>`

---
""",
    "OBJECTIVES.md": """\
# OBJECTIVES.md

## Objective Framework (OKR)
Use this format for all planning. Every active task should map to one KR.

## Objective Table
| Objective ID | Objective (Outcome) | Owner | Horizon | Status | Confidence |
| --- | --- | --- | --- | --- | --- |
| OBJ-1 | <clear outcome statement> | <owner> | <Qx YYYY> | <on-track/at-risk/off-track> | <high/med/low> |

## Key Results Table
| KR ID | Objective ID | Key Result (Measurable) | Baseline | Target | Current | Due Date | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| KR-1.1 | OBJ-1 | <metric target> | <value> | <value> | <value> | <YYYY-MM-DD> | <on-track/at-risk/off-track> |

## Weekly Execution Plan
| Week | KR ID | Planned Actions | Owner | Evidence |
| --- | --- | --- | --- | --- |
| <YYYY-Www> | KR-1.1 | <planned work> | <owner> | <ticket/PR/doc path> |

## Result Log
| Date | KR ID | Result Update | Delta | Evidence | Notes |
| --- | --- | --- | --- | --- | --- |
| <YYYY-MM-DD> | KR-1.1 | <what changed> | <+/- value> | <link-or-path> | <context> |

## Agent Alignment Notes
- Agents must cite objective and KR IDs when proposing or executing work.
- If work does not map to an objective, classify it as maintenance and justify it.
- Escalate any KR with off-track status in the next progress update.

---
""",
    "sessions.json": "[]\n",
}


# ── Routes ───────────────────────────────────────────────────────────────────


@router.post("/api/files/upload")
async def upload_file(
    file: UploadFile = File(...),
    workspace: str = Form(""),
):
    """Save an uploaded file into the workspace (or ~/uploads fallback)."""
    content = await file.read()
    content_hash = hashlib.sha256(content).hexdigest()

    if workspace:
        dest_dir = Path(workspace).resolve()
    else:
        dest_dir = Path.home() / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)

    filename = file.filename or "upload"
    dest = dest_dir / filename

    # Avoid overwriting — append hash suffix if name collides with different content
    if dest.exists():
        existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        if existing_hash != content_hash:
            stem = dest.stem
            suffix = dest.suffix
            dest = dest_dir / f"{stem}_{content_hash[:8]}{suffix}"

    dest.write_bytes(content)

    mime = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    return {
        "file_id": content_hash[:16],
        "name": dest.name,
        "path": str(dest),
        "size": len(content),
        "mime_type": mime,
        "source": "uploaded",
        "content_hash": content_hash,
    }


@router.post("/api/files/list-directory")
async def list_directory(request: Request):
    """List files and directories at a given path."""
    body = await request.json()
    raw_path = body.get("path")
    base = Path.home()

    if raw_path:
        target = Path(raw_path).resolve()
        # Prevent traversal outside home
        if not str(target).startswith(str(base)):
            return JSONResponse(status_code=403, content={"detail": "Access denied"})
    else:
        target = base

    if not target.is_dir():
        return JSONResponse(status_code=404, content={"detail": "Not a directory"})

    dirs = []
    files = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": str(entry)})
            else:
                files.append({"name": entry.name, "path": str(entry)})
    except PermissionError:
        pass

    parent = str(target.parent) if target != base else None

    return {
        "path": str(target),
        "parent": parent,
        "dirs": dirs,
        "files": files,
    }


@router.post("/api/files/content")
async def file_content(request: Request):
    """Read text content of a file."""
    body = await request.json()
    raw_path = body.get("path")
    if not raw_path:
        return JSONResponse(status_code=400, content={"detail": "Missing path"})

    base = Path.home()
    target = Path(raw_path).resolve()

    if not str(target).startswith(str(base)):
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    if not target.is_file():
        return JSONResponse(status_code=404, content={"detail": "File not found"})

    try:
        content = target.read_text(errors="replace")
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

    return {"content": content, "path": str(target)}


@router.post("/api/files/content-binary")
async def file_content_binary(request: Request):
    """Read binary file and return as a downloadable response."""
    body = await request.json()
    raw_path = body.get("path")
    if not raw_path:
        return JSONResponse(status_code=400, content={"detail": "Missing path"})

    base = Path.home()
    target = Path(raw_path).resolve()

    if not str(target).startswith(str(base)):
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    if not target.is_file():
        return JSONResponse(status_code=404, content={"detail": "File not found"})

    return FileResponse(str(target), filename=target.name)


@router.post("/api/files/save")
async def file_save(request: Request):
    """Write text content to a file under the user's home directory.

    Body fields:
    - ``path`` (str, required): absolute path of the file to write.
    - ``content`` (str, required): UTF-8 text content.

    Creates parent directories if missing. Intended for known workspace
    files (e.g. `IDENTITY.md`, `SOUL.md`, etc.) — for generic uploads,
    use `/api/files/upload` instead.
    """
    body = await request.json()
    raw_path = body.get("path")
    content = body.get("content")

    if not raw_path:
        return JSONResponse(status_code=400, content={"detail": "Missing path"})
    if content is None:
        return JSONResponse(status_code=400, content={"detail": "Missing content"})
    if not isinstance(content, str):
        return JSONResponse(status_code=400, content={"detail": "Content must be a string"})

    base = Path.home()
    target = Path(raw_path).resolve()

    if not str(target).startswith(str(base)):
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        target.write_bytes(data)
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

    return {"path": str(target), "bytes": len(data)}


@router.post("/api/files/mkdir")
async def make_directory(request: Request):
    """Create a new directory under the user's home directory.

    Body fields:
    - ``path`` (str, required): absolute path of the directory to create.
    - ``scaffold`` (bool, optional): when true, writes WORKSPACE.md, AGENTS.md,
      OBJECTIVES.md, and sessions.json inside the new directory.
    - ``files`` (list[str], optional): additional local file paths to copy into
      the new directory. Each entry must be an absolute path that already exists
      under the user's home directory. The file is copied using its original
      filename; existing scaffold files with the same name are not overwritten.
    """
    body = await request.json()
    raw_path = body.get("path")
    scaffold = bool(body.get("scaffold", False))
    extra_files: list[str] = body.get("files") or []

    if not raw_path:
        return JSONResponse(status_code=400, content={"detail": "Missing path"})

    base = Path.home()
    target = Path(raw_path).resolve()

    if not str(target).startswith(str(base)):
        return JSONResponse(status_code=403, content={"detail": "Access denied"})

    if target.exists():
        return JSONResponse(status_code=409, content={"detail": "Already exists"})

    # Validate extra file paths before creating anything
    resolved_extras: list[Path] = []
    for raw_file in extra_files:
        fp = Path(raw_file).resolve()
        if not str(fp).startswith(str(base)):
            return JSONResponse(
                status_code=403,
                content={"detail": f"Access denied: {raw_file}"},
            )
        if not fp.is_file():
            return JSONResponse(
                status_code=404,
                content={"detail": f"File not found: {raw_file}"},
            )
        resolved_extras.append(fp)

    try:
        target.mkdir(parents=True, exist_ok=False)

        if scaffold:
            for filename, content in _PROJECT_SCAFFOLD.items():
                (target / filename).write_text(content)

        copied = []
        for src in resolved_extras:
            dest = target / src.name
            if not dest.exists():
                shutil.copy2(src, dest)
            copied.append(src.name)
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

    return {"path": str(target), "name": target.name, "copied": copied}
