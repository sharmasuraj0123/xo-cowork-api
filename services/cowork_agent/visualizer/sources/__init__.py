"""Source modules — read runtime storage, emit normalised events.

One module per runtime. Today Claude Code is the only working source;
Codex is stubbed (see docs/watcher-design.md §10) and OpenClaw
inherits adapter-written ``sessionslist.json`` rows directly without
needing a watcher source for counters (covered by a follow-up).
"""
