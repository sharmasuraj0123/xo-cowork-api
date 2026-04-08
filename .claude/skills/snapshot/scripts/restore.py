#!/usr/bin/env python3
"""
Snapshot restore: clone repo → pick version → reassemble chunks → verify → decrypt → extract

Usage:
    python3 restore.py              # Interactive — prompts user to pick a version
    python3 restore.py --latest     # Restore the most recent backup (non-interactive)
    python3 restore.py --version TIMESTAMP   # Restore a specific version
    python3 restore.py --list       # List available versions and exit
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from config import get_config

RESTORE_DIR = Path.home() / ".openclaw"
TEMP_DIR = Path.home() / "openclaw-transport-temp"


def check_gpg():
    """Verify GPG and gpg-agent are both available."""
    for cmd in ["gpg", "gpg-agent"]:
        if shutil.which(cmd) is None:
            print(f"Error: {cmd} not found. Install with:")
            print("  sudo apt-get update && sudo apt-get install -y gnupg gpg-agent")
            sys.exit(1)


def human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def load_versions(backups_dir: Path) -> list[dict]:
    """
    Scan backups/ for version folders with manifest.json.
    Also detects legacy single-file backups (pre-chunking format).
    Returns list of version dicts sorted newest first.
    """
    versions = []

    if not backups_dir.is_dir():
        return versions

    # New format: folders with manifest.json
    for d in backups_dir.iterdir():
        if d.is_dir() and d.name.startswith("openclaw-"):
            manifest_file = d / "manifest.json"
            if manifest_file.is_file():
                manifest = json.loads(manifest_file.read_text())
                manifest["_path"] = d
                manifest["_format"] = "chunked"
                versions.append(manifest)

    # Legacy format: standalone .tgz.gpg files (backwards compatibility)
    for f in backups_dir.glob("openclaw-*.tgz.gpg"):
        if f.is_file():
            # Extract timestamp from filename: openclaw-YYYYMMDD-HHMMSS.tgz.gpg
            ts = f.name.replace("openclaw-", "").replace(".tgz.gpg", "")
            versions.append({
                "timestamp": ts,
                "chunked": False,
                "chunk_count": 1,
                "total_size_bytes": f.stat().st_size,
                "sha256": None,  # legacy backups don't have checksums
                "parts": [f.name],
                "_path": f,
                "_format": "legacy",
            })

    # Sort newest first
    versions.sort(key=lambda v: v["timestamp"], reverse=True)
    return versions


def print_versions(versions: list[dict]):
    print(f"\nAvailable versions ({len(versions)}):")
    print("-" * 60)
    for i, v in enumerate(versions, 1):
        size = human_size(v["total_size_bytes"])
        parts_info = f"{v['chunk_count']} part(s)" if v["chunked"] else "single file"
        label = " ← latest" if i == 1 else ""
        fmt = " [legacy]" if v.get("_format") == "legacy" else ""
        print(f"  [{i}] openclaw-{v['timestamp']}  ({size}, {parts_info}){fmt}{label}")
    print("-" * 60)


def reassemble_chunks(version: dict, dest_file: Path):
    """Concatenate chunk parts into a single encrypted file."""
    v_path = version["_path"]

    if version["_format"] == "legacy":
        # Legacy: the path IS the file
        shutil.copy2(v_path, dest_file)
        return

    # Chunked format: cat parts in order
    parts = version.get("parts")
    if not parts:
        # Fallback: glob and sort
        parts = sorted(p.name for p in v_path.glob("part-*.gpg"))

    with open(dest_file, "wb") as out:
        for part_name in parts:
            part_path = v_path / part_name
            if not part_path.is_file():
                print(f"Error: missing chunk {part_name}")
                sys.exit(1)
            with open(part_path, "rb") as inp:
                while True:
                    data = inp.read(8192)
                    if not data:
                        break
                    out.write(data)


def verify_checksum(file_path: Path, expected: str | None) -> bool:
    """Verify SHA-256 if we have an expected checksum."""
    if not expected:
        return True  # legacy backups have no checksum

    actual = sha256_file(file_path)
    if actual != expected:
        print(f"Error: checksum mismatch!")
        print(f"  Expected: {expected}")
        print(f"  Actual:   {actual}")
        return False
    return True


def restore_version(version: dict, password: str):
    """Reassemble, verify, decrypt, and extract a backup version."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        encrypted_file = tmpdir / "backup.tgz.gpg"

        # Reassemble chunks
        print("  Reassembling chunks..." if version["chunked"] else "  Preparing backup file...")
        reassemble_chunks(version, encrypted_file)

        # Verify checksum
        if not verify_checksum(encrypted_file, version.get("sha256")):
            print("Error: backup file is corrupted")
            sys.exit(1)
        if version.get("sha256"):
            print("  Checksum verified ✓")

        # Decrypt and extract
        print("  Decrypting and extracting...")
        RESTORE_DIR.mkdir(parents=True, exist_ok=True)

        gpg = subprocess.Popen(
            [
                "gpg", "--batch", "--yes", "--passphrase", password,
                "--decrypt", str(encrypted_file),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        tar = subprocess.Popen(
            ["tar", "xzf", "-", "-C", str(RESTORE_DIR)],
            stdin=gpg.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        gpg.stdout.close()
        tar_stdout, tar_stderr = tar.communicate()
        gpg_stderr = gpg.stderr.read()

        if gpg.wait() != 0:
            print("Decryption failed — wrong password?")
            if gpg_stderr:
                print(f"  gpg: {gpg_stderr.decode().strip()}")
            sys.exit(1)
        if tar.returncode != 0:
            print("Extraction failed")
            if tar_stderr:
                print(f"  tar: {tar_stderr.decode().strip()}")
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Restore an OpenClaw snapshot")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--latest", action="store_true", help="Restore the most recent backup")
    group.add_argument("--version", type=str, help="Restore a specific version by timestamp")
    group.add_argument("--list", action="store_true", help="List available versions and exit")
    args = parser.parse_args()

    # Preflight check (skip for --list since we don't need GPG)
    if not args.list:
        check_gpg()

    config = get_config()
    repo_url = config["REPO_URL"]
    password = config["BACKUP_PASSWORD"]

    # Clone repo to temp dir
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)

    print("Cloning backup repo...")
    result = subprocess.run(
        ["git", "clone", "--quiet", repo_url, str(TEMP_DIR)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("Clone failed:", result.stderr.strip())
        sys.exit(1)

    try:
        versions = load_versions(TEMP_DIR / "backups")
        if not versions:
            print("No backups found in repo")
            sys.exit(1)

        # --list: just print and exit
        if args.list:
            print_versions(versions)
            return

        # --latest: pick the first one
        if args.latest:
            chosen = versions[0]

        # --version TIMESTAMP: find matching version
        elif args.version:
            matches = [v for v in versions if v["timestamp"] == args.version]
            if not matches:
                print(f"Error: no backup found with timestamp '{args.version}'")
                print_versions(versions)
                sys.exit(1)
            chosen = matches[0]

        # Interactive mode
        else:
            print_versions(versions)
            choice = input("\nSelect version [1] or press Enter for latest: ").strip()
            if not choice:
                chosen = versions[0]
            else:
                try:
                    chosen = versions[int(choice) - 1]
                except (ValueError, IndexError):
                    print("Invalid selection")
                    sys.exit(1)

        print(f"\nRestoring: openclaw-{chosen['timestamp']}")
        restore_version(chosen, password)
        print(f"\nRestored .openclaw from openclaw-{chosen['timestamp']}")

    finally:
        shutil.rmtree(TEMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()