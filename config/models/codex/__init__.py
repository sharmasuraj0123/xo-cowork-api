"""Legacy codex model client (Plane A — the /ask_question path).

Codex stays a legacy model client with no agent adapter; selected via
``AI_PROVIDER=codex``. See docs/refactor/README.md.
"""
from config.models.codex.client import CodexCodeClient

__all__ = ["CodexCodeClient"]
