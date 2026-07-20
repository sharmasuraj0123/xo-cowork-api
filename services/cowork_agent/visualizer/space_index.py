"""Space graph builder — maps ``~/xo-projects`` to the xo-atlas space.json shape.

Pure reader: scans the projects root and returns the graph document that
``v3.html`` consumes. Writes nothing.
Served by ``routers/space.py`` (GET /space/data/space.json).

Watcher-owner seam: ``materialize(path)`` writes the same output atomically
for event-driven freshness; nothing calls it in v1 — see
docs/space-module-design.md.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from services.cowork_agent.project_layout import (
    list_projects,
    project_dir,
    xo_projects_root,
)

# Muted category palette, >=3:1 contrast on the UI background #0b0c0f.
_PALETTE = [
    "#a2b56b", "#7fb3c8", "#c8a06b", "#b58a9e",
    "#8fbf9f", "#c4bd72", "#9a93d0", "#c88585",
]

_CODE_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c",
    ".cpp", ".h", ".sh", ".ps1", ".html", ".css", ".json", ".yml",
    ".yaml", ".toml", ".sql",
}
_DOC_EXT = {".md", ".txt", ".rst", ".pdf", ".docx"}

# Mirrors routers/cowork_agent/bff/filters.is_hidden_name — duplicated
# because services must not import from routers (dependency direction).
_TEMP_SUFFIXES = (".tmp", ".swp", ".swo", ".bak", ".orig")
_TEMP_PREFIXES = ("~$",)
_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build", "target"}

# Hard bounds — the API runs inside every user's workspace, so the builder
# must stay cheap regardless of how much is on disk. Each stage is capped.
MAX_LEAVES_PER_PROJECT = 400          # per-project output bound (newest-first)
MAX_TOTAL_LEAVES = 1500               # whole-graph output bound (browser must render it)
MAX_FILES_SCANNED_PER_PROJECT = 2000  # traversal bound (walk stops here)
BUILD_DEADLINE_S = 10.0               # whole-build wall-clock bound
MAX_LEAVES_PER_GROUP = 40             # bigger dirs split into per-subdir groups
_MAX_GROUP_SPLIT_DEPTH = 4            # deepest path segment the split recurses to
MAX_TIES = 60                         # cross-tie output bound

# Cross-tie derivation bounds. Ties are derived facts, never editorial:
# files that share commits, docs that name a file's path, test_x <-> x.
_TIE_MIN_COCHANGES = 3                # pairs must share >= this many commits
_TIE_MAX_FILES_PER_COMMIT = 20        # bulk commits are noise for co-change
_TIE_MAX_COMMITS = 500                # most recent commits considered
_TIE_MAX_DOCS_PER_PROJECT = 30        # docs scanned for path references
_DOC_SCAN_BYTES = 65536               # max bytes read per doc
_DOC_SCAN_EXT = {".md", ".rst", ".txt"}


def _is_hidden(name: str) -> bool:
    if not name or name.startswith("."):
        return True
    if name in _SKIP_DIRS:
        return True
    if any(name.endswith(s) for s in _TEMP_SUFFIXES):
        return True
    return any(name.startswith(p) for p in _TEMP_PREFIXES)


def _shape_for(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in _CODE_EXT:
        return "disc"
    if ext in _DOC_EXT:
        return "ring"
    return "diamond"


# ---- folder archetypes ------------------------------------------------------
# Every folder (group) classifies as exactly one of five archetypes; the type
# drives the node glyph and the detail panel's overview. Rules are ordered by
# signal strength; they were tuned against the real workspace tree (vendored
# slides.tsx files, engineering docs/ folders inside app repos, and container
# folders are the known traps).
_TYPE_SHAPE = {"app": "disc", "readme": "ring", "docs": "stack",
               "slides": "slab", "unknown": "diamond"}
_TYPE_LABEL = {"app": "App", "readme": "One-pager", "docs": "Docs",
               "slides": "Slides", "unknown": "Unknown"}
_SLIDE_FILE_EXT = {".pptx", ".key", ".odp"}
_SLIDE_NAME_EXT = {".md", ".mdx", ".pdf", ".html"}   # slides*/deck* must be prose, not code
_APP_MANIFESTS = {"package.json", "pyproject.toml", "requirements.txt", "setup.py"}
# "JS or Python app" means real program source; markup/config (.html/.css/
# .json/.yml) must not make a drafts folder look like an app.
_APP_CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
                 ".go", ".rs", ".java", ".c", ".cpp", ".h"}
_WRITING_EXT = {".md", ".mdx", ".docx", ".pdf", ".txt", ".rst"}
_ASSET_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp"}
_FACT_READ_BYTES = 16384        # max bytes read per fact file
_FACT_MAX_READS = 4             # max content reads per group
_FACT_DEADLINE_RESERVE_S = 2.0  # stop content reads this close to the deadline

# Environment-category signals (see environments_graph.py). Filename-only —
# no content reads — tallied for free inside _folder_facts's existing loop.
# Deliberately NOT Dockerfile/docker-compose/CI-workflow: those are ubiquitous
# in ordinary app repos (every deployable Next.js/FastAPI project has one) and
# would misclassify most of "app" as "ops". Terraform/Helm/Pulumi/Ansible are
# rare enough outside genuine infra-as-code repos to be a precise signal.
_IAC_EXT = {".tf", ".tfvars"}
# Full lowercased filenames only (not a stem/substring set): "chart" or
# "pulumi" alone would match ordinary React files like Chart.tsx or a
# pulumi.ts helper in any JS codebase — false-positived on real projects
# during calibration against this workspace.
_IAC_FULL_NAMES = {"chart.yaml", "chart.yml", "ansible.cfg"}
_CONTRACT_PREFIXES = ("contract", "sow", "msa", "statement-of-work", "invoice", "proposal")
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
              ".psd", ".ai", ".eps", ".bmp", ".tiff"}
# Research artefacts: LaTeX papers, Jupyter/R notebooks, bibliographies.
# Rare outside genuine research/paper projects, so a precise Research signal.
_RESEARCH_EXT = {".tex", ".ipynb", ".bib", ".rmd"}

# ---- XO data-type tags ------------------------------------------------------
# Every file/folder node carries exactly one of XO's four data types:
# output (artifacts, the default), inbox (human intent), system (manifests/
# config/locks), session (agent session data — the whole Sessions dataset;
# nothing in the Projects walk earns it). Filename-only, tallied in
# _folder_facts's existing loop (zero new I/O); the watcher's classification
# sink persists the per-project tallies into .xo/project.json.
XOTYPES = ("output", "inbox", "session", "system")
XOTYPE_LABEL = {"output": "Output", "inbox": "Inbox",
                "session": "Sessions", "system": "System"}
# Lightweight types render dimmed in the graph until their filter is chosen.
XOTYPE_WEIGHT = {"session": "dim", "system": "dim"}
_INBOX_PREFIXES = ("readme", "objectives", "plan", "progress", "todo",
                   "notes", "memory", "agents", "soul", "identity")
# Intent must be prose: a bare prefix match would tag identity.ts or
# progressive.css as human intent.
_INBOX_EXT = {"", ".md", ".mdx", ".txt", ".rst", ".docx", ".pdf"}
_SYSTEM_NAMES = {
    "package.json", "pyproject.toml", "setup.py", "pnpm-lock.yaml",
    "yarn.lock", "package-lock.json", "poetry.lock", "cargo.lock",
    "dockerfile", "makefile", "mkdocs.yml", "mkdocs.yaml", "biome.json",
    "components.json", "xo.json", ".env.example",
}
_SYSTEM_PREFIXES = ("requirements", "tsconfig", "docker-compose")
_SYSTEM_EXT = {".lock", ".cfg", ".ini", ".tf", ".tfvars"}


def _xotype_for(rel: str, name: str) -> str:
    """One of XOTYPES for a file, from its name/relpath only."""
    n = name.lower()
    ext = ("." + n.rsplit(".", 1)[-1]) if "." in n else ""
    stem = n[: -len(ext)] if ext else n
    if stem.startswith(_INBOX_PREFIXES) and ext in _INBOX_EXT:
        return "inbox"
    if ext == ".docx" and "notes" in rel.lower():
        return "inbox"
    if n in _SYSTEM_NAMES or n.startswith(_SYSTEM_PREFIXES):
        return "system"
    if ".config." in n or ext in _SYSTEM_EXT:
        return "system"
    if "/.github/" in f"/{rel.lower()}":
        return "system"
    return "output"


def dominant_xotype(counts: dict) -> str:
    """Majority tag for a folder; ties and empties resolve to output."""
    if not counts:
        return "output"
    best = max(counts.items(), key=lambda kv: (kv[1], kv[0] == "output"))
    return best[0] if best[1] > 0 else "output"


def _is_docs_site_signal(rel: str, name: str) -> bool:
    """Docs-SITE configs only; a bare docs/ folder of md is not docs."""
    if name in ("mkdocs.yml", "mkdocs.yaml") or name.startswith("docusaurus.config"):
        return True
    if name in ("source.config.ts", "source.config.tsx"):  # fumadocs
        return True
    if name == "meta.json":
        parts = rel.lower().split("/")
        return "docs" in parts or "content" in parts
    return False


def _classify_folder(rels: list[str]) -> str:
    """One of app/readme/docs/slides/unknown from project-relative paths."""
    names = [r.rsplit("/", 1)[-1].lower() for r in rels]
    exts = [("." + n.rsplit(".", 1)[-1]) if "." in n else "" for n in names]

    for n, e in zip(names, exts):
        if e in _SLIDE_FILE_EXT:
            return "slides"
        if (n.startswith("slides") or n.startswith("deck")) and e in _SLIDE_NAME_EXT:
            return "slides"
    if any(_is_docs_site_signal(r, n) for r, n in zip(rels, names)):
        return "docs"
    if any(n in _APP_MANIFESTS for n in names):
        return "app"

    meaningful = [e for e in exts if e not in _ASSET_EXT]
    md_count = sum(1 for e in meaningful if e in (".md", ".mdx"))
    if len(meaningful) <= 2 and md_count == 1:
        return "readme"
    code = sum(1 for e in exts if e in _APP_CODE_EXT)
    if len(rels) and code >= 1 and code / len(rels) >= 0.5:
        return "app"
    if len(rels) and code >= 3 and code / len(rels) >= 0.35:
        return "app"
    writing = sum(1 for e in exts if e in _WRITING_EXT)
    if writing >= 2 and writing / len(rels) >= 0.5:
        return "docs"
    return "unknown"


def _md_title_excerpt(path: Path) -> tuple[str | None, str | None, int]:
    """(first heading, first prose paragraph, approx word count) of a markdown
    file. Skips HTML/badge preambles by scanning for the first '#' heading."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fp:
            text = fp.read(_FACT_READ_BYTES)
    except OSError:
        return None, None, 0
    title, excerpt = None, None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#"):
            title = s.lstrip("#").strip() or None
            for cand in lines[i + 1:]:
                c = cand.strip()
                if not c or c.startswith(("#", "<", "!", "[!", "|", "```", "---")):
                    if excerpt:
                        break
                    continue
                excerpt = ((excerpt + " ") if excerpt else "") + c
                if len(excerpt) > 240:
                    break
            break
    if excerpt:
        excerpt = re.sub(r"[*_`]", "", excerpt)
        if len(excerpt) > 280:
            excerpt = excerpt[:277] + "…"
    return title, excerpt, len(text.split())


