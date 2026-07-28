#!/usr/bin/env python3
"""Scrub a raw `claude --output-format stream-json` stdout capture into a
committable fixture.

Usage:
    venv/bin/python tools/scrub_capture.py raw.jsonl tests/fixtures/captures/name.jsonl

What it does, and why each rule exists:

  * **ids** — every UUID, `toolu_*`, `msg_*`, `req_*` and `agent-*` id is
    replaced by a *stable, deterministic* stand-in derived from its position of
    first appearance (`00000000-0000-4000-8000-0000000000NN`). Deterministic so
    re-scrubbing the same capture is a no-op in git; positional so cross
    references (``tool_use.id`` ↔ ``tool_result.tool_use_id``) survive, which is
    exactly what the tool-call/tool-result pairing tests need.
  * **paths** — the real workspace root, `$HOME` and any `/home/<user>` prefix
    become `/workspace`. Keeps relative structure.
  * **username** — the operator's login name is replaced with `operator`
    *wherever it appears*, not only as a `/home/<user>` prefix. A path rule
    alone does not do this and the first version of this script wrongly claimed
    it did: the captures are full of the name in forms no path regex sees —
    the session cwd arrives path-mangled as `-home-coder-xo-cowork-api` (no
    leading slash before `home`), and `ls -la` output inside a `tool_result`
    reads `drwxr-xr-x 2 coder coder 4096 …`. Scrubbed with `\b` boundaries;
    names shorter than 3 characters are refused rather than substituted
    blindly. Override with `SCRUB_USERNAME=` for a differently-named host.
  * **secrets** — `sk-ant-*`, `sk-*`, `gh[pousr]_*`, bearer tokens and anything
    matching a long base64/hex run inside a `signature`/`token`/`key` field is
    replaced with `REDACTED`.
  * **cost/usage** — left intact. It is not sensitive and some gates read it.
  * **content** — left intact. Captures MUST be produced from the synthetic
    sandbox prompts in `tests/fixtures/captures/README.md`, never from a real
    user session; scrubbing free text is not a thing this script attempts and
    you should not rely on it to.

The output is re-serialised with `json.dumps(..., ensure_ascii=False,
separators=(",", ":"))`. Key order is preserved (dicts keep insertion order),
so the fixture stays diffable, but byte offsets change — regenerate goldens
after re-scrubbing.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
OPAQUE_ID_RE = re.compile(r"\b(?:toolu|msg|req|call)_[A-Za-z0-9]{8,}\b")
AGENT_ID_RE = re.compile(r"\bagent-[0-9a-f]{8,}\b")
SECRET_RE = re.compile(
    r"\b(?:sk-ant-[A-Za-z0-9_\-]{8,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,})\b"
)
# base64-ish blobs (thinking `signature`, JWTs) — length-gated to avoid eating prose
BLOB_RE = re.compile(r"\b[A-Za-z0-9+/_\-]{120,}={0,2}\b")

SENSITIVE_KEYS = {"signature", "apiKeySource", "token", "access_token", "api_key"}


class Anonymiser:
    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def _uuid_for(self, original: str) -> str:
        if original not in self._seen:
            n = len(self._seen)
            self._seen[original] = f"00000000-0000-4000-8000-{n:012d}"
        return self._seen[original]

    def _opaque_for(self, original: str, prefix: str) -> str:
        if original not in self._seen:
            n = len(self._seen)
            self._seen[original] = f"{prefix}_{'0' * 16}{n:08d}"
        return self._seen[original]

    def text(self, s: str, home: str, workspace: str, username: str = "") -> str:
        s = UUID_RE.sub(lambda m: self._uuid_for(m.group(0)), s)
        s = OPAQUE_ID_RE.sub(
            lambda m: self._opaque_for(m.group(0), m.group(0).split("_", 1)[0]), s
        )
        s = AGENT_ID_RE.sub(lambda m: self._opaque_for(m.group(0), "agent"), s)
        s = SECRET_RE.sub("REDACTED", s)
        if workspace:
            s = s.replace(workspace, "/workspace")
        if home:
            s = s.replace(home, "/workspace")
        s = re.sub(r"/home/[A-Za-z0-9_.-]+", "/workspace", s)
        s = re.sub(r"/Users/[A-Za-z0-9_.-]+", "/workspace", s)
        # LAST, and independent of the path rules above. The path rules only
        # ever matched a literal `/home/<user>` prefix, so they left the name
        # untouched in `-home-coder-xo-cowork-api` (mangled cwd), in
        # `drwxr-xr-x 2 coder coder` (ls output captured in a tool_result), and
        # anywhere else it appears as a bare word.
        if username:
            s = re.sub(rf"\b{re.escape(username)}\b", "operator", s)
        return s

    def walk(self, node, home: str, workspace: str, username: str = ""):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in SENSITIVE_KEYS and isinstance(v, str) and len(v) > 24:
                    out[k] = "REDACTED"
                else:
                    out[k] = self.walk(v, home, workspace, username)
            return out
        if isinstance(node, list):
            return [self.walk(v, home, workspace, username) for v in node]
        if isinstance(node, str):
            s = self.text(node, home, workspace, username)
            return BLOB_RE.sub("REDACTED", s) if len(s) > 120 else s
        return node


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    src, dst = argv[1], argv[2]
    home = os.path.expanduser("~").rstrip("/")
    workspace = os.environ.get("SCRUB_WORKSPACE", "").rstrip("/")

    username = os.environ.get("SCRUB_USERNAME")
    if username is None:
        username = os.path.basename(home) or getpass.getuser()
    username = username.strip()
    if username and len(username) < 3:
        # Substituting a 1-2 character name would corrupt unrelated text far
        # more often than it would protect anything. Refuse instead of guessing.
        print(
            f"!! refusing to scrub the username {username!r}: too short to "
            f"substitute safely. Set SCRUB_USERNAME to a longer value, or to "
            f"the empty string to skip the rule deliberately.",
            file=sys.stderr,
        )
        return 2

    anon = Anonymiser()

    kept = 0
    with open(src, "rb") as fh, open(dst, "w", encoding="utf-8") as out:
        for raw in fh:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"!! dropping non-JSON line ({len(line)}B)", file=sys.stderr)
                continue
            out.write(
                json.dumps(
                    anon.walk(event, home, workspace, username),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            kept += 1

    # Fail loudly rather than committing a leak.
    body = open(dst, encoding="utf-8").read()
    leaks = []
    for pattern, label in (
        (re.compile(re.escape(home)) if home else None, "home dir"),
        (SECRET_RE, "secret-shaped token"),
        # No `(?!.*workspace)` lookahead. It made this check unfirable: the
        # lookahead is satisfied by the word "workspace" appearing ANYWHERE
        # later on the same line, and after scrubbing almost every line
        # contains it. Any surviving `/home/<something>` is a leak, full stop.
        (re.compile(r"/home/[A-Za-z0-9_.-]+"), "unscrubbed /home path"),
        # The rule the docstring promised and the path patterns cannot deliver.
        (re.compile(rf"\b{re.escape(username)}\b") if username else None,
         "operator username"),
    ):
        if pattern and pattern.search(body):
            leaks.append(label)
    if leaks:
        print(f"!! LEAK CHECK FAILED: {leaks} — fixture NOT safe to commit", file=sys.stderr)
        return 1

    print(f"ok: {kept} lines, {len(body)}B -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
