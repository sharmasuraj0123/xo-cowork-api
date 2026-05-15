from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from services.cowork_agent.adapters.base import BaseAgentAdapter
from services.cowork_agent.helpers import iso_now
from services.cowork_agent.project_layout import (
    project_dir as _xo_project_dir,
    sessions_dir as _xo_sessions_dir,
    xo_dir as _xo_dir,
    xo_projects_root,
)


# ── Module-level native session ID cache (session_key → native_session_id) ───

_native_map: dict[str, str] = {}


# ── Index I/O ──────────────────────────────────────────────────────────────────


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


def _index_path(agent_id: str) -> Path:
    return _xo_sessions_dir(agent_id) / "sessionslist.json"


def _load_agent_index(agent_id: str) -> tuple[dict, Path]:
    """Return (index_dict, path) for the agent's sessionslist.json in xo-projects."""
    path = _index_path(agent_id)
    # Fall back to legacy sessions.json so existing projects keep working.
    if not path.exists():
        legacy = _xo_sessions_dir(agent_id) / "sessions.json"
        if legacy.exists():
            return _load_index(legacy), legacy
    return _load_index(path), path


# ── Pure helpers ───────────────────────────────────────────────────────────────


def _extract_native_session_id(event: dict) -> str | None:
    for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def make_session_key(agent_id: str) -> str:
    return f"claude:{agent_id}:web:{uuid.uuid4().hex[:8]}"


def _agent_id_from_key(session_key: str) -> str:
    parts = session_key.split(":")
    return parts[1] if len(parts) >= 2 else "default"


def find_session_id_by_key(session_key: str) -> str | None:
    agent_id = _agent_id_from_key(session_key)
    index, _ = _load_agent_index(agent_id)
    meta = index.get(session_key)
    return meta.get("sessionId") if meta else None


def get_native_session_id(session_key: str) -> str | None:
    cached = _native_map.get(session_key)
    if cached:
        return cached
    agent_id = _agent_id_from_key(session_key)
    index, _ = _load_agent_index(agent_id)
    meta = index.get(session_key)
    if meta:
        native = meta.get("nativeSessionId")
        if native:
            _native_map[session_key] = native
            return native
    return None


def get_session_directory(session_key: str) -> str | None:
    agent_id = _agent_id_from_key(session_key)
    index, _ = _load_agent_index(agent_id)
    meta = index.get(session_key)
    return meta.get("directory") if meta else None


