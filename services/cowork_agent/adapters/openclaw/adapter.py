from __future__ import annotations

import pathlib
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
        from services.cowork_agent.agent_registry import get_active_agent
        from services.cowork_agent.sessions_io import find_session_key

        if not session_id:
            from services.cowork_agent.streaming import create_new_session
            agent = get_active_agent()
            oc_agent = "main"
            session_key = f"agent:{oc_agent}:web:{uuid.uuid4().hex[:8]}"
            _key, native_id, response_text = await create_new_session(question, session_key=session_key)
            return {"message": response_text, "native_session_id": native_id}

        session_key = find_session_key(session_id)
        if not session_key:
            raise ValueError(f"OpenClaw session key not found for session_id={session_id!r}")

        import json
        import httpx
        agent = get_active_agent()
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
        from services.cowork_agent.adapters.openclaw.transcript import tee_exchange
        from services.cowork_agent.sessions_io import find_session_key
        from services.cowork_agent.streaming import find_session_id_by_key
        from services.cowork_agent.settings import OPENCLAW_MODEL

        # _dispatcher_sse always passes session_id=None; the real ID is in our_session_id
        if not session_id:
            session_id = kwargs.get("our_session_id")

        xo_agent_id = kwargs.get("agent_id") or kwargs.get("xo_agent_id")
        oc_agent = kwargs.get("agent_type") or "main"
        prefetch_task = kwargs.get("openclaw_prefetch_task")

        if prefetch_task is not None:
            # chat_prompt already started the openclaw HTTP call; await it rather
            # than making a second request. Fake-stream the accumulated response.
            try:
                _key, native_id, response_text = await prefetch_task
            except Exception as exc:
                yield {"type": "error", "error": str(exc)}
                yield {"done": True, "native_session_id": None}
                return
            # If chat_prompt's poll didn't resolve session_id in time, signal it
            # now so _dispatcher_sse can emit session-created before any tokens.
            if not session_id and native_id:
                yield {"type": "session-id-resolved", "session_id": native_id}
            for char in response_text:
                yield {"type": "token", "token": char}
            yield {"done": True, "native_session_id": native_id}
            return

        if session_id:
            session_key = find_session_key(session_id)
            if not session_key:
                yield {"type": "error", "error": f"Session key not found for {session_id!r}"}
                yield {"done": True, "native_session_id": None}
                return
            native_session_id = session_id
        else:
            session_key = f"agent:{oc_agent}:web:{uuid.uuid4().hex[:8]}"
            native_session_id = None

        accumulated: list[str] = []
        async for event in stream_to_normalized(question, session_key, native_session_id):
            if event.get("type") == "heartbeat":
                continue
            if event.get("type") == "token":
                accumulated.append(event["token"])
                yield event
            elif event.get("done"):
                resolved = native_session_id or find_session_id_by_key(session_key)
                response_text = "".join(accumulated)
                if response_text:
                    try:
                        tee_exchange(
                            session_key,
                            resolved or session_key,
                            question,
                            response_text,
                            model_id=OPENCLAW_MODEL,
                            xo_agent_id=xo_agent_id,
                        )
                    except Exception:
                        pass
                yield {"done": True, "native_session_id": resolved}
                return
            else:
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
