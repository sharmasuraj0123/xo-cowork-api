"""
routers/cowork_agent/relay.py — fully self-contained commit hash relay.

No xo-swarm-api calls. Workspaces pair and communicate directly.

Endpoints:
  POST   /api/relay/channels/invite               generate invite code + my_url
  POST   /api/relay/channels/finalize             called BY peer to complete pairing here
  POST   /api/relay/channels/accept               accept invite — calls peer's /finalize
  GET    /api/relay/channels                      list from local relay.json
  DELETE /api/relay/channels/{channel_id}         remove from local relay.json
  PUT    /api/relay/channels/{channel_id}/repos   update watched repo paths
  POST   /api/relay/channels/{channel_id}/push    receive commit hash from peer scanner
  GET    /api/relay/channels/{channel_id}/stream  SSE to browser (local broker)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import secrets
import time
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from services.cowork_agent.commit_relay import config as relay_config
from services.cowork_agent.commit_relay.broker import broker

router = APIRouter(prefix="/api/relay", tags=["relay"])

_FINALIZE_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_KEEPALIVE_INTERVAL = 20.0

# ── Invite code wordlist ───────────────────────────────────────────────────────

_ADJECTIVES = [
    "swift", "bright", "calm", "dark", "eager", "fair", "gold", "hard", "icy",
    "jade", "keen", "lean", "mild", "neat", "opal", "pure", "quick", "rich",
    "soft", "tall", "urban", "vast", "warm", "wild", "young", "zeal", "amber",
    "blue", "crisp", "deep", "early", "frost", "green", "happy", "iron",
    "just", "kind", "light", "magic", "noble", "open", "plain", "quiet",
    "rapid", "sharp", "still", "true", "ultra", "vivid", "white", "zinc",
]
_NOUNS = [
    "bear", "crane", "dove", "eagle", "fox", "goat", "hawk", "ibis", "jay",
    "kite", "lark", "mole", "newt", "owl", "puma", "quail", "raven", "swan",
    "toad", "vole", "wolf", "yak", "zebra", "ant", "bee", "crab", "deer",
    "elk", "frog", "gnu", "hare", "koi", "lion", "mink", "orca", "pike",
    "rat", "seal", "tern", "viper", "wren", "asp", "bison", "clam", "emu",
]
_VERBS = [
    "runs", "flies", "swims", "leaps", "dives", "drifts", "glows", "spins",
    "climbs", "sings", "soars", "walks", "hunts", "plays", "rests", "wakes",
    "calls", "falls", "fades", "grows", "heals", "jumps", "kicks", "lands",
    "meets", "nests", "opens", "pours", "rides", "rises", "rolls", "roams",
    "seeks", "sits", "skips", "stays", "steps", "turns", "waits", "waves",
]


def _generate_invite_code() -> str:
    return f"{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}-{random.choice(_VERBS)}"


def _encode_invite_token(invite_code: str, my_url: str) -> str:
    """Pack invite_code + my_url into a single base64url string to share."""
    data = json.dumps({"code": invite_code, "url": my_url})
    return base64.urlsafe_b64encode(data.encode()).decode().rstrip("=")


def _decode_invite_token(token: str) -> tuple[str, str]:
    """Unpack invite_token → (invite_code, peer_url). Raises ValueError on bad token."""
    # Restore base64 padding
    padded = token + "=" * (4 - len(token) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded.encode()))
    return data["code"], data["url"]


# ── In-memory pending invites (15-min TTL, lost on restart) ───────────────────
# invite_code → {channel_id, my_push_secret, expires_at}
_pending: dict[str, dict] = {}


def _workspace_id() -> str:
    return os.getenv("CODER_WORKSPACE_ID", "unknown")


def _my_url() -> str:
    return os.getenv("RELAY_PUBLIC_URL", "").rstrip("/")


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/channels/invite")
async def create_invite(request: Request):
    """Generate an invite code. Share invite_code + my_url with the peer workspace."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    project_id = body.get("project_id", "") if body else ""

    channel_id = str(uuid.uuid4())
    invite_code = _generate_invite_code()
    while invite_code in _pending:
        invite_code = _generate_invite_code()

    my_push_secret = secrets.token_urlsafe(32)
    expires_at = time.time() + 900  # 15 minutes

    _pending[invite_code] = {
        "channel_id": channel_id,
        "project_id": project_id,
        "my_push_secret": my_push_secret,
        "expires_at": expires_at,
    }

    return {
        "invite_code": invite_code,
        "invite_token": _encode_invite_token(invite_code, _my_url()),
        "my_url": _my_url(),
        "channel_id": channel_id,
        "project_id": project_id,
        "expires_at": int(expires_at),
    }


