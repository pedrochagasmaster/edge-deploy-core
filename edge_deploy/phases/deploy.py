"""Deploy phase: per-node resumable rollout."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from edge_deploy.config import OperatorConfig
from edge_deploy.ledger import RunLedger
from edge_deploy.phases import PhaseSpec, enter_phase, load_run, run_repo_root
from edge_deploy.posture import PHASE_ENDPOINTS
from edge_deploy.release import ReleaseSelection, resolve_nodes, run_release

DEPLOY_SPEC = PhaseSpec(name="deploy", order=30, endpoints=PHASE_ENDPOINTS["deploy"])

_ROLLOUT_TO_LEDGER = {
    "rolled_out": "passed",
    "failed": "failed",
    "refused": "failed",
    "skipped": "pending",
}


def _load_run(args: argparse.Namespace, operator: OperatorConfig) -> tuple[RunLedger, Path]:
    return load_run(args, operator)


def _deploy_next_command(run_id: str, nodes: str | None) -> str:
    command = f"python -m edge_deploy deploy --run {run_id}"
    if nodes:
        command += f" --nodes {nodes}"
    return command


def _pending_nodes(ledger: RunLedger, requested: list[str]) -> list[str]:
    return [
        node
        for node in requested
        if ledger.phase_state("deploy", node=node) != "passed"
    ]


def _all_requested_passed(ledger: RunLedger, requested: list[str]) -> bool:
    return all(ledger.phase_state("deploy", node=node) == "passed" for node in requested)


def _apply_rollout_states(ledger: RunLedger, rollouts: list[dict]) -> None:
    for rollout in rollouts:
        status = rollout.get("status", "")
        mapped = _ROLLOUT_TO_LEDGER.get(status)
        if mapped is None:
            continue
        node = rollout.get("node")
        if not node:
            continue
        ledger.set_phase("deploy", mapped, node=str(node), evidence=dict(rollout))


def run_deploy(args: argparse.Namespace, operator: OperatorConfig) -> int:
    ledger, repo_root = _load_run(args, operator)
    run_id = ledger.state["run_id"]
    tool = ledger.state["tool"]
    repo_root = run_repo_root(ledger, operator, repo_root)
    effective_operator = replace(operator, tools={tool: str(repo_root)})
    if args.nodes:
        requested_nodes = resolve_nodes(effective_operator, args.nodes)
    else:
        requested_nodes = sorted(ledger.state["nodes"])

    next_command = _deploy_next_command(run_id, args.nodes)
    stack = enter_phase(
        DEPLOY_SPEC,
        effective_operator,
        ledger,
        next_command=next_command,
        force_lock=args.force_lock,
        repo_root=repo_root,
    )
    with stack:
        if ledger.phase_state("publish") != "passed":
            print(f"deploy refused: publish has not passed for run {run_id}", file=sys.stderr)
            return 2

        pending_nodes = _pending_nodes(ledger, requested_nodes)
        if not pending_nodes:
            print("deploy: all nodes already rolled out (skipping)")
            return 0

        publish_evidence = ledger.state["phases"]["publish"]["evidence"]
        snapshot_sha = publish_evidence.get("snapshot_sha")
        if not snapshot_sha:
            print(f"deploy refused: publish has not passed for run {run_id}", file=sys.stderr)
            return 2

        selection = ReleaseSelection(
            tools=[tool],
            nodes=pending_nodes,
            snapshot_by_tool={tool: str(snapshot_sha)},
            smoke=args.smoke,
        )
        report = run_release(
            effective_operator,
            selection,
            report_dir=ledger.run_dir,
            auth_mode=args.auth_mode,
            auth_wait_seconds=args.auth_wait_seconds,
            pane_log_dir=ledger.run_dir,
        )
        _apply_rollout_states(ledger, report.rollouts)

    return 0 if _all_requested_passed(ledger, requested_nodes) else 1


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("deploy", help="Roll out pending nodes for a run")
    parser.add_argument("--run", required=True, help="Run id under edge-deploy/runs/")
    parser.add_argument("--nodes", default=None, help="Comma list (e.g. 03,04); default: all run nodes")
    parser.add_argument("--smoke", choices=("standard", "deep"), default="standard")
    parser.add_argument("--auth-mode", choices=("prompt", "pane"), default="prompt")
    parser.add_argument("--auth-wait-seconds", type=float, default=300.0)
    parser.add_argument("--force-lock", action="store_true")
    parser.set_defaults(func=run_deploy)
