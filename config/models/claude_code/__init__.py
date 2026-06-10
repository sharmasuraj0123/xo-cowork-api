"""Legacy claude_code model client (Plane A — the /ask_question path).

Separate from the agent adapter system: this is a model client for the legacy
direct-CLI plane, selected via ``AI_PROVIDER``. See docs/refactor/README.md.
"""
from config.models.claude_code.client import ClaudeCodeClient

__all__ = ["ClaudeCodeClient"]
