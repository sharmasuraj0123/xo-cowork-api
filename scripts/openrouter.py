#!/usr/bin/env python3
"""Point Claude Code at OpenRouter (or back to Anthropic) by editing settings.json.

Standalone CLI over ``services/cowork_agent/openrouter_settings`` — no server, no
AGENT_NAME, no xo-cowork-api runtime involved. It writes the OpenRouter ``env``
block into Claude Code's own settings file (``~/.claude/settings.json`` by default),
which the ``claude`` CLI reads natively on every run. The same helper backs the
"API Keys" Save flow in the server, so both paths behave identically.

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

  python3 scripts/openrouter.py --show     # print current provider + model
  python3 scripts/openrouter.py --off      # revert to native Anthropic
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Allow running as a plain script (`python3 scripts/openrouter.py`) without the
# venv: put the repo root on sys.path so the pure-stdlib helper imports cleanly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.cowork_agent.openrouter_settings import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_SETTINGS,
    clear_openrouter_settings,
    current_api_key,
    mask,
    read_openrouter_state,
    write_openrouter_settings,
)


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

    path = os.path.expanduser(args.settings)

    if args.show:
        st = read_openrouter_state(settings_path=path)
        if st["connected"]:
            print("provider: openrouter")
            print(f"  base url: {DEFAULT_BASE_URL}")
            print(f"  model:    {st['model']}")
        else:
            print("provider: anthropic (native) — OpenRouter not configured in settings.json")
        return

    if args.off:
        try:
            clear_openrouter_settings(settings_path=path)
        except (json.JSONDecodeError, ValueError) as e:
            sys.exit(f"error: {path} is not valid JSON ({e}). Fix or remove it, then retry.")
        print(f"✓ OpenRouter disabled in {path}. Claude Code now uses native Anthropic.")
        return

    # Key resolution: --key  >  $OPENROUTER_API_KEY  >  existing settings value.
    key = (args.key or os.environ.get("OPENROUTER_API_KEY") or current_api_key(settings_path=path) or "").strip()
    if not key:
        sys.exit("error: no OpenRouter key. Pass --key sk-or-…, set $OPENROUTER_API_KEY, "
                 "or run once with --key so it's stored in settings.json.")
    if not key.startswith("sk-or-"):
        print(f"warning: key does not look like an OpenRouter key (expected sk-or-…): {mask(key)}", file=sys.stderr)

    per_tier = {"opus": args.opus, "sonnet": args.sonnet, "haiku": args.haiku, "subagent": args.subagent}
    try:
        write_openrouter_settings(key, model=args.model, per_tier=per_tier,
                                  base_url=args.base_url, settings_path=path)
    except (json.JSONDecodeError, ValueError) as e:
        sys.exit(f"error: {path} is not valid JSON ({e}). Fix or remove it, then retry.")

    default_model = (args.model or "").strip() or DEFAULT_MODEL
    print(f"✓ Claude Code pointed at OpenRouter via {path}")
    print(f"  key:   {mask(key)}")
    print(f"  model: {default_model}" + (" (with per-tier overrides)" if any(per_tier.values()) else ""))
    print("  run `claude` (or restart your Claude Code session) to use it; `--show` to re-check, `--off` to revert.")


if __name__ == "__main__":
    main()
