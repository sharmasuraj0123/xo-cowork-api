"""
GitHub access: token resolution, repo existence/creation, git operations on the staging clone.

Auth model — `resolve_auth()`:
  1. Try `github_connector.get_github_token()`. This covers users who
     completed the GitHub flow in the cowork UI (whether they pasted a
     PAT or did `gh auth login` through the connector).
  2. Fall back to `os.environ["GITHUB_PAT"]` (loaded from
     `xo-cowork-api/.env` via dotenv at process start, kept fresh by
     `config.upsert_env`).
  3. Return None if neither path produces a token. Callers turn this
     into a 401 with explicit setup instructions for the user.

Repo creation prefers the `gh` CLI when available + authenticated,
falls back to REST `POST /user/repos`. The user must never have to
open GitHub manually — this is non-negotiable per product decision.

Git ops use a per-command extraheader injection rather than embedding
the token in the remote URL, so `.git/config` in the staging clone
never persists the credential.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from services.cowork_agent.connectors import github_connector

from .config import ENV_GITHUB_PAT, LOCAL_STAGING_DIR


GITHUB_API = "https://api.github.com"
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(frozen=True)
class GitHubAuth:
    token: str
    source: str  # "connector" | "env"


class AuthMissingError(RuntimeError):
    """No GitHub token available from any configured source."""


class GitHubAPIError(RuntimeError):
    """REST call returned a non-2xx response. `.status` carries the HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status
        self.message = message


# ── Token + identity ─────────────────────────────────────────────────────────


async def resolve_auth() -> GitHubAuth:
    """Pick a GitHub token from the configured sources, in priority order."""
    connector_token = github_connector.get_github_token()
    if connector_token:
        return GitHubAuth(token=connector_token, source="connector")
    import os
    env_token = (os.environ.get(ENV_GITHUB_PAT) or "").strip()
    if env_token:
        return GitHubAuth(token=env_token, source="env")
    raise AuthMissingError(
        "No GitHub token available. Either complete the GitHub connector "
        f"flow in xo-cowork UI or set {ENV_GITHUB_PAT}=<your_token> in "
        "xo-cowork-api/.env."
    )


async def discover_owner(auth: GitHubAuth) -> str:
    """Return the GitHub login of the user the token belongs to."""
    data = await _get(auth, "/user")
    login = data.get("login")
    if not isinstance(login, str) or not login:
        raise GitHubAPIError(500, "GET /user returned no login")
    return login


# ── Repo existence + creation ────────────────────────────────────────────────


