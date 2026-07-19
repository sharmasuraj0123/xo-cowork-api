"""Project the multi-runtime session telemetry into the Space graph schema.

The Graph tab's "Sessions" dataset: the same payload shape space_index.py
produces for the artifact map (meta / categories / hubAngles / root / hubs /
groups / leaves / ties / milestones), built from build_session_telemetry()
instead of the filesystem. Runtimes become hubs, projects become clusters,
sessions become leaves, so the existing renderer (and its Timeline and Six
Degrees lenses) work unchanged.

Agent-agnostic by construction: runtimes come from the telemetry payload's
meta.sources; nothing here names an adapter.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from services.cowork_agent.visualizer.session_telemetry import (
    build_session_telemetry,
)

# Distinct from space_index._PALETTE on purpose: the two datasets should not
# be mistaken for one another at a glance. Assigned per-source by order.
_PALETTE = [
    "#e8a15c", "#6fb7e0", "#c792ea", "#7fd0a8", "#e0708a", "#d6c86a",
]

MAX_LEAVES_PER_GROUP = 150   # newest kept; caption in group blurb stays true
MAX_TOTAL_LEAVES = 1500      # matches the artifact graph's workspace-wide cap
MAX_TIES = 60


def _fmt_tokens(n: float) -> str:
    n = float(n or 0)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}{suffix}"
    return str(int(n))


def _fmt_duration(sec: float | None) -> str | None:
    if not sec or sec <= 0:
        return None
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m"
    return f"{sec}s"


def _parse_started(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _session_blurb(s: dict) -> str:
    parts = [f"{int(s.get('turns') or 0)} turns",
             f"{_fmt_tokens(s.get('tokens'))} tokens"]
    if s.get("cost_known") and isinstance(s.get("cost"), (int, float)):
        parts.append(f"~${s['cost']:.2f}")
    dur = _fmt_duration(s.get("duration_sec"))
    if dur:
        parts.append(dur)
    subs = s.get("subagents") or []
    if subs:
        parts.append(f"{len(subs)} sub-agent{'s' if len(subs) != 1 else ''}")
    return " · ".join(parts)


def _leaf_shape(s: dict) -> str:
    if s.get("subagents"):
        return "diamond"
    if int(s.get("turns") or 0) <= 2:
        return "ring"
    return "disc"


def build_sessions_graph() -> dict:
    """Space-schema graph of every locally readable agent session."""
    telemetry = build_session_telemetry()
    sources = [s for s in telemetry.get("meta", {}).get("sources", [])
               if s.get("available")]
    sessions = telemetry.get("sessions", [])

    categories: dict = {}
    hub_angles: dict = {}
    hubs: list[dict] = []
    n_sources = max(len(sources), 1)
    label_by_source: dict[str, str] = {}
    for i, src in enumerate(sources):
        sid = str(src["id"])
        cat = f"s_{sid}"
        label = str(src.get("label") or sid)
        label_by_source[sid] = label
        categories[cat] = {"name": label, "color": _PALETTE[i % len(_PALETTE)]}
        hub_angles[cat] = -math.pi / 2 + i * 2 * math.pi / n_sources
        hubs.append({
            "id": cat, "cat": cat, "label": label,
            "blurb": f"{int(src.get('session_count') or 0)} sessions · "
                     f"{_fmt_tokens(src.get('token_count'))} tokens",
        })

    # Cluster sessions per (source, project). Group ids are opaque strings.
    grouped: dict[tuple[str, str], list[dict]] = {}
    for s in sessions:
        sid = str(s.get("agent") or "")
        if sid not in label_by_source:
            continue
        path = str(s.get("project_path") or "")
        grouped.setdefault((sid, path), []).append(s)

    groups: list[dict] = []
    leaves: list[dict] = []
    for (sid, path), rows in sorted(
        grouped.items(),
        key=lambda kv: max(str(r.get("started_at") or "") for r in kv[1]),
        reverse=True,
    ):
        gid = f"g:{sid}:{path}"
        rows.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
        kept = rows[:MAX_LEAVES_PER_GROUP]
        display = str(rows[0].get("project") or "(unknown)")
        tokens = sum(float(r.get("tokens") or 0) for r in rows)
        groups.append({
            "id": gid, "cat": f"s_{sid}", "label": display,
            "blurb": f"{len(rows)} session{'s' if len(rows) != 1 else ''} · "
                     f"{_fmt_tokens(tokens)} tokens · {path or '(unknown path)'}",
        })
        for s in kept:
            started = _parse_started(s.get("started_at"))
            if started is None:
                continue
            leaves.append({
                "id": f"l:{s.get('key') or s.get('id')}",
                "group": gid,
                "shape": _leaf_shape(s),
                "tag": str(s.get("model") or "unknown"),
                "label": started.strftime("%b %d · %H:%M"),
                "date": started.date().isoformat(),
                "blurb": _session_blurb(s),
                "path": path or "(unknown path)",
            })

    if len(leaves) > MAX_TOTAL_LEAVES:
        dropped = len(leaves) - MAX_TOTAL_LEAVES
        leaves.sort(key=lambda leaf: leaf["date"], reverse=True)
        leaves = leaves[:MAX_TOTAL_LEAVES]
        kept_groups = {leaf["group"] for leaf in leaves}
        groups = [g for g in groups if g["id"] in kept_groups]
        print(f"sessions_graph: dropped {dropped} oldest sessions "
              f"(cap {MAX_TOTAL_LEAVES}); empty clusters pruned")

    # Cross-ties: the same project driven by more than one runtime.
    by_path: dict[str, list[dict]] = {}
    for g in groups:
        path = g["id"].split(":", 2)[2]
        if path:
            by_path.setdefault(path, []).append(g)
    tie_cands = sorted(
        (v for v in by_path.values() if len(v) > 1),
        key=lambda v: -sum(len(grouped.get((g["cat"][2:], g["id"].split(":", 2)[2]), []))
                           for g in v),
    )
    ties: list[dict] = []
    for group_set in tie_cands:
        for a, b in zip(group_set, group_set[1:]):
            ties.append({"s": a["id"], "t": b["id"], "label": "same project"})
        if len(ties) >= MAX_TIES:
            ties = ties[:MAX_TIES]
            break

    milestones = []
    first_by_source: dict[str, str] = {}
    for leaf in leaves:
        sid = leaf["group"].split(":", 2)[1]
        if sid not in first_by_source or leaf["date"] < first_by_source[sid]:
            first_by_source[sid] = leaf["date"]
    for sid, d in sorted(first_by_source.items(), key=lambda kv: kv[1]):
        milestones.append(
            {"d": d, "t": f"first {label_by_source.get(sid, sid)} session"})

    today = date.today()
    if leaves:
        dates = sorted(leaf["date"] for leaf in leaves)
        start = (date.fromisoformat(dates[0]) - timedelta(days=7)).isoformat()
        end = (date.fromisoformat(dates[-1]) + timedelta(days=7)).isoformat()
    else:
        start = (today - timedelta(days=7)).isoformat()
        end = (today + timedelta(days=7)).isoformat()

    total = len(leaves)
    runtime_names = " and ".join(label_by_source.values()) or "any runtime"
    return {
        "meta": {
            "title": "Sessions",
            "tagline": "an agent-session telemetry graph",
            "mappedOn": today.strftime("%d %B %Y"),
            "workspace": f"{total} sessions across {len(sources)} runtimes",
            "noun": "sessions",
            "rootEdgeLabel": "a runtime on this workspace",
            "leafDateLabel": "Started",
            "kickers": {"hub": "Runtime", "group": "Project"},
            "shapeLegend": [
                {"shape": "disc", "label": "session"},
                {"shape": "ring", "label": "quick session"},
                {"shape": "diamond", "label": "with sub-agents"},
            ],
            "introEyebrow": "A telemetry graph",
            "introTitle": "Every session leaves a trail.",
            "intro": f"Wander through {total} agent sessions from {runtime_names}: "
                     "every conversation, its project, model, and sub-agents, "
                     "mapped as one graph.",
            "timelineTitle": "Every session, in order.",
            "timelineSub": "Scrub through the workspace's agent activity as it "
                           "happened. Open any project from the graph to watch "
                           "its run unfold here.",
        },
        "categories": categories,
        "hubAngles": hub_angles,
        "timeline": {"start": start, "end": end},
        "root": {
            "id": "sessions-root",
            "label": "Sessions",
            "blurb": f"{total} sessions · "
                     f"{_fmt_tokens(telemetry.get('totals', {}).get('tokens'))} tokens · "
                     f"{int(telemetry.get('totals', {}).get('projects') or 0)} projects",
        },
        "hubs": hubs,
        "groups": groups,
        "leaves": leaves,
        "ties": ties,
        "milestones": milestones,
    }
