# Cross-Workspace Commit Relay — How It Works

## 1. The problem (one sentence)

Multiple people work on the **same project**, each in their own workspace, each with
their own clone of the **same shared GitHub repo**. When one person pushes a commit,
everyone else should automatically find out and pull it — without manually checking.

## 2. The big idea

Don't send the code through some custom system — **GitHub already moves the code.**
We only need to send a tiny notification: *"project X is now at commit Y — go fetch."*

A small middle service (the **relay**) broadcasts that notification to everyone.

```
  The only thing the relay ever carries:

        { "project_id": "relay-demo", "commit": "9f3c1a2b..." }

  No diffs, no files — just "which project" + "which commit".
  Each workspace recovers the actual code from GitHub using that commit hash.
```

## 3. The three roles

```
   WORKSPACE A                      RELAY (broker)                  WORKSPACE B
   "publisher"                      "hotline"                       "subscriber"
   ┌───────────────┐               ┌──────────────────┐           ┌───────────────┐
   │ cowork-api    │               │ /publish         │           │ cowork-api    │
   │               │  {project,    │ /subscribe (SSE) │   push    │               │
   │ POST          │   commit}     │ /ledger (admin)  │  down the │ subscriber    │
   │ /api/relay/   ├──────────────►│                  │  open line│ (listening)   │
   │ ping          │               │ global ledger:   ├──────────►│               │
   └──────┬────────┘               │  seq│project│sha │           └──────┬────────┘
          │ git push               │   1 │demo   │9f3c│                  │ git fetch
          ▼                        └──────────────────┘                  ▼
   ┌──────────────────────  shared GitHub repo  ──────────────────────────────┐
   │            both clone it into  ~/xo-projects/<project_id>                 │
   └────────────────────────────────────────────────────────────────────────┘
```

- **Publisher (A):** after pushing, tells the relay "I pushed commit Y to project X."
- **Relay (broker):** records it in a global ledger and broadcasts to all listeners.
- **Subscriber (B):** hears it and runs `git fetch` so the commit is available locally.

### Why no port forwarding is needed
Neither workspace accepts incoming connections. **Both dial OUT to the relay** (allowed
through any firewall, like loading a website). B keeps its outbound connection open; the
relay speaks back down that same line. Only the relay needs a public address.

## 4. The happy path (live)

```
 A: git push                         ── code goes to GitHub
 A: POST /api/relay/ping {project}   ── cowork-api resolves HEAD, sends {project, commit}
        │
        ▼
 RELAY /publish:  assign seq → append to ledger → write down every open /subscribe line
        │
        ▼
 B (already listening): receives {seq, project, commit}
     → git fetch origin          (commit now present locally)
     → save cursor = seq         (so it knows where it's up to)
```

## 5. The catch-up path (B was offline)

This is what makes it reliable instead of "only works if you're online."

```
 B is DOWN (restarting / network blip)
 A publishes seq=5, seq=6   →  relay stores them in the ledger, B misses them

 B comes back up:
     reads its saved cursor (e.g. 4)
     dials in:  GET /subscribe?since=4
        │
        ▼
 RELAY: "you missed 5 and 6" → replays them, then continues live
        │
        ▼
 B: git fetch for 5 and 6 → fully caught up, cursor = 6
```

## 6. Every function, explained

### Relay broker — `relay.py` (runs on Render / one host)

| Function | What it does |
|----------|--------------|
| `_load_ledger()` | On startup, loads the saved ledger file so `seq` continues across restarts. |
| `publish(ping)` | Handles `POST /publish`. Assigns the next `seq`, appends `{seq, project_id, commit, ts}` to the ledger (memory + file), and pushes it to every connected subscriber. Returns `{seq, delivered_to}`. |
| `subscribe(request, since)` | Handles `GET /subscribe?since=N`. Replays every ledger event after `N`, then streams new ones live over SSE. Sends a keepalive every 15s so the line stays open. |
| `ledger(since, limit)` | Handles `GET /ledger` — admin/debug view of the global history. Workspaces never call this. |
| `health()` | Liveness + counts (subscribers, ledger size, head seq). |

### cowork-api client — `services/cowork_agent/relay.py` (runs in every workspace)

| Function | What it does |
|----------|--------------|
| `ping_commit(project_id, commit)` | Publisher side. POSTs `{project_id, commit}` to the relay's `/publish`. Fire-and-forget: never crashes the caller, no-op if `RELAY_URL` unset. |
| `_fetch_on_receive(project_id, commit)` | The auto-fetch. Runs `git fetch origin` in `~/xo-projects/<project_id>` so the commit is local. **Fetch only — never merge/checkout** (the agent decides when to apply). |
| `run_relay_subscriber()` | The subscriber loop. Reads the cursor, opens `GET /subscribe?since=<cursor>`, and for each event calls `_fetch_on_receive` then advances the cursor. Auto-reconnects every 5s if the line drops. |
| `_read_cursor()` / `_write_cursor(seq)` | Persist the "last seq I processed" to `~/.xo-cowork/relay-cursor` so restarts resume instead of losing/replaying everything. |
| `_log(msg)` | `print(..., flush=True)` so log lines actually show up in the background service's log file. |

### cowork-api router — `routers/cowork_agent/relay.py`

| Endpoint | What it does |
|----------|--------------|
| `POST /api/relay/ping` | What you (or the agent) call after a push. Validates the project is a git repo, resolves `HEAD` if no commit is given, then calls `ping_commit`. |

### Server wiring — `server.py`

| Where | What it does |
|-------|--------------|
| lifespan startup | If `RELAY_URL` is set, `asyncio.create_task(run_relay_subscriber())` — this is the moment B "dials the hotline." Happens on every boot/restart. |
| lifespan shutdown | Cancels the subscriber task (closes the connection cleanly). |

## 7. The data (the only payload)

```json
{ "project_id": "relay-demo", "commit": "9f3c1a2b4d5e6f70..." }
```
The relay enriches it with a `seq` and `ts` for the ledger:
```json
{ "seq": 6, "project_id": "relay-demo", "commit": "9f3c...", "ts": "2026-06-09T11:40:02Z" }
```

## 8. Trigger summary (who starts what)

```
 git commit            → nothing (no auto-trigger on commit)
 git push              → code on GitHub, still nothing on B
 POST /api/relay/ping  → THE trigger (manual today; a git hook could automate it)
 relay broadcast       → automatic (no git)
 git fetch on B        → AUTOMATIC (the subscriber does it on receive)
 git merge/checkout    → manual (agent decides)
```

## 9. Current limits (v1)

- **No auth** on the relay yet (open `/publish` `/subscribe` `/ledger`).
- **Plaintext** payload (project_id + commit visible to the relay/subscribers).
- **Broadcast to everyone** (no per-project targeting yet).
- **Manual ping** (no auto-trigger on push yet).
- **In-memory broker** (must run on an always-on host, not serverless).
