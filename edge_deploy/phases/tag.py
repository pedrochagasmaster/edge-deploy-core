"""Posture-scoped GitHub and Bitbucket release tag finalization phases."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from edge_deploy.config import OperatorConfig, load_tool_profile
from edge_deploy.ledger import RunLedger
from edge_deploy.phases import PHASE_REGISTRY, PhaseSpec, enter_phase, load_run, run_repo_root
from edge_deploy.posture import PHASE_ENDPOINTS

# ADR-0012/0013: tag_bitbucket runs before tag_github so it shares the deploy
# posture (both-vpns); tag_github (firewall-off) is the single final switch.
TAG_BITBUCKET_SPEC = PhaseSpec(
    name="tag_bitbucket",
    order=40,
    endpoints=PHASE_ENDPOINTS["tag_bitbucket"],
)
TAG_GITHUB_SPEC = PhaseSpec(
    name="tag_github",
    order=50,
    endpoints=PHASE_ENDPOINTS["tag_github"],
)


def _tag_successful_release(repo_root: Path, commit: str) -> str:
    """Create the local annotated source release tag."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"release-{stamp}-{commit[:7]}"
    subprocess.run(
        ["git", "tag", "-a", tag, commit, "-m", f"Successful release {tag}"],
        cwd=repo_root,
        check=True,
    )
    return tag


def _merge_phase_evidence(ledger: RunLedger, phase: str, updates: dict) -> None:
    evidence = dict(ledger.state["phases"][phase]["evidence"])
    evidence.update(updates)
    ledger.set_phase(phase, ledger.phase_state(phase), evidence=evidence)


def _ensure_release_tag(ledger: RunLedger, repo_root: Path, source_sha: str, *, phase: str) -> str:
    """Return the run's release tag, minting the local annotated tag on first use.

    Either tag phase may run first (ADR-0012 puts tag_bitbucket before
    tag_github), so the tag name is looked up in both phases' evidence before
    minting a new one.
    """
    for candidate in ("tag_bitbucket", "tag_github"):
        tag = ledger.state["phases"][candidate]["evidence"].get("tag")
        if tag:
            return str(tag)
    tag = _tag_successful_release(repo_root, source_sha)
    _merge_phase_evidence(ledger, phase, {"tag": tag})
    return tag


def _complete_when_both_tags_passed(ledger: RunLedger) -> None:
    if ledger.phase_state("tag_github") == "passed" and ledger.phase_state("tag_bitbucket") == "passed":
        ledger.complete()


def _all_deploy_nodes_passed(ledger: RunLedger) -> bool:
    deploy = ledger.state["phases"]["deploy"]
    return all(deploy[node]["state"] == "passed" for node in ledger.state["nodes"])


def _load_ledger(args: argparse.Namespace, operator: OperatorConfig) -> tuple[RunLedger, Path]:
    ledger, repo_root = load_run(args, operator)
    return ledger, run_repo_root(ledger, operator, repo_root)


def _dereferenced_tag_sha(
    repo_root: Path,
    remote: str,
    tag: str,
    *,
    git_prefix: list[str] | None = None,
) -> str:
    cmd = list(git_prefix) if git_prefix is not None else ["git"]
    cmd.extend(["ls-remote", "--exit-code", "--tags", remote, f"refs/tags/{tag}^{{}}"])
    completed = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=True)
    rows = [line for line in completed.stdout.splitlines() if line.strip()]
    if not rows:
        raise RuntimeError(f"tag verification returned no rows for {tag!r} on {remote}")
    return rows[0].split()[0]


def _bitbucket_git_prefix() -> list[str]:
    token = os.environ.get("BB_TOKEN")
    if not token:
        raise RuntimeError("BB_TOKEN is required for Bitbucket tag finalization")
    return ["git", "-c", f"http.extraHeader=Authorization: Bearer {token}"]


def _release_overall(run_dir: Path) -> str:
    report_path = run_dir / "release.json"
    if not report_path.is_file():
        return "passed"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    return str(payload.get("summary", {}).get("overall", "passed"))


