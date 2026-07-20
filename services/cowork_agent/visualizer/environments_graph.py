"""Environments: a collection of xo-projects, each classified as one thing.

Five fixed hubs — App, Ops, Wiki, Docs, Customer. Every project
(list_projects()) is walked once via space_index._walk_project purely to
compute classification signals (folder-content archetype, infra-as-code /
deck / contract / image-asset / docs-site counts) — that per-file detail is
internal only and never reaches the output graph. Each project collapses to
exactly one leaf, parented under a single trivial group per hub, so the
graph is a flat collection of one node per project sorted into 5 clusters,
not an explosion of every project's internal files (that's what the
Projects space is for).

Classification precedence (first match wins; every project lands in one of
the five — there is no sixth "unknown" bucket here):
  1. Manual override — .xo/project.json's optional "category" field. Read
     only; nothing in this module or the UI writes to .xo/. A tag of the
     retired "marketing" name still resolves, to "customer".
  2. Ops      — infrastructure-as-code files anywhere in the project
                (terraform/helm/pulumi/ansible). Deliberately NOT
                Dockerfile/docker-compose/CI-workflow: those are ubiquitous
                in ordinary deployable app repos and would misclassify most
                of "app" as "ops".
  3. Customer — outward-facing material: any folder the Projects-space
                classifier already calls "slides" (deck files), the project
                is overwhelmingly non-code/non-doc material (brand assets,
                images) with negligible source, or it carries contract/SOW/
                invoice paperwork. Content alone is a weak signal for "built
                for a client" — the manual tag is the primary path for that
                case; this is the fallback.
  4. Docs     — the project's overall folder-type (space_index's app/docs/
                slides/readme/unknown "ptype") is docs AND it carries an
                actual documentation-site signal (fumadocs/mkdocs/
                docusaurus config, or a content/docs tree) — a genuine
                documentation product, not just a pile of markdown.
  5. Wiki     — ptype is docs (without a site signal) or readme: informal
                notes, meeting minutes, research write-ups, one-pagers.
  6. App      — the project's ptype is app (has a manifest / real source).
  7. Fallback — nothing above matched (ptype unknown, no strong signal):
                closest is a note, so it lands in Wiki.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta, timezone

from services.cowork_agent.project_layout import (
    list_projects,
    project_dir,
    xo_projects_root,
)
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.space_index import (
    BUILD_DEADLINE_S,
    XOTYPE_LABEL,
    XOTYPE_WEIGHT,
    XOTYPES,
    _TYPE_LABEL,
    _TYPE_SHAPE,
    _git_facts,
    _walk_project,
)

ENV_CATEGORIES = ("engineering", "ops", "documentation", "research", "marketing")
_ENV_LABEL = {"engineering": "Engineering", "ops": "Ops",
              "documentation": "Documentation", "research": "Research",
              "marketing": "Marketing"}
# Distinct from both space_index._PALETTE (per-project) and
# sessions_graph._PALETTE (per-runtime) on purpose — three different
# graphs should never be mistaken for one another at a glance.
_ENV_COLOR = {"engineering": "#6fb7e0", "ops": "#e8a15c",
              "documentation": "#c792ea", "research": "#7fd0a8",
              "marketing": "#e0708a"}
# One-line explanation of each cluster, shown as the enclosure's caption.
_ENV_DESC = {
    "engineering": "Apps, services, and libraries — real source you build and ship.",
    "ops": "Infrastructure-as-code and deployment tooling (terraform, helm, ansible).",
    "documentation": "Docs sites, wikis, notes, and one-pagers — written knowledge.",
    "research": "Papers, notebooks, and studies — exploratory write-ups and analysis.",
    "marketing": "Decks, brand assets, and outward-facing material.",
}
# Manual .xo/project.json tags and retired cluster names resolve to the
# current vocabulary, so old persisted blocks and hand-set tags keep working.
_TAG_ALIASES = {
    "app": "engineering", "eng": "engineering",
    "docs": "documentation", "wiki": "documentation",
    "customer": "marketing",
}
# Project-name tokens that mark a research project even without .tex/.ipynb
# files (a markdown research write-up looks identical to a wiki note by
# content alone; the name is the discriminator).
_RESEARCH_NAME_TOKENS = ("research", "paper", "whitepaper", "study", "thesis")

# Outward-facing material: slide decks, contract paperwork, or a project
# majority-composed of image/brand-asset files (logos, screenshots, exported
# artwork). Calibrated against this workspace: a broader "mostly non-code/
# non-doc" rule also caught experiment-output dumps and empty prototype
# folders that have nothing to do with this — the image-extension ratio is
# the precise version of that same idea.
_IMAGE_MAJORITY_RATIO = 0.5


def _environment_memberships(meta: dict, ptype: str, agg: dict) -> list[str]:
    """Every cluster this project belongs to, primary first.

    A project can satisfy several cluster predicates at once (a Next.js app
    that also ships slide decks is Engineering AND Marketing); the graph
    shows shared projects pulled to the midpoint of their clusters.
    Precedence sets memberships[0], the primary cluster the roll-up and any
    single-category consumer use.
    """
    pid = str(meta.get("name") or "").lower()
    members: list[str] = []
    if agg["iac_signal"] > 0:
        members.append("ops")
    if (agg["has_slides"] or agg["contract_signal"] > 0
            or (agg["files"] and agg["image_files"] / agg["files"] >= _IMAGE_MAJORITY_RATIO)):
        members.append("marketing")
    # Research beats Documentation: a paper/notebook or a research-named
    # project is Research, not just written knowledge.
    if agg.get("research_signal", 0) > 0 or any(t in pid for t in _RESEARCH_NAME_TOKENS):
        members.append("research")
    # Documentation absorbs both a genuine docs SITE (fumadocs/mkdocs/
    # docusaurus) and plain writing-majority folders (notes, one-pagers).
    if agg["docs_site"] or ptype in ("docs", "readme"):
        members.append("documentation")
    if ptype == "app":
        members.append("engineering")
    if not members:
        members.append("documentation")  # nothing fired: a note is written knowledge

    # A manual .xo/project.json tag pins the PRIMARY cluster (and joins the
    # membership set if the heuristics missed it).
    tag = str(meta.get("category") or "").strip().lower()
    tag = _TAG_ALIASES.get(tag, tag)
    if tag in ENV_CATEGORIES:
        if tag in members:
            members.remove(tag)
        members.insert(0, tag)
    return members


def _classify_environment(meta: dict, ptype: str, agg: dict) -> str:
    """Primary cluster (kept for single-category consumers)."""
    return _environment_memberships(meta, ptype, agg)[0]


def _classify_project_meta(meta: dict, deadline: float) -> dict | None:
    """Full classification of one project (the expensive per-file walk).
    Returns the classify_projects() item shape, or None if unreadable."""
    pid = str(meta["name"])
    display = str(meta.get("display_name") or pid)
    created_dates, first_commit, p_commits = _git_facts(project_dir(pid))

    try:
        # Full per-file walk, same as the Projects space — but only to
        # compute classification signals. p_groups/p_leaves never reach
        # the environments graph; only the single roll-up leaf does.
        p_groups, p_leaves = _walk_project(pid, "_pending", created_dates, deadline)
    except OSError:
        print(f"environments_graph: skipping unreadable project {pid}")
        return None

    root_group = next((g for g in p_groups if g["id"] == f"g_{pid}_root"), None)
    type_weight: dict[str, int] = {}
    for g in p_groups:
        type_weight[g["ftype"]] = (type_weight.get(g["ftype"], 0)
                                   + int(g["facts"].get("files") or 0))
    known = {t: w for t, w in type_weight.items() if t != "unknown"}
    if root_group and root_group["ftype"] != "unknown":
        ptype = root_group["ftype"]
    elif known:
        prio = {"app": 0, "docs": 1, "slides": 2, "readme": 3}
        ptype = max(known, key=lambda t: (known[t], -prio[t]))
    else:
        ptype = "unknown"

    agg = {
        "files": sum(int(g["facts"].get("files") or 0) for g in p_groups),
        "iac_signal": sum(int(g["facts"].get("iac_signal") or 0) for g in p_groups),
        "contract_signal": sum(int(g["facts"].get("contract_signal") or 0) for g in p_groups),
        "image_files": sum(int(g["facts"].get("image_files") or 0) for g in p_groups),
        "research_signal": sum(int(g["facts"].get("research_signal") or 0) for g in p_groups),
        "has_slides": any(g["ftype"] == "slides" for g in p_groups),
        "docs_site": any(g["ftype"] == "docs" and g["facts"].get("docs_site")
                         for g in p_groups),
    }
    memberships = _environment_memberships(meta, ptype, agg)
    return {
        "id": pid, "label": display, "category": memberships[0],
        "categories": memberships,
        "first_commit": first_commit, "ptype": ptype, "agg": agg,
        "p_groups": p_groups, "p_leaves": p_leaves, "root_group": root_group,
    }


def classify_projects(deadline: float | None = None):
    """Yield one dict per readable project: id, label, category, first_commit,
    ptype, agg, p_groups, p_leaves, root_group. The full per-file walk this
    does (same cost as the Projects space) is run once here and reused by
    every consumer that needs "which of the 5 clusters is this project in"
    — build_environments_graph() (the graph) and commit_timeline.py's
    environment-scoped commit history (the growth-trunk data) both call
    this instead of re-deriving classification independently."""
    if deadline is None:
        deadline = time.monotonic() + BUILD_DEADLINE_S
    for meta in list_projects():
        if time.monotonic() > deadline:
            print("environments_graph: classify deadline hit; remaining projects skipped")
            break
        item = _classify_project_meta(meta, deadline)
        if item is not None:
            yield item


def classification_block(item: dict) -> dict:
    """The persistable .xo/project.json "classification" payload for one
    classify_projects() item. The watcher's classification sink writes it;
    build_environments_graph() rebuilds the graph from it without walking."""
    xo_counts: dict[str, int] = {}
    for g in item["p_groups"]:
        for k, v in (g["facts"].get("xotype_counts") or {}).items():
            xo_counts[k] = xo_counts.get(k, 0) + int(v)
    root_group, ptype = item["root_group"], item["ptype"]
    facts = dict(root_group["facts"]) if root_group and root_group["ftype"] == ptype else {}
    facts.pop("xotype_counts", None)
    agg = item["agg"]
    last_date = max((l["date"] for l in item["p_leaves"]), default=None)
    return {
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "category": item["category"],
        "categories": item.get("categories") or [item["category"]],
        "ptype": ptype,
        "label": item["label"],
        "files": agg["files"],
        "xotype_counts": xo_counts,
        "signals": {
            "iac": agg["iac_signal"], "contract": agg["contract_signal"],
            "images": agg["image_files"], "has_slides": agg["has_slides"],
            "docs_site": agg["docs_site"],
        },
        "last_touched": last_date,
        "first_commit": item["first_commit"],
        "facts": facts,
    }


def classify_one_project(pid: str, deadline: float | None = None) -> dict | None:
    """Classify a single project and return its persistable block (or None
    if the project is unknown/unreadable). Used by the watcher sink."""
    if deadline is None:
        deadline = time.monotonic() + BUILD_DEADLINE_S
    meta = next((m for m in list_projects() if str(m["name"]) == pid), None)
    if meta is None:
        return None
    item = _classify_project_meta(meta, deadline)
    return classification_block(item) if item is not None else None


def _persisted_classification(pid: str) -> tuple[dict | None, str | None]:
    """(classification block, manual category override) from the project's
    .xo/project.json — read-only; the watcher owns writes."""
    doc = read_json(project_dir(pid) / ".xo" / "project.json") or {}
    block = doc.get("classification")
    manual = str(doc.get("category") or "").strip().lower() or None
    if not isinstance(block, dict) or not block.get("category"):
        return None, manual
    return block, manual


def build_environments_graph() -> dict:
    root = xo_projects_root()

    categories = {cid: {"name": _ENV_LABEL[cid], "color": _ENV_COLOR[cid]}
                  for cid in ENV_CATEGORIES}
    n = len(ENV_CATEGORIES)
    hub_angles = {cid: -math.pi / 2 + i * 2 * math.pi / n
                 for i, cid in enumerate(ENV_CATEGORIES)}
    hub_counts = {cid: 0 for cid in ENV_CATEGORIES}
    # One trivial pass-through group per hub: projects need a "group" parent
    # to sit in (the schema's leaf->group->hub chain), but there is no
    # meaningful intermediate cluster here — the hub IS the cluster.
    groups: list[dict] = [{"id": f"g_{cid}", "cat": cid, "label": _ENV_LABEL[cid],
                          "blurb": "", "desc": _ENV_DESC[cid], "xotype": "output"}
                          for cid in ENV_CATEGORIES]
    leaves: list[dict] = []
    ties: list[dict] = []  # one per secondary cluster membership
    milestones: list[dict] = []

    # Disk-first: read each project's watcher-persisted classification from
    # .xo/project.json; a project without a block yet (cold boot, new
    # project) is classified live as fallback so the route never 503s.
    # A manual top-level `category` in project.json wins even over a stale
    # persisted block (the user may have edited it after the last sweep).
    deadline = time.monotonic() + BUILD_DEADLINE_S
    from_disk = live = 0
    for meta in list_projects():
        if time.monotonic() > deadline:
            print("environments_graph: build deadline hit; remaining projects skipped")
            break
        pid = str(meta["name"])
        block, manual = _persisted_classification(pid)
        if block is None:
            item = _classify_project_meta(meta, deadline)
            if item is None:
                continue
            block = classification_block(item)
            live += 1
        else:
            from_disk += 1
        manual = _TAG_ALIASES.get(manual, manual)
        # Persisted blocks written before a taxonomy change carry old cluster
        # names (app/docs/wiki/customer); alias them to the current
        # vocabulary so the graph is correct before the watcher re-sweeps.
        raw = block.get("categories") or [block.get("category")]
        seen: set[str] = set()
        memberships: list[str] = []
        for c in raw:
            c = _TAG_ALIASES.get(c, c)
            if c in ENV_CATEGORIES and c not in seen:
                seen.add(c)
                memberships.append(c)
        if manual in ENV_CATEGORIES:
            memberships = [manual] + [c for c in memberships if c != manual]
        if not memberships:
            memberships = ["documentation"]
        env_cat = memberships[0]
        ptype = str(block.get("ptype") or "unknown")
        display = str(block.get("label") or pid)
        for c in memberships:
            hub_counts[c] += 1

        # This leaf carries its own ftype+facts (unlike the Projects space,
        # where only a leaf's *parent group* is classified) so the detail
        # panel's search-result card renders directly from it.
        facts = dict(block.get("facts") or {})
        files = int(block.get("files") or facts.get("files") or 0)
        facts["files"] = files
        bits = [f"{files} file{'s' if files != 1 else ''}"]
        if facts.get("name"):
            bits.append(str(facts["name"]))
        elif facts.get("language"):
            bits.append(str(facts["language"]))
        elif facts.get("title"):
            bits.append(str(facts["title"]))

        if len(memberships) > 1:
            bits.append("in " + " + ".join(_ENV_LABEL[c] for c in memberships))
        leaves.append({
            "id": pid,
            "group": f"g_{env_cat}",
            "clusters": memberships,
            "shape": _TYPE_SHAPE.get(ptype, "diamond"),
            "tag": _TYPE_LABEL.get(ptype, "Unknown"),
            "label": display,
            "date": (block.get("last_touched") or block.get("first_commit")
                     or date.today().isoformat()),
            "blurb": " · ".join(bits),
            "path": pid,
            "ftype": ptype,
            "facts": facts,
            "xotype": "output",
        })
        # Secondary memberships become cross-ties to those clusters' groups:
        # the sim's tie springs (strengthened via meta.tieSpring below) pull
        # a shared project toward every cluster, so it settles at their
        # midpoint.
        for c in memberships[1:]:
            ties.append({"s": pid, "t": f"g_{c}", "label": "also in"})
        if block.get("first_commit"):
            milestones.append({"d": block["first_commit"],
                               "t": f"{display} first commit"})
    print(f"environments_graph: classification {from_disk} from .xo, {live} live")

    for g in groups:
        c = hub_counts[g["cat"]]
        g["blurb"] = f"{c} project{'s' if c != 1 else ''}"

    today = date.today()
    if leaves:
        dates = sorted(leaf["date"] for leaf in leaves)
        start = (date.fromisoformat(dates[0]) - timedelta(days=7)).isoformat()
        end = (date.fromisoformat(dates[-1]) + timedelta(days=7)).isoformat()
    else:
        start = (today - timedelta(days=7)).isoformat()
        end = (today + timedelta(days=7)).isoformat()

    hubs = [{
        "id": cid, "cat": cid, "label": _ENV_LABEL[cid],
        "blurb": f"{hub_counts[cid]} project{'s' if hub_counts[cid] != 1 else ''} · {_ENV_DESC[cid]}",
        "desc": _ENV_DESC[cid],
    } for cid in ENV_CATEGORIES]

    total_projects = sum(hub_counts.values())
    return {
        "meta": {
            "title": "Environments",
            "tagline": "the workspace clustered by business purpose",
            "mappedOn": today.strftime("%d %B %Y"),
            "workspace": str(root),
            "noun": "projects",
            "rootEdgeLabel": "a cluster of this workspace",
            "leafDateLabel": "Last touched",
            "kickers": {"hub": "Cluster", "group": "Cluster"},
            "shapeLegend": [
                {"shape": "disc", "label": "app"},
                {"shape": "ring", "label": "one-pager"},
                {"shape": "stack", "label": "docs"},
                {"shape": "slab", "label": "slides"},
                {"shape": "diamond", "label": "unknown"},
            ],
            "typeLegend": [
                {"id": t, "label": XOTYPE_LABEL[t],
                 "weight": XOTYPE_WEIGHT.get(t, "full")}
                for t in XOTYPES
            ],
            # Enclosed clusters: the client draws a soft hull around each
            # cluster's members. tieSpring matches the member spring so a
            # multi-cluster project settles at the midpoint of its clusters
            # instead of hugging the primary.
            "enclose": True,
            "tieSpring": {"d": 80, "k": 0.07},
            "introEyebrow": "Five clusters",
            "introTitle": "Every project has a purpose.",
            "intro": f"{total_projects} projects, one node each, sorted into App, Ops, "
                     "Wiki, Docs, and Customer — clustered by what they're "
                     "actually for, not where they live on disk.",
            "timelineTitle": "The workspace, by purpose, over time.",
            "timelineSub": "Scrub through when each project was last touched. Open "
                           "a cluster from the graph to see its projects here.",
        },
        "categories": categories,
        "hubAngles": hub_angles,
        "timeline": {"start": start, "end": end},
        "root": {
            "id": "environments-root", "label": "Environments",
            "blurb": f"{total_projects} projects across 5 clusters",
        },
        "hubs": hubs,
        "groups": groups,
        "leaves": leaves,
        "ties": ties,
        "milestones": milestones,
    }
