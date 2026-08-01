"""Fabricated checkouts so the console renders — and drives — without a release.

``--demo`` builds two throwaway tool checkouts with their own run ledgers and
points every console button at :mod:`edge_console.demo_engine` instead of the
real ``edge_deploy`` CLI. Nothing here touches the network, an Edge Node, or a
real ledger.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from edge_console.demo_engine import DEMO_AHEAD_BY_TOOL, DEMO_HEAD_BY_TOOL
from edge_console.demo_engine import __file__ as _DEMO_ENGINE_FILE
from edge_console.ledger import SCHEMA

DEMO_ENGINE_PATH = Path(_DEMO_ENGINE_FILE).resolve()


def demo_git(root: Path, *args: str, timeout: float | None = None) -> str | None:
    del timeout
    head = DEMO_HEAD_BY_TOOL.get(root.name)
    if head is None or not args:
        return None
    if args[0] == "rev-parse":
        return head
    if args[0] == "ls-remote":
        return f"{head}\trefs/heads/main"
    if args[0] == "rev-list":
        return DEMO_AHEAD_BY_TOOL.get(root.name)
    if args[0] == "status":
        return "## main...origin/main"  # on main, clean tree
    return None


def demo_argv_builder(spec, args: list[str], cwd: Path) -> list[str]:
    """Route every allowlisted command to the offline simulator."""
    del cwd
    return [sys.executable, "-u", str(DEMO_ENGINE_PATH), spec.kind, *args]


def _phase(state: str, at: str | None, evidence: dict | None = None) -> dict:
    return {"state": state, "updated_at": at, "evidence": evidence or {}}


def build_demo_checkouts() -> list[Path]:
    """Two fabricated tool checkouts (autobench, robocop), each with its own ledger."""
    base = Path(tempfile.mkdtemp(prefix="edge-console-demo-"))
    try:  # standalone by design (ADR-0008); demo just mirrors the installed version
        from edge_deploy import __version__ as engine_version
    except Exception:
        engine_version = "unknown"
    engine = {"version": engine_version, "package_dir": "(demo)", "content_sha256": "d3m0" + "0" * 60}

    checkouts: dict[str, Path] = {}
    for tool in ("autobench", "robocop"):
        checkout = base / tool
        (checkout / "edge-deploy" / "runs").mkdir(parents=True)
        # An empty marker: the console refuses release commands outside a git
        # checkout, and the demo has to look like one. Git itself is never run
        # against these — demo divergence facts come from demo_git.
        (checkout / ".git").mkdir()
        (checkout / "edge_deploy.yaml").write_text(f"tool: {tool}\n", encoding="utf-8")
        checkouts[tool] = checkout

    def write(run: dict, events: list[dict]) -> None:
        run_dir = checkouts[run["tool"]] / "edge-deploy" / "runs" / run["run_id"]
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
        lines = "".join(json.dumps(e) + "\n" for e in events)
        (run_dir / "events.jsonl").write_text(lines, encoding="utf-8")

    def rollout_evidence(tool: str, node: str, status: str, sha: str, **overrides: object) -> dict:
        # Mirrors release._compact_rollout: the deploy phase stores each
        # node's compact rollout report verbatim as ledger evidence.
        evidence: dict = {
            "tool": tool,
            "node": node,
            "status": status,
            "state_left": "",
            "deployment_commit": sha,
            "previous_remote_commit": "5f01d77c2a9b4e6d8f3a1c5b7e9d2f4a6c8b0d1e",
            "sensitive_changed": [],
            "drift": "passed",
            "smoke": "passed",
            "report_path": f"(demo)/rollout-{tool}-{node}.json",
            "dependency": None,
        }
        evidence.update(overrides)
        return evidence

    # 1) Open autobench release, paused mid-deploy: node03 is rolled out,
    #    node04 failed its smoke check, node05 has not been touched. No lock is
    #    held, so the console can resume it — this is the run the cockpit puts
    #    in the spotlight.
    sha_a = DEMO_HEAD_BY_TOOL["autobench"]
    write(
        {
            "schema": SCHEMA,
            "run_id": "run-20260707T131512Z-9c4f2ae",
            "tool": "autobench",
            "source_sha": sha_a,
            "operator": "pedro.chagas@mastercard.com",
            "created_at": "2026-07-07T13:15:12+00:00",
            "kind": "release",
            "rollback_tag": None,
            "engine": engine,
            "nodes": ["node03", "node04", "node05"],
            "status": "open",
            "abandon_reason": None,
            "phases": {
                "verify": _phase(
                    "passed",
                    "2026-07-07T13:16:02+00:00",
                    {"commit": sha_a, "ci": "success", "tests": "passed",
                     "verified_at": "2026-07-07T13:16:02+00:00"},
                ),
                "publish": _phase(
                    "passed",
                    "2026-07-07T13:31:44+00:00",
                    {
                        "snapshot_sha": sha_a,
                        "source_commit": sha_a,
                        "previous_remote_commit": "5f01d77c2a9b4e6d8f3a1c5b7e9d2f4a6c8b0d1e",
                    },
                ),
                "deploy": {
                    "node03": _phase(
                        "passed",
                        "2026-07-07T13:40:19+00:00",
                        rollout_evidence("autobench", "node03", "rolled_out", sha_a),
                    ),
                    "node04": _phase(
                        "failed",
                        "2026-07-07T13:44:51+00:00",
                        rollout_evidence(
                            "autobench", "node04", "failed", sha_a,
                            state_left="rolled out but verification failed: smoke:hive_connectivity",
                            smoke="failed",
                        ),
                    ),
                    "node05": _phase("pending", None),
                },
                "tag_bitbucket": _phase("pending", None),
                "tag_github": _phase("pending", None),
            },
        },
        # The engine only ledgers run_created / phase_entered / phase_skipped /
        # lock_stolen / run_abandoned / run_completed; outcomes live in
        # state.json phases, not the event log.
        [
            {"ts": "2026-07-07T13:15:12+00:00", "event": "run_created", "phase": None, "node": None},
            {"ts": "2026-07-07T13:15:40+00:00", "event": "phase_entered", "phase": "verify", "node": None},
            {"ts": "2026-07-07T13:29:10+00:00", "event": "phase_entered", "phase": "publish", "node": None},
            {"ts": "2026-07-07T13:33:05+00:00", "event": "phase_entered", "phase": "deploy", "node": None},
        ],
    )

    # 2) Completed robocop release: the deployed baseline that robocop's
    #    divergence card is judged against (the fabricated git answers say
    #    three reviewed commits have merged since, so with no open run the
    #    console recommends a release).
    sha_b = "41d9b0c7e2f5a8d1b4c7e0f3a6d9b2c5e8f1a4d7"
    tag_b = "release-20260707T153012Z-41d9b0c"
    write(
        {
            "schema": SCHEMA,
            "run_id": "run-20260707T140233Z-41d9b0c",
            "tool": "robocop",
            "source_sha": sha_b,
            "operator": "pedro.chagas@mastercard.com",
            "created_at": "2026-07-07T14:02:33+00:00",
            "kind": "release",
            "rollback_tag": None,
            "engine": engine,
            "nodes": ["node03", "node04"],
            "status": "complete",
            "abandon_reason": None,
            "phases": {
                "verify": _phase(
                    "passed",
                    "2026-07-07T14:03:20+00:00",
                    {"commit": sha_b, "ci": "success", "tests": "passed",
                     "verified_at": "2026-07-07T14:03:20+00:00"},
                ),
                "publish": _phase(
                    "passed",
                    "2026-07-07T14:21:08+00:00",
                    {
                        "snapshot_sha": sha_b,
                        "source_commit": sha_b,
                        "previous_remote_commit": "b82c1f04a7d3e6b9c2f5a8d1e4b7c0f3a6d9b2c5",
                    },
                ),
                "deploy": {
                    "node03": _phase(
                        "passed", "2026-07-07T14:39:47+00:00",
                        rollout_evidence("robocop", "node03", "rolled_out", sha_b),
                    ),
                    "node04": _phase(
                        "passed", "2026-07-07T14:52:30+00:00",
                        rollout_evidence("robocop", "node04", "rolled_out", sha_b),
                    ),
                },
                "tag_bitbucket": _phase(
                    "passed", "2026-07-07T14:58:11+00:00", {"tag": tag_b, "pushed_sha": sha_b}
                ),
                "tag_github": _phase(
                    "passed", "2026-07-07T15:30:12+00:00", {"tag": tag_b, "pushed_sha": sha_b}
                ),
            },
        },
        [
            {"ts": "2026-07-07T14:02:33+00:00", "event": "run_created", "phase": None, "node": None},
            {"ts": "2026-07-07T14:02:41+00:00", "event": "phase_entered", "phase": "verify", "node": None},
            {"ts": "2026-07-07T14:19:52+00:00", "event": "phase_entered", "phase": "publish", "node": None},
            {"ts": "2026-07-07T14:23:31+00:00", "event": "phase_entered", "phase": "deploy", "node": None},
            {"ts": "2026-07-07T14:57:48+00:00", "event": "phase_entered", "phase": "tag_bitbucket", "node": None},
            {"ts": "2026-07-07T15:29:40+00:00", "event": "phase_entered", "phase": "tag_github", "node": None},
            {"ts": "2026-07-07T15:30:12+00:00", "event": "run_completed", "phase": None, "node": None},
        ],
    )

    # 3) Completed rollback. Publish is seeded "passed" at run creation (the
    #    rollback tag supplies the snapshot); verify is entered, then skipped.
    sha_c = "5f01d77c2a9b4e6d8f3a1c5b7e9d2f4a6c8b0d1e"
    rollback_tag = "release-20260622T110402Z-5f01d77"
    minted_tag = "release-20260630T094001Z-5f01d77"
    write(
        {
            "schema": SCHEMA,
            "run_id": "run-20260630T091501Z-5f01d77",
            "tool": "autobench",
            "source_sha": sha_c,
            "operator": "pedro.chagas@mastercard.com",
            "created_at": "2026-06-30T09:15:01+00:00",
            "kind": "rollback",
            "rollback_tag": rollback_tag,
            "engine": engine,
            "nodes": ["node03", "node04", "node05"],
            "status": "complete",
            "abandon_reason": None,
            "phases": {
                "verify": _phase(
                    "skipped",
                    "2026-06-30T09:15:20+00:00",
                    {
                        "reason": "rollback tag provides reviewed source and snapshot SHA",
                        "rollback_tag": rollback_tag,
                        "source_sha": sha_c,
                    },
                ),
                "publish": _phase(
                    "passed", "2026-06-30T09:15:01+00:00", {"snapshot_sha": sha_c, "source_commit": sha_c}
                ),
                "deploy": {
                    "node03": _phase(
                        "passed", "2026-06-30T09:30:05+00:00",
                        rollout_evidence("autobench", "node03", "rolled_out", sha_c),
                    ),
                    "node04": _phase(
                        "passed", "2026-06-30T09:33:41+00:00",
                        rollout_evidence("autobench", "node04", "rolled_out", sha_c),
                    ),
                    "node05": _phase(
                        "passed", "2026-06-30T09:37:12+00:00",
                        rollout_evidence("autobench", "node05", "rolled_out", sha_c),
                    ),
                },
                "tag_bitbucket": _phase(
                    "passed", "2026-06-30T09:40:02+00:00", {"tag": minted_tag, "pushed_sha": sha_c}
                ),
                "tag_github": _phase(
                    "passed", "2026-06-30T09:55:47+00:00", {"tag": minted_tag, "pushed_sha": sha_c}
                ),
            },
        },
        [
            {"ts": "2026-06-30T09:15:01+00:00", "event": "run_created", "phase": None, "node": None},
            {"ts": "2026-06-30T09:15:12+00:00", "event": "phase_entered", "phase": "verify", "node": None},
            {"ts": "2026-06-30T09:15:20+00:00", "event": "phase_skipped", "phase": "verify", "node": None},
            {"ts": "2026-06-30T09:21:44+00:00", "event": "phase_entered", "phase": "deploy", "node": None},
            {"ts": "2026-06-30T09:39:30+00:00", "event": "phase_entered", "phase": "tag_bitbucket", "node": None},
            {"ts": "2026-06-30T09:55:02+00:00", "event": "phase_entered", "phase": "tag_github", "node": None},
            {"ts": "2026-06-30T09:55:47+00:00", "event": "run_completed", "phase": None, "node": None},
        ],
    )

    # 4) Abandoned run (engine identity changed under it).
    write(
        {
            "schema": SCHEMA,
            "run_id": "run-20260629T160248Z-b82c1f0",
            "tool": "robocop",
            "source_sha": "b82c1f04a7d3e6b9c2f5a8d1e4b7c0f3a6d9b2c5",
            "operator": "pedro.chagas@mastercard.com",
            "created_at": "2026-06-29T16:02:48+00:00",
            "kind": "release",
            "rollback_tag": None,
            "engine": {**engine, "version": "1.4.0"},
            "nodes": ["node03", "node04"],
            "status": "abandoned",
            "abandon_reason": f"engine identity changed (1.4.0 -> {engine_version}); recreate the run",
            "phases": {
                "verify": _phase("passed", "2026-06-29T16:03:30+00:00"),
                "publish": _phase("pending", None),
                "deploy": {
                    "node03": _phase("pending", None),
                    "node04": _phase("pending", None),
                },
                "tag_bitbucket": _phase("pending", None),
                "tag_github": _phase("pending", None),
            },
        },
        [
            {"ts": "2026-06-29T16:02:48+00:00", "event": "run_created", "phase": None, "node": None},
            {"ts": "2026-06-29T16:03:02+00:00", "event": "phase_entered", "phase": "verify", "node": None},
            {"ts": "2026-06-30T08:58:12+00:00", "event": "run_abandoned", "phase": None, "node": None,
             "reason": f"engine identity changed (1.4.0 -> {engine_version})"},
        ],
    )
    return [checkouts["autobench"], checkouts["robocop"]]