def _cmd_tag_github(args: argparse.Namespace, operator: OperatorConfig) -> int:
    ledger, repo_root = _load_ledger(args, operator)
    run_id = ledger.state["run_id"]
    source_sha = ledger.state["source_sha"]
    next_command = f"python -m edge_deploy tag-github --run {run_id}"

    with enter_phase(
        TAG_GITHUB_SPEC,
        operator,
        ledger,
        next_command=next_command,
        force_lock=args.force_lock,
        repo_root=repo_root,
    ):
        if not _all_deploy_nodes_passed(ledger):
            print(
                f"tag-github refused: not all deploy nodes passed for run {run_id}",
                file=sys.stderr,
            )
            return 2

        if ledger.phase_state("tag_github") == "passed":
            print("tag-github: already pushed (skipping)")
            return 0

        tag = _ensure_release_tag(ledger, repo_root, source_sha, phase="tag_github")

        subprocess.run(["git", "push", "origin", f"refs/tags/{tag}"], cwd=repo_root, check=True)
        pushed_sha = _dereferenced_tag_sha(repo_root, "origin", tag)
        if pushed_sha != source_sha:
            ledger.set_phase("tag_github", "failed", evidence={"tag": tag, "pushed_sha": pushed_sha})
            raise RuntimeError(
                f"GitHub tag resolved to {pushed_sha}; expected {source_sha}"
            )

        ledger.set_phase(
            "tag_github",
            "passed",
            evidence={"tag": tag, "pushed_sha": pushed_sha},
        )
        _complete_when_both_tags_passed(ledger)
    return 0


def _cmd_tag_bitbucket(args: argparse.Namespace, operator: OperatorConfig) -> int:
    ledger, repo_root = _load_ledger(args, operator)
    profile = load_tool_profile(repo_root)
    run_id = ledger.state["run_id"]
    source_sha = ledger.state["source_sha"]
    next_command = f"python -m edge_deploy tag-bitbucket --run {run_id}"

    with enter_phase(
        TAG_BITBUCKET_SPEC,
        operator,
        ledger,
        next_command=next_command,
        force_lock=args.force_lock,
        repo_root=repo_root,
    ):
        if not _all_deploy_nodes_passed(ledger):
            print(
                f"tag-bitbucket refused: not all deploy nodes passed for run {run_id}",
                file=sys.stderr,
            )
            return 2

        if ledger.phase_state("tag_bitbucket") == "passed":
            print("tag-bitbucket: already pushed (skipping)")
            return 0

        tag = _ensure_release_tag(ledger, repo_root, source_sha, phase="tag_bitbucket")
        publish_evidence = ledger.state["phases"]["publish"]["evidence"]
        snapshot_sha = str(publish_evidence.get("snapshot_sha") or source_sha)
        deployed = snapshot_sha
        bb_prefix = _bitbucket_git_prefix()

        if deployed == source_sha:
            subprocess.run(
                [*bb_prefix, "push", "bitbucket", f"refs/tags/{tag}"],
                cwd=repo_root,
                check=True,
            )
        else:
            temp_tag = f"edge-deploy-mirror/{tag}"
            message = f"Successful release {tag} (source {source_sha}) [edge-deploy]"
            subprocess.run(
                ["git", "tag", "-a", "-f", temp_tag, deployed, "-m", message],
                cwd=repo_root,
                check=True,
            )
            try:
                subprocess.run(
                    [*bb_prefix, "push", "bitbucket", f"refs/tags/{temp_tag}:refs/tags/{tag}"],
                    cwd=repo_root,
                    check=True,
                )
            finally:
                subprocess.run(["git", "tag", "-d", temp_tag], cwd=repo_root, check=True)

        pushed_sha = _dereferenced_tag_sha(repo_root, "bitbucket", tag, git_prefix=bb_prefix)
        if pushed_sha != deployed:
            ledger.set_phase(
                "tag_bitbucket",
                "failed",
                evidence={"tag": tag, "pushed_sha": pushed_sha},
            )
            raise RuntimeError(
                f"Bitbucket tag resolved to {pushed_sha}; expected {deployed}"
            )

        # cli imports PHASE_REGISTRY; defer to avoid import cycle.
        from edge_deploy.cli import _record_release_attempt

        _record_release_attempt(
            operator,
            profile.tool,
            source_sha,
            ledger.run_dir,
            _release_overall(ledger.run_dir),
            linked_attempt=ledger.state.get("rollback_tag"),
        )
        ledger.set_phase(
            "tag_bitbucket",
            "passed",
            evidence={"tag": tag, "pushed_sha": pushed_sha},
        )
        _complete_when_both_tags_passed(ledger)
    return 0


def register_tag_github(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("tag-github", help="Push the release tag to GitHub")
    parser.add_argument("--run", required=True)
    parser.add_argument("--force-lock", action="store_true")
    parser.set_defaults(func=_cmd_tag_github)


def register_tag_bitbucket(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("tag-bitbucket", help="Push the release tag to Bitbucket")
    parser.add_argument("--run", required=True)
    parser.add_argument("--force-lock", action="store_true")
    parser.set_defaults(func=_cmd_tag_bitbucket)


PHASE_REGISTRY.append((TAG_BITBUCKET_SPEC, register_tag_bitbucket))
PHASE_REGISTRY.append((TAG_GITHUB_SPEC, register_tag_github))
