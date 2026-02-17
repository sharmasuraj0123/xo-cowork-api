"""
Centralized Claude Code client with instruction-based agent profiles.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Optional, Dict, Any, AsyncGenerator


class ClaudeCodeClient:
    """Interface for Claude Code CLI with instruction-backed agents."""

    def __init__(
        self,
        cli_path: str = "claude",
        timeout_seconds: int = 300,
        instructions_dir: str = "instructions",
        default_agent: str = "default",
    ):
        self.cli_path = cli_path
        self.timeout_seconds = timeout_seconds
        self.instructions_dir = Path(instructions_dir)
        self.default_agent = default_agent

    def _ensure_instructions_dir(self) -> None:
        """Create instructions directory if it does not exist."""
        self.instructions_dir.mkdir(parents=True, exist_ok=True)

    def _load_agent_instructions(self) -> Dict[str, str]:
        """
        Load instruction files into a map:
        - instructions/<agent>.md
        - instructions/<agent>.txt
        """
        self._ensure_instructions_dir()
        instructions: Dict[str, str] = {}

        for file_path in self.instructions_dir.iterdir():
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in {".md", ".txt"}:
                continue

            agent_name = file_path.stem.strip()
            if not agent_name:
                continue

            try:
                content = file_path.read_text(encoding="utf-8").strip()
                if content:
                    instructions[agent_name] = content
            except Exception as e:
                print(f"⚠️ Failed to read instruction file {file_path}: {str(e)}")

        return instructions

    def list_agents(self) -> list:
        """List available agents from instruction files."""
        return sorted(self._load_agent_instructions().keys())

    def _build_prompt(self, question: str, agent_type: Optional[str]) -> str:
        """
        Build final prompt using selected agent instructions.

        Fallback order:
        1) requested agent_type
        2) default_agent
        3) no instruction (raw question)
        """
        selected_agent = (agent_type or "").strip() or self.default_agent
        instructions = self._load_agent_instructions()

        instruction_text = instructions.get(selected_agent)
        if instruction_text is None and selected_agent != self.default_agent:
            instruction_text = instructions.get(self.default_agent)

        if not instruction_text:
            return question

        # Keep a stable, explicit format so instruction files are easy to reason about.
        return (
            f"{instruction_text}\n\n"
            "User request:\n"
            f"{question}"
        )

    async def ask(
        self,
        question: str,
        session_id: Optional[str] = None,
        is_new_session: bool = False,
        agent_type: Optional[str] = None,
    ) -> str:
        """Send a question to Claude Code CLI (non-streaming)."""
        prompt = self._build_prompt(question=question, agent_type=agent_type)
        cmd = [self.cli_path]

        if is_new_session:
            cmd.extend(["--session-id", session_id])
        else:
            cmd.extend(["--resume", session_id])

        cmd.append("--print")
        cmd.extend(["--output-format", "json"])
        cmd.extend(["-p", prompt])

        print(f"🚀 Running: {' '.join(cmd[:6])} ...")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout_seconds,
            )

            if process.returncode != 0:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                print(f"❌ Claude Code error (code {process.returncode}): {error_msg}")
                raise Exception(f"Claude Code failed: {error_msg}")

            output = stdout.decode().strip()

            try:
                result = json.loads(output)
                response_text = result.get("result", output)
            except json.JSONDecodeError:
                response_text = output

            print(f"✅ Claude Code responded ({len(response_text)} chars)")
            return response_text

        except asyncio.TimeoutError:
            print(f"❌ Claude Code timeout after {self.timeout_seconds}s")
            raise Exception(f"Claude Code timed out after {self.timeout_seconds} seconds")

    async def ask_streaming(
        self,
        question: str,
        session_id: Optional[str] = None,
        is_new_session: bool = False,
        agent_type: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream response from Claude Code CLI using stream-json format."""
        prompt = self._build_prompt(question=question, agent_type=agent_type)
        cmd = [self.cli_path]

        if is_new_session:
            cmd.extend(["--session-id", session_id])
        else:
            cmd.extend(["--resume", session_id])

        cmd.append("--print")
        cmd.append("--verbose")
        cmd.extend(["--output-format", "stream-json"])
        cmd.extend(["-p", prompt])

        print(f"🚀 Streaming: {' '.join(cmd[:7])} ...")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            while True:
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=self.timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    print("❌ Stream timeout")
                    yield {"type": "error", "error": "Stream timeout"}
                    break

                if not line:
                    break

                line_str = line.decode().strip()
                if not line_str:
                    continue

                try:
                    event = json.loads(line_str)
                    yield event
                except json.JSONDecodeError:
                    yield {"type": "text", "content": line_str}

            await process.wait()

            if process.returncode != 0:
                stderr = await process.stderr.read()
                error_msg = stderr.decode().strip()
                if error_msg:
                    print(f"❌ Stream stderr: {error_msg}")

            print("✅ Stream completed")

        except Exception as e:
            print(f"❌ Stream error: {str(e)}")
            yield {"type": "error", "error": str(e)}
