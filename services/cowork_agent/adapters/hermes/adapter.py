"""
Hermes adapter: drives the local Hermes gateway (``hermes gateway``) on
``http://127.0.0.1:8642/v1/chat/completions``.

Session model
-------------
Hermes owns session storage in ``~/.hermes/state.db`` (and one DB per profile
under ``~/.hermes/profiles/<name>/state.db``). xo-cowork is stateless: it
sends the previous session id via the ``X-Hermes-Session-Id`` request header
to continue, or omits it to start fresh. The hermes server derives a new id
and returns it back in the same header on the response — that becomes the
``native_session_id`` we hand back to chat_prompt.

Reads happen via ``services.cowork_agent.hermes_state_db`` (read-only) so
the sidebar can list/transcript sessions without going through the API.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from services.cowork_agent.adapters.base import BaseAgentAdapter


class HermesAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return "hermes"

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
        """Non-streaming chat: post once, return collected message + native id."""
        from services.cowork_agent.adapters.hermes.streaming import run_collected
        from services.cowork_agent.adapters.hermes.sessionslist import write_session_row

        gateway_base = self._resolve_gateway_base(kwargs.get("agent_id"), session_id)
        response_text, native_session_id = await run_collected(
            question, session_id, gateway_base=gateway_base,
        )
        write_session_row(
            agent_id=kwargs.get("agent_id"),
            our_session_id=kwargs.get("our_session_id") or session_id,
            native_session_id=native_session_id,
        )
        return {"message": response_text, "native_session_id": native_session_id}

    async def stream(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming chat: yields ``{type: token, token: ...}`` then exactly one
        ``{done: True, native_session_id: ...}``.

        On a brand-new session (no session_id), the native id surfaces in the
        ``X-Hermes-Session-Id`` response header — the streaming helper resolves
        it and includes it in the done event. We also emit a
        ``session-id-resolved`` event before tokens so the SSE dispatcher can
        emit ``session-created`` to the frontend immediately on the first chunk.

        Profile routing: hermes's api_server inherits a single profile at
        process startup, so to route different agents to different profiles
        we maintain a per-profile gateway pool (see ``gateway_pool.py``).
        ``agent_id`` from the caller picks which gateway URL to hit; the
        default profile keeps using the hermes.sh-managed gateway on 8642.
        """
        from services.cowork_agent.adapters.hermes.streaming import stream_to_normalized
        from services.cowork_agent.adapters.hermes.sessionslist import write_session_row
        from services.cowork_agent.hermes_state_db import register_inflight_exchange
        from services.cowork_agent.settings import HERMES_MODEL

        # _dispatcher_sse always passes session_id=None; the real ID is in our_session_id
        our_session_id = kwargs.get("our_session_id")
        if not session_id:
            session_id = our_session_id

        gateway_base = self._resolve_gateway_base(kwargs.get("agent_id"), session_id)
        is_fresh_session = not session_id
        accumulated: list[str] = []
        async for event in stream_to_normalized(
            question, session_id, gateway_base=gateway_base,
        ):
            if event.get("type") == "error":
                yield event
                continue
            if event.get("type") == "token":
                accumulated.append(event.get("token", ""))
                yield event
                continue
            if event.get("done"):
                native_id = event.get("native_session_id")
                # Cache the just-completed exchange so /api/messages can serve it
                # during the 3-10 s window before hermes commits to state.db.
                if native_id:
                    register_inflight_exchange(
                        native_id,
                        user_text=question,
                        assistant_text="".join(accumulated),
                        model=HERMES_MODEL,
                    )
                # Upsert the per-project sessionslist row so the xo-coworker
                # dashboard sees this hermes session. No-op when no agent_id
                # was supplied (agent-only chat with no project selected).
                write_session_row(
                    agent_id=kwargs.get("agent_id"),
                    our_session_id=our_session_id or session_id,
                    native_session_id=native_id,
                )
                # Surface the resolved id for the dispatcher's session-created
                # event whenever this was a brand-new session.
                if is_fresh_session and native_id:
                    yield {"type": "session-id-resolved", "session_id": native_id}
                yield {"done": True, "native_session_id": native_id}
                return
            yield event

    # ── Concrete overrides ────────────────────────────────────────────────────

    @staticmethod
    def _resolve_gateway_base(agent_id: str | None, session_id: str | None = None) -> str | None:
        """Pick the gateway base URL for ``agent_id`` via the per-profile pool.

        Returns ``None`` for the default profile (or unknown agent_id) so the
        streaming helper falls back to ``HERMES_API_URL`` — i.e. the
        hermes.sh-managed gateway on port 8642. Any pool failure (invalid
        profile, spawn timeout) is logged and downgraded to ``None`` rather
        than failing the chat outright: the user just hits the default
        profile, same as before this feature shipped.

        Session-continuation fallback: the FE only sends ``agent_id`` on
        new sessions. When continuing an existing session it sends just
        the session_id, so without this fallback the request would hit
        the default gateway, hermes wouldn't recognize the X-Hermes-Session-Id
        (because the session lives in a different profile's state.db),
        and the chat would silently start a fresh session under
        ``default`` — exactly the cross-profile leak we built the pool to
        prevent. We back-resolve the owning profile from state.db via
        ``find_hermes_profile`` so continuations land in the right place.
        """
        from services.cowork_agent.adapters.hermes import gateway_pool

        if not agent_id and session_id:
            try:
                from services.cowork_agent.hermes_state_db import find_hermes_profile
                resolved = find_hermes_profile(session_id)
                if resolved and resolved != "default":
                    agent_id = resolved
            except Exception:  # noqa: BLE001 — never fail chat for a routing hint
                pass

        try:
            return gateway_pool.ensure_gateway(agent_id)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            import logging
            logging.getLogger(__name__).warning(
                "hermes pool: falling back to default gateway for agent_id=%r (%s)",
                agent_id, exc,
            )
            return None

    async def setup(self) -> bool:
        """Hermes gateway is external (started by ``hermes gateway run``)."""
        return True

    async def health(self) -> dict[str, Any]:
        """Ping the hermes gateway's ``/v1/models`` to determine liveness."""
        import httpx
        from services.cowork_agent.settings import HERMES_API_TOKEN, HERMES_API_URL

        if not HERMES_API_TOKEN:
            return {"ok": False, "gateway": "API_SERVER_KEY not set"}

        try:
            models_url = HERMES_API_URL.replace("/v1/chat/completions", "/v1/models")
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
                resp = await client.get(
                    models_url,
                    headers={"Authorization": f"Bearer {HERMES_API_TOKEN}"},
                )
                ok = resp.status_code < 500
                return {"ok": ok, "gateway": "up" if ok else f"http_{resp.status_code}"}
        except Exception as exc:
            return {"ok": False, "gateway": str(exc)}
