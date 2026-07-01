#!/usr/bin/env python3
"""Point Claude Code at OpenRouter (or back to Anthropic) by editing settings.json.

This is a standalone utility — no server, no AGENT_NAME, no xo-cowork-api runtime
involved. It writes the OpenRouter ``env`` block into Claude Code's own settings
file (``~/.claude/settings.json`` by default), which the ``claude`` CLI reads
natively on every run. See OpenRouter's "Claude Code integration" docs.

Only the OpenRouter-related keys in the ``env`` block are managed; every other
setting (theme, model, effortLevel, other env vars, …) is preserved.

Examples:
  # enable with the default model (reads key from --key or $OPENROUTER_API_KEY)
  python3 scripts/openrouter.py --key sk-or-v1-xxxx

  # switch the model later (reuses the key already in settings.json)
  python3 scripts/openrouter.py --model google/gemma-4-31b-it:free

  # different model per Claude Code tier
  python3 scripts/openrouter.py --key sk-or-... \\
      --opus nvidia/nemotron-3-ultra-550b-a55b:free \\
      --sonnet qwen/qwen3-next-80b-a3b-instruct:free \\
      --haiku meta-llama/llama-3.2-3b-instruct:free

  python3 scripts/openrouter.py --show     # print current provider + models
  python3 scripts/openrouter.py --off      # revert to native Anthropic
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_SETTINGS = "~/.claude/settings.json"
DEFAULT_BASE_URL = "https://openrouter.ai/api"          # no /v1; the CLI appends it
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"

# The env keys this script owns. --off removes exactly these; everything else in
# the env block (and every other top-level setting) is left untouched.
BASE_URL_KEY = "ANTHROPIC_BASE_URL"
AUTH_TOKEN_KEY = "ANTHROPIC_AUTH_TOKEN"
API_KEY_KEY = "ANTHROPIC_API_KEY"
TIER_KEYS = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "subagent": "CLAUDE_CODE_SUBAGENT_MODEL",
}
MANAGED_KEYS = [BASE_URL_KEY, AUTH_TOKEN_KEY, API_KEY_KEY, *TIER_KEYS.values()]


def _mask(token: str) -> str:
    token = (token or "").strip()
    if len(token) <= 10:
        return "set" if token else "(none)"
    return f"{token[:8]}…{token[-4:]}"


def _load_settings(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"error: {path} is not valid JSON ({exc}). Fix or remove it, then retry.")
    if not isinstance(data, dict):
        sys.exit(f"error: {path} does not contain a JSON object.")
    return data


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _show(env: dict) -> None:
    base = (env.get(BASE_URL_KEY) or "").strip()
    on = base == DEFAULT_BASE_URL and bool((env.get(AUTH_TOKEN_KEY) or "").strip())
    if on:
        print("provider: openrouter")
        print(f"  base url: {base}")
        print(f"  key:      {_mask(env.get(AUTH_TOKEN_KEY))}")
        for label, key in TIER_KEYS.items():
            val = (env.get(key) or "").strip()
            if val:
                print(f"  {label:8s}: {val}")
    else:
        print("provider: anthropic (native) — OpenRouter not configured in settings.json")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Point Claude Code at OpenRouter via settings.json.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--key", help="OpenRouter API key (sk-or-…). Else uses $OPENROUTER_API_KEY, "
                                 "else the key already in settings.json.")
    p.add_argument("--model", help=f"OpenRouter model for all tiers (default: {DEFAULT_MODEL}).")
    p.add_argument("--opus", help="Override the Opus-tier model only.")
    p.add_argument("--sonnet", help="Override the Sonnet-tier model only.")
    p.add_argument("--haiku", help="Override the Haiku-tier model only.")
    p.add_argument("--subagent", help="Override the subagent model only.")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"OpenRouter base URL (default: {DEFAULT_BASE_URL}).")
    p.add_argument("--settings", default=DEFAULT_SETTINGS, help=f"settings.json path (default: {DEFAULT_SETTINGS}).")
    p.add_argument("--off", action="store_true", help="Remove OpenRouter config — revert to native Anthropic.")
    p.add_argument("--show", action="store_true", help="Print the current provider/model and exit.")
    args = p.parse_args()

    path = Path(os.path.expanduser(args.settings))
    settings = _load_settings(path)
    env = settings.get("env")
    if not isinstance(env, dict):
        env = {}

    if args.show:
        _show(env)
        return

    if args.off:
        removed = [k for k in MANAGED_KEYS if k in env]
        for k in removed:
            env.pop(k, None)
        settings["env"] = env
        if not env:                       # tidy up an empty env block
            settings.pop("env", None)
        _write_settings(path, settings)
        print(f"✓ OpenRouter disabled in {path} (removed {len(removed)} keys). Claude Code now uses native Anthropic.")
        return

    # ── enable / update ──────────────────────────────────────────────────────
    # Key resolution: --key  >  $OPENROUTER_API_KEY  >  existing settings value.
    key = (args.key or os.environ.get("OPENROUTER_API_KEY") or env.get(AUTH_TOKEN_KEY) or "").strip()
    if not key:
        sys.exit("error: no OpenRouter key. Pass --key sk-or-…, set $OPENROUTER_API_KEY, "
                 "or run once with --key so it's stored in settings.json.")
    if not key.startswith("sk-or-"):
        print(f"warning: key does not look like an OpenRouter key (expected sk-or-…): {_mask(key)}", file=sys.stderr)

    default_model = (args.model or "").strip() or DEFAULT_MODEL
    per_tier = {"opus": args.opus, "sonnet": args.sonnet, "haiku": args.haiku, "subagent": args.subagent}

    env[BASE_URL_KEY] = args.base_url
    env[AUTH_TOKEN_KEY] = key
    env[API_KEY_KEY] = ""                 # must be present-but-empty for OpenRouter
    for label, key_name in TIER_KEYS.items():
        override = (per_tier[label] or "").strip()
        env[key_name] = override or default_model

    settings["env"] = env
    _write_settings(path, settings)

    print(f"✓ Claude Code pointed at OpenRouter via {path}")
    print(f"  key:   {_mask(key)}")
    print(f"  model: {default_model}" + (" (with per-tier overrides)" if any(per_tier.values()) else ""))
    print("  run `claude` (or restart your Claude Code session) to use it; `--show` to re-check, `--off` to revert.")


if __name__ == "__main__":
    main()
