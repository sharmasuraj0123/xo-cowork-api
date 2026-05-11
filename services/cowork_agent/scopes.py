"""Centralised scope→handle resolution for the BFF layer.

BFF routes never construct paths themselves. They call
`resolve_scope("xo-projects")` (returns a Path) or
`resolve_scope("secrets")` (returns a SecretsScope handle) and then
delegate to service-layer helpers that own the actual filesystem
access.

This is principle P3 from docs/bff-endpoints-design.md: one place to
look when you need to know which on-disk location a frontend "noun"
maps to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from services.cowork_agent import openclaw_env, project_layout


class ScopeNotFound(Exception):
    """Raised when a caller asks for a scope name we don't recognise."""


class SecretsScope:
    """Handle exposing the secret store via openclaw_env helpers only.

    The BFF route only ever sees this object — never a raw Path — so it
    cannot accidentally read or write the underlying .env file
    directly. Future migrations (e.g. moving secrets out of .env into a
    real secret store) only need to swap this class's implementation.
    """

    def load(self) -> list[dict]:
        """Return current entries as [{key, value}, ...]."""
        return openclaw_env.load_env_entries()

    def save(self, items: list[dict]) -> None:
        """Bulk-replace the entire store."""
        openclaw_env.save_env_entries(items)

    def upsert(self, key: str, value: str) -> None:
        """Insert or update a single key (preserves comments/ordering)."""
        openclaw_env.upsert_env_entry(key, value)

    def delete(self, key: str) -> bool:
        """Remove a single key. Returns True if it was present."""
        entries = openclaw_env.load_env_entries()
        before = len(entries)
        kept = [e for e in entries if e.get("key") != key]
        if len(kept) == before:
            return False
        openclaw_env.save_env_entries(kept)
        return True


ScopeHandle = Union[Path, SecretsScope]


def resolve_scope(name: str) -> ScopeHandle:
    """Resolve a scope name to its handle.

    Returns a Path for filesystem scopes, or a domain-specific handle
    (e.g. SecretsScope) for non-filesystem scopes.
    """
    if name == "xo-projects":
        return project_layout.xo_projects_root()
    if name == "secrets":
        return SecretsScope()
    raise ScopeNotFound(f"Unknown scope: {name!r}")
