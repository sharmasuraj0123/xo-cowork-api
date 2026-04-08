#!/usr/bin/env python3
"""
OpenClaw Snapshot — One-Time Setup
Installs GPG and sets up the GitHub transport repo.
Safe to run multiple times.
"""

import os
import sys
import subprocess
from pathlib import Path
from config import get_config, SKILL_DIR

BACKUP_REPO = Path.home() / "openclaw-transport"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("check", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("capture_output", True)
    return subprocess.run(cmd, **kwargs)


def install_gpg():
    result = subprocess.run(["which", "gpg"], capture_output=True)
    if result.returncode == 0:
        print("✓ GPG already installed")
        return

    print("  Installing GPG...")
    subprocess.run(
        ["sudo", "apt-get", "update", "-qq"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["sudo", "apt-get", "install", "-y", "-qq", "gnupg", "gpg-agent"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("✓ GPG installed")


def setup_repo(config: dict):
    repo_url = config["REPO_URL"]

    if (BACKUP_REPO / ".git").is_dir():
        run(["git", "-C", str(BACKUP_REPO), "remote", "set-url", "origin", repo_url])
        run(["git", "-C", str(BACKUP_REPO), "pull", "origin", "main", "--quiet"], check=False)
        print(f"✓ Repo already exists at {BACKUP_REPO} — remote URL updated")
        return

    # Try cloning first (preserves existing backups on GitHub)
    print("  Cloning from GitHub...")
    result = run(["git", "clone", repo_url, str(BACKUP_REPO)], check=False)

    if result.returncode == 0:
        print("✓ Repo cloned (existing backups preserved)")
        return

    # Clone failed — repo is probably empty/new, so init fresh
    print("  Repo appears empty, initializing...")
    BACKUP_REPO.mkdir(parents=True, exist_ok=True)
    os.chdir(BACKUP_REPO)

    run(["git", "init"])
    run(["git", "remote", "add", "origin", repo_url])
    run(["git", "commit", "--allow-empty", "-m", "init"])
    run(["git", "branch", "-M", "main"])

    result = run(["git", "push", "-u", "origin", "main"], check=False)
    if result.returncode != 0:
        print(f"✗ Push failed: {result.stderr.strip()}")
        print("  Check your GITHUB_USERNAME, GITHUB_PAT, and make sure the repo exists on GitHub.")
        sys.exit(1)

    print("✓ Repo initialized and pushed")


def main():
    print("=" * 50)
    print("  OpenClaw Snapshot — Setup")
    print("=" * 50)
    print()

    config = get_config()
    print("✓ Config loaded from .env")
    print(f"  GitHub user: {config['GITHUB_USERNAME']}")
    print(f"  Repo: {config['REPO_NAME']}")
    print()

    print("[1/2] Checking GPG...")
    install_gpg()
    print()

    print("[2/2] Setting up GitHub transport repo...")
    setup_repo(config)
    print()

    print("=" * 50)
    print("  Setup complete!")
    print("=" * 50)
    print()
    print(f"  To backup:   python3 {SKILL_DIR}/scripts/backup.py")
    print(f"  To restore:  python3 {SKILL_DIR}/scripts/restore.py")
    print()


if __name__ == "__main__":
    main()