def write_preliminary_entry(session_key: str, session_id: str, cwd: str) -> None:
    """
    Write a sessionslist.json entry BEFORE the subprocess starts so the polling
    loop in chat.py can resolve session_id without waiting for the full response.
    Messages are NOT stored here — they live in ~/.claude/projects/.
    """
    agent_id = _agent_id_from_key(session_key)
    sd = _xo_sessions_dir(agent_id)
    sd.mkdir(parents=True, exist_ok=True)
    index_path = sd / "sessionslist.json"
    index = _load_index(index_path)
    index[session_key] = {
        "sessionId": session_id,
        "nativeSessionId": "",
        "directory": cwd,
        "backend": "claude_code",
        "updatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
        "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    _write_index(index_path, index)


def find_session_key_for_session_id(session_id: str) -> str | None:
    """Search xo-projects sessions for a matching session_id."""
    root = xo_projects_root()
    if not root.exists():
        return None
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        sessions_base = entry / ".xo" / "sessions"
        # Try new name first, then legacy
        for fname in ("sessionslist.json", "sessions.json"):
            index_path = sessions_base / fname
            if not index_path.exists():
                continue
            index = _load_index(index_path)
            for key, meta in index.items():
                if isinstance(meta, dict) and meta.get("sessionId") == session_id:
                    native = meta.get("nativeSessionId")
                    if native:
                        _native_map[key] = native
                    return key
    return None


# ── Adapter class ──────────────────────────────────────────────────────────────


class ClaudeCodeAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return "claude_code"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()

    def _resolve_cwd(self, agent_id: str | None) -> str:
        """Compute the working directory for a Claude subprocess.

        All agents run inside ``~/xo-projects/<agent_id>/``. When agent_id
        is None or "default", falls back to the xo-projects root itself.
        """
        if agent_id and agent_id not in ("default", ""):
            project = _xo_project_dir(agent_id)
            project.mkdir(parents=True, exist_ok=True)
            return str(project)
        return str(xo_projects_root())

    def _build_cmd(
        self,
        question: str,
        native_session_id: str | None,
        stream: bool,
        agent_type: str | None = None,
        cwd: str | None = None,
    ) -> list[str]:
        cli = self.config.get("cli_path") or "claude"
        workspace = cwd or str(xo_projects_root())
        fmt = "stream-json" if stream else "json"

        skill_prefix = None
        if agent_type:
            skills = self.commands.get("skills", {})
            skill_prefix = skills.get(agent_type.lower().replace("_", "-"))

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
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            if env.get(key) in (None, "", "sk-ant-none"):
                env.pop(key, None)
        return env

    # ── Convenience wrappers ───────────────────────────────────────────────────

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

    def get_session_directory(self, session_key: str) -> str | None:
        return get_session_directory(session_key)

    # ── BaseAgentAdapter implementation ───────────────────────────────────────

    async def run(
        self,
        question: str,
        session_id: str | None = None,
        agent_type: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        agent_id = kwargs.get("agent_id")
        cwd = self._resolve_cwd(agent_id)
        cmd = self._build_cmd(question, session_id, stream=False, agent_type=agent_type, cwd=cwd)
        timeout = self.config.get("timeout", 300)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
            cwd=cwd,
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

        our_session_id: str | None = kwargs.get("our_session_id") or session_id
        is_new: bool = kwargs.get("is_new_session", session_id is None)
        agent_id: str | None = kwargs.get("agent_id")

        # Resolve session_key: generate for new sessions, look up for existing ones.
        sk: str | None = kwargs.get("session_key")
        if not sk:
            if is_new:
                sk = make_session_key(agent_id or "default")
            elif our_session_id:
                sk = find_session_key_for_session_id(our_session_id)

        if not agent_id and sk:
            agent_id = _agent_id_from_key(sk)

        # For existing sessions, use the stored directory so user project selections are preserved.
        if not is_new and sk:
            stored_dir = get_session_directory(sk)
            effective_cwd = stored_dir if stored_dir and stored_dir not in (".", "") else self._resolve_cwd(agent_id)
        else:
            effective_cwd = self._resolve_cwd(agent_id)

        # Write preliminary sessions.json entry only for new sessions.
        if is_new and sk and our_session_id:
            write_preliminary_entry(sk, our_session_id, effective_cwd)

        # Resolve native --resume ID for existing sessions.
        native_resume_id: str | None = None
        if not is_new and sk:
            native_resume_id = get_native_session_id(sk)

        cmd = self._build_cmd(question, native_resume_id, stream=True, agent_type=agent_type, cwd=effective_cwd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
            cwd=effective_cwd,
        )

        native_session_id: str | None = None
        response_parts: list[str] = []
        result_text: str = ""
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
                result_text = (event.get("result") or "").strip()
                continue

            if event.get("type") == "token":
                response_parts.append(event.get("token", ""))

            yield event

        await proc.wait()

        # Fall back to result event text when no token events were captured.
        if not response_parts and result_text:
            response_parts.append(result_text)
            yield {"type": "token", "token": result_text}

        if sk and native_session_id:
            # Persist nativeSessionId so resume works; messages live in ~/.claude/projects/.
            agent_id_for_key = _agent_id_from_key(sk)
            index, index_path = _load_agent_index(agent_id_for_key)
            if sk in index:
                existing_usage = index[sk].get("usage") or {}
                index[sk]["nativeSessionId"] = native_session_id
                index[sk]["updatedAt"] = int(datetime.now(timezone.utc).timestamp() * 1000)
                index[sk]["usage"] = {
                    "input_tokens": existing_usage.get("input_tokens", 0) + usage.get("input_tokens", 0),
                    "output_tokens": existing_usage.get("output_tokens", 0) + usage.get("output_tokens", 0),
                    "cache_creation_input_tokens": existing_usage.get("cache_creation_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
                    "cache_read_input_tokens": existing_usage.get("cache_read_input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
                }
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
