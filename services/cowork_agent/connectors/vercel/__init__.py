"""
Vercel connector — OAuth (PKCE) with a pasted-token fallback.

Credentials land in mcp-tokens.json under the provider keys "vercel" and
"vercel_client" (see the package-level ``token_store``).
"""

from .connector import (
    VERCEL_OAUTH_AUTHORIZE_URL,
    VERCEL_OAUTH_REGISTER_URL,
    VERCEL_OAUTH_TOKEN_URL,
    VERCEL_USER_URL,
    delete_vercel_token,
    ensure_oauth_client,
    exchange_code_for_tokens,
    get_oauth_client,
    get_status,
    get_valid_access_token,
    get_vercel_token,
    refresh_oauth_token,
    register_oauth_client,
    save_oauth_tokens,
    save_vercel_token,
    start_oauth_flow,
    validate_token,
)

__all__ = [
    "VERCEL_OAUTH_AUTHORIZE_URL",
    "VERCEL_OAUTH_REGISTER_URL",
    "VERCEL_OAUTH_TOKEN_URL",
    "VERCEL_USER_URL",
    "delete_vercel_token",
    "ensure_oauth_client",
    "exchange_code_for_tokens",
    "get_oauth_client",
    "get_status",
    "get_valid_access_token",
    "get_vercel_token",
    "refresh_oauth_token",
    "register_oauth_client",
    "save_oauth_tokens",
    "save_vercel_token",
    "start_oauth_flow",
    "validate_token",
]
