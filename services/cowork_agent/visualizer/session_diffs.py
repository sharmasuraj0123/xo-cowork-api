"""Aggregate every installed read-only commit_diffs capability, for the
Sessions-space Timeline. Same shape as session_telemetry.py's aggregation of
session_telemetry providers: discovered by capability, not named here, each
runtime fails independently, and the endpoint 503s only when every provider
is unavailable.
"""

from __future__ import annotations

from services.cowork_agent.adapters.loader import (
    list_capability_providers,
    try_load_capability,
)

_CAPABILITY = "commit_diffs"


class CommitDiffsUnavailable(RuntimeError):
    """Raised only when no discovered provider can be read."""


def build_session_diffs() -> dict:
    events: list[dict] = []
    sources: list[dict] = []
    for provider in list_capability_providers(_CAPABILITY):
        module = try_load_capability(_CAPABILITY, agent=provider)
        if module is None:
            continue
        try:
            contribution = module.collect_commit_diffs()
            provider_events = contribution.get("events") or []
            sources.append({
                "id": str(contribution["source"]["id"]),
                "label": str(contribution["source"].get("label") or provider),
                "status": "available",
                "event_count": len(provider_events),
                "sessions_scanned": contribution.get("sessions_scanned", 0),
                "truncated": bool(contribution.get("truncated")),
            })
            events.extend(provider_events)
        except Exception as exc:
            print(f"session_diffs: provider '{provider}' failed: {exc}")
            sources.append({"id": provider, "label": provider, "status": "unavailable"})
            continue

    if not any(s["status"] == "available" for s in sources):
        raise CommitDiffsUnavailable("no runtime commit-diff provider readable")

    events.sort(key=lambda e: e["date"] or "", reverse=True)
    dates = [e["date"] for e in events if e["date"]]
    projects: dict[str, dict] = {}
    for e in events:
        pid = e.get("project") or "(unknown)"
        p = projects.setdefault(pid, {"id": pid, "label": e.get("project_label") or pid,
                                      "commits": 0})
        p["commits"] += 1

    return {
        "kind": "session_diffs",
        "events": events,
        "sources": sources,
        "projects": sorted(projects.values(), key=lambda p: -p["commits"]),
        "range": {"start": min(dates) if dates else None,
                  "end": max(dates) if dates else None},
    }
