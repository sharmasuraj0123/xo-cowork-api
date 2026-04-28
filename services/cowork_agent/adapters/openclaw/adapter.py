from __future__ import annotations

import uuid
from typing import Any, AsyncIterator

from services.cowork_agent.adapters.base import BaseAgentAdapter


class OpenclawAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return "openclaw"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()

    # ── BaseAgentAdapter implementation ───────────────────────────────────────

    async def run(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Non-streaming execution via OpenClaw HTTP API.

        - New session (session_id=None): calls create_new_session which bootstraps
          an OpenClaw session and returns accumulated response text.
        - Existing session: streams to OpenClaw and accumulates the response.
        """
        from services.cowork_agent.settings import (
            OPENCLAW_API_URL,
            OPENCLAW_GATEWAY_TOKEN,
            OPENCLAW_MODEL,
        )
        from services.cowork_agent.agent_registry import get_default_agent
        from services.cowork_agent.sessions_io import find_session_key

        if not session_id:
            from services.cowork_agent.streaming import create_new_session
            agent = get_default_agent()
            oc_agent = "main"
            session_key = f"agent:{oc_agent}:web:{uuid.uuid4().hex[:8]}"
            _key, native_id, response_text = await create_new_session(question, session_key=session_key)
            return {"message": response_text, "native_session_id": native_id}

        session_key = find_session_key(session_id)
        if not session_key:
            raise ValueError(f"OpenClaw session key not found for session_id={session_id!r}")

        import json
        import httpx
        agent = get_default_agent()
        header = agent.session_header
        response_text = ""

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
                    raise RuntimeError(
                        f"OpenClaw API error: {response.status_code} {body.decode()}"
                    )
                async for line in response.aiter_lines():
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
                    content = choices[0].get("delta", {}).get("content")
                    if content:
                        response_text += content

        return {"message": response_text, "native_session_id": session_id}

    async def stream(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Streaming via OpenClaw HTTP API, yielding normalized events.
        Wraps adapters.openclaw.streaming.stream_to_normalized.
        """
        from services.cowork_agent.adapters.openclaw.streaming import stream_to_normalized
        from services.cowork_agent.sessions_io import find_session_key
        from services.cowork_agent.streaming import find_session_id_by_key

        if session_id:
            session_key = find_session_key(session_id)
            if not session_key:
                yield {"type": "error", "error": f"Session key not found for {session_id!r}"}
                yield {"done": True, "native_session_id": None}
                return
            native_session_id = session_id
        else:
            oc_agent = "main"
            session_key = f"agent:{oc_agent}:web:{uuid.uuid4().hex[:8]}"
            native_session_id = None

        async for event in stream_to_normalized(question, session_key, native_session_id):
            if event.get("done") and native_session_id is None:
                # Attempt to resolve the session id created during this stream
                resolved = find_session_id_by_key(session_key)
                yield {"done": True, "native_session_id": resolved}
                return
            yield event

    async def setup(self) -> bool:
        """OpenClaw gateway readiness — returns True (gateway is external)."""
        return True

    async def health(self) -> dict[str, Any]:
        """Ping the OpenClaw API URL to determine liveness."""
        import httpx
        from services.cowork_agent.settings import OPENCLAW_API_URL, OPENCLAW_GATEWAY_TOKEN

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
                resp = await client.get(
                    OPENCLAW_API_URL.replace("/v1/chat/completions", "/v1/models"),
                    headers={"Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}"},
                )
                ok = resp.status_code < 500
                return {"ok": ok, "gateway": "up" if ok else f"http_{resp.status_code}"}
        except Exception as exc:
            return {"ok": False, "gateway": str(exc)}
