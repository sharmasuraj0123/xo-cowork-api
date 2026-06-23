"""
The broker's chat/session engine — agent-agnostic runtime that turns an
HTTP request into normalized chat events and persists session metadata.

- dispatcher    : routes a prompt to the active agent's adapter
- messages      : normalized MessageResponse shapes + converters
- sessions_io   : session listing / file lookup over the project layout
- chat_state    : in-process active-stream registry
- usage_loader  : thin alias resolving the active agent's usage capability

None of these name a specific agent; they resolve behavior via the adapters
capability loader.
"""
