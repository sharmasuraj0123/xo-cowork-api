"""
XO Cowork API Server
FastAPI server that interfaces with local Claude Code CLI.
"""

import os
import json
import datetime
import asyncio
import uuid
import threading
from typing import Optional, Dict, Any, AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
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

# Optional static token fallback for xo-swarm-api auth.
# Browser auth flow can populate token dynamically at runtime.
CHAT_API_TOKEN = os.getenv("CHAT_API_TOKEN", "").strip() or None

# XO backend browser-auth endpoints (new flow)
XO_AUTH_START_PATH = os.getenv("XO_AUTH_START_PATH", "/auth/browser/start")
XO_AUTH_STATUS_PATH = os.getenv("XO_AUTH_STATUS_PATH", "/auth/browser/status")
XO_AUTH_CONSUME_PATH = os.getenv("XO_AUTH_CONSUME_PATH", "/auth/browser/consume")
XO_GET_USER_ID_PATH = os.getenv("XO_GET_USER_ID_PATH", "/get-user-id")

# Claude Code CLI path (defaults to 'claude' assuming it's in PATH)
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH", "claude")

# HTTP client timeout settings
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Claude Code timeout (in seconds) - allow longer for complex queries
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))  # 5 minutes default


# =============================================================================
# Session Management
# =============================================================================


session_store: Dict[str, str] = {}
auth_lock = threading.Lock()
auth_state: Dict[str, Any] = {
    "access_token": CHAT_API_TOKEN,
    "refresh_token": None,
    "expires_at": None,
    "user_id": None,
    "auth_session_id": None,
}


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


def set_auth_token(
    access_token: str,
    refresh_token: Optional[str] = None,
    expires_in: Optional[int] = None,
    user_id: Optional[str] = None,
    auth_session_id: Optional[str] = None,
) -> None:
    """Store active auth token for outbound requests to xo-swarm-api."""
    expires_at = None
    if expires_in:
        expires_at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)).isoformat()
    with auth_lock:
        auth_state["access_token"] = access_token
        auth_state["refresh_token"] = refresh_token
        auth_state["expires_at"] = expires_at
        auth_state["user_id"] = user_id
        auth_state["auth_session_id"] = auth_session_id


def clear_auth_token() -> None:
    """Clear active auth token state."""
    with auth_lock:
        auth_state["access_token"] = None
        auth_state["refresh_token"] = None
        auth_state["expires_at"] = None
        auth_state["user_id"] = None
        auth_state["auth_session_id"] = None


def get_auth_token() -> Optional[str]:
    """Get active access token for outbound calls."""
    with auth_lock:
        return auth_state.get("access_token")


def get_auth_state() -> Dict[str, Any]:
    """Return a safe auth state snapshot (without exposing token value)."""
    with auth_lock:
        token = auth_state.get("access_token")
        return {
            "authenticated": bool(token),
            "user_id": auth_state.get("user_id"),
            "expires_at": auth_state.get("expires_at"),
            "auth_session_id": auth_state.get("auth_session_id"),
            "token_source": "dynamic_or_env" if token else "none",
        }



class AskQuestionRequest(BaseModel):
    """Request model for ask_question endpoints"""
    project_name: str
    question: str
    user_id: Optional[str] = "default_user"
    message_type: Optional[str] = "@xo"


class XOAuthStartRequest(BaseModel):
    """Start browser auth flow via xo-swarm-api."""
    scopes: Optional[str] = None
    client_reference: Optional[str] = None


class XOAuthConsumeRequest(BaseModel):
    """Consume completed browser auth flow."""
    auth_session_id: str
    poll_token: str


# =============================================================================
# External Chat API Client
# =============================================================================

