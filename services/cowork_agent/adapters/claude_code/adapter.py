from __future__ import annotations
import asyncio
import json
import os
from typing import Any, AsyncIterator

from services.cowork_agent.adapters.base import BaseAgentAdapter


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

        cmd = self._build_cmd(question, session_id, stream=True, agent_type=agent_type)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
        )

        native_session_id: str | None = None

        async for raw_line in proc.stdout:
            event = parse_stream_line(raw_line)
            if event is None:
                continue
            if event.get("type") == "result":
                native_session_id = event.get("session_id")
                continue
            yield event

        await proc.wait()
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
