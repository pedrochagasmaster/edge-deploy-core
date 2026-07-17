"""edge_console: ledger reading, divergence verdicts, and demo-shape guards."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edge_console import (  # noqa: E402
    _SCHEMA,
    PAGE,
    ToolsProber,
    _tool_name,
    build_demo_checkouts,
    collect_runs,
    collect_runs_multi,
    probe_divergence,
)


def _write_state(
    run_dir: Path,
    run_id: str,
    *,
    tool: str = "autobench",
    status: str = "open",
    source_sha: str = "a" * 40,
    created_at: str = "2026-07-10T00:00:00+00:00",
    kind: str = "release",
) -> None:
    run_dir.mkdir(parents=True)
    state = {
        "schema": _SCHEMA,
        "run_id": run_id,
        "tool": tool,
        "source_sha": source_sha,
        "operator": "pedro.chagas",
        "created_at": created_at,
        "kind": kind,
        "rollback_tag": None,
        "engine": {"version": "1.4.0", "package_dir": "(test)", "content_sha256": "b" * 64},
        "nodes": ["node03"],
        "status": status,
        "abandon_reason": None,
        "phases": {},
    }
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _fake_git(
    head: str | None = None,
    origin: str | None = None,
    ahead: str | None = None,
    behind_origin: str | None = None,
    ahead_of_origin: str | None = None,
):
    def git(root: Path, *args: str, timeout: float | None = None) -> str | None:
        del root, timeout
        if args[0] == "rev-parse":
            return head
        if args[0] == "ls-remote":
            return f"{origin}\trefs/heads/main" if origin else None
        if args[0] == "rev-list":
            if args[2] == f"HEAD..{origin}":
                return behind_origin
            if args[2] == f"{origin}..HEAD":
                return ahead_of_origin
            return ahead  # the deployed..HEAD count
        return None

    return git


# ---------------------------------------------------------------------------
# collect_runs / collect_runs_multi
# ---------------------------------------------------------------------------


def test_collect_runs_includes_progress_file_unchanged(tmp_path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    run_dir = runs_root / "run-20260710T000000Z-aaaaaaa"
    _write_state(run_dir, run_dir.name)
    progress_payload = {
        "schema": "edge-deploy/release-progress/1",
        "updated_at": "2026-07-10T00:00:05+00:00",
        "elapsed_s": 5.0,
        "active": {
            "phase": "rollout",
            "label": "rollout autobench/node03",
            "tool": "autobench",
            "node": "node03",
            "tmux_session": None,
            "last_meaningful_output_at": "2026-07-10T00:00:05+00:00",
            "waiting_on": None,
            "transfer": {
                "artifact": "autobench dependency bundle",
                "bytes_sent": 25,
                "total_bytes": 100,
                "percent": 25.0,
                "bytes_per_second": 12.5,
                "updated_at": "2026-07-10T00:00:05+00:00",
            },
        },
        "inactive_s": 0.0,
    }
    (run_dir / "release-progress.json").write_text(json.dumps(progress_payload), encoding="utf-8")

    runs = collect_runs(runs_root)

    assert len(runs) == 1
    assert runs[0]["progress"] == progress_payload


def test_collect_runs_returns_none_progress_when_file_absent(tmp_path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    run_dir = runs_root / "run-20260710T000100Z-bbbbbbb"
    _write_state(run_dir, run_dir.name)

    runs = collect_runs(runs_root)

    assert len(runs) == 1
    assert runs[0]["progress"] is None


def test_collect_runs_multi_tags_roots_and_puts_open_first(tmp_path) -> None:
    root_a = tmp_path / "autobench"
    root_b = tmp_path / "robocop"
    _write_state(
        root_a / "edge-deploy" / "runs" / "run-20260711T000000Z-ccccccc",
        "run-20260711T000000Z-ccccccc",
        status="complete",
        created_at="2026-07-11T00:00:00+00:00",
    )
    _write_state(
        root_b / "edge-deploy" / "runs" / "run-20260710T000000Z-ddddddd",
        "run-20260710T000000Z-ddddddd",
        tool="robocop",
        status="open",
        created_at="2026-07-10T00:00:00+00:00",
    )

    runs = collect_runs_multi([root_a, root_b])

    assert [r["state"]["status"] for r in runs] == ["open", "complete"]
    assert runs[0]["root"] == str(root_b)
    assert runs[1]["root"] == str(root_a)


# ---------------------------------------------------------------------------
# Divergence: deployed SHA vs checkout HEAD vs GitHub main
# ---------------------------------------------------------------------------


def _runs_with_complete(tmp_path: Path, sha: str) -> list[dict]:
    runs_root = tmp_path / "edge-deploy" / "runs"
    _write_state(
        runs_root / "run-20260709T000000Z-deploy1",
        "run-20260709T000000Z-deploy1",
        status="complete",
        source_sha=sha,
        created_at="2026-07-09T00:00:00+00:00",
    )
    return collect_runs(runs_root)


def test_divergence_up_to_date(tmp_path) -> None:
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(tmp_path, runs, git=_fake_git(head=deployed, origin=deployed))
    assert result["verdict"] == "up_to_date"
    assert result["stale"] is False


def test_divergence_diverged_counts_undeployed_commits(tmp_path) -> None:
    deployed = "d" * 40
    head = "e" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(tmp_path, runs, git=_fake_git(head=head, origin=head, ahead="3"))
    assert result["verdict"] == "diverged"
    assert result["ahead"] == 3
    assert result["ahead_exact"] is True  # live ls-remote matches HEAD: count is exact
    assert result["stale"] is False
    assert result["deployed"]["sha"] == deployed


def test_divergence_stale_count_is_a_lower_bound(tmp_path) -> None:
    """Remote-only commits are uncountable without a fetch: when GitHub main
    has moved past the checkout, ahead stays the vs-HEAD count (what a release
    would ship right now) and is flagged as not exact."""
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(
        tmp_path, runs, git=_fake_git(head="e" * 40, origin="f" * 40, ahead="2")
    )
    assert result["verdict"] == "diverged"
    assert result["ahead"] == 2
    assert result["ahead_exact"] is False
    assert result["stale"] is True
    assert result["stale_direction"] is None  # origin object not fetched: can't tell


def test_divergence_stale_direction_local_behind(tmp_path) -> None:
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(
        tmp_path,
        runs,
        git=_fake_git(
            head="e" * 40, origin="f" * 40, ahead="2",
            behind_origin="4", ahead_of_origin="0",
        ),
    )
    assert result["stale_direction"] == "local_behind"
    assert result["behind_origin"] == 4


def test_divergence_stale_direction_local_ahead_unpushed(tmp_path) -> None:
    """GitHub main is an ancestor of HEAD: local commits are unpushed, the
    vs-HEAD count is complete, and the fix is push/PR — not pull."""
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(
        tmp_path,
        runs,
        git=_fake_git(
            head="e" * 40, origin="f" * 40, ahead="11",
            behind_origin="0", ahead_of_origin="11",
        ),
    )
    assert result["verdict"] == "diverged"
    assert result["stale_direction"] == "local_ahead"
    assert result["ahead_of_origin"] == 11


def test_divergence_stale_direction_forked(tmp_path) -> None:
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(
        tmp_path,
        runs,
        git=_fake_git(
            head="e" * 40, origin="f" * 40, ahead="2",
            behind_origin="3", ahead_of_origin="2",
        ),
    )
    assert result["stale_direction"] == "forked"
    assert result["behind_origin"] == 3
    assert result["ahead_of_origin"] == 2


def test_divergence_count_not_exact_when_github_unreachable(tmp_path) -> None:
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(tmp_path, runs, git=_fake_git(head="e" * 40, ahead="2"))
    assert result["verdict"] == "diverged"
    assert result["ahead"] == 2
    assert result["ahead_exact"] is False  # no live proof without ls-remote
    assert result["stale"] is False


def test_divergence_checkout_stale_when_only_github_moved(tmp_path) -> None:
    deployed = "d" * 40
    runs = _runs_with_complete(tmp_path, deployed)
    result = probe_divergence(tmp_path, runs, git=_fake_git(head=deployed, origin="f" * 40))
    assert result["verdict"] == "checkout_stale"
    assert result["stale"] is True


def test_divergence_never_released_without_complete_run(tmp_path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    _write_state(
        runs_root / "run-20260710T000000Z-eeeeeee",
        "run-20260710T000000Z-eeeeeee",
        status="abandoned",
    )
    runs = collect_runs(runs_root)
    result = probe_divergence(tmp_path, runs, git=_fake_git(head="e" * 40, origin="e" * 40))
    assert result["verdict"] == "never_released"
    assert result["deployed"] is None


def test_divergence_unknown_when_git_unavailable(tmp_path) -> None:
    runs = _runs_with_complete(tmp_path, "d" * 40)
    result = probe_divergence(tmp_path, runs, git=_fake_git())
    assert result["verdict"] == "unknown"
    assert result["head"] is None


def test_tool_name_prefers_profile_then_ledger_then_dirname(tmp_path) -> None:
    root = tmp_path / "some-checkout"
    runs_root = root / "edge-deploy" / "runs"
    _write_state(runs_root / "run-20260710T000000Z-fffffff", "run-20260710T000000Z-fffffff",
                 tool="robocop")
    runs = collect_runs(runs_root)

    assert _tool_name(root, runs) == "robocop"  # ledger, no profile yet

    (root / "edge_deploy.yaml").write_text('tool: "autobench"\nnodes: []\n', encoding="utf-8")
    assert _tool_name(root, runs) == "autobench"  # committed profile wins

    assert _tool_name(root, []) == "autobench"
    (root / "edge_deploy.yaml").unlink()
    assert _tool_name(root, []) == "some-checkout"  # nothing left but the directory


# ---------------------------------------------------------------------------
# Demo checkouts: must stay shaped like real ledgers and real tool cards
# ---------------------------------------------------------------------------


def test_demo_ledger_matches_engine_conventions() -> None:
    """The fabricated demo must stay shaped like a real ledger.

    Guards the drift this console already suffered once: deploy keys are
    operator-config node names, deploy evidence is the compact rollout report,
    and only event names the engine actually records appear in events.jsonl.
    """
    from edge_deploy import __version__
    from edge_deploy.ledger import _VALID_PHASE_STATES, _VALID_STATUSES

    engine_events = {
        "run_created",
        "phase_entered",
        "phase_skipped",
        "lock_stolen",
        "run_abandoned",
        "run_completed",
    }
    roots = build_demo_checkouts()
    runs = collect_runs_multi(roots)
    assert runs, "demo checkouts produced no readable runs"
    current_engine_runs = 0
    for run in runs:
        state = run["state"]
        assert state["status"] in _VALID_STATUSES
        current_engine_runs += state["engine"]["version"] == __version__
        deploy = state["phases"]["deploy"]
        assert sorted(deploy) == sorted(state["nodes"])
        for name, node_phase in deploy.items():
            assert name.startswith("node")
            assert node_phase["state"] in _VALID_PHASE_STATES
            if node_phase["state"] in ("passed", "failed"):
                evidence = node_phase["evidence"]
                assert evidence["node"] == name
                assert {"status", "state_left", "deployment_commit", "drift", "smoke"} <= set(evidence)
        for event in run["events"]:
            assert event["event"] in engine_events, event
    assert current_engine_runs >= len(runs) - 1  # one run demos an engine-identity mismatch


def test_demo_tools_show_guide_and_inflight_states() -> None:
    """The demo must exercise both tool-card states: a release in flight
    (autobench) and a diverged tool with no open run, where the console
    suggests a release (robocop)."""
    roots = build_demo_checkouts()
    snapshot = ToolsProber(roots, demo=True).snapshot()
    by_tool = {entry["tool"]: entry for entry in snapshot["tools"]}

    assert set(by_tool) == {"autobench", "robocop"}
    for entry in by_tool.values():
        assert entry["deployed"] is not None
        assert entry["nodes"], "guide needs a node name for preflight/transport-smoke"

    autobench = by_tool["autobench"]
    assert autobench["open_run_id"], "autobench demos the release-in-flight state"

    robocop = by_tool["robocop"]
    assert robocop["open_run_id"] is None, "robocop demos the start-a-release guide"
    assert robocop["verdict"] == "diverged"
    assert robocop["ahead"] == 3
    assert robocop["ahead_exact"] is True
    assert robocop["stale"] is False


# ---------------------------------------------------------------------------
# Copied next-command must not assume cwd == the run's checkout
# ---------------------------------------------------------------------------


def test_next_command_cds_into_the_runs_root() -> None:
    """Regression guard: a multi-root console must never hand the operator a
    bare 'py -m edge_deploy ...' command. load_run() falls back to cwd when a
    run isn't under a configured operator tool path, so copying one tool's
    command while standing in another tool's checkout fails with 'no such
    run' — nextCommand() must cd into run.root first."""
    assert "function nextCommand(run, phase){" in PAGE
    body = PAGE.split("function nextCommand(run, phase){", 1)[1].split("\n}", 1)[0]
    assert "run.root" in body
    assert 'cd "${run.root}"' in body or "cd \\\"${run.root}\\\"" in body
