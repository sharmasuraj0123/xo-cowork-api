"""
External-service connectors for the cowork_agent subsystem.

Cloud storage (gdrive/onedrive via rclone), source/deploy (github/vercel),
and the manus integration, plus the shared rclone primitives
(``rclone_connector``/``rclone_oauth_lock``) and the ``token_store`` that owns
``mcp-tokens.json``. These are agent-agnostic; their HTTP surfaces live in the
matching ``routers/cowork_agent/<service>.py`` modules.
"""
