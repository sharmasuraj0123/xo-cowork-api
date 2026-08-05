#!/usr/bin/env python3
"""Check the live antigravity install against its manifest.json paths.

Reads:
  - sibling manifest.json                        (canonical agy paths)
  - sibling settings.json                        (which env var overrides the CLI path)
  - ~/.gemini/antigravity-cli/settings.json      (agy CLI config — JSON)
  - ~/.gemini/antigravity-cli/antigravity-oauth-token  (login state)
  - ~/.gemini/antigravity-cli/brain/<cid>/…      (per-conversation transcript)
  - ~/.gemini/antigravity-cli/conversations/<cid>.db   (token accounting, SQLite)

Reports OK / WARN / FAIL per check. WARN and FAIL rows are also echoed as a
timestamped line in the usage_sync format so they're greppable when stdout is
captured to /tmp/xo-cowork-api.log.

The store checks IMPORT the antigravity adapter (auth / paths / tokens) instead
of re-implementing agy's layout, so this script and the API agree by
construction — that is the whole point of running it. Those imports need the
repo root on sys.path (see _REPO_ROOT) and are done lazily inside each check, so
a box without the API's dependencies still gets the install/config rows instead
of a traceback. Nothing imports this script.

Note there is no `.env` check even though the manifest declares one: agy
authenticates with a Google OAuth token file and consumes no API keys, so a
missing `.env` is the healthy state, not a warning.

Exit codes: 0 all-ok, 1 any FAIL, 2 only WARNs.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMANDS_JSON = HERE / "manifest.json" if (HERE / "manifest.json").exists() else HERE / "commands.json"
SETTINGS_JSON = HERE / "settings.json"

# config/agents/antigravity/ → config/agents/ → config/ → repo root.
_REPO_ROOT = HERE.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

OK, WARN, FAIL = "OK", "WARN", "FAIL"

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
COLORS = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m"}
RESET = "\033[0m"


def _paint(status: str) -> str:
    if not USE_COLOR:
        return status
    return f"{COLORS[status]}{status}{RESET}"


def _timestamp_prefix() -> str:
    tz_pref = (os.getenv("USAGE_SYNC_LOG_TZ", "UTC") or "UTC").strip().upper()
    if tz_pref == "IST":
        tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")
        tz_name = "IST"
    else:
        tz = datetime.timezone.utc
        tz_name = "UTC"
    ts = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    return f"[{ts} {tz_name}]"


class Report:
    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        print(f"  [{_paint(status)}] {name}" + (f" — {detail}" if detail else ""))
        if status in (WARN, FAIL):
            line = f"{_timestamp_prefix()} troubleshooting {self.agent}: {status} {name}"
            if detail:
                line += f" — {detail}"
            print(line)

    def exit_code(self) -> int:
        statuses = {s for s, _, _ in self.rows}
        if FAIL in statuses:
            return 1
        if WARN in statuses:
            return 2
        return 0

    def summary(self) -> str:
        counts = {OK: 0, WARN: 0, FAIL: 0}
        for s, _, _ in self.rows:
            counts[s] += 1
        return f"{counts[OK]} ok, {counts[WARN]} warn, {counts[FAIL]} fail"


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def load_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        return json.loads(path.read_text()), None
    except FileNotFoundError:
        return None, f"missing: {path}"
    except json.JSONDecodeError as e:
        return None, f"invalid JSON ({e.msg} at line {e.lineno})"
    except OSError as e:
        return None, f"unreadable: {e}"


def newest_child(directory: Path, pattern: str) -> Path | None:
    """The most recently modified entry matching `pattern`, or None.

    The store checks report on one conversation rather than all of them — the
    newest is the one a "my last run misbehaved" report is about."""
    try:
        entries = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return entries[0] if entries else None


# --------------------------------------------------------------------------- checks

def check_install(report: Report, binary: str, paths: dict[str, Path], cli_path_env: str) -> bool:
    home = paths["home_dir"]
    if not home.is_dir():
        report.add(FAIL, f"{binary} installed", f"{home} does not exist")
        return False
    report.add(OK, f"{binary} installed", str(home))

    # Resolve the CLI the way the adapter does (adapters/antigravity/routes.py:137-141):
    # an explicit <cli_path_env> wins, and a non-absolute value is still looked
    # up on PATH. A pointed-at-but-missing override is a FAIL, not a WARN — it
    # means the deployment is configured for a binary that isn't there.
    override = (os.getenv(cli_path_env, "") or "").strip()
    candidate = override or binary
    if os.path.isabs(candidate):
        resolved = candidate if os.access(candidate, os.X_OK) else None
    else:
        resolved = shutil.which(candidate)
    if resolved:
        source = f"via {cli_path_env}" if override else "on PATH"
        report.add(OK, f"{binary} CLI resolved", f"{resolved} ({source})")
    elif override:
        report.add(FAIL, f"{binary} CLI resolved", f"{cli_path_env}={override} is not executable")
    else:
        report.add(WARN, f"{binary} CLI resolved", "not found on PATH (install may be incomplete)")
    return True


def check_config_file(report: Report, paths: dict[str, Path]) -> None:
    # agy writes settings.json lazily (it appears after the first trusted
    # workspace), so absent => WARN, present-but-unparseable => FAIL.
    cfg_path = paths["config_file"]
    if not cfg_path.is_file():
        report.add(WARN, "settings.json present", f"missing: {cfg_path} (agy will use defaults)")
        return
    _, err = load_json(cfg_path)
    if err:
        report.add(FAIL, "settings.json parseable", err)
        return
    report.add(OK, "settings.json parseable", str(cfg_path))


def check_login(report: Report) -> None:
    """OAuth token state, via the same helper the chat/status paths use."""
    try:
        from services.cowork_agent.adapters.antigravity import auth
        from services.cowork_agent.adapters.antigravity import paths as agy_paths
    except Exception as e:  # noqa: BLE001 — the adapter is optional for this script
        report.add(WARN, "oauth token usable", f"adapter unavailable: {e}")
        return

    state = auth.login_state()
    if auth.has_usable_login():
        report.add(OK, "oauth token usable", f"{agy_paths.TOKEN_PATH} (state: {state})")
        return
    # Every non-"ok" state blocks a run and none of them are self-healing —
    # the fix is the same interactive sign-in, so report the actionable message.
    report.add(FAIL, "oauth token usable", f"state: {state} — {auth.LOGIN_REQUIRED_MESSAGE}")


def check_brain_store(report: Report) -> None:
    """The brain dir and the transcript the visualizer source tails."""
    try:
        from services.cowork_agent.adapters.antigravity import paths as agy_paths
    except Exception as e:  # noqa: BLE001
        report.add(WARN, "brain store readable", f"adapter unavailable: {e}")
        return

    brain = agy_paths.BRAIN_DIR
    if not brain.is_dir():
        report.add(WARN, "brain store readable", f"missing: {brain} (no conversation yet)")
        return
    newest = newest_child(brain, "*")
    if newest is None:
        report.add(WARN, "brain store readable", f"{brain} is empty (no conversation yet)")
        return

    transcript = agy_paths.transcript_path(newest.name)
    if not transcript.is_file():
        report.add(WARN, "transcript readable", f"missing: {transcript}")
        return
    try:
        lines = sum(1 for _ in transcript.open("r", encoding="utf-8", errors="replace"))
    except OSError as e:
        report.add(FAIL, "transcript readable", f"unreadable: {e}")
        return
    report.add(OK, "transcript readable", f"{transcript} ({lines} steps)")


def check_conversation_db(report: Report) -> None:
    """The SQLite trajectory store — where agy's token counts actually live."""
    try:
        from services.cowork_agent.adapters.antigravity import paths as agy_paths
        from services.cowork_agent.adapters.antigravity.tokens import extract_usage
    except Exception as e:  # noqa: BLE001
        report.add(WARN, "conversation db readable", f"adapter unavailable: {e}")
        return

    conversations = agy_paths.CONVERSATIONS_DIR
    if not conversations.is_dir():
        report.add(WARN, "conversation db readable", f"missing: {conversations} (no conversation yet)")
        return
    newest = newest_child(conversations, "*.db")
    if newest is None:
        report.add(WARN, "conversation db readable", f"{conversations} has no .db (no conversation yet)")
        return

    # Read a SNAPSHOT, never agy's live store. extract_usage falls back to a
    # read-write open that runs PRAGMA wal_checkpoint(TRUNCATE) when its
    # read-only path surfaces nothing, and an operator runs this script exactly
    # when agy may be mid-write — checkpointing someone else's live WAL is not
    # something a diagnostic gets to do. Copying the db plus its -wal/-shm
    # sidecars keeps the fallback confined to our throwaway copy.
    try:
        with tempfile.TemporaryDirectory(prefix="agy-troubleshoot-") as tmp:
            snapshot = Path(tmp) / newest.name
            for suffix in ("", "-wal", "-shm"):
                sidecar = Path(f"{newest}{suffix}")
                if sidecar.is_file():
                    shutil.copy2(sidecar, f"{snapshot}{suffix}")
            usage = extract_usage(snapshot)
    except OSError as e:
        report.add(FAIL, "conversation db readable", f"unreadable: {newest} ({e})")
        return

    if not usage["num_calls"]:
        # The db exists but carries no gen_metadata rows: either a conversation
        # that never reached the model, or the token field paths have drifted.
        report.add(WARN, "conversation db readable", f"{newest.name}: no model calls recorded")
        return
    report.add(
        OK, "conversation db readable",
        f"{newest.name}: {usage['num_calls']} calls, "
        f"in {usage['total_input']} / out {usage['total_output']} "
        f"(model: {usage['model']})",
    )


# --------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Troubleshoot the antigravity agent install.")
    parser.parse_args()

    commands, err = load_json(COMMANDS_JSON)
    if err or commands is None:
        print(f"FATAL: cannot read {COMMANDS_JSON}: {err}", file=sys.stderr)
        return 1

    binary = commands.get("binary") or commands.get("name") or "agy"
    paths = {
        "home_dir":    expand(commands["home_dir"]),
        "config_file": expand(commands["config_file"]),
    }
    # The CLI-path override env var is declared in settings.json, not the
    # manifest — same key load_agent_config() resolves at runtime.
    settings, _ = load_json(SETTINGS_JSON)
    cli_path_env = (settings or {}).get("cli_path_env") or "AGY_CLI_PATH"

    print(f"{binary} troubleshoot — home: {paths['home_dir']}\n")

    report = Report(agent=commands.get("name") or binary)

    print("Install:")
    installed = check_install(report, binary, paths, cli_path_env)

    if installed:
        print("\nConfig files:")
        check_config_file(report, paths)
        check_login(report)

        print("\nStores:")
        check_brain_store(report)
        check_conversation_db(report)

    print(f"\nSummary: {report.summary()}")
    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
