"""
Centralized Claude Code client with Claude-native skills support.
"""

import asyncio
import json
import os
from typing import Optional, Dict, Any, AsyncGenerator


class ClaudeCodeClient:
    """Interface for Claude Code CLI with optional skill selection."""

    def __init__(
        self,
        cli_path: str = "claude",
        timeout_seconds: int = 300,
        permission_mode: Optional[str] = None,
        working_directory: Optional[str] = None,
        allowed_directories: Optional[list] = None,
    ):
        self.cli_path = cli_path
        self.timeout_seconds = timeout_seconds
        self.permission_mode = permission_mode or os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions")
        self.working_directory = working_directory or os.getenv("AI_WORKSPACE_ROOT", "/home/coder")
        self.allowed_directories = allowed_directories or [self.working_directory]

    @staticmethod
    def _skill_name(agent_type: Optional[str]) -> Optional[str]:
        """
        Convert optional frontend agent_type into a Claude skill name.

        Keeps backward compatibility with existing API clients while routing to
        Claude-native skills under .claude/skills.
        """
        if not agent_type:
            return None
        normalized = agent_type.strip().lower().replace("_", "-")
        return normalized or None

    def _build_prompt(self, question: str, agent_type: Optional[str]) -> str:
        """
        Build prompt using Claude-native skill invocation when agent_type is set.
        """
        skill_name = self._skill_name(agent_type)
        if not skill_name:
            return question
        return f"/{skill_name} {question}"

    def _base_cmd(self, session_id: Optional[str], is_new_session: bool) -> list:
        cmd = [self.cli_path]

        if is_new_session:
            cmd.extend(["--session-id", session_id])
        else:
            cmd.extend(["--resume", session_id])

        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])

        for directory in self.allowed_directories:
            if directory:
                cmd.extend(["--add-dir", directory])

        return cmd

    async def ask(
        self,
        question: str,
        session_id: Optional[str] = None,
        is_new_session: bool = False,
        agent_type: Optional[str] = None,
    ) -> str:
        """Send a question to Claude Code CLI (non-streaming)."""
        prompt = self._build_prompt(question=question, agent_type=agent_type)
        cmd = self._base_cmd(session_id=session_id, is_new_session=is_new_session)

        cmd.append("--print")
        cmd.extend(["--output-format", "json"])
        cmd.extend(["-p", prompt])

        print(f"🚀 Running: {' '.join(cmd[:6])} ...")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_directory,
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
        """
        Stream response from Claude Code CLI using normalized token events.

        Emits events:
        - {"type": "token", "token": "..."}
        - {"type": "error", "error": "..."}
        - {"type": "done"}
        """
        prompt = self._build_prompt(question=question, agent_type=agent_type)
        cmd = self._base_cmd(session_id=session_id, is_new_session=is_new_session)

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
                cwd=self.working_directory,
            )
            saw_token = False

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
                except json.JSONDecodeError:
                    event = {"type": "text", "content": line_str}

                event_type = event.get("type", "")

                if event_type == "assistant":
                    message = event.get("message", {})
                    content = message.get("content", [])
                    for block in content:
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                saw_token = True
                                yield {"type": "token", "token": text}

                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            saw_token = True
                            yield {"type": "token", "token": text}

                elif event_type == "result":
                    result = event.get("result", "")
                    if result and not saw_token:
                        saw_token = True
                        yield {"type": "token", "token": result}

                elif event_type == "text":
                    content = event.get("content", "")
                    if content:
                        saw_token = True
                        yield {"type": "token", "token": content}

                elif event_type == "error":
                    yield {"type": "error", "error": event.get("error", "Unknown error")}

            await process.wait()

            if process.returncode != 0:
                stderr = await process.stderr.read()
                error_msg = stderr.decode().strip()
                if error_msg:
                    print(f"❌ Stream stderr: {error_msg}")
                    yield {"type": "error", "error": error_msg}

            print("✅ Stream completed")
            yield {"type": "done"}

        except Exception as e:
            print(f"❌ Stream error: {str(e)}")
            yield {"type": "error", "error": str(e)}
