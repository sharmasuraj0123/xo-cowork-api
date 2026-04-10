"""
XO Cowork API Server
FastAPI server that interfaces with local Claude Code CLI.
"""

import os
import json
import datetime
import uuid
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import uvicorn
from claude_code_client import ClaudeCodeClient
from codex_code_client import CodexCodeClient

# Load environment variables
load_dotenv()

from routers.auth import (
    XO_API_KEY,
    consume_auth_flow,
    get_auth_token,
    get_auth_state,
    router as auth_router,
)
from routers.claude_setup_token import router as claude_setup_token_router
from routers.codex_setup import router as codex_setup_router
from routers.openclaw_usage import router as openclaw_usage_router


# =============================================================================
# Configuration
# =============================================================================

# External Chat API base URL (xo-swarm-api or similar)
CHAT_API_BASE_URL = os.getenv("CHAT_API_BASE_URL", "https://api-swarm-beta.xo.builders")
STAGE = (os.getenv("STAGE", "beta") or "beta").strip().lower()
IS_LOCAL_STAGE = STAGE == "local"

# Resolve runtime paths using stage-aware defaults.
def _resolve_cli_path(env_var: str, default_cmd: str, beta_default: str) -> str:
    configured = (os.getenv(env_var, "") or "").strip()
    if configured:
        if IS_LOCAL_STAGE and os.path.isabs(configured) and not os.path.exists(configured):
            print(
                f"⚠️ {env_var} points to missing path: {configured}. "
                f"Falling back to '{default_cmd}' from PATH."
            )
        elif IS_LOCAL_STAGE:
            return configured
        else:
            return configured

    if IS_LOCAL_STAGE:
        discovered = shutil.which(default_cmd)
        if discovered:
            return discovered
        return default_cmd

    return beta_default


def _resolve_workspace_root() -> str:
    configured = (os.getenv("AI_WORKSPACE_ROOT", "") or "").strip()
    if configured:
        if IS_LOCAL_STAGE and not os.path.isdir(configured):
            print(
                f"⚠️ AI_WORKSPACE_ROOT does not exist: {configured}. "
                "Falling back to project directory."
            )
        else:
            return configured

    if IS_LOCAL_STAGE:
        return str(Path(__file__).resolve().parent)

    return "/home/coder"


# Claude Code CLI path
CLAUDE_CLI_PATH = _resolve_cli_path(
    env_var="CLAUDE_CLI_PATH",
    default_cmd="claude",
    beta_default="/home/coder/.local/bin/claude",
)
# Codex CLI path
CODEX_CLI_PATH = _resolve_cli_path(
    env_var="CODEX_CLI_PATH",
    default_cmd="codex",
    beta_default="codex",
)


AI_WORKSPACE_ROOT = _resolve_workspace_root()

if IS_LOCAL_STAGE and not os.path.isdir(AI_WORKSPACE_ROOT):
    print(
        f"⚠️ AI_WORKSPACE_ROOT does not exist: {AI_WORKSPACE_ROOT}. "
        "Falling back to project directory (local stage)."
    )
    AI_WORKSPACE_ROOT = str(Path(__file__).resolve().parent)

# HTTP client timeout settings
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Claude Code timeout (in seconds) - allow longer for complex queries
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))  # 5 minutes default
# Codex timeout (in seconds)
CODEX_TIMEOUT = int(os.getenv("CODEX_TIMEOUT", str(CLAUDE_TIMEOUT)))

# Runtime provider switch: claude | codex
AI_PROVIDER = os.getenv("AI_PROVIDER", "claude").strip().lower()
CLAUDE_PERMISSION_MODE = os.getenv("CLAUDE_PERMISSION_MODE", "bypassPermissions").strip()

