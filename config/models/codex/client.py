"""
Codex CLI client with agent-skill support and normalized streaming events.
"""

import asyncio
import json
from typing import Optional, Dict, Any, AsyncGenerator


class CodexCodeClient:
    """Interface for Codex CLI in non-interactive mode."""

    def __init__(
        self,
        cli_path: str = "codex",
        timeout_seconds: int = 300,
    ):
        self.cli_path = cli_path
        self.timeout_seconds = timeout_seconds
        # Maps API-level logical session IDs to Codex thread IDs.
        self._thread_map: Dict[str, str] = {}

    @staticmethod
    def _skill_name(agent_type: Optional[str]) -> Optional[str]:
        """Convert frontend agent_type into a Codex skill name."""
        if not agent_type:
            return None
        normalized = agent_type.strip().lower().replace("_", "-")
        return normalized or None

    def _build_prompt(self, question: str, agent_type: Optional[str]) -> str:
        """
        Build prompt using explicit Codex skill invocation when agent_type is set.
        """
        skill_name = self._skill_name(agent_type)
        if not skill_name:
            return question
        return f"${skill_name} {question}"

    def _resolve_thread_id(self, session_id: Optional[str]) -> Optional[str]:
        if not session_id:
            return None
        return self._thread_map.get(session_id, session_id)

    @staticmethod
    def _extract_text_from_item(item: Dict[str, Any]) -> str:
        """Extract best-effort text from Codex JSON event item payloads."""
        if not item:
            return ""

        text = item.get("text")
        if isinstance(text, str) and text:
            return text

        message = item.get("message")
        if isinstance(message, dict):
            msg_text = message.get("text")
            if isinstance(msg_text, str) and msg_text:
                return msg_text

            content = message.get("content", [])
            if isinstance(content, list):
                chunks = []
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        chunks.append(part["text"])
                return "".join(chunks)

        return ""

    def _build_cmd(
        self,
        prompt: str,
        session_id: Optional[str],
        is_new_session: bool,
    ) -> list:
        if is_new_session:
            return [self.cli_path, "exec", "--json", prompt]

        resolved_thread_id = self._resolve_thread_id(session_id)
        if not resolved_thread_id:
            raise Exception("Codex resume requires a session_id")
        return [self.cli_path, "exec", "resume", "--json", resolved_thread_id, prompt]

    async def ask(
        self,
        question: str,
        session_id: Optional[str] = None,
        is_new_session: bool = False,
        agent_type: Optional[str] = None,
    ) -> str:
        """Send a question to Codex CLI (non-streaming)."""
        prompt = self._build_prompt(question=question, agent_type=agent_type)
        cmd = self._build_cmd(prompt=prompt, session_id=session_id, is_new_session=is_new_session)

        print(f"🚀 Running: {' '.join(cmd[:5])} ...")

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
                print(f"❌ Codex error (code {process.returncode}): {error_msg}")
                raise Exception(f"Codex failed: {error_msg}")

            output = stdout.decode().strip()
            if not output:
                return ""

            full_parts = []
            thread_id = None

            for line in output.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")
                if event_type == "thread.started":
                    thread_id = event.get("thread_id") or thread_id
                    continue

                item = event.get("item", {})
                if event_type.startswith("item.") and isinstance(item, dict):
                    text = self._extract_text_from_item(item)
                    if text:
                        full_parts.append(text)

            if is_new_session and session_id and thread_id:
                self._thread_map[session_id] = thread_id

            response_text = "".join(full_parts).strip()
            print(f"✅ Codex responded ({len(response_text)} chars)")
            return response_text

        except asyncio.TimeoutError:
            print(f"❌ Codex timeout after {self.timeout_seconds}s")
            raise Exception(f"Codex timed out after {self.timeout_seconds} seconds")

    async def ask_streaming(
        self,
        question: str,
        session_id: Optional[str] = None,
        is_new_session: bool = False,
        agent_type: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Stream response from Codex CLI with normalized token events.

        Emits events:
        - {"type": "token", "token": "..."}
        - {"type": "error", "error": "..."}
        - {"type": "done"}
        """
        prompt = self._build_prompt(question=question, agent_type=agent_type)
        cmd = self._build_cmd(prompt=prompt, session_id=session_id, is_new_session=is_new_session)

        print(f"🚀 Streaming: {' '.join(cmd[:5])} ...")

        thread_id = None

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
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")
                if event_type == "thread.started":
                    thread_id = event.get("thread_id") or thread_id
                    continue

                item = event.get("item", {})
                if event_type.startswith("item.") and isinstance(item, dict):
                    text = self._extract_text_from_item(item)
                    if text:
                        yield {"type": "token", "token": text}

                elif event_type == "error":
                    err = event.get("error")
                    if isinstance(err, dict):
                        err = err.get("message", "Unknown error")
                    yield {"type": "error", "error": str(err or "Unknown error")}

                elif event_type == "turn.failed":
                    yield {"type": "error", "error": "Codex turn failed"}

            await process.wait()

            if process.returncode != 0:
                stderr = await process.stderr.read()
                error_msg = stderr.decode().strip()
                if error_msg:
                    print(f"❌ Stream stderr: {error_msg}")
                    yield {"type": "error", "error": error_msg}

            if is_new_session and session_id and thread_id:
                self._thread_map[session_id] = thread_id

            print("✅ Stream completed")
            yield {"type": "done"}

        except Exception as e:
            print(f"❌ Stream error: {str(e)}")
            yield {"type": "error", "error": str(e)}
