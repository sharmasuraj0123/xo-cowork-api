# Timekeeper watcher — research & final design

A new watcher that records file events under a configurable root (default
`$HOME`) into a `timekeeper/` folder. Distinct from the existing
`services/cowork_agent/visualizer/watcher.py`, which only tails agent
JSONL output.

> **Final design (implemented in `services/timekeeper/`):**
> Recursive `inotify_simple` watcher over `$HOME` (or any directory set
> via `TIMEKEEPER_WATCH_ROOT`). Runs as the user — no root needed. Pushes
> modification events through a thread→asyncio bridge into the same
> JsonlWriter (`current.jsonl` + daily gzip rotation, 14-day retention).
> Race-safe against fast subdir+files creation via a synthetic-event
> scan on every new-dir watch. See "Final design" section at the bottom.
>
> The fanotify / machine-scope path discussed below was explored first
> and rejected because the dev environment is a container without
> `CAP_SYS_ADMIN`. The user requested directory-scope after that finding.

## Scope: whole machine (decided)

Watch the entire filesystem — every file event on every mount, including
`/etc`, `/var`, `/home`, `/tmp`, etc. This rules out the per-directory
`inotify` approach (would require walking and watching millions of dirs)
and forces a mount-level API.

Two scopes considered earlier but rejected:
- **xo-projects subtree only** — too narrow; misses installs, system edits.
- **user home only** — same problem; misses anything outside `$HOME`.

## Kernel APIs available on Linux

### inotify
- Per-directory watches; **does not recurse** — you walk the tree at startup
  and add a watch per dir. New subdirs need `IN_CREATE` handling to add watches.
- Event payload: filename + mask (CREATE/MODIFY/DELETE/MOVED_FROM/...).
  **No PID, no comm** of the writer.
- Limits on this box: `max_user_watches=1048576`, `max_user_instances=1024`.
  Already generous — no tuning needed.
- Runs as the invoking user. No privileges required.

### fanotify
- Can attach to a **whole mount** (`FAN_MARK_MOUNT`) — one watch covers
  everything. Far cheaper for scope B/C.
- With `FAN_REPORT_FID | FAN_REPORT_DIR_FID` (kernel ≥ 5.1) gets file/dir
  identity without needing per-dir state.
- Can report PID of the writer.
- **Needs `CAP_SYS_ADMIN`** (root) to mark mounts. Disqualifying unless the
  service runs as root or with that capability granted.

### audit / eBPF
- Both can produce file events with PID/comm. Both are **not lightweight**
  (kernel module / verifier overhead, root, ops burden). Reject for this use.

## Userspace options at machine scope

inotify-based options are off the table — they need a watch per directory,
and there's no clean way to watch a whole mount. Remaining candidates all
build on fanotify:

| Option | How | Pros | Cons |
|---|---|---|---|
| `fatrace` (subprocess) | spawn `fatrace -t`, parse stdout | Battle-tested; gives `pid comm op path` per line; handles fanotify quirks; small (~50KB binary) | External `apt install fatrace`; line-parsing fragility on paths with newlines |
| Direct fanotify via `ctypes` | call `fanotify_init` / `fanotify_mark` ourselves | No external binary; full control | ~300 LOC of plumbing for FID resolution; we own the bug surface |
| `pyfanotify` (PyPI) | Python binding | Pythonic API | Small maintainer base; FID/path resolution still hand-rolled in places |

**Recommended:** `fatrace` subprocess. It already solves path resolution,
PID/comm extraction, and pseudo-fs exclusion (`/proc`, `/sys`, `/dev`,
`/run` are filtered by default). We parse one line per event and write it
to JSONL. If `fatrace` exits we restart it with backoff.

Trade-off accepted: external binary dependency (`apt-get install fatrace`).
Worth it vs. ~300 LOC of fanotify ctypes we'd have to maintain.

## Storage layout

```
timekeeper/
  current.jsonl              # active log, append-only
  2026-05-27.jsonl.gz        # rotated daily, gzipped
  2026-05-26.jsonl.gz
  state.json                 # last-seen wd→path map, rotation cursor
```

- One JSONL line per event. **Append-only**, never rewritten.
- Daily rotation by UTC date; gzip rotated files (≈10× smaller for text events).
- Optional size cap (e.g. 100 MB) to force mid-day rotation.
- Optional retention (e.g. delete `.gz` older than N days). Default: no
  deletion — the operator decides.

