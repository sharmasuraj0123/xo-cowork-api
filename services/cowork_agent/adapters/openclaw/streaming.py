"""
OpenClaw-specific streaming functions.

Moved from services/cowork_agent/streaming.py (OpenClaw-specific logic).
The shared SSE emitter stays in the original module.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import AsyncIterator

import httpx

from services.cowork_agent.settings import (
    OPENCLAW_API_URL,
    OPENCLAW_GATEWAY_TOKEN,
    OPENCLAW_MODEL,
)
from services.cowork_agent.agent_registry import get_default_agent


def _session_header() -> str:
    return get_default_agent().session_header


async def stream_to_normalized(
    question: str,
    session_key: str,
    native_session_id: str | None = None,
) -> AsyncIterator[dict]:
    """
    POST question to OpenClaw HTTP API using session_key header.
    Yields normalized { "type": "token", "token": str } events.
    Ends with { "done": True, "native_session_id": str | None }.
    """
    header = _session_header()

    async with httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=10.0)) as client:
        async with client.stream(
            "POST",
            OPENCLAW_API_URL,
            headers={
                "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
                "Content-Type": "application/json",
                header: session_key,
            },
            json={
                "model": OPENCLAW_MODEL,
                "stream": True,
                "messages": [{"role": "user", "content": question}],
            },
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                yield {
                    "type": "error",
                    "error": f"OpenClaw API error: {response.status_code} {body.decode()}",
                }
                yield {"done": True, "native_session_id": native_session_id}
                return

            line_iter = response.aiter_lines().__aiter__()
            while True:
                try:
                    line = await asyncio.wait_for(line_iter.__anext__(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
                    continue
                except StopAsyncIteration:
                    break

                if not line.startswith("data: "):
                    continue

                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield {"type": "token", "token": content}

    yield {"done": True, "native_session_id": native_session_id}


async def create_session(question: str) -> tuple[str, str, str]:
    """
    Create a new OpenClaw session by sending the first message.
    Returns (session_key, session_id, response_text).

    Wraps services.cowork_agent.streaming.create_new_session.
    """
    from services.cowork_agent.streaming import create_new_session

    agent = get_default_agent()
    oc_agent = "main"
    session_key = f"agent:{oc_agent}:web:{uuid.uuid4().hex[:8]}"
    return await create_new_session(question, session_key=session_key)