class ChatAPIClient:
    """Client for external Chat API endpoints."""

    def __init__(self, base_url: str = CHAT_API_BASE_URL, token: Optional[str] = CHAT_API_TOKEN):
        self.base_url = base_url.rstrip("/")
        self._fallback_token = token

    def _headers(self) -> Dict[str, str]:
        token = get_auth_token() or self._fallback_token
        return {"Authorization": f"Bearer {token}"} if token else {}

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
                response = await client.post(url, json=payload, headers=self._headers())
                if response.status_code == 200:
                    print(f"✅ Pushed message: project={project_id}, type={message_type}")
                    return response.json()
                else:
                    print(f"⚠️ Failed to push message: {response.status_code} - {response.text}")
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
                response = await client.get(url, params=params, headers=self._headers())
                if response.status_code == 200:
                    data = response.json()
                    messages = data.get("messages", [])
                    print(f"✅ Fetched {len(messages)} messages: project={project_id}")
                    return messages
                else:
                    print(f"⚠️ Failed to fetch messages: {response.status_code} - {response.text}")
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
    print(f"   Chat API auth: {'enabled (dynamic token or CHAT_API_TOKEN)' if (get_auth_token() or CHAT_API_TOKEN) else 'not set'}")
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
        "auth": get_auth_state(),
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


@app.post("/xo-auth/start")
async def xo_auth_start(data: XOAuthStartRequest):
    """
    Start XO backend browser auth flow.
    Returns authorize_url + auth_session_id + poll_token.
    """
    url = f"{CHAT_API_BASE_URL.rstrip('/')}{XO_AUTH_START_PATH}"
    payload = {
        "scopes": data.scopes,
        "client_reference": data.client_reference,
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(url, json=payload)
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail={"error": "Failed to start auth flow", "upstream": response.text},
            )
        return response.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": f"Failed to start auth flow: {str(e)}"})


@app.get("/xo-auth/status/{auth_session_id}")
async def xo_auth_status(auth_session_id: str, poll_token: str):
    """Poll XO backend auth flow status."""
    url = f"{CHAT_API_BASE_URL.rstrip('/')}{XO_AUTH_STATUS_PATH}/{auth_session_id}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url, params={"poll_token": poll_token})
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail={"error": "Failed to check auth status", "upstream": response.text},
            )
        return response.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": f"Failed to check auth status: {str(e)}"})


@app.post("/xo-auth/consume")
async def xo_auth_consume(data: XOAuthConsumeRequest):
    """
    Consume auth flow and store token in-memory for outgoing XO backend calls.
    """
    url = f"{CHAT_API_BASE_URL.rstrip('/')}{XO_AUTH_CONSUME_PATH}"
    payload = {"auth_session_id": data.auth_session_id, "poll_token": data.poll_token}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(url, json=payload)
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail={"error": "Failed to consume auth flow", "upstream": response.text},
            )

        result = response.json()
        access_token = result.get("access_token")
        if not access_token:
            raise HTTPException(status_code=500, detail={"error": "No access token in consume response"})

        set_auth_token(
            access_token=access_token,
            refresh_token=result.get("refresh_token"),
            expires_in=result.get("expires_in"),
            user_id=result.get("user_id"),
            auth_session_id=result.get("auth_session_id"),
        )
        return {
            "success": True,
            "message": "Authentication completed and token stored",
            "user_id": result.get("user_id"),
            "expires_in": result.get("expires_in"),
            "scope": result.get("scope"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": f"Failed to consume auth flow: {str(e)}"})


@app.get("/xo-auth/whoami")
async def xo_auth_whoami():
    """
    Validate stored token against XO backend /get-user-id endpoint.
    """
    token = get_auth_token()
    if not token:
        raise HTTPException(status_code=401, detail={"error": "No stored access token. Complete /xo-auth flow first."})

    url = f"{CHAT_API_BASE_URL.rstrip('/')}{XO_GET_USER_ID_PATH}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail={"error": "Token validation failed", "upstream": response.text},
            )
        data = response.json()
        with auth_lock:
            auth_state["user_id"] = data.get("user_id")
        return {"success": True, "user_id": data.get("user_id")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": f"Failed to validate token: {str(e)}"})


@app.get("/xo-auth/state")
async def xo_auth_state():
    """Get current auth state (safe view)."""
    return get_auth_state()


@app.post("/xo-auth/logout")
async def xo_auth_logout():
    """Clear stored auth token state."""
    clear_auth_token()
    return {"success": True, "message": "Auth token cleared"}


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