def _pptx_slide_count(path: Path) -> int | None:
    try:
        import zipfile
        with zipfile.ZipFile(path) as z:
            return sum(1 for n in z.namelist()
                       if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
    except Exception:
        return None


def _folder_facts(ftype: str, files: list[Path], rels: list[str],
                  deadline: float | None) -> dict:
    """Type-specific facts for the detail panel. Content reads are bounded
    (count + bytes) and stop entirely near the build deadline.

    Also tallies two filename-only signals every group carries regardless of
    ftype — infrastructure-as-code and contract/SOW paperwork — for free,
    since environments_graph.py's business-category classifier needs them
    and this loop already visits every relative path. No extra I/O."""
    facts: dict = {"files": len(rels)}
    ext_counts: dict[str, int] = {}
    xo_counts: dict[str, int] = {}
    iac = contract = image = research = 0
    for r in rels:
        n = r.rsplit("/", 1)[-1]
        e = ("." + n.rsplit(".", 1)[-1].lower()) if "." in n else "(none)"
        ext_counts[e] = ext_counts.get(e, 0) + 1
        name_lower = n.lower()
        if e in _IAC_EXT or name_lower in _IAC_FULL_NAMES:
            iac += 1
        elif name_lower.startswith(_CONTRACT_PREFIXES):
            contract += 1
        if e in _IMAGE_EXT:
            image += 1
        if e in _RESEARCH_EXT:
            research += 1
        xt = _xotype_for(r, n)
        xo_counts[xt] = xo_counts.get(xt, 0) + 1
    facts["exts"] = sorted(ext_counts.items(), key=lambda kv: -kv[1])[:4]
    facts["iac_signal"] = iac
    facts["contract_signal"] = contract
    facts["image_files"] = image
    facts["research_signal"] = research
    facts["xotype_counts"] = xo_counts

    can_read = deadline is None or time.monotonic() < deadline - _FACT_DEADLINE_RESERVE_S
    reads = 0

    if ftype == "readme":
        for f, r in zip(files, rels):
            if r.lower().endswith((".md", ".mdx")) and can_read and reads < _FACT_MAX_READS:
                title, excerpt, words = _md_title_excerpt(f)
                reads += 1
                facts.update({"title": title, "excerpt": excerpt, "words": words})
                break
    elif ftype == "slides":
        decks = []
        for f, r in zip(files, rels):
            name = r.rsplit("/", 1)[-1]
            ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
            if ext in _SLIDE_FILE_EXT:
                count = None
                if ext == ".pptx" and can_read and reads < _FACT_MAX_READS:
                    count = _pptx_slide_count(f)
                    reads += 1
                decks.append({"name": name, "slides": count})
            elif (name.lower().startswith(("slides", "deck"))
                  and ext in _SLIDE_NAME_EXT):
                decks.append({"name": name, "slides": None})
            if len(decks) >= 4:
                break
        facts["decks"] = decks
    elif ftype == "docs":
        pages = [r for r in rels if r.lower().endswith((".md", ".mdx"))]
        facts["pages"] = len(pages)
        sections: list[str] = []
        for r in pages:
            seg = r.split("/")[-2] if "/" in r else "(top)"
            if seg not in sections:
                sections.append(seg)
            if len(sections) >= 6:
                break
        facts["sections"] = sections
        # "docs" conflates two different things: a genuine documentation
        # SITE (fumadocs/mkdocs/docusaurus config, or a content/docs tree)
        # vs a folder that's just writing-majority (meeting notes, research
        # dumps) with no site tooling. environments_graph.py's wiki/docs
        # split needs to tell them apart; re-checking the site signal here
        # is cheap (rels/names are already in memory, no new I/O).
        names = [r.rsplit("/", 1)[-1].lower() for r in rels]
        facts["docs_site"] = any(_is_docs_site_signal(r, nm)
                                 for r, nm in zip(rels, names))
    elif ftype == "app":
        names = {r.rsplit("/", 1)[-1].lower(): f for r, f in zip(rels, files)}
        code = sum(1 for r in rels
                   if ("." + r.rsplit(".", 1)[-1].lower()) in _CODE_EXT)
        facts["code_files"] = code
        facts["tests"] = sum(1 for r in rels
                             if "test" in r.rsplit("/", 1)[-1].lower())
        if "package.json" in names:
            facts["language"] = "JavaScript"
            if can_read and reads < _FACT_MAX_READS:
                try:
                    import json as _json
                    with open(names["package.json"], encoding="utf-8",
                              errors="ignore") as fp:
                        pkg = _json.loads(fp.read(_FACT_READ_BYTES))
                    facts["name"] = pkg.get("name")
                    facts["description"] = pkg.get("description")
                    facts["scripts"] = list(pkg.get("scripts", {}))[:6]
                except Exception:
                    pass
        elif any(m in names for m in ("pyproject.toml", "requirements.txt", "setup.py")):
            facts["language"] = "Python"
            target = names.get("pyproject.toml")
            if target and can_read and reads < _FACT_MAX_READS:
                try:
                    with open(target, encoding="utf-8", errors="ignore") as fp:
                        text = fp.read(_FACT_READ_BYTES)
                    m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.M)
                    d = re.search(r'^description\s*=\s*"([^"]+)"', text, re.M)
                    facts["name"] = m.group(1) if m else None
                    facts["description"] = d.group(1) if d else None
                except OSError:
                    pass
        else:
            facts["language"] = "code"
    return facts


