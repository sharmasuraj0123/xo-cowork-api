"""
Shared in-memory state for the cowork_agent chat streaming path.

stream_id -> { session_id, text, session_key } or { task, prefetched }

Process-local; not persisted. Incompatible with uvicorn --workers > 1 (same
constraint as the bridge service this was migrated from).
"""

active_streams: dict[str, dict] = {}
