"""
Secrets endpoints backed by the `~/.openclaw/.env` file.

GET parses the file into key/value entries, PUT overwrites it with a new list
of entries. The parser/serializer lives in `openclaw_env` so other route files
(e.g. the onboarding provider-key flow) share the same format.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.cowork_agent.openclaw_env import load_env_entries, save_env_entries

router = APIRouter()


@router.get("/api/secrets/env")
async def get_env_secrets():
    """Return the OpenClaw .env file as a list of key-value entries."""
    try:
        return {"entries": load_env_entries()}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.get("/api/secrets/env/keys")
async def get_env_keys():
    """Return only the keys with non-empty values — no secret material is
    transmitted. Used by onboarding to detect which provider keys are
    already configured without the full /env payload (and without
    sending plaintext values to the browser)."""
    try:
        keys = [
            e["key"]
            for e in load_env_entries()
            if (e.get("value") or "").strip()
        ]
        return {"keys": keys}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})


@router.put("/api/secrets/env")
async def put_env_secrets(request: Request):
    """Overwrite the OpenClaw .env file with the provided key-value entries."""
    body = await request.json()
    entries = body.get("entries", [])
    try:
        save_env_entries(entries)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})
