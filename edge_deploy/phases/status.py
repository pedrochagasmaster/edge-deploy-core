"""Status command: show run state and the next posture-scoped phase command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from edge_deploy.config import OperatorConfig
from edge_deploy.ledger import RunLedger
from edge_deploy.posture import PHASE_ENDPOINTS

_PHASE_ORDER: tuple[str, ...] = (
    "verify",
    "publish",
    "deploy",
    "tag_github",
    "tag_bitbucket",
)


def _runs_root(repo_root: Path) -> Path:
    return repo_root / "edge-deploy" / "runs"


def _phase_line(label: str, content: str) -> str:
    prefix = f"  {label}:"
    return prefix + " " * (17 - len(prefix)) + content


def _phase_passed(ledger: RunLedger, phase: str) -> bool:
    if phase == "deploy":
        deploy = ledger.state["phases"]["deploy"]
        return all(node["state"] == "passed" for node in deploy.values())
    # "skipped" is satisfied (e.g. verify on a rollback run); pointing "next:"
    # at a permanently-skipped phase would strand the operator on it forever.
    return ledger.phase_state(phase) in ("passed", "skipped")


def _format_publish_line(ledger: RunLedger) -> str:
    state = ledger.phase_state("publish")
    if state == "passed":
        snapshot = ledger.state["phases"]["publish"]["evidence"].get("snapshot_sha", "")
        sha7 = snapshot[:7] if snapshot else "???????"
        return _phase_line("publish", f"passed (snapshot {sha7})")
    return _phase_line("publish", state)


def _format_deploy_line(ledger: RunLedger) -> str:
    deploy = ledger.state["phases"]["deploy"]
    parts = " ".join(f"{node}={info['state']}" for node, info in sorted(deploy.items()))
    return _phase_line("deploy", parts)


def _next_command_for_phase(ledger: RunLedger, phase: str) -> str:
    run_id = ledger.state["run_id"]
    if phase == "verify":
        return f"python -m edge_deploy verify --run {run_id}"
    if phase == "publish":
        return f"python -m edge_deploy publish-phase --run {run_id}"
    if phase == "deploy":
        pending = [
            node
            for node, info in sorted(ledger.state["phases"]["deploy"].items())
            if info["state"] != "passed"
        ]
        nodes = ",".join(pending)
        return f"python -m edge_deploy deploy --run {run_id} --nodes {nodes}"
    if phase == "tag_github":
        return f"python -m edge_deploy tag-github --run {run_id}"
    if phase == "tag_bitbucket":
        return f"python -m edge_deploy tag-bitbucket --run {run_id}"
    raise KeyError(phase)


def _resolve_next_line(ledger: RunLedger) -> str:
    run_status = ledger.state["status"]
    if run_status == "complete":
        return "next: none (complete)"
    if run_status == "abandoned":
        return "next: none (abandoned)"
    for phase in _PHASE_ORDER:
        if not _phase_passed(ledger, phase):
            command = _next_command_for_phase(ledger, phase)
            posture = "+".join(PHASE_ENDPOINTS[phase])
            return f"next: {command}   [posture: {posture}]"
    return "next: none (complete)"


def format_run_status(ledger: RunLedger) -> str:
    state = ledger.state
    run_id = state["run_id"]
    sha7 = state["source_sha"][:7]
    header = (
        f"run {run_id}  tool={state['tool']}  kind={state['kind']}  "
        f"source={sha7}  created={state['created_at']}"
    )
    lines = [
        header,
        _phase_line("verify", ledger.phase_state("verify")),
        _format_publish_line(ledger),
        _format_deploy_line(ledger),
        _phase_line("tag_github", ledger.phase_state("tag_github")),
        _phase_line("tag_bitbucket", ledger.phase_state("tag_bitbucket")),
        _resolve_next_line(ledger),
    ]
    return "\n".join(lines)


def print_run_statuses(
    ledgers: list[RunLedger],
    *,
    runs_root: Path | None = None,
) -> int:
    if not ledgers:
        root = runs_root or Path("edge-deploy/runs")
        print(f"no open runs under {root}")
        return 0
    for index, ledger in enumerate(ledgers):
        if index:
            print()
        print(format_run_status(ledger))
    return 0


def _cmd_status(args: argparse.Namespace, operator: OperatorConfig) -> int:
    del operator
    repo_root = Path.cwd().resolve()
    runs_root = _runs_root(repo_root)
    if args.run:
        run_dir = runs_root / args.run
        if not run_dir.is_dir() or not (run_dir / "state.json").is_file():
            print(f"no such run: {args.run} under {runs_root}", file=sys.stderr)
            return 2
        ledgers = [RunLedger.load(run_dir)]
    else:
        ledgers = list(reversed(RunLedger.find_open(runs_root)))
    return print_run_statuses(ledgers, runs_root=runs_root)


def register_status(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "status",
        help="Show open run state and the next posture-scoped command",
    )
    parser.add_argument("--run", default=None, help="Show one run by id (any status)")
    parser.set_defaults(func=_cmd_status)