@router.post("/channels/finalize")
async def finalize_invite(request: Request):
    """Called BY the peer workspace to complete pairing on this side.

    Peer sends their invite_code, their URL, their workspace ID, and the
    push_secret we should use when they call our /push endpoint.
    We validate the code, store the pairing, and return our push_secret
    (which the peer's scanner will use to authenticate pushes to us).
    """
    body = await request.json()
    invite_code = body.get("invite_code", "")
    peer_workspace_id = body.get("peer_workspace_id", "unknown")
    peer_url = body.get("peer_url", "")
    peer_push_secret = body.get("peer_push_secret", "")

    pending = _pending.get(invite_code)
    if not pending:
        raise HTTPException(status_code=404, detail="Invite code not found or expired")
    if time.time() > pending["expires_at"]:
        _pending.pop(invite_code, None)
        raise HTTPException(status_code=410, detail="Invite code has expired")

    _pending.pop(invite_code)

    channel_id = pending["channel_id"]
    project_id = pending.get("project_id", "")
    my_push_secret = pending["my_push_secret"]

    relay_config.add_channel(
        channel_id=channel_id,
        project_id=project_id,
        peer_workspace_id=peer_workspace_id,
        peer_cowork_url=peer_url,
        peer_push_secret=peer_push_secret,
        my_push_secret=my_push_secret,
    )

    return {
        "channel_id": channel_id,
        "project_id": project_id,
        "push_secret_for_peer": my_push_secret,
        "workspace_id": _workspace_id(),
    }


@router.post("/channels/accept")
async def accept_invite(request: Request):
    """Accept an invite. Calls peer's /finalize to complete the handshake."""
    body = await request.json()
    invite_token = body.get("invite_token", "").strip()
    invite_code  = body.get("invite_code", "").strip()
    peer_url     = body.get("peer_url", "").rstrip("/")
    watched_repos = body.get("watched_repos", [])

    if invite_token:
        try:
            invite_code, peer_url = _decode_invite_token(invite_token)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid invite_token")

    if not invite_code:
        raise HTTPException(status_code=400, detail="invite_code or invite_token required")
    if not peer_url:
        raise HTTPException(status_code=400, detail="peer_url missing — set RELAY_PUBLIC_URL on the inviting workspace")

    my_push_secret = secrets.token_urlsafe(32)
    my_url = _my_url()

    try:
        async with httpx.AsyncClient(timeout=_FINALIZE_TIMEOUT) as client:
            r = await client.post(
                f"{peer_url}/api/relay/channels/finalize",
                json={
                    "invite_code": invite_code,
                    "peer_workspace_id": _workspace_id(),
                    "peer_url": my_url,
                    "peer_push_secret": my_push_secret,
                },
            )
            r.raise_for_status()
            result = r.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Peer rejected finalize: {exc.response.text}",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach peer: {exc}")

    channel_id = result["channel_id"]
    push_secret_for_peer = result["push_secret_for_peer"]
    peer_workspace_id = result.get("workspace_id", "unknown")

    project_id = result.get("project_id", "")

    relay_config.add_channel(
        channel_id=channel_id,
        project_id=project_id,
        peer_workspace_id=peer_workspace_id,
        peer_cowork_url=peer_url,
        peer_push_secret=push_secret_for_peer,
        my_push_secret=my_push_secret,
    )
    if watched_repos:
        relay_config.set_watched_repos(channel_id, watched_repos)

    return {
        "channel_id": channel_id,
        "project_id": project_id,
        "peer_workspace_id": peer_workspace_id,
        "status": "active",
    }


@router.get("/channels")
async def list_channels():
    """List all paired channels from local relay.json."""
    return [
        {
            "channel_id": e["channel_id"],
            "project_id": e.get("project_id", ""),
            "peer_workspace_id": e.get("peer_workspace_id"),
            "peer_cowork_url": e.get("peer_cowork_url"),
            "watched_repos": e.get("watched_repos", []),
        }
        for e in relay_config.load_relay_config()
    ]


@router.delete("/channels/{channel_id}")
async def remove_channel(channel_id: str):
    """Remove a channel from local relay.json."""
    relay_config.remove_channel(channel_id)
    return {"ok": True}


@router.put("/channels/{channel_id}/repos")
async def update_watched_repos(channel_id: str, request: Request):
    """Update the list of git repo paths to watch for this channel."""
    body = await request.json()
    watched_repos = body.get("watched_repos", [])
    relay_config.set_watched_repos(channel_id, watched_repos)
    return {"ok": True, "watched_repos": watched_repos}


@router.post("/channels/{channel_id}/push")
async def receive_push(channel_id: str, request: Request):
    """Receive an incoming commit hash from the peer's scanner.

    Authenticated by the per-channel push secret exchanged during pairing.
    """
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    entry = relay_config.get_channel(channel_id)

    if not entry:
        raise HTTPException(status_code=404, detail="Channel not found")

    my_push_secret = entry.get("my_push_secret", "")
    if not my_push_secret or not secrets.compare_digest(token, my_push_secret):
        raise HTTPException(status_code=403, detail="Invalid push secret")

    payload = await request.json()
    delivered = broker.publish(channel_id, payload)
    return {"ok": True, "delivered_to": delivered}


@router.get("/channels/{channel_id}/stream")
async def commit_stream(channel_id: str):
    """SSE endpoint. Browser receives live commit-hash events from the peer."""
    return StreamingResponse(
        _local_sse(channel_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _local_sse(channel_id: str):
    q = broker.subscribe(channel_id)
    try:
        event_id = 1
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_INTERVAL)
                yield f"id: {event_id}\nevent: commit-hash\ndata: {json.dumps(item)}\n\n"
                event_id += 1
            except asyncio.TimeoutError:
                yield "event: heartbeat\ndata: {}\n\n"
    finally:
        broker.unsubscribe(channel_id, q)
