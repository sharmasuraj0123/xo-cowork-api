"""Cross-workspace commit relay client (pull-based, workspace-anchored)."""
from datetime import datetime


def log_line(msg: str) -> None:
    """Timestamped print(flush=True) — relay activity must be visible in the
    service log file, and module-level logging is invisible under the default
    logging config. Timestamps make latency questions answerable from logs."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)
