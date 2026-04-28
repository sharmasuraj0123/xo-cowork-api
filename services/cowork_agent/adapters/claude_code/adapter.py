from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from services.cowork_agent.adapters.base import BaseAgentAdapter
from services.cowork_agent.settings import CLAUDE_COWORK_DIR
from services.cowork_agent.helpers import short_id, iso_now


# ── Module-level native session ID cache (session_key → native_session_id) ───

_native_map: dict[str, str] = {}


# ── Pure helpers (no self needed) ─────────────────────────────────────────────


def _sessions_dir(agent_id: str) -> Path:
    """Return the sessions directory for a given agent_id under CLAUDE_COWORK_DIR."""
    return CLAUDE_COWORK_DIR / "agents" / agent_id / "sessions"


def _load_index(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_index(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _extract_native_session_id(event: dict) -> str | None:
    """
    Extract the claude-native session/conversation ID from a result event.
    Checks both snake_case and camelCase variants to be robust against CLI changes.
    """
    for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def make_session_key(agent_id: str) -> str:
    """Generate a new session key in the format claude:{agent_id}:web:{hex8}."""
    return f"claude:{agent_id}:web:{uuid.uuid4().hex[:8]}"


def find_session_id_by_key(session_key: str) -> str | None:
    """Read sessions.json for the agent derived from session_key and return the sessionId."""
    agent_id = _agent_id_from_key(session_key)
    index = _load_index(_sessions_dir(agent_id) / "sessions.json")
    meta = index.get(session_key)
    return meta.get("sessionId") if meta else None


def get_native_session_id(session_key: str) -> str | None:
    """Return the native Claude session ID for a key — cache first, then disk."""
    cached = _native_map.get(session_key)
    if cached:
        return cached
    agent_id = _agent_id_from_key(session_key)
    meta = _load_index(_sessions_dir(agent_id) / "sessions.json").get(session_key)
    if meta:
        native = meta.get("nativeSessionId")
        if native:
            _native_map[session_key] = native
            return native
    return None


def write_preliminary_entry(session_key: str, session_id: str, cwd: str) -> None:
    """
    Write a sessions.json entry BEFORE the subprocess starts so the polling
    loop in chat.py can resolve session_id without waiting for the full response.
    """
    agent_id = _agent_id_from_key(session_key)
    sd = _sessions_dir(agent_id)
    sd.mkdir(parents=True, exist_ok=True)
    index_path = sd / "sessions.json"
    index = _load_index(index_path)
    index[session_key] = {
        "sessionId": session_id,
        "nativeSessionId": "",  # filled in once the result event arrives
        "directory": cwd,
        "updatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
    }
    _write_index(index_path, index)


def _append_exchange(
    session_id: str,
    session_key: str,
    user_text: str,
    assistant_text: str,
    usage: dict,
    model_id: str,
) -> None:
    """
    Append a user+assistant turn to {session_id}.jsonl in OpenClaw-compatible format.
    Also bumps updatedAt in sessions.json so the session list stays sorted.
    """
    agent_id = _agent_id_from_key(session_key)
    sd = _sessions_dir(agent_id)
    sd.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()

    with open(sd / f"{session_id}.jsonl", "a", encoding="utf-8") as f:
        f.write(
            json.dumps({
                "type": "message",
                "id": short_id(),
                "timestamp": now_iso,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": user_text}],
                },
            }) + "\n"
        )
        f.write(
            json.dumps({
                "type": "message",
                "id": short_id(),
                "timestamp": now_iso,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": assistant_text}],
                    "model": model_id,
                    "stopReason": "stop",
                    "usage": {
                        "input": usage.get("input_tokens", 0),
                        "output": usage.get("output_tokens", 0),
                        "cacheRead": usage.get("cache_read_input_tokens", 0),
                        "cacheWrite": usage.get("cache_creation_input_tokens", 0),
                    },
                },
            }) + "\n"
        )

    # Bump updatedAt in the index
    index_path = sd / "sessions.json"
    index = _load_index(index_path)
    if session_key in index:
        index[session_key]["updatedAt"] = int(datetime.now(timezone.utc).timestamp() * 1000)
        _write_index(index_path, index)


def find_session_key_for_session_id(session_id: str) -> str | None:
    """Search all agents' sessions.json files and return the session_key for session_id."""
    agents_root = CLAUDE_COWORK_DIR / "agents"
    if not agents_root.exists():
        return None
    for agent_dir in agents_root.iterdir():
        if not agent_dir.is_dir():
            continue
        index_path = agent_dir / "sessions" / "sessions.json"
        if not index_path.exists():
            continue
        index = _load_index(index_path)
        for key, meta in index.items():
            if isinstance(meta, dict) and meta.get("sessionId") == session_id:
                # Warm the native cache while we're here
                native = meta.get("nativeSessionId")
                if native:
                    _native_map[key] = native
                return key
    return None


def _agent_id_from_key(session_key: str) -> str:
    """Extract agent_id from 'claude:{agent_id}:web:{random}' format."""
    parts = session_key.split(":")
    return parts[1] if len(parts) >= 2 else "default"


