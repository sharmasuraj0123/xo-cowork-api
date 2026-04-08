#!/usr/bin/env python3
"""
Snapshot backup: .openclaw → tar.gz → GPG encrypt → chunk if >95MB → push to GitHub
Keeps the last 10 backup versions, auto-deletes older ones.
"""

import hashlib
import json
import os
import shutil
import sys
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from config import get_config, SKILL_DIR

SOURCE_DIR = Path.home() / ".openclaw"
BACKUP_REPO = Path.home() / "openclaw-transport"
CHUNK_SIZE_BYTES = 95 * 1024 * 1024  # 95 MB
MAX_VERSIONS = 10

EXCLUDE = [
    "backups-repo",
    ".git",
    ".env",           # excludes any file named .env at any depth
    ".env.*",         # excludes .env.local, .env.production, etc.
    "node_modules",
    "*.sock",
    "whatsapp/store/sessions-*",
    "credentials/whatsapp",
]

# Compute the relative path of the skill's .env from SOURCE_DIR
# so we can exclude it from the tar (it contains secrets)
try:
    _skill_env_rel = (SKILL_DIR / ".env").relative_to(SOURCE_DIR)
    EXCLUDE.append(str(_skill_env_rel))
except ValueError:
    pass  # skill dir is not inside SOURCE_DIR, nothing to exclude


def check_gpg():
    """Verify GPG and gpg-agent are both available."""
    for cmd in ["gpg", "gpg-agent"]:
        if shutil.which(cmd) is None:
            print(f"Error: {cmd} not found. Install with:")
            print("  sudo apt-get update && sudo apt-get install -y gnupg gpg-agent")
            sys.exit(1)


def check_transport_repo():
    """Verify the transport repo exists, or tell the user to run setup."""
    if not (BACKUP_REPO / ".git").is_dir():
        print("Error: Transport repo not initialized.")
        print(f"Run setup first:  python3 {SKILL_DIR}/scripts/setup.py")
        sys.exit(1)


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def split_file(src: Path, dest_dir: Path, chunk_size: int) -> list[str]:
    """Split src into chunk_size pieces in dest_dir. Returns list of filenames."""
    parts = []
    part_num = 0
    with open(src, "rb") as f:
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            name = f"part-{part_num:03d}.gpg"
            (dest_dir / name).write_bytes(data)
            parts.append(name)
            part_num += 1
    return parts


def cleanup_old_versions(backups_dir: Path):
    """Keep only the newest MAX_VERSIONS backup folders."""
    if not backups_dir.is_dir():
        return

    # Each backup is a folder like openclaw-YYYYMMDD-HHMMSS
    versions = sorted(
        [d for d in backups_dir.iterdir() if d.is_dir() and d.name.startswith("openclaw-")],
        reverse=True,
    )

    if len(versions) <= MAX_VERSIONS:
        return

    for old in versions[MAX_VERSIONS:]:
        print(f"  Removing old backup: {old.name}")
        shutil.rmtree(old)


def main():
    if not SOURCE_DIR.is_dir():
        print(f"Error: {SOURCE_DIR} not found")
        sys.exit(1)

    # Preflight checks
    check_gpg()
    check_transport_repo()

    config = get_config()
    password = config["BACKUP_PASSWORD"]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    # Pull latest from GitHub first (preserve backups from other workspaces)
    print("Syncing with GitHub...")
    subprocess.run(
        ["git", "-C", str(BACKUP_REPO), "pull", "origin", "main", "--quiet"],
        capture_output=True,
    )

    backups_dir = BACKUP_REPO / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    version_dir = backups_dir / f"openclaw-{timestamp}"
    version_dir.mkdir(parents=True, exist_ok=True)

    # Create a temp directory for intermediate files
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        encrypted_file = tmpdir / "backup.tgz.gpg"

        # tar | gpg
        print(f"Creating backup: openclaw-{timestamp}")
        exclude_flags = []
        for p in EXCLUDE:
            exclude_flags += ["--exclude", p]

        tar = subprocess.Popen(
            ["tar", "czf", "-", *exclude_flags, "-C", str(SOURCE_DIR), "."],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        gpg = subprocess.Popen(
            [
                "gpg", "--batch", "--yes", "--passphrase", password,
                "--symmetric", "--cipher-algo", "AES256",
                "-o", str(encrypted_file),
            ],
            stdin=tar.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        tar.stdout.close()
        gpg_stdout, gpg_stderr = gpg.communicate()
        _, tar_stderr = tar.communicate()

        if tar.returncode != 0 or gpg.returncode != 0:
            print("Error: backup creation failed")
            if tar_stderr:
                print(f"  tar: {tar_stderr.decode().strip()}")
            if gpg_stderr:
                print(f"  gpg: {gpg_stderr.decode().strip()}")
            shutil.rmtree(version_dir, ignore_errors=True)
            sys.exit(1)

        total_size = encrypted_file.stat().st_size
        checksum = sha256_file(encrypted_file)

        # Chunk if needed
        if total_size > CHUNK_SIZE_BYTES:
            print(f"  Encrypted size: {total_size / (1024*1024):.1f} MB — splitting into chunks...")
            parts = split_file(encrypted_file, version_dir, CHUNK_SIZE_BYTES)
        else:
            # Single chunk
            shutil.copy2(encrypted_file, version_dir / "part-000.gpg")
            parts = ["part-000.gpg"]

        # Write manifest
        manifest = {
            "timestamp": timestamp,
            "chunked": len(parts) > 1,
            "chunk_count": len(parts),
            "chunk_size_mb": 95,
            "total_size_bytes": total_size,
            "sha256": checksum,
            "parts": parts,
        }
        (version_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    size_display = total_size / 1024
    unit = "KB"
    if size_display > 1024:
        size_display /= 1024
        unit = "MB"
    print(f"Backup created: openclaw-{timestamp} ({size_display:.1f} {unit}, {len(parts)} part(s))")

    # Cleanup old versions
    cleanup_old_versions(backups_dir)

    # Push to GitHub — if anything fails, remove this version folder so it
    # doesn't get picked up as an orphan by the next run's git add -A
    os.chdir(BACKUP_REPO)
    try:
        subprocess.run(["git", "add", "-A"], check=True, capture_output=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)

        if diff.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", f"snapshot: {timestamp}", "--quiet"],
                check=True, capture_output=True,
            )
            result = subprocess.run(
                ["git", "push", "origin", "main", "--quiet"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                print("Pushed to GitHub")
            else:
                print("Push failed:", result.stderr.strip())
                raise RuntimeError("push failed")
        else:
            print("No changes to push")
    except Exception as e:
        # Remove the version folder so next run doesn't find orphaned files
        print(f"  Cleaning up failed backup: {version_dir.name}")
        shutil.rmtree(version_dir, ignore_errors=True)
        # Reset git staging so the orphan isn't staged either
        subprocess.run(
            ["git", "reset", "HEAD", "--quiet"],
            capture_output=True, cwd=str(BACKUP_REPO),
        )
        sys.exit(1)

    print(f"Backup complete: {timestamp}")


if __name__ == "__main__":
    main()