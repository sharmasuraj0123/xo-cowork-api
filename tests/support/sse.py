"""A raw-ASGI SSE driver with explicit disconnect control.

**Why not `httpx.ASGITransport` / `TestClient`.** `ASGITransport.handle_async_request`
accumulates the response into `body_parts` and only returns once the app has
sent `more_body: False` (httpx 0.28, `_transports/asgi.py`). Against an SSE
endpoint that never completes, it hangs; and its `receive()` only yields
`http.disconnect` *after* the response is complete, so there is no way to model
"client vanished mid-turn" — which is precisely the condition gates #5, #10 and
#11 are about. `TestClient` is worse: it also runs the lifespan.

So we call the app directly. It is ~60 lines, has no dependencies, and gives
frame-level timing plus a disconnect we control to the millisecond.
"""
from __future__ import annotations

import asyncio
import time


class SSESession:
    """One SSE connection to an ASGI app.

    Usage:
        s = SSESession(app, "/api/chat/stream/abc")
        await s.start()
        frames = await s.read_until(lambda f: f["event"] == "text-delta", timeout=2)
        await s.disconnect()
    """

    def __init__(self, app, path: str, *, query: bytes = b"", headers=None):
        self.app = app
        self.path = path
        self.query = query
        self.headers = headers or []
        self.status: int | None = None
        self.frames: list[dict] = []
        self._buf = ""
        self._out: asyncio.Queue = asyncio.Queue()
        self._in: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._t0 = 0.0

    # ── ASGI plumbing ────────────────────────────────────────────────────────

    def _scope(self) -> dict:
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": self.query,
            "root_path": "",
            "headers": [(b"host", b"testserver")] + list(self.headers),
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 50000),
        }

    async def _receive(self) -> dict:
        return await self._in.get()

    async def _send(self, message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                await self._out.put((time.monotonic() - self._t0, body))
            if not message.get("more_body", False):
                await self._out.put(None)

    async def start(self) -> None:
        self._t0 = time.monotonic()
        await self._in.put({"type": "http.request", "body": b"", "more_body": False})
        self._task = asyncio.create_task(
            self.app(self._scope(), self._receive, self._send)
        )

    async def disconnect(self) -> None:
        """Model a client that went away. Exactly what a backgrounded tab, a
        laptop lid or a dropped LTE connection does."""
        await self._in.put({"type": "http.disconnect"})
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    # ── frame reading ────────────────────────────────────────────────────────

    def _drain_buffer(self) -> list[dict]:
        out = []
        while "\n\n" in self._buf:
            block, self._buf = self._buf.split("\n\n", 1)
            frame = {"event": "message", "data": "", "id": None}
            for line in block.split("\n"):
                if line.startswith("event: "):
                    frame["event"] = line[7:]
                elif line.startswith("data: "):
                    frame["data"] = line[6:]
                elif line.startswith("id: "):
                    frame["id"] = line[4:]
            if block.strip():
                out.append(frame)
        return out

    async def read_frames(self, *, timeout: float = 1.0, stop_on_close: bool = True):
        """Read whatever has arrived within `timeout`. Returns [] on silence."""
        got = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(self._out.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if item is None:
                if stop_on_close:
                    break
                continue
            at, chunk = item
            self._buf += chunk.decode("utf-8", "replace")
            for frame in self._drain_buffer():
                frame["at"] = at
                got.append(frame)
                self.frames.append(frame)
        return got

    async def read_until(self, predicate, *, timeout: float = 5.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        collected = []
        while time.monotonic() < deadline:
            batch = await self.read_frames(timeout=deadline - time.monotonic())
            collected += batch
            if any(predicate(f) for f in collected):
                return collected
            if not batch:
                break
        return collected

    @property
    def gaps(self) -> list[float]:
        """Inter-frame gaps in seconds — the quantity gate #8 bounds."""
        times = [f["at"] for f in self.frames]
        return [b - a for a, b in zip(times, times[1:])]
