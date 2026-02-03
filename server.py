"""
XO Cowork API Server
FastAPI server that interfaces with local Claude Code CLI.
"""

import os
import json
import datetime
import asyncio
import uuid
from typing import Optional, Dict, Any, AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import uvicorn

# Load environment variables
load_dotenv()


# =============================================================================
# Configuration
# =============================================================================

# External Chat API base URL (xo-swarm-api or similar)
CHAT_API_BASE_URL = os.getenv("CHAT_API_BASE_URL", "http://localhost:5001")

# Claude Code CLI path (defaults to 'claude' assuming it's in PATH)
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH", "claude")

# HTTP client timeout settings
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Claude Code timeout (in seconds) - allow longer for complex queries
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))  # 5 minutes default


# =============================================================================
# Session Management
# =============================================================================

# In-memory session storage: { project_id: session_id }
# In production, consider using Redis or database
session_store: Dict[str, str] = {}


def get_session_id(project_id: str) -> Optional[str]:
    """Get existing session ID for a project."""
    return session_store.get(project_id)


def create_session_id(project_id: str) -> str:
    """Create and store a new session ID for a project."""
    session_id = str(uuid.uuid4())
    session_store[project_id] = session_id
    print(f"📝 Created session {session_id} for project {project_id}")
    return session_id


def clear_session(project_id: str) -> None:
    """Clear session for a project."""
    if project_id in session_store:
        del session_store[project_id]
        print(f"🗑️ Cleared session for project {project_id}")


# =============================================================================
# Pydantic Models
# =============================================================================

class AskQuestionRequest(BaseModel):
    """Request model for ask_question endpoints"""
    project_name: str
    question: str
    user_id: Optional[str] = "default_user"
    message_type: Optional[str] = "@xo"


class AskQuestionResponse(BaseModel):
    """Response model for ask_question endpoint"""
    id: Optional[str] = None
    message: str
    project_id: str
    user_id: str
    session_id: Optional[str] = None
    timestamp: str


# =============================================================================
# External Chat API Client
# =============================================================================

class ChatAPIClient:
    """Client for external Chat API endpoints."""

    def __init__(self, base_url: str = CHAT_API_BASE_URL):
        self.base_url = base_url.rstrip("/")

    async def push_message(
        self,
        project_id: str,
        user_id: str,
        message: str,
        message_type: str = "@xo"
    ) -> Optional[Dict[str, Any]]:
        """Push a message to the chat storage via external API."""
        url = f"{self.base_url}/chat/add_message"
        payload = {
            "project_id": project_id,
            "user_id": user_id,
            "message": message,
            "type": message_type
        }

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 200:
                    print(f"✅ Pushed message: project={project_id}, type={message_type}")
                    return response.json()
                else:
                    print(f"⚠️ Failed to push message: {response.status_code}")
                    return None
        except Exception as e:
            print(f"⚠️ Chat API error: {str(e)}")
            return None

    async def fetch_messages(
        self,
        project_id: str,
        limit: int = 50
    ) -> Optional[list]:
        """Fetch messages from the chat storage."""
        url = f"{self.base_url}/chat/get_messages"
        params = {"project_id": project_id, "limit": limit}

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.get(url, params=params)
                if response.status_code == 200:
                    data = response.json()
                    messages = data.get("messages", [])
                    print(f"✅ Fetched {len(messages)} messages: project={project_id}")
                    return messages
                else:
                    print(f"⚠️ Failed to fetch messages: {response.status_code}")
                    return None
        except Exception as e:
            print(f"⚠️ Chat API error: {str(e)}")
            return None

    async def get_message_count(self, project_id: str) -> int:
        """Get message count for a project."""
        messages = await self.fetch_messages(project_id, limit=100)
        return len(messages) if messages else 0


# Global chat client
chat_client = ChatAPIClient()


# =============================================================================
# Claude Code CLI Interface
# =============================================================================

