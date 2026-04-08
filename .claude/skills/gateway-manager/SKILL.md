---
name: gateway-manager
description: Manage the OpenClaw gateway service (start, stop, restart, status, logs). Use when asked to restart the gateway, check gateway status, view gateway logs, or perform any gateway lifecycle operation. Do NOT use `openclaw gateway restart` or `systemctl` — always use the managed wrapper script.
---

# Gateway Manager

This deployment uses a managed wrapper script for all gateway lifecycle operations. The wrapper provides PID tracking, auto-restart with backoff, log rotation, and orphan process cleanup.

**Never use:**
- `openclaw gateway restart`
- `openclaw gateway run`
- `systemctl` commands

## Commands

All commands go through the wrapper:

```bash
~/xo-cowork-api/openclaw.sh start     # Start gateway
~/xo-cowork-api/openclaw.sh stop      # Stop gateway
~/xo-cowork-api/openclaw.sh restart   # Stop + start (graceful, with orphan cleanup)
~/xo-cowork-api/openclaw.sh status    # Check if running + show orphan warnings
~/xo-cowork-api/openclaw.sh logs      # Tail gateway logs (tail -f)
```

## Key Details

- **PID file:** `/tmp/openclaw-gateway.pid`
- **Log file:** `/tmp/openclaw-gateway.log`
- **Auto-restart:** Up to 10 consecutive restarts with 5s delay between attempts
- **Log rotation:** At 10 MB
- **Orphan detection:** Finds and kills unmanaged gateway processes on start/stop/restart

## When to Use What

| Need | Command |
|---|---|
| Config change reload | Use the `gateway` tool with `config.patch` or `config.apply` (sends SIGUSR1, no full restart needed) |
| Full gateway restart | `~/xo-cowork-api/openclaw.sh restart` |
| Gateway won't respond | `~/xo-cowork-api/openclaw.sh stop && ~/xo-cowork-api/openclaw.sh start` |
| Check health | `~/xo-cowork-api/openclaw.sh status` |
| Debug issues | `~/xo-cowork-api/openclaw.sh logs` |

## Important

- For config changes, prefer the `gateway` tool's `config.patch` action with SIGUSR1 — it's faster and doesn't require a full restart.
- Only use the shell script restart when a full process restart is genuinely needed.