def _mtime_date(path: Path) -> str:
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def _iter_files_pruned(base: Path):
    """Yield files under ``base``, pruning hidden/junk dirs DURING traversal.

    ``os.walk`` with in-place ``dirnames`` filtering never *enters* a pruned
    directory — a project with a 100k-file node_modules costs nothing here.
    (``rglob`` + post-filter would enumerate all of it first: filtering after
    enumeration is O(everything on disk); pruning is O(what we keep).)"""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(n for n in dirnames if not _is_hidden(n))
        for name in sorted(filenames):
            if not _is_hidden(name):
                yield Path(dirpath) / name


_GIT_TIMEOUT_S = 5


def _git_facts(pdir: Path) -> tuple[dict[str, str], Optional[str], list[list[str]]]:
    """Per-file first-appearance date, the project's first-commit date, and
    each commit's file list (oldest first), from one ``git log``. Any failure
    (not a repo, no git binary, no commits, timeout) → empty facts, and
    callers fall back to mtime dates.

    The full history (no ``--diff-filter``) serves two consumers at once: a
    path's first appearance is its add date, and the per-commit file lists
    feed co-change ties. ``%x01`` makes git emit a control byte prefix on
    each commit-date line so file-path lines can never be confused with
    dates."""
    try:
        out = subprocess.run(
            [
                "git", "-C", str(pdir), "log",
                "--reverse", "--date=short",
                "--pretty=format:%x01%ad", "--name-only",
            ],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except Exception:
        return {}, None, []
    if out.returncode != 0:
        return {}, None, []

    created: dict[str, str] = {}
    first_commit: Optional[str] = None
    current: Optional[str] = None
    commits: list[list[str]] = []
    files: list[str] = []
    for line in out.stdout.splitlines():
        if line.startswith("\x01"):
            if current is not None:
                commits.append(files)
            current = line[1:].strip()
            files = []
            if first_commit is None:
                first_commit = current
        elif line.strip() and current:
            rel = line.strip()
            # oldest-first (--reverse): setdefault keeps the first
            # appearance, surviving later delete/re-add churn.
            created.setdefault(rel, current)
            files.append(rel)
    if current is not None:
        commits.append(files)
    return created, first_commit, commits


def _walk_project(pid: str, cat: str, created_dates: dict,
                  deadline: float | None = None) -> tuple[list[dict], list[dict]]:
    """Groups + leaves for one project. Level-1 dirs become groups; files at
    any depth roll up into their level-1 group; root files get a root group.
    Each group classifies as one of five folder archetypes (ftype) with
    type-specific facts; leaves take their group's type glyph. Traversal is
    pruned and stops at MAX_FILES_SCANNED_PER_PROJECT.
    Raises OSError if the project directory is unreadable (caller skips)."""
    pdir = project_dir(pid)
    groups: list[dict] = []
    leaves: list[dict] = []
    scanned = 0

    def add_leaf(group_id: str, rel: str, f: Path, shape: str) -> None:
        leaves.append({
            "id": f"{pid}:{rel}",
            "group": group_id,
            "shape": shape,
            "tag": (f.suffix.lstrip(".").upper() or "FILE"),
            "label": f.name,
            "date": created_dates.get(rel) or _mtime_date(f),
            "blurb": rel,
            "path": f"{pid}/{rel}",
            "xotype": _xotype_for(rel, f.name),
        })

    def emit_classified(files: list[Path], rels: list[str],
                        gid: str, label: str, blurb_prefix: str = "") -> None:
        ftype = _classify_folder(rels)
        facts = _folder_facts(ftype, files, rels, deadline)
        shape = _TYPE_SHAPE[ftype]
        for f, rel in zip(files, rels):
            add_leaf(gid, rel, f, shape)
        groups.append({
            "id": gid, "cat": cat, "label": label,
            "blurb": blurb_prefix or f"{len(files)} files",
            "ftype": ftype, "facts": facts, "shape": shape,
            "xotype": dominant_xotype(facts.get("xotype_counts") or {}),
        })

    entries = sorted(pdir.iterdir(), key=lambda e: e.name)
    root_files = [e for e in entries if e.is_file() and not _is_hidden(e.name)]
    subdirs = [e for e in entries if e.is_dir() and not _is_hidden(e.name)]

    if root_files:
        kept = root_files[:MAX_FILES_SCANNED_PER_PROJECT]
        emit_classified(kept, [f.name for f in kept],
                        f"g_{pid}_root", "(root)", "Files at the project root.")
        scanned += len(kept)

    for d in subdirs:
        if scanned >= MAX_FILES_SCANNED_PER_PROJECT:
            break
        collected: list[Path] = []
        for f in _iter_files_pruned(d):
            if scanned >= MAX_FILES_SCANNED_PER_PROJECT:
                print(f"space_index: {pid}: scan budget hit "
                      f"({MAX_FILES_SCANNED_PER_PROJECT}); rest of project skipped")
                break
            collected.append(f)
            scanned += 1
        if not collected:
            continue

        # A dir over the group cap splits into one group per subdir,
        # recursing (bounded) while a bucket still exceeds the cap —
        # balanced clusters render as constellations instead of one dense
        # ball, and keep the force sim far from its stiffness limit. Files
        # sitting directly at a split level stay in that level's group.
        def emit_group(files: list[Path], depth: int, gid: str, label: str) -> None:
            if len(files) > MAX_LEAVES_PER_GROUP and depth < _MAX_GROUP_SPLIT_DEPTH:
                buckets: dict = {}
                for f in files:
                    parts = f.relative_to(pdir).parts
                    seg = parts[depth] if len(parts) > depth + 1 else None
                    buckets.setdefault(seg, []).append(f)
                if set(buckets) != {None}:
                    for seg in sorted(buckets, key=lambda s: (s is not None, s or "")):
                        if seg is None:
                            emit_final(buckets[None], gid, label)
                        else:
                            emit_group(buckets[seg], depth + 1,
                                       f"{gid}__{seg}", f"{label} · {seg}")
                    return
            emit_final(files, gid, label)

        def emit_final(files: list[Path], gid: str, label: str) -> None:
            emit_classified(files, [f.relative_to(pdir).as_posix() for f in files],
                            gid, label)

        emit_group(collected, 1, f"g_{pid}_{d.name}", d.name)

    if len(leaves) > MAX_LEAVES_PER_PROJECT:
        dropped = len(leaves) - MAX_LEAVES_PER_PROJECT
        leaves.sort(key=lambda leaf: leaf["date"], reverse=True)
        leaves = leaves[:MAX_LEAVES_PER_PROJECT]
        print(f"space_index: {pid}: dropped {dropped} oldest leaves (cap {MAX_LEAVES_PER_PROJECT})")

    return groups, leaves


def _build_ties(leaves: list[dict], commits_by_pid: dict[str, list[list[str]]]) -> list[dict]:
    """Derived cross-ties between kept leaves, strongest first, capped.

    Three honest derivations (no editorial content): files that repeatedly
    share commits (git co-change), docs whose text names another file's
    relative path, and test_x <-> x name pairing. Runs after every leaf cap
    so a tie can never reference a dropped node — v3 crashes on unknown
    edge endpoints."""
    rel_to_id: dict[str, dict[str, str]] = {}
    for leaf in leaves:
        pid, rel = leaf["id"].split(":", 1)
        rel_to_id.setdefault(pid, {})[rel] = leaf["id"]

    seen: set = set()
    cands: list[tuple[int, str, str, str]] = []  # (strength, s, t, label)

    def add(strength: int, s: str, t: str, label: str) -> None:
        key = tuple(sorted((s, t)))
        if key not in seen:
            seen.add(key)
            cands.append((strength, s, t, label))

    # 1. git co-change: pairs sharing >= _TIE_MIN_COCHANGES recent commits.
    for pid, commits in commits_by_pid.items():
        rels = rel_to_id.get(pid)
        if not rels:
            continue
        counts: dict = {}
        for files in commits[-_TIE_MAX_COMMITS:]:
            if len(files) > _TIE_MAX_FILES_PER_COMMIT:
                continue
            kept = sorted({f for f in files if f in rels})
            for i in range(len(kept)):
                for j in range(i + 1, len(kept)):
                    pair = (kept[i], kept[j])
                    counts[pair] = counts.get(pair, 0) + 1
        for (a, b), n in counts.items():
            if n >= _TIE_MIN_COCHANGES:
                add(n, rels[a], rels[b], f"changed together ×{n}")

    # 2. docs referencing a file's relative path.
    for pid, rels in rel_to_id.items():
        scanned_docs = 0
        for rel, lid in rels.items():
            if Path(rel).suffix.lower() not in _DOC_SCAN_EXT:
                continue
            if scanned_docs >= _TIE_MAX_DOCS_PER_PROJECT:
                break
            scanned_docs += 1
            try:
                with open(project_dir(pid) / rel, encoding="utf-8", errors="ignore") as fp:
                    text = fp.read(_DOC_SCAN_BYTES)
            except OSError:
                continue
            for other_rel, oid in rels.items():
                if oid != lid and other_rel in text:
                    add(2, lid, oid, "references")

    # 3. test_x <-> x pairing by filename.
    for pid, rels in rel_to_id.items():
        by_name: dict = {}
        for rel, lid in rels.items():
            by_name.setdefault(Path(rel).name, []).append(lid)
        for rel, lid in rels.items():
            name = Path(rel).name
            if name.startswith("test_"):
                for oid in by_name.get(name[5:], []):
                    add(2, lid, oid, "tests")

    cands.sort(key=lambda c: (-c[0], c[1], c[2]))
    if len(cands) > MAX_TIES:
        print(f"space_index: kept strongest {MAX_TIES} of {len(cands)} derived ties")
    return [{"s": s, "t": t, "label": label} for _, s, t, label in cands[:MAX_TIES]]


def build_space_data() -> dict:
    root = xo_projects_root()
    projects = list_projects()

    categories: dict = {}
    hub_angles: dict = {}
    hubs: list[dict] = []
    groups: list[dict] = []
    leaves: list[dict] = []
    milestones: list[dict] = []
    commits_by_pid: dict[str, list[list[str]]] = {}

    n = max(len(projects), 1)
    deadline = time.monotonic() + BUILD_DEADLINE_S
    for i, meta in enumerate(projects):
        if time.monotonic() > deadline:
            print(f"space_index: build deadline ({BUILD_DEADLINE_S}s) hit; "
                  f"skipped {len(projects) - i} of {len(projects)} projects")
            break
        pid = str(meta["name"])
        cat = f"p_{pid}"
        display = str(meta.get("display_name") or pid)
        created_dates, first_commit, p_commits = _git_facts(project_dir(pid))

        try:
            p_groups, p_leaves = _walk_project(pid, cat, created_dates, deadline)
        except OSError:
            print(f"space_index: skipping unreadable project {pid}")
            continue

        categories[cat] = {
            "name": display,
            "color": _PALETTE[i % len(_PALETTE)],
        }
        hub_angles[cat] = -math.pi / 2 + i * 2 * math.pi / n
        # The project takes its root folder's archetype when the root has one
        # (a manifest or docs-site config at the root is the project's real
        # identity); otherwise the weightiest non-unknown folder wins — asset
        # dumps (unknown) must not outvote actual content.
        type_weight: dict[str, int] = {}
        for g in p_groups:
            type_weight[g["ftype"]] = (type_weight.get(g["ftype"], 0)
                                       + int(g["facts"].get("files") or 0))
        root_group = next((g for g in p_groups if g["id"] == f"g_{pid}_root"), None)
        known = {t: w for t, w in type_weight.items() if t != "unknown"}
        if root_group and root_group["ftype"] != "unknown":
            ptype = root_group["ftype"]
        elif known:
            prio = {"app": 0, "docs": 1, "slides": 2, "readme": 3}
            ptype = max(known, key=lambda t: (known[t], -prio[t]))
        else:
            ptype = "unknown"
        pfacts: dict = {"files": sum(type_weight.values()), "types": type_weight}
        if root_group and root_group["ftype"] == ptype:
            for k in ("name", "description", "language", "title", "excerpt"):
                if root_group["facts"].get(k):
                    pfacts[k] = root_group["facts"][k]
        hub_xo: dict[str, int] = {}
        for g in p_groups:
            for k, v in (g["facts"].get("xotype_counts") or {}).items():
                hub_xo[k] = hub_xo.get(k, 0) + int(v)
        hubs.append({
            "id": cat, "cat": cat, "label": display,
            "blurb": str(meta.get("description") or f"Project {display}."),
            "ftype": ptype, "facts": pfacts, "shape": _TYPE_SHAPE[ptype],
            "xotype": dominant_xotype(hub_xo),
        })
        groups.extend(p_groups)
        leaves.extend(p_leaves)
        commits_by_pid[pid] = p_commits
        if first_commit:
            milestones.append({"d": first_commit, "t": f"{display} first commit"})

    if len(leaves) > MAX_TOTAL_LEAVES:
        dropped = len(leaves) - MAX_TOTAL_LEAVES
        leaves.sort(key=lambda leaf: leaf["date"], reverse=True)
        leaves = leaves[:MAX_TOTAL_LEAVES]
        kept_groups = {leaf["group"] for leaf in leaves}
        groups = [g for g in groups if g["id"] in kept_groups]
        print(f"space_index: dropped {dropped} oldest leaves workspace-wide "
              f"(cap {MAX_TOTAL_LEAVES}); empty groups pruned")

    ties = _build_ties(leaves, commits_by_pid)

    today = date.today()
    if leaves:
        dates = sorted(leaf["date"] for leaf in leaves)
        start = (date.fromisoformat(dates[0]) - timedelta(days=7)).isoformat()
        end = (date.fromisoformat(dates[-1]) + timedelta(days=7)).isoformat()
    else:
        start = (today - timedelta(days=7)).isoformat()
        end = (today + timedelta(days=7)).isoformat()

    return {
        "meta": {
            "title": "Space",
            "tagline": "an xo-projects knowledge graph",
            "mappedOn": today.strftime("%d %B %Y"),
            "workspace": str(root),
            # Folder-archetype glyphs (see _TYPE_SHAPE); the client's legend,
            # canvas, and detail panel are all driven by these names.
            "shapeLegend": [
                {"shape": "disc", "label": "app"},
                {"shape": "ring", "label": "one-pager"},
                {"shape": "stack", "label": "docs"},
                {"shape": "slab", "label": "slides"},
                {"shape": "diamond", "label": "unknown"},
            ],
            # XO data-type overlay (chips + dimming); weight 'dim' types
            # render faded until their chip is selected.
            "typeLegend": [
                {"id": t, "label": XOTYPE_LABEL[t],
                 "weight": XOTYPE_WEIGHT.get(t, "full")}
                for t in XOTYPES
            ],
        },
        "categories": categories,
        "hubAngles": hub_angles,
        "timeline": {"start": start, "end": end},
        "root": {
            "id": "xo",
            "label": "xo-projects",
            "blurb": f"{len(projects)} projects under {root}",
        },
        "hubs": hubs,
        "groups": groups,
        "leaves": leaves,
        "ties": ties,
        "milestones": milestones,
    }


def materialize(path: Path) -> None:
    """Atomically write ``build_space_data()`` output to ``path``.

    NOT called anywhere in v1. Integration seam for the watcher owner:
    call from the workspace re-aggregate step in ``watcher.tick()`` for
    event-driven freshness, then point the route at the file. See
    docs/space-module-design.md."""
    from services.cowork_agent.visualizer.atomic_write import write_json_atomic

    write_json_atomic(path, build_space_data())
