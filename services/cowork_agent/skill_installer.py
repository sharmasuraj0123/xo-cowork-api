"""
Bundled-skill installer.

Copies skills shipped with this repo into the on-disk locations Claude Code
and OpenClaw read from. Runs at server startup. Non-fatal on missing parents
or write errors — matches the rclone/usage-sync bootstrap pattern in server.py.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _REPO_ROOT / ".agents" / "skills" / "xo-projects" / "SKILL.md"


def install_xo_projects_skill() -> None:
    """
    Install the bundled xo-projects skill into Claude Code and OpenClaw skill dirs.

    Always overwrites: the repo is the source of truth. Creates the
    `skills/xo-projects/` subtree under each target's home if missing.
    Skips a target (with a warning) if its parent dir (`~/.claude` or
    `~/.openclaw`) is absent — that means the corresponding CLI isn't
    installed on this host.
    """
    if not _SOURCE.is_file():
        print(f"⚠️ xo-projects skill source missing: {_SOURCE}")
        return

    home = Path.home()
    targets = (
        ("Claude Code", home / ".claude", home / ".claude" / "skills" / "xo-projects" / "SKILL.md"),
        ("OpenClaw", home / ".openclaw", home / ".openclaw" / "skills" / "xo-projects" / "SKILL.md"),
    )

    for label, parent, target in targets:
        if not parent.is_dir():
            print(f"⚠️ {label} skill install skipped: {parent} does not exist")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_SOURCE.read_bytes())
            print(f"✅ {label} skill installed: {target}")
        except Exception as exc:
            print(f"⚠️ {label} skill install failed ({target}): {exc}")