async def repo_exists(owner: str, name: str, *, auth: GitHubAuth) -> bool:
    """True iff the repo exists and the token can see it."""
    if await _gh_cli_authenticated():
        # gh respects its own auth, which may be a different identity than
        # `auth.token`. We still trust it because the design routes both
        # gh-via-UI and PAT-via-UI through the same connector lookup.
        proc = await asyncio.create_subprocess_exec(
            "gh", "repo", "view", f"{owner}/{name}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode == 0:
            return True
        # Fall through to REST — gh may have failed for reasons other than 404
        # (e.g. unauthenticated host). REST gives a precise status.
    try:
        await _get(auth, f"/repos/{owner}/{name}")
        return True
    except GitHubAPIError as exc:
        if exc.status == 404:
            return False
        raise


async def create_repo(owner: str, name: str, *, auth: GitHubAuth) -> str:
    """Create a private repo. Returns the HTTPS clone URL.

    Tries `gh repo create` first; if gh isn't available or that path
    fails, falls back to REST `POST /user/repos`. The latter only
    creates repos under the token's owning user, so `owner` must match
    `discover_owner(auth)` for the fallback to succeed.
    """
    if await _gh_cli_authenticated():
        proc = await asyncio.create_subprocess_exec(
            "gh", "repo", "create", f"{owner}/{name}",
            "--private",
            "--description", "Encrypted xo-projects backups (auto-managed by xo-cowork-api).",
            "--confirm",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            return f"https://github.com/{owner}/{name}.git"
        # gh failed; surface the error but try REST too so we don't fail
        # the whole setup on transient gh issues.
        gh_err = stderr.decode("utf-8", "replace").strip()
    else:
        gh_err = None

    # REST fallback. `auto_init=True` gives us an initial commit on main
    # so subsequent push doesn't have to set up the first ref.
    data = await _post(auth, "/user/repos", {
        "name": name,
        "private": True,
        "auto_init": True,
        "description": "Encrypted xo-projects backups (auto-managed by xo-cowork-api).",
    })
    clone_url = data.get("clone_url")
    if not isinstance(clone_url, str):
        raise GitHubAPIError(502, f"create_repo: REST response missing clone_url. gh said: {gh_err}")
    return clone_url


# ── Git operations on the staging clone ──────────────────────────────────────


def staging_path() -> Path:
    """Where the staging clone lives. Created on first use."""
    return LOCAL_STAGING_DIR


async def ensure_clone(repo_url: str, *, auth: GitHubAuth, path: Path | None = None) -> Path:
    """Clone the repo into the staging dir if missing, else `git pull --ff-only`.

    Returns the path of the staging clone (caller may need it).
    """
    target = path or staging_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if (target / ".git").is_dir():
        await _git(["fetch", "--prune", "origin"], cwd=target, auth=auth)
        await _git(["pull", "--ff-only", "origin"], cwd=target, auth=auth)
        return target

    # First-time clone. Use the bare HTTPS URL — the credential is
    # passed via http.extraheader so it isn't written to .git/config.
    await _git(["clone", repo_url, str(target)], cwd=target.parent, auth=auth)
    # Configure identity locally so future commits don't depend on a
    # global git config that may be missing in container deployments.
    await _git(["config", "user.email", "xo-cowork-api@xo.local"], cwd=target, auth=None)
    await _git(["config", "user.name", "xo-cowork-api"], cwd=target, auth=None)
    return target


async def commit_and_push(message: str, *, auth: GitHubAuth, path: Path | None = None) -> bool:
    """Stage all changes in the clone, commit if anything changed, push.

    Returns True if a commit was created (and pushed), False if there
    was nothing to commit (caller may want to log a warning).
    """
    target = path or staging_path()
    await _git(["add", "-A"], cwd=target, auth=None)
    # `git status --porcelain` lists nothing iff working tree is clean.
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(target), "status", "--porcelain",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if not stdout.strip():
        return False
    await _git(["commit", "-m", message], cwd=target, auth=None)
    await _git(["push", "origin", "HEAD"], cwd=target, auth=auth)
    return True


# ── Internal helpers ─────────────────────────────────────────────────────────


def _auth_header(auth: GitHubAuth) -> str:
    return f"Bearer {auth.token}"


def _git_extraheader(auth: GitHubAuth) -> str:
    """Basic auth header for git's http.extraheader override.

    GitHub accepts username `x-access-token` with the PAT as password
    for HTTPS git operations — same trick gh CLI uses internally.
    """
    raw = f"x-access-token:{auth.token}".encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"AUTHORIZATION: basic {encoded}"


async def _git(args: list[str], *, cwd: Path, auth: GitHubAuth | None) -> str:
    """Run a git subcommand. If ``auth`` is given, inject the credential via -c.

    Returns stdout (decoded). Raises ``RuntimeError`` on non-zero exit.
    """
    full: list[str] = ["git"]
    if auth is not None:
        full += ["-c", f"http.https://github.com/.extraheader={_git_extraheader(auth)}"]
    full += ["-C", str(cwd)] + args
    proc = await asyncio.create_subprocess_exec(
        *full,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        # Strip the auth header from any echoed args before surfacing.
        err = stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {err}")
    return stdout.decode("utf-8", "replace")


async def _gh_cli_authenticated() -> bool:
    """`gh` is on PATH AND `gh auth status` exits zero for github.com."""
    if shutil.which("gh") is None:
        return False
    proc = await asyncio.create_subprocess_exec(
        "gh", "auth", "status", "--hostname", "github.com",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def _get(auth: GitHubAuth, path: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(
            f"{GITHUB_API}{path}",
            headers={
                "Authorization": _auth_header(auth),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if resp.status_code >= 400:
        raise GitHubAPIError(resp.status_code, _short_error(resp))
    return resp.json()


async def _post(auth: GitHubAuth, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{GITHUB_API}{path}",
            json=payload,
            headers={
                "Authorization": _auth_header(auth),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if resp.status_code >= 400:
        raise GitHubAPIError(resp.status_code, _short_error(resp))
    return resp.json()


def _short_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            msg = data.get("message") or ""
            if msg:
                return str(msg)
    except Exception:
        pass
    return (resp.text or "").strip()[:200]