class ClaudeCodeClient:
    """Interface for Claude Code CLI."""

    def __init__(self, cli_path: str = CLAUDE_CLI_PATH):
        self.cli_path = cli_path

    async def ask(
        self,
        question: str,
        session_id: Optional[str] = None,
        is_new_session: bool = False
    ) -> str:
        """
        Send a question to Claude Code CLI (non-streaming).

        Args:
            question: The question to ask
            session_id: Session ID (required)
            is_new_session: Whether this is a new session

        Returns:
            Response text
        """
        cmd = [self.cli_path]

        # Session management
        if is_new_session:
            # New session: use --session-id to set the ID
            cmd.extend(["--session-id", session_id])
        else:
            # Existing session: use --resume
            cmd.extend(["--resume", session_id])

        # Use print mode for non-interactive output
        cmd.append("--print")

        # Output as JSON for easier parsing
        cmd.extend(["--output-format", "json"])

        # Add the question
        cmd.extend(["-p", question])

        print(f"🚀 Running: {' '.join(cmd)}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=CLAUDE_TIMEOUT
            )

            if process.returncode != 0:
                error_msg = stderr.decode().strip() if stderr else "Unknown error"
                print(f"❌ Claude Code error (code {process.returncode}): {error_msg}")
                raise Exception(f"Claude Code failed: {error_msg}")

            # Parse response
            output = stdout.decode().strip()

            # Try to parse as JSON
            try:
                result = json.loads(output)
                # JSON output has "result" field
                response_text = result.get("result", output)
            except json.JSONDecodeError:
                # If not JSON, use raw output
                response_text = output

            print(f"✅ Claude Code responded ({len(response_text)} chars)")
            return response_text

        except asyncio.TimeoutError:
            print(f"❌ Claude Code timeout after {CLAUDE_TIMEOUT}s")
            raise Exception(f"Claude Code timed out after {CLAUDE_TIMEOUT} seconds")

    async def ask_streaming(
        self,
        question: str,
        session_id: Optional[str] = None,
        is_new_session: bool = False
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Stream response from Claude Code CLI.

        Uses --output-format stream-json for real-time streaming.

        Yields parsed JSON events from Claude Code.
        """
        cmd = [self.cli_path]

        # Session management
        if is_new_session:
            cmd.extend(["--session-id", session_id])
        else:
            cmd.extend(["--resume", session_id])

        # Use print mode with streaming JSON output (requires --verbose)
        cmd.append("--print")
        cmd.append("--verbose")
        cmd.extend(["--output-format", "stream-json"])

        # Add the question
        cmd.extend(["-p", question])

        print(f"🚀 Streaming: {' '.join(cmd)}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            # Stream stdout line by line
            while True:
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=CLAUDE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    print(f"❌ Stream timeout")
                    yield {"type": "error", "error": "Stream timeout"}
                    break

                if not line:
                    break

                line_str = line.decode().strip()
                if not line_str:
                    continue

                # Parse JSON event from Claude Code
                try:
                    event = json.loads(line_str)
                    yield event
                except json.JSONDecodeError:
                    # Non-JSON line, yield as text
                    yield {"type": "text", "content": line_str}

            await process.wait()

            if process.returncode != 0:
                stderr = await process.stderr.read()
                error_msg = stderr.decode().strip()
                if error_msg:
                    print(f"❌ Stream stderr: {error_msg}")

            print(f"✅ Stream completed")

        except Exception as e:
            print(f"❌ Stream error: {str(e)}")
            yield {"type": "error", "error": str(e)}


# Global Claude Code client
claude_client = ClaudeCodeClient()


# =============================================================================
# Helper Functions
# =============================================================================

async def save_chat_messages(
    project_id: str,
    user_id: str,
    user_message: str,
    agent_response: str,
    message_type: str = "@xo"
) -> None:
    """Save both user message and agent response to chat storage."""
    await chat_client.push_message(
        project_id=project_id,
        user_id=user_id,
        message=user_message,
        message_type=message_type
    )
    await chat_client.push_message(
        project_id=project_id,
        user_id=user_id,
        message=agent_response,
        message_type="agent"
    )


# =============================================================================
# FastAPI Application
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    print("🚀 Starting XO Cowork API Server...")
    print(f"   Chat API: {CHAT_API_BASE_URL}")
    print(f"   Claude CLI: {CLAUDE_CLI_PATH}")
    print(f"   Timeout: {CLAUDE_TIMEOUT}s")
    yield
    print("👋 Shutting down XO Cowork API Server...")


app = FastAPI(
    title="XO Cowork API",
    description="XO Cowork API - Claude Code Interface",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# Endpoints
# =============================================================================

@app.get("/")
async def root():
    """Root endpoint."""
    return {"status": "XO Cowork API running"}


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.datetime.now().isoformat(),
        "chat_api_url": CHAT_API_BASE_URL,
        "claude_cli": CLAUDE_CLI_PATH,
        "active_sessions": len(session_store)
    }


@app.get("/sessions")
async def list_sessions():
    """List all active sessions."""
    return {
        "sessions": session_store,
        "count": len(session_store)
    }


@app.delete("/sessions/{project_id}")
async def delete_session(project_id: str):
    """Delete a session for a project."""
    if project_id in session_store:
        clear_session(project_id)
        return {"success": True, "message": f"Session cleared for {project_id}"}
    return {"success": False, "message": f"No session found for {project_id}"}


@app.post("/ask_question")
async def ask_question(data: AskQuestionRequest):
    """
    Send a question to Claude Code (non-streaming).

    - First message creates a new session
    - Subsequent messages resume the existing session
    """
    try:
        # Check if this is a new session
        session_id = get_session_id(data.project_name)
        is_new = session_id is None

        if is_new:
            # Generate session ID but don't store yet (only store after success)
            session_id = str(uuid.uuid4())
            print(f"🆕 New session for project: {data.project_name} -> {session_id}")
        else:
            print(f"🔄 Resuming session {session_id} for project: {data.project_name}")

        # Send to Claude Code
        response = await claude_client.ask(
            question=data.question,
            session_id=session_id,
            is_new_session=is_new
        )

        # Store session ID only after successful first call
        if is_new:
            session_store[data.project_name] = session_id
            print(f"📝 Stored session {session_id} for project {data.project_name}")

        # Save to chat storage
        await save_chat_messages(
            project_id=data.project_name,
            user_id=data.user_id,
            user_message=data.question,
            agent_response=response,
            message_type=data.message_type
        )

        return {
            "id": None,
            "message": response,
            "project_id": data.project_name,
            "user_id": data.user_id,
            "session_id": session_id,
            "is_new_session": is_new,
            "timestamp": datetime.datetime.now().isoformat()
        }

    except Exception as e:
        import traceback
        print(f"❌ Error: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail={"error": f"Failed to process question: {str(e)}"}
        )


@app.post("/ask_question_streaming")
async def ask_question_streaming(data: AskQuestionRequest):
    """
    Send a question to Claude Code (streaming).

    Returns SSE stream of response tokens.

    Claude Code stream-json events include:
    - {"type": "assistant", "message": {...}} - Assistant message chunks
    - {"type": "result", "result": "..."} - Final result
    """
    try:
        # Check if this is a new session
        session_id = get_session_id(data.project_name)
        is_new = session_id is None

        if is_new:
            # Generate session ID but don't store yet (only store after success)
            session_id = str(uuid.uuid4())
            print(f"🆕 New streaming session: {data.project_name} -> {session_id}")
        else:
            print(f"🔄 Resuming streaming session: {session_id}")

        # Buffer for full response
        full_response_parts = []
        stream_success = False

        async def generate_stream():
            nonlocal stream_success
            try:
                async for event in claude_client.ask_streaming(
                    question=data.question,
                    session_id=session_id,
                    is_new_session=is_new
                ):
                    # Handle different event types from Claude Code
                    event_type = event.get("type", "")

                    if event_type == "assistant":
                        # Assistant message with content
                        message = event.get("message", {})
                        content = message.get("content", [])
                        for block in content:
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                if text:
                                    full_response_parts.append(text)
                                    yield f"data: {json.dumps({'type': 'token', 'token': text})}\n\n"

                    elif event_type == "content_block_delta":
                        # Streaming delta
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                full_response_parts.append(text)
                                yield f"data: {json.dumps({'type': 'token', 'token': text})}\n\n"

                    elif event_type == "result":
                        # Final result
                        result = event.get("result", "")
                        if result and not full_response_parts:
                            full_response_parts.append(result)
                            yield f"data: {json.dumps({'type': 'token', 'token': result})}\n\n"

                    elif event_type == "error":
                        stream_success = False  # Mark as failed
                        yield f"data: {json.dumps({'type': 'error', 'error': event.get('error', 'Unknown error')})}\n\n"

                    elif event_type == "text":
                        # Raw text fallback
                        content = event.get("content", "")
                        if content:
                            full_response_parts.append(content)
                            yield f"data: {json.dumps({'type': 'token', 'token': content})}\n\n"

                # Send done event
                yield f"data: {json.dumps({'done': True})}\n\n"
                stream_success = True

            except Exception as e:
                print(f"❌ Stream generation error: {str(e)}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                stream_success = False  # Ensure we don't store session on error

            finally:
                # Store session ID only after successful first call
                if is_new and stream_success and full_response_parts:
                    session_store[data.project_name] = session_id
                    print(f"📝 Stored session {session_id} for project {data.project_name}")

                # Save to chat storage
                final_response = "".join(full_response_parts)
                if final_response:
                    await save_chat_messages(
                        project_id=data.project_name,
                        user_id=data.user_id,
                        user_message=data.question,
                        agent_response=final_response,
                        message_type=data.message_type
                    )
                    print(f"✅ Saved response ({len(final_response)} chars)")

        return StreamingResponse(
            generate_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            }
        )

    except Exception as e:
        import traceback
        print(f"❌ Error: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail={"error": f"Failed to process question: {str(e)}"}
        )


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5002"))

    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        reload=True
    )