def _claude_auth_debug_snapshot() -> Dict[str, Any]:
    """
    Return a safe, non-secret snapshot of Claude auth-related environment state.
    """
    claude_oauth_token = (os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "") or "").strip()
    anthropic_api_key = (os.getenv("ANTHROPIC_API_KEY", "") or "").strip()
    anthropic_oauth_api_key = (os.getenv("ANTHROPIC_OAUTH_API_KEY", "") or "").strip()

    has_claude_oauth = bool(claude_oauth_token)
    has_anthropic_api = bool(anthropic_api_key)
    has_anthropic_oauth_api = bool(anthropic_oauth_api_key)

    present_sources = []
    if has_claude_oauth:
        present_sources.append("CLAUDE_CODE_OAUTH_TOKEN")
    if has_anthropic_oauth_api:
        present_sources.append("ANTHROPIC_OAUTH_API_KEY")
    if has_anthropic_api:
        present_sources.append("ANTHROPIC_API_KEY")

    if has_claude_oauth:
        likely_source = "CLAUDE_CODE_OAUTH_TOKEN"
    elif has_anthropic_oauth_api:
        likely_source = "ANTHROPIC_OAUTH_API_KEY"
    elif has_anthropic_api:
        likely_source = "ANTHROPIC_API_KEY"
    else:
        likely_source = "none_detected"

    return {
        "env_presence": {
            "CLAUDE_CODE_OAUTH_TOKEN": has_claude_oauth,
            "ANTHROPIC_OAUTH_API_KEY": has_anthropic_oauth_api,
            "ANTHROPIC_API_KEY": has_anthropic_api,
        },
        "present_sources": present_sources,
        "multiple_sources_set": len(present_sources) > 1,
        "likely_auth_source": likely_source,
    }


# =============================================================================
# Session Management
# =============================================================================


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


class AskQuestionRequest(BaseModel):
    """Request model for ask_question endpoints"""
    project_name: str
    question: str
    user_id: Optional[str] = "default_user"
    message_type: Optional[str] = "@xo"
    agent_type: Optional[str] = None


# =============================================================================
# External Chat API Client
# =============================================================================

class ChatAPIClient:
    """Client for external Chat API endpoints."""

    def __init__(self, base_url: str = CHAT_API_BASE_URL):
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        token = get_auth_token()
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


if AI_PROVIDER == "codex":
    ai_client = CodexCodeClient(
        cli_path=CODEX_CLI_PATH,
        timeout_seconds=CODEX_TIMEOUT,
    )
else:
    ai_client = ClaudeCodeClient(
        cli_path=CLAUDE_CLI_PATH,
        timeout_seconds=CLAUDE_TIMEOUT,
        permission_mode=CLAUDE_PERMISSION_MODE,
        working_directory=AI_WORKSPACE_ROOT,
        allowed_directories=[AI_WORKSPACE_ROOT],
    )


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
    _tok = get_auth_token()
    _src = get_auth_state().get("token_source", "none")
    print(f"   Chat API auth: {'enabled (' + _src + ')' if _tok else 'not set'}")
    print(f"   Stage: {STAGE}")
    print(f"   AI Provider: {AI_PROVIDER}")
    print(f"   Claude CLI: {CLAUDE_CLI_PATH} (timeout={CLAUDE_TIMEOUT}s)")
    print(f"   Claude Permission Mode: {CLAUDE_PERMISSION_MODE}")
    print(f"   AI Workspace Root: {AI_WORKSPACE_ROOT}")
    print(f"   Codex CLI: {CODEX_CLI_PATH} (timeout={CODEX_TIMEOUT}s)")
    print("   Skills: .claude/skills (Claude-native)")
    print("   Skills: .agents/skills + AGENTS.md (Codex-native)")
    startup_auth_session_id = os.getenv("XO_AUTH_SESSION_ID", "").strip()
    startup_poll_token = os.getenv("XO_POLL_TOKEN", "").strip()
    if XO_API_KEY:
        print("   XO auth: using XO_API_KEY (no consume)")
    elif startup_auth_session_id and startup_poll_token:
        print("   XO startup consume: attempting token consume")
        try:
            await consume_auth_flow(
                auth_session_id=startup_auth_session_id,
                poll_token=startup_poll_token,
            )
            print("✅ XO startup consume succeeded")
        except HTTPException as e:
            print(f"⚠️ XO startup consume failed: status={e.status_code}, detail={e.detail}")
        except Exception as e:
            print(f"⚠️ XO startup consume failed unexpectedly: {str(e)}")
    elif startup_auth_session_id or startup_poll_token:
        print(
            "⚠️ XO startup consume skipped: set both XO_AUTH_SESSION_ID and XO_POLL_TOKEN."
        )
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
app.include_router(auth_router)
app.include_router(claude_setup_token_router)
app.include_router(codex_setup_router)
app.include_router(openclaw_usage_router)

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
        "stage": STAGE,
        "auth": get_auth_state(),
        "ai_provider": AI_PROVIDER,
        "claude_cli": CLAUDE_CLI_PATH,
        "codex_cli": CODEX_CLI_PATH,
        "active_sessions": len(session_store)
    }