# ── Adapter class ──────────────────────────────────────────────────────────────


class ClaudeCodeAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return "claude_code"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _build_cmd(
        self,
        question: str,
        native_session_id: str | None,
        stream: bool,
        agent_type: str | None = None,
    ) -> list[str]:
        cli = self.config.get("cli_path") or "claude"
        workspace = self.config.get("workspace_root") or "/home/coder"
        fmt = "stream-json" if stream else "json"

        skill_prefix = None
        if agent_type:
            skills = self.commands.get("skills", {})
            normalised = agent_type.lower().replace("_", "-")
            skill_prefix = skills.get(normalised)

        prompt = f"{skill_prefix} {question}" if skill_prefix else question

        cmd = [
            cli,
            "--dangerously-skip-permissions",
            "--add-dir", workspace,
            "--print",
            "--output-format", fmt,
        ]
        if stream:
            cmd.append("--verbose")
        if native_session_id:
            cmd += ["--resume", native_session_id]
        cmd += ["-p", prompt]
        return cmd

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Let the CLI use its own credentials file — don't inject tokens.
        # Strip placeholder values injected by the workspace that would confuse Claude.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            if env.get(key) in (None, "", "sk-ant-none"):
                env.pop(key, None)
        return env

    # ── Convenience wrappers that delegate to module-level functions ───────────

    def make_session_key(self, agent_id: str) -> str:
        return make_session_key(agent_id)

    def find_session_id_by_key(self, session_key: str) -> str | None:
        return find_session_id_by_key(session_key)

    def get_native_session_id(self, session_key: str) -> str | None:
        return get_native_session_id(session_key)

    def write_preliminary_entry(self, session_key: str, session_id: str, cwd: str) -> None:
        write_preliminary_entry(session_key, session_id, cwd)

    def find_session_key_for_session_id(self, session_id: str) -> str | None:
        return find_session_key_for_session_id(session_id)

    # ── BaseAgentAdapter implementation ───────────────────────────────────────

    async def run(
        self,
        question: str,
        session_id: str | None = None,
        agent_type: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        cmd = self._build_cmd(question, session_id, stream=False, agent_type=agent_type)
        timeout = self.config.get("timeout", 120)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"ClaudeCodeAdapter.run timed out after {timeout}s")

        if proc.returncode != 0:
            raise RuntimeError(
                f"Claude CLI exited with code {proc.returncode}: {stderr.decode()[:500]}"
            )

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Claude CLI returned non-JSON output: {exc}") from exc

        return {
            "message": data.get("result", ""),
            "native_session_id": data.get("session_id"),
        }

    async def stream(
        self,
        question: str,
        session_id: str | None = None,
        agent_type: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        from services.cowork_agent.adapters.claude_code.streaming import parse_stream_line

        # Named kwargs injected by chat.py for session tracking
        sk: str | None = kwargs.get("session_key")
        our_session_id: str | None = kwargs.get("our_session_id")
        agent_id: str | None = kwargs.get("agent_id")
        question_text: str = question  # use the positional arg directly
        cwd: str | None = kwargs.get("cwd")

        # Determine which native_session_id to resume with
        # (session_id positional arg is the native_session_id for existing sessions)
        native_resume_id = session_id

        # Write the preliminary index entry if we have a session_key
        if sk and our_session_id:
            effective_cwd = cwd or self.config.get("workspace_root") or "/home/coder"
            write_preliminary_entry(sk, our_session_id, effective_cwd)

        cmd = self._build_cmd(question_text, native_resume_id, stream=True, agent_type=agent_type)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
        )

        native_session_id: str | None = None
        response_parts: list[str] = []
        usage: dict = {}
        model_id = ""

        async for raw_line in proc.stdout:
            event = parse_stream_line(raw_line)
            if event is None:
                continue

            if event.get("type") == "result":
                native_session_id = _extract_native_session_id(event)
                usage = event.get("usage") or {}
                model_id = event.get("model", "")
                continue

            if event.get("type") == "token":
                response_parts.append(event.get("token", ""))

            yield event

        await proc.wait()

        # Persist the exchange and update the index if we have session tracking info
        if sk and our_session_id and response_parts:
            full_response = "".join(response_parts)
            _append_exchange(our_session_id, sk, question_text, full_response, usage, model_id)

            # Update nativeSessionId in the index
            if native_session_id:
                agent_id_for_key = _agent_id_from_key(sk)
                sd = _sessions_dir(agent_id_for_key)
                index_path = sd / "sessions.json"
                index = _load_index(index_path)
                if sk in index:
                    index[sk]["nativeSessionId"] = native_session_id
                    _write_index(index_path, index)
                _native_map[sk] = native_session_id

        yield {"done": True, "native_session_id": native_session_id}

    async def health(self) -> dict[str, Any]:
        cli = self.config.get("cli_path") or "claude"
        try:
            proc = await asyncio.create_subprocess_exec(
                cli, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            version = stdout.decode().strip()
            return {"ok": True, "version": version}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
