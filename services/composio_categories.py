"""
Per-toolkit "Write vs Read" classification for Composio actions.

The Connectors UI groups action toggles into Write / Read sections — see
the screenshot the user shared. Composio's API doesn't expose this tag,
so we maintain per-toolkit classification and merge it into the catalogue
inside `composio_service.list_tools`.

Scope today (toolkits with the configure-tools UI):
- googlecalendar — exact map (48 actions)
- gmail          — exact map (63 actions)
- stripe         — verb-based heuristic (400+ actions across 40 verbs,
                   too many to hand-curate; verbs are stable and the
                   heuristic is exhaustive for the current vocabulary)

Other toolkits' slugs return `None` from `classify()` — the agent path
doesn't care, and the UI doesn't render a configure-tools panel for them.

When adding a toolkit:

1. Pull the catalogue:
       composio_service.list_tools("default_user", "<toolkit_id>")
2. Either add an exact map under `_CATEGORIES` (preferred for catalogues
   under ~100 actions) or implement a heuristic dispatcher similar to
   `_classify_stripe` below.
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


_GMAIL: dict[str, Category] = {
    # Reads (Fetch / Get / List / Search)
    "GMAIL_FETCH_EMAILS": "read",
    "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID": "read",
    "GMAIL_FETCH_MESSAGE_BY_THREAD_ID": "read",
    "GMAIL_GET_ATTACHMENT": "read",
    "GMAIL_GET_AUTO_FORWARDING": "read",
    "GMAIL_GET_CONTACTS": "read",
    "GMAIL_GET_DRAFT": "read",
    "GMAIL_GET_FILTER": "read",
    "GMAIL_GET_LABEL": "read",
    "GMAIL_GET_LANGUAGE_SETTINGS": "read",
    "GMAIL_GET_PEOPLE": "read",
    "GMAIL_GET_PROFILE": "read",
    "GMAIL_GET_VACATION_SETTINGS": "read",
    "GMAIL_LIST_CSE_IDENTITIES": "read",
    "GMAIL_LIST_CSE_KEYPAIRS": "read",
    "GMAIL_LIST_DRAFTS": "read",
    "GMAIL_LIST_FILTERS": "read",
    "GMAIL_LIST_FORWARDING_ADDRESSES": "read",
    "GMAIL_LIST_HISTORY": "read",
    "GMAIL_LIST_LABELS": "read",
    "GMAIL_LIST_MESSAGES": "read",
    "GMAIL_LIST_SEND_AS": "read",
    "GMAIL_LIST_SMIME_INFO": "read",
    "GMAIL_LIST_THREADS": "read",
    "GMAIL_SEARCH_PEOPLE": "read",
    "GMAIL_SETTINGS_GET_IMAP": "read",
    "GMAIL_SETTINGS_GET_POP": "read",
    "GMAIL_SETTINGS_SEND_AS_GET": "read",
    # Writes (Add / Batch / Create / Delete / Forward / Modify / Move / Patch /
    # Remove / Reply / Send / Stop / Untrash / Update / Insert / Import)
    "GMAIL_ADD_LABEL_TO_EMAIL": "write",
    "GMAIL_BATCH_DELETE_MESSAGES": "write",
    "GMAIL_BATCH_MODIFY_MESSAGES": "write",
    "GMAIL_CREATE_EMAIL_DRAFT": "write",
    "GMAIL_CREATE_FILTER": "write",
    "GMAIL_CREATE_LABEL": "write",
    "GMAIL_CREATE_PROMPT_POST": "write",
    "GMAIL_DELETE_DRAFT": "write",
    "GMAIL_DELETE_FILTER": "write",
    "GMAIL_DELETE_LABEL": "write",
    "GMAIL_DELETE_MESSAGE": "write",
    "GMAIL_DELETE_THREAD": "write",
    "GMAIL_FORWARD_MESSAGE": "write",
    "GMAIL_IMPORT_MESSAGE": "write",
    "GMAIL_INSERT_MESSAGE": "write",
    "GMAIL_MODIFY_THREAD_LABELS": "write",
    "GMAIL_MOVE_THREAD_TO_TRASH": "write",
    "GMAIL_MOVE_TO_TRASH": "write",
    "GMAIL_PATCH_LABEL": "write",
    "GMAIL_PATCH_SEND_AS": "write",
    "GMAIL_REMOVE_LABEL": "write",
    "GMAIL_REPLY_TO_THREAD": "write",
    "GMAIL_SEND_DRAFT": "write",
    "GMAIL_SEND_EMAIL": "write",
    "GMAIL_STOP_WATCH": "write",  # stops a push notification channel
    "GMAIL_UNTRASH_MESSAGE": "write",
    "GMAIL_UNTRASH_THREAD": "write",
    "GMAIL_UPDATE_DRAFT": "write",
    "GMAIL_UPDATE_IMAP_SETTINGS": "write",
    "GMAIL_UPDATE_LABEL": "write",
    "GMAIL_UPDATE_LANGUAGE_SETTINGS": "write",
    "GMAIL_UPDATE_POP_SETTINGS": "write",
    "GMAIL_UPDATE_SEND_AS": "write",
    "GMAIL_UPDATE_USER_ATTRIBUTES_VALUES": "write",
    "GMAIL_UPDATE_VACATION_SETTINGS": "write",
}


# Stripe verb classification. Maintained as two sets — every slug starts
# with `STRIPE_<VERB>_...`. Every verb currently observed in the live
# catalogue (40 distinct, 400+ slugs as of 2026-05-18) is covered here;
# Composio adding a new verb later returns `None` so the UI bucket falls
# back to "Other" and we know to extend this set.
_STRIPE_READ_VERBS: frozenset[str] = frozenset({
    "GET", "LIST", "RETRIEVE", "FIND", "SEARCH", "DOWNLOAD", "REPORT",
})
_STRIPE_WRITE_VERBS: frozenset[str] = frozenset({
    "ACCEPT", "ACTIVATE", "ADD", "ADVANCE", "APPLY", "ARCHIVE", "ATTACH",
    "CANCEL", "CAPTURE", "CLOSE", "COLLECT", "CONFIRM", "CREATE",
    "DEACTIVATE", "DELETE", "DELETEV1", "DETACH", "EXPIRE", "FINALIZE",
    "FUND", "MARK", "MIGRATE", "PAY", "POST", "PRESENT", "PROCESS",
    "REACTIVATE", "RESUME", "SEND", "SET", "SUCCEED", "TIMEOUT", "UPDATE",
})


def _classify_stripe(slug: str) -> Optional[Category]:
    """Verb-based classifier for Stripe's 400+ slug catalogue."""
    parts = slug.split("_", 2)
    if len(parts) < 2:
        return None
    verb = parts[1].upper()
    if verb in _STRIPE_READ_VERBS:
        return "read"
    if verb in _STRIPE_WRITE_VERBS:
        return "write"
    return None


_CATEGORIES: dict[str, dict[str, Category]] = {
    "googlecalendar": _GOOGLECALENDAR,
    "gmail": _GMAIL,
}


def classify(toolkit_id: str, slug: str) -> Optional[Category]:
    """Return `"read"` / `"write"` for known slugs, `None` otherwise.

    Unknown slugs (e.g. a new Composio action we haven't classified yet)
    return `None` so the UI can decide whether to omit them or fall back
    to an "Other" bucket. The agent path ignores the category entirely
    and isn't affected.
    """
    if toolkit_id == "stripe":
        return _classify_stripe(slug)
    return _CATEGORIES.get(toolkit_id, {}).get(slug)
