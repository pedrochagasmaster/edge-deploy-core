from __future__ import annotations

import argparse

from edge_deploy.onboarding.manifest import normalize_tool_id


def _confirm_restart() -> bool:
    try:
        answer = input("Discard onboarding evidence only? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def run_onboarding(args: argparse.Namespace) -> int:
    tools = [normalize_tool_id(t) for t in (args.tool or [])]
    if args.restart and not (args.yes or _confirm_restart()):
        print("restart cancelled")
        return 1
    print(f"onboard: selected tools={tools or '(prompt later)'}")
    return 0
