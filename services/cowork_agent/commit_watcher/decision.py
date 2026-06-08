"""Pure decision logic for the commit watcher.

Given the local main SHA, the remote-tracking origin/main SHA, and the
last-seen SHA, decide what the watcher should do this tick. No I/O here so
it is trivially unit-testable.

Returns a (action, sha) tuple:
  - ("baseline", origin)     first run: persist origin, do not report
  - ("noop", None)           origin unchanged since last seen
  - ("report", origin)       origin advanced AND local == origin: we pushed
  - ("fetch_update", origin) origin advanced via a fetch (local != origin)
"""
from __future__ import annotations


def decide(local_main: str, origin_main: str, last_seen: str | None):
    if last_seen is None:
        return ("baseline", origin_main)
    if origin_main == last_seen:
        return ("noop", None)
    if local_main == origin_main:
        return ("report", origin_main)
    return ("fetch_update", origin_main)
