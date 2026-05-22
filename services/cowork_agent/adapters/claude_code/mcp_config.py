"""
Per-session MCP config injection for the Claude CLI.

We materialize a Composio MCP URL into a JSON file and pass
`--mcp-config <file>` to `claude` so the model can reach Composio's session
meta-tools (SEARCH_TOOLS, MULTI_EXECUTE_TOOL, MANAGE_CONNECTIONS,
REMOTE_WORKBENCH) without listing every toolkit action up-front.

Primary URL is the Composio-hosted Tool Router session
(composio_service.get_session_mcp_url). On failure we fall back to the
local FastMCP proxy (composio_service.get_meta_mcp_url) so chat doesn't
break during rollout.

The file lives under /tmp/xo-cowork/<session>/mcp.json and is unlinked
after the subprocess exits.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_MCP_TMP_ROOT = Path(os.getenv("XO_MCP_TMP_ROOT", "/tmp/xo-cowork"))


def write_session_mcp_config(user_id: Optional[str], session_key: Optional[str]) -> Optional[Path]:
    """Write the local cowork meta-tool MCP URL to a per-session JSON file.

    Returns the path to pass to `claude --mcp-config`, or None when MCP
    is unavailable / unconfigured. `user_id` is currently unused — the
    cowork MCP server resolves the user itself (single-tenant
    "default_user" today); kept on the signature for parity with the
    other gateway install paths and for forward compatibility.
    """
    if not user_id:
        return None

    try:
        from services import composio_service
    except Exception as exc:
        log.debug("mcp_config: composio_service not importable: %s", exc)
        return None

    # Prefer the Composio Tool Router session URL (4 meta-tools + workbench);
    # fall back to the local FastMCP proxy if the session call fails.
    try:
        mcp_url = composio_service.get_session_mcp_url(user_id)
    except Exception as exc:
        log.debug("mcp_config: get_session_mcp_url failed: %s", exc)
        mcp_url = None

    if not mcp_url:
        try:
            mcp_url = composio_service.get_meta_mcp_url()
        except Exception as exc:
            log.debug("mcp_config: get_meta_mcp_url fallback failed: %s", exc)
            return None

    if not mcp_url:
        return None

    server_entry: dict[str, Any] = {"type": "http", "url": mcp_url}
    if mcp_url.startswith("https://mcp.composio.dev"):
        api_key = os.getenv("COMPOSIO_API_KEY", "").strip()
        if api_key:
            server_entry["headers"] = {"X-API-Key": api_key}

    session_dir = _MCP_TMP_ROOT / (session_key or uuid.uuid4().hex)
    session_dir.mkdir(parents=True, exist_ok=True)
    config_path = session_dir / "mcp.json"
    payload = {"mcpServers": {"composio": server_entry}}

    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".mcp.", suffix=".json", dir=str(session_dir))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        Path(tmp_path).replace(config_path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise

    return config_path


def cleanup_session_mcp_config(config_path: Optional[Path]) -> None:
    """Best-effort cleanup. The session directory is removed; missing is fine."""
    if not config_path:
        return
    try:
        session_dir = config_path.parent
        if session_dir.exists() and session_dir.is_relative_to(_MCP_TMP_ROOT):
            shutil.rmtree(session_dir, ignore_errors=True)
    except Exception as exc:
        log.debug("mcp_config: cleanup failed for %s: %s", config_path, exc)
