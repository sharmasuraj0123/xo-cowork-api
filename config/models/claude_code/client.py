"""
Centralized Claude Code client with Claude-native skills support.
"""

import asyncio
import json
import os
from typing import Optional, Dict, Any, AsyncGenerator

from services.cowork_agent.adapters.stream_lines import LineOverflow, iter_lines
from services.cowork_agent.adapters.subprocess_io import (
    STREAM_LIMIT,
    drain_stderr,
    overflow_notice,
    reap,
)


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
        # Maps API-level logical session IDs to provider-native conversation IDs.
        self._session_map: Dict[str, str] = {}
        # Enforce OAuth token auth for Claude subprocesses.
        self._required_oauth_env_key = "CLAUDE_CODE_OAUTH_TOKEN"

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

    def _resolve_session_id(self, session_id: Optional[str]) -> Optional[str]:
        if not session_id:
            return None
        return self._session_map.get(session_id, session_id)

    @staticmethod
    def _extract_session_id(payload: Any) -> Optional[str]:
        """Best-effort extraction of provider-native session/conversation id."""
        keys = {"session_id", "sessionId", "conversation_id", "conversationId"}

        if isinstance(payload, dict):
            for key in keys:
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in payload.values():
                discovered = ClaudeCodeClient._extract_session_id(value)
                if discovered:
                    return discovered
            return None

        if isinstance(payload, list):
            for item in payload:
                discovered = ClaudeCodeClient._extract_session_id(item)
                if discovered:
                    return discovered
            return None

        return None

    def _base_cmd(self, session_id: Optional[str], is_new_session: bool) -> list:
        cmd = [self.cli_path]

        if not is_new_session:
            resolved_session_id = self._resolve_session_id(session_id)
            if not resolved_session_id:
                raise Exception("Claude resume requires a session_id")
            cmd.extend(["--resume", resolved_session_id])

        if self.permission_mode:
            if self.permission_mode == "bypassPermissions":
                cmd.append("--dangerously-skip-permissions")
            else:
                cmd.extend(["--permission-mode", self.permission_mode])

        for directory in self.allowed_directories:
            if directory:
                cmd.extend(["--add-dir", directory])

        return cmd

    def _build_subprocess_env(self) -> Dict[str, str]:
        """
        Build subprocess env that strictly uses CLAUDE_CODE_OAUTH_TOKEN auth.
        """
        oauth_token = (os.getenv(self._required_oauth_env_key, "") or "").strip()
        if not oauth_token:
            raise Exception(
                f"Missing required env var: {self._required_oauth_env_key}. "
                "Cowork enforces OAuth-token auth for Claude."
            )

        env = os.environ.copy()
        env[self._required_oauth_env_key] = oauth_token

        # Prevent fallback to other Anthropic auth modes.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_OAUTH_API_KEY", None)

        return env

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
            subprocess_env = self._build_subprocess_env()
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_directory,
                env=subprocess_env,
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
            native_session_id: Optional[str] = None

            try:
                result = json.loads(output)
                response_text = result.get("result", output)
                native_session_id = self._extract_session_id(result)
            except json.JSONDecodeError:
                response_text = output

            if is_new_session and session_id and native_session_id:
                self._session_map[session_id] = native_session_id

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
            subprocess_env = self._build_subprocess_env()
            process = await asyncio.create_subprocess_exec(
                *cmd,
                # Hygiene only (the server's stdin is already /dev/null): the CLI
                # must never inherit a stdin it could block on.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Transport buffer bound. It is NOT what makes an oversized line
                # survivable — ``iter_lines`` below reads with ``read(n)``, which
                # has no line-length limit, replacing a ``readline()`` that raised
                # ValueError at 64 KiB and destroyed the rest of the turn.
                limit=STREAM_LIMIT,
                cwd=self.working_directory,
                env=subprocess_env,
            )

            # Drain stderr from the instant it exists. Piped-and-unread is a
            # deadlock, not a nuisance: the child blocks in write(2) once the
            # transport pauses (measured at 2,228,224 B with limit=1 MiB), so it
            # never exits, so ``process.wait()`` below never returns, so the SSE
            # turn never terminates and the process is wedged permanently.
            # Reproduced end-to-end against this client with a CLI that writes
            # 6 MiB to stderr. The tail is bounded, decoded with errors=replace
            # and secret-scrubbed, which is also what stops a non-UTF-8 stderr
            # from raising ``UnicodeDecodeError`` out of the error path below
            # and skipping the terminal ``done``.
            stderr_tail = drain_stderr(process.stderr)

            saw_token = False
            timed_out = False
            native_session_id: Optional[str] = None
            overflows: list[LineOverflow] = []

            # The per-line timeout is load-bearing here and is preserved exactly:
            # it used to wrap ``readline()``, it now wraps "the next complete
            # line", which may span several chunk reads.
            lines = iter_lines(process.stdout, on_overflow=overflows.append)
            while True:
                try:
                    line = await asyncio.wait_for(
                        lines.__anext__(),
                        timeout=self.timeout_seconds,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    print("❌ Stream timeout")
                    yield {"type": "error", "error": "Stream timeout"}
                    timed_out = True
                    break

                # One dropped oversized line costs that line and nothing else,
                # so it is LOGGED, not yielded. Plane A's event vocabulary is
                # only token/error/done, and server.py treats any ``error``
                # event as turn failure: it sets ``stream_success = False``,
                # which suppresses both the session store and the chat save. A
                # notice whose own text says the turn continued must not be the
                # thing that discards the turn. (Plane B has a non-fatal channel
                # and routes the identical notice through it.)
                while overflows:
                    o = overflows.pop(0)
                    print(f"⚠️  {overflow_notice(o.dropped_bytes, o.max_line, o.at_eof)}")

                line_str = line.decode(errors="replace").strip()
                if not line_str:
                    continue

                try:
                    event = json.loads(line_str)
                    extracted = self._extract_session_id(event)
                    if extracted:
                        native_session_id = extracted
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

            # An overflow can fire on the very last line before EOF, i.e. after
            # the final iteration — drain what the callback collected.
            while overflows:
                o = overflows.pop(0)
                print(f"⚠️  {overflow_notice(o.dropped_bytes, o.max_line, o.at_eof)}")
            await lines.aclose()

            # Bounded and non-killing; see subprocess_io.reap. ``abandoned``
            # says we stopped reading first, so the child is still running and
            # there is nothing to wait for — waiting anyway is what left the SSE
            # turn hanging forever after a "Stream timeout".
            returncode = await reap(process, abandoned=timed_out)

            if returncode not in (0, None):
                await stderr_tail.settle()
                error_msg = stderr_tail.text()
                if error_msg:
                    print(f"❌ Stream stderr: {error_msg}")
                    yield {"type": "error", "error": error_msg}

            if is_new_session and session_id and native_session_id:
                self._session_map[session_id] = native_session_id

            print("✅ Stream completed")
            yield {"type": "done"}

        except Exception as e:
            print(f"❌ Stream error: {str(e)}")
            yield {"type": "error", "error": str(e)}