@app.get("/debug/ai-auth")
async def debug_ai_auth():
    """Debug endpoint to inspect effective AI auth configuration (safe output)."""
    snapshot = _claude_auth_debug_snapshot()
    return {
        "stage": STAGE,
        "ai_provider": AI_PROVIDER,
        "claude_cli": CLAUDE_CLI_PATH,
        "claude_permission_mode": CLAUDE_PERMISSION_MODE,
        "ai_workspace_root": AI_WORKSPACE_ROOT,
        "auth_debug": snapshot,
        "note": (
            "Secrets are never returned. This is a best-effort hint based on server env; "
            "CLI internals may apply their own precedence."
        ),
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


@app.post("/gateway/restart")
async def gateway_restart():
    """Restart the OpenClaw gateway."""
    import subprocess
    script = os.path.expanduser("~/xo-cowork-api/openclaw.sh")
    try:
        result = subprocess.run(
            [script, "restart"],
            capture_output=True, text=True, timeout=30
        )
        return {
            "status": "restarted" if result.returncode == 0 else "error",
            "output": result.stdout,
            "error": result.stderr if result.returncode != 0 else None
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Restart timed out after 30s"}
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e)})


@app.post("/ask_question")
async def ask_question(data: AskQuestionRequest):
    """
    Send a question to configured AI CLI provider (non-streaming).

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

        # Send to selected AI provider CLI
        response = await ai_client.ask(
            question=data.question,
            session_id=session_id,
            is_new_session=is_new,
            agent_type=data.agent_type,
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
    Send a question to configured AI CLI provider (streaming).

    Returns SSE stream of response tokens.

    Streaming returns normalized token events.
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
                async for event in ai_client.ask_streaming(
                    question=data.question,
                    session_id=session_id,
                    is_new_session=is_new,
                    agent_type=data.agent_type,
                ):
                    # Handle normalized provider event types
                    event_type = event.get("type", "")

                    if event_type == "token":
                        text = event.get("token", "")
                        if text:
                            full_response_parts.append(text)
                            yield f"data: {json.dumps({'type': 'token', 'token': text})}\n\n"

                    elif event_type == "error":
                        stream_success = False  # Mark as failed
                        yield f"data: {json.dumps({'type': 'error', 'error': event.get('error', 'Unknown error')})}\n\n"

                    elif event_type == "done":
                        # Provider signals completion.
                        stream_success = True
                        yield f"data: {json.dumps({'done': True})}\n\n"

            except Exception as e:
                print(f"❌ Stream generation error: {str(e)}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                stream_success = False  # Ensure we don't store session on error

            finally:
                # Store session ID only after successful first call
                if is_new and stream_success and full_response_parts:
                    session_store[data.project_name] = session_id
                    print(f"📝 Stored session {session_id} for project {data.project_name}")

                # Save full chat only after successful stream completion.
                # This avoids persisting partial responses when streaming errors out.
                final_response = "".join(full_response_parts)
                if stream_success and final_response:
                    await save_chat_messages(
                        project_id=data.project_name,
                        user_id=data.user_id,
                        user_message=data.question,
                        agent_response=final_response,
                        message_type=data.message_type
                    )
                    print(f"✅ Saved response ({len(final_response)} chars)")
                elif final_response and not stream_success:
                    print("⚠️ Skipped chat save due to incomplete streaming response")

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