### Event schema (one line)

```json
{
  "ts": "2026-05-27T15:43:12.481Z",
  "op": "modify",                    // open | modify | create | delete | close_write
  "path": "/etc/hosts",
  "pid": 4821,
  "comm": "vim",                     // process name from fatrace
  "uid": 1000                        // optional, only if we choose to enrich from /proc
}
```

`fatrace` line format is `comm(pid): OP path` — straight parse. UID
enrichment is best-effort by reading `/proc/<pid>/status`; skip on race.

## Filtering — required for "lightweight" at machine scope

Without filters, a `pip install`, `apt upgrade`, or even normal browser
activity floods the log with thousands of events/sec. `fatrace` already
skips `/proc`, `/sys`, `/dev`, `/run`. On top of that we filter:

**Path prefixes to drop:**
```
/var/log/**          # log churn; you have logs already
/var/cache/**
/var/lib/dpkg/**     # package manager noise
/var/lib/apt/**
/tmp/** /var/tmp/**
/home/*/.cache/**
/home/*/.mozilla/** /home/*/.config/google-chrome/**   # browser churn
**/__pycache__/** **/node_modules/** **/.git/**
**/.venv/** **/venv/**
**/.xo/**            # feedback loop with existing watcher
```

**Process names to drop (`comm`):**
```
fatrace              # self
chrome chromium firefox
systemd-journald rsyslogd
```

**Ops to drop:** plain `open` (read-only) events — keep modifications only
(`modify`, `create`, `delete`, `close_write`, `rename`). Cuts volume ~10×.

All three lists overridable via `config/timekeeper.yaml`.

## Backpressure and safety

- **Bounded queue** between the `fatrace` reader and the JSONL writer
  (`asyncio.Queue(maxsize=10_000)`). On overflow: drop, increment a counter,
  emit one `{"op":"overflow","dropped":N}` event.
- **Batched writes:** at machine scope, individual line writes are too slow.
  Buffer ~100 lines or 200 ms, write once. `fsync` only on rotation.
- **fatrace lifecycle:** spawn with `asyncio.create_subprocess_exec`. If it
  exits, restart with exponential backoff (1s → 30s cap). Log SIGTERM cleanly
  on shutdown.
- **Restart recovery:** none — events between shutdown and startup are lost.
  Document this; fanotify has no replay.
- **Service isolation:** root requirement makes this awkward to colocate
  with the FastAPI process. See "Deployment" below.

## Cost estimate at machine scope

On a typical dev machine, post-filter:
- **Idle:** 1–10 events/sec. Negligible CPU.
- **Active dev** (editor saves, builds): 50–500 events/sec.
- **Worst case** (`apt upgrade`, container image build): 5k–20k events/sec
  spikes. Queue may overflow on 10k cap; drop-counter handles it.

Volume estimate post-filter:
- ~150 bytes/event JSONL → ~50 MB/day idle, ~500 MB/day active.
- Gzipped daily files ~10× smaller → ~50 MB/day on disk active.
- Suggested retention: 14 days. ~700 MB steady-state footprint.

Memory: `fatrace` ~5 MB RSS, Python reader+writer ~30 MB. Well under
"lightweight" if we hold this discipline.

## Deployment — the root problem

`fatrace` (and fanotify generally) **must run as root or with
`CAP_SYS_ADMIN`**. Three deployment shapes:

1. **Separate systemd service.** Recommended. `timekeeperd` runs as root,
   writes to `/var/lib/xo-cowork/timekeeper/` (or `~/.xo-cowork/timekeeper/`
   with the dir owned by the cowork user, fanotify still works as root).
   FastAPI stays unprivileged. Communication is one-way: the daemon writes
   files, the API reads them when needed.
2. **Sudo wrapper from FastAPI lifespan.** Spawn `sudo fatrace ...` from
   the FastAPI process. Requires passwordless sudo for that exact command.
   Brittle and unusual.
3. **Run the whole API as root.** Rejected — wide blast radius for the
   sake of one feature.

Option 1 is the only sane choice. Means this is **not** an in-process
asyncio task like the existing watcher. It's its own daemon.

## Proposed module layout

