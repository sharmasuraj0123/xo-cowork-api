"""
Per-toolkit "Write vs Read" classification for Composio actions.

The Connectors UI groups action toggles into Write / Read sections — see
the screenshot the user shared. Composio's API doesn't expose this tag,
so we maintain a static map and merge it into the catalogue inside
`composio_service.list_tools`.

Scope today: Google Calendar only (the only toolkit the UI surfaces
per-action toggles for in v1). Other toolkits' slugs return `None` from
`classify()` — the agent path doesn't care, and the UI doesn't render a
configure-tools panel for them.

When adding a toolkit:

1. Pull the catalogue:
       composio_service.list_tools("default_user", "<toolkit_id>")
2. Append the slug list under the toolkit's key in `_CATEGORIES`.
3. Drop the `meta.id === "<toolkit_id>"` gate in the UI to surface the
   panel for that toolkit.

Classification rule of thumb: anything that creates, mutates, deletes,
imports, moves, or sets up/down a notification channel (Watch / Stop)
is a `write`. Pure reads (Get / List / Find / Search / FreeBusy
queries) are `read`.
"""

from __future__ import annotations

from typing import Literal, Optional


Category = Literal["read", "write"]


_GOOGLECALENDAR: dict[str, Category] = {
    # ACL
    "GOOGLECALENDAR_ACL_DELETE": "write",
    "GOOGLECALENDAR_ACL_GET": "read",
    "GOOGLECALENDAR_ACL_INSERT": "write",
    "GOOGLECALENDAR_ACL_LIST": "read",
    "GOOGLECALENDAR_ACL_PATCH": "write",
    "GOOGLECALENDAR_ACL_UPDATE": "write",
    "GOOGLECALENDAR_ACL_WATCH": "write",   # creates notification channel
    # Batch
    "GOOGLECALENDAR_BATCH_EVENTS": "write",
    # Calendar list
    "GOOGLECALENDAR_CALENDAR_LIST_DELETE": "write",
    "GOOGLECALENDAR_CALENDAR_LIST_GET": "read",
    "GOOGLECALENDAR_CALENDAR_LIST_INSERT": "write",
    "GOOGLECALENDAR_CALENDAR_LIST_PATCH": "write",
    "GOOGLECALENDAR_CALENDAR_LIST_UPDATE": "write",
    "GOOGLECALENDAR_CALENDAR_LIST_WATCH": "write",
    # Calendars
    "GOOGLECALENDAR_CALENDARS_DELETE": "write",
    "GOOGLECALENDAR_CALENDARS_UPDATE": "write",
    # Channels (notification subscriptions)
    "GOOGLECALENDAR_CHANNELS_STOP": "write",
    # Clear / create / delete / duplicate
    "GOOGLECALENDAR_CLEAR_CALENDAR": "write",
    "GOOGLECALENDAR_CREATE_EVENT": "write",
    "GOOGLECALENDAR_DELETE_EVENT": "write",
    "GOOGLECALENDAR_DUPLICATE_CALENDAR": "write",
    # Events
    "GOOGLECALENDAR_EVENTS_GET": "read",
    "GOOGLECALENDAR_EVENTS_IMPORT": "write",
    "GOOGLECALENDAR_EVENTS_INSTANCES": "read",
    "GOOGLECALENDAR_EVENTS_LIST": "read",
    "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS": "read",
    "GOOGLECALENDAR_EVENTS_MOVE": "write",
    "GOOGLECALENDAR_EVENTS_WATCH": "write",
    "GOOGLECALENDAR_FIND_EVENT": "read",
    "GOOGLECALENDAR_FIND_FREE_SLOTS": "read",
    "GOOGLECALENDAR_FREE_BUSY_QUERY": "read",
    # Single-calendar reads
    "GOOGLECALENDAR_GET_CALENDAR": "read",
    "GOOGLECALENDAR_GET_CALENDAR_PROFILE": "read",
    "GOOGLECALENDAR_GET_CURRENT_DATE_TIME": "read",
    # Misc reads
    "GOOGLECALENDAR_COLORS_GET": "read",
    "GOOGLECALENDAR_LIST_BUILDINGS": "read",
    "GOOGLECALENDAR_LIST_CALENDAR_RESOURCES": "read",
    "GOOGLECALENDAR_LIST_CALENDARS": "read",
    "GOOGLECALENDAR_LIST_SETTINGS": "read",
    # Patch / quick add / remove attendee / update
    "GOOGLECALENDAR_PATCH_CALENDAR": "write",
    "GOOGLECALENDAR_PATCH_EVENT": "write",
    "GOOGLECALENDAR_QUICK_ADD": "write",
    "GOOGLECALENDAR_REMOVE_ATTENDEE": "write",
    # Settings
    "GOOGLECALENDAR_SETTINGS_GET": "read",
    "GOOGLECALENDAR_SETTINGS_LIST": "read",
    "GOOGLECALENDAR_SETTINGS_WATCH": "write",
    # Sync (deprecated alias for events list, but still surfaces)
    "GOOGLECALENDAR_SYNC_EVENTS": "read",
    "GOOGLECALENDAR_UPDATE_EVENT": "write",
}


_CATEGORIES: dict[str, dict[str, Category]] = {
    "googlecalendar": _GOOGLECALENDAR,
}


def classify(toolkit_id: str, slug: str) -> Optional[Category]:
    """Return `"read"` / `"write"` for known slugs, `None` otherwise.

    Unknown slugs (e.g. a new Composio action we haven't classified yet)
    return `None` so the UI can decide whether to omit them or fall back
    to an "Other" bucket. The agent path ignores the category entirely
    and isn't affected.
    """
    return _CATEGORIES.get(toolkit_id, {}).get(slug)