```
services/timekeeper/
  __init__.py
  daemon.py          # main entry: spawn fatrace, read stdout, route to writer
  parser.py          # fatrace line → event dict
  writer.py          # JSONL append + daily gzip rotation
  filters.py         # path/comm/op ignore matching
  config.py          # ignore lists, retention, output dir

scripts/
  timekeeperd.py     # python -m services.timekeeper.daemon entrypoint
  timekeeperd.service # systemd unit (root, Restart=on-failure)
```

The FastAPI app gets a read-only `routers/timekeeper.py` later if we want
to surface recent events in the UI — out of scope for v1.

## Open questions before implementing

1. **Output location** — `/var/lib/xo-cowork/timekeeper/` (FHS-correct,
   needs setup) vs `~/.xo-cowork/timekeeper/` (sibling of existing watcher
   offsets, works without root-owned dirs). Recommend the latter for
   parity with current conventions.
2. **Retention** — default 14 days of gzipped JSONL, configurable. OK?
3. **Reads of own files** — when a user (or the API) reads
   `~/.xo-cowork/timekeeper/current.jsonl`, that's itself an event. Add
   the timekeeper output dir to the ignore list to avoid amplification.
4. **PII** — at machine scope we capture filename paths under every user's
   `$HOME`. If the box is multi-user or has secrets in path names,
   document that timekeeper logs are sensitive (mode 0640, root-owned).

## Recommendation (superseded — see "Final design")

The fanotify/fatrace recommendation above was the right call for true
machine-wide capture, but is unusable in this dev container (no
`CAP_SYS_ADMIN`). The user then pivoted to "watch the dir the daemon
runs from, default `$HOME`", which is naturally an inotify problem.

## Final design (implemented)

**Backend:** recursive `inotify` via `inotify_simple` (one tiny PyPI dep,
no kernel extensions, no root). One watch per directory; the source
walks the tree at startup and adds watches dynamically for any directory
created at runtime.

**Race handling:** when a new subdir is created at runtime, inotify
delivers `IN_CREATE | IN_ISDIR` for it but anything written into the
new dir before we register the watch is invisible. Fix: as soon as a
new-dir watch is added, the source does `os.scandir` on it and emits
synthetic `create` events for anything present, recursing into any
subdirs found. Verified by a deliberate `mkdir … && echo … > file`
test — 7 events captured including all nested files.

**Event schema (no PID/comm — inotify doesn't carry them):**

```json
{
  "ts": "2026-05-27T16:51:27.002Z",
  "op": "close_write",       // create | modify | delete | moved_from
                             // | moved_to | close_write | attrib
  "path": "/home/coder/xo-projects/foo/bar.py",
  "is_dir": false
}
```

**Filtering:** path prefix/substring matching only (`/.cache/`,
`/.git/`, `/node_modules/`, `__pycache__`, `/.xo/`, the timekeeper
output dir itself, …). Read-only ops (`IN_OPEN`, `IN_ACCESS`,
`IN_CLOSE_NOWRITE`) are not requested from the kernel in the first
place — saves work end-to-end.

**Cost on this dev box (`$HOME` rooted):**
- 2,997 directories watched after pruning — well under the 1M kernel ceiling.
- Idle daemon ~30 MB RSS, ~0% CPU.
- VSCode + Claude + git background activity produced ~140 events in a
  minute. No queue overflow.

**Deployment:** runs as the invoking user (the unit file sets
`User=coder`). `apt-get install fatrace` is no longer needed.
`pip install -r requirements.txt` picks up `inotify_simple`. Start with
`python -m services.timekeeper` or `sudo systemctl enable --now
/home/coder/xo-cowork-api/deploy/timekeeperd.service`.

**Configuration:** `TIMEKEEPER_WATCH_ROOT`, `TIMEKEEPER_OUTPUT_DIR`,
`TIMEKEEPER_RETENTION_DAYS`. All ignore lists are constants in
`services/timekeeper/config.py`.

**Known limits:**
- Files created **and** deleted within ~500 ms of their parent dir's
  creation may be missed (race window between inotify event delivery
  and synthetic-scan). Real editor/git/agent activity is well outside
  that window.
- No attribution — we can't tell which process triggered an event.
  That would require fanotify (root) or eBPF.
- Symlinks not followed (would risk loops blowing up the watch count).
