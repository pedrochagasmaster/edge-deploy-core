"""Status command: golden output, filtering, and terminal run states."""

from __future__ import annotations

from pathlib import Path

from edge_deploy.ledger import RunLedger
from edge_deploy.phases.status import format_run_status, print_run_statuses

SOURCE_SHA = "aa6d9a5" + "0" * 33
SNAPSHOT_SHA = "bb7c8d1" + "0" * 33
CREATED_AT = "2026-07-03T12:00:00+00:00"


def _runs_root(tmp_path: Path) -> Path:
    return tmp_path / "edge-deploy" / "runs"


def _create_ledger(
    tmp_path: Path,
    *,
    source_sha: str = SOURCE_SHA,
) -> RunLedger:
    ledger = RunLedger.create(
        _runs_root(tmp_path),
        tool="autobench",
        source_sha=source_sha,
        nodes=["node03", "node04"],
        operator="e176097@mastercard.com",
    )
    ledger.state["created_at"] = CREATED_AT
    ledger._persist_state()
    return ledger


def _golden_in_progress_ledger(tmp_path: Path) -> RunLedger:
    ledger = _create_ledger(tmp_path)
    ledger.set_phase("verify", "passed")
    ledger.set_phase(
        "publish",
        "passed",
        evidence={"snapshot_sha": SNAPSHOT_SHA, "source_commit": SOURCE_SHA},
    )
    ledger.set_phase("deploy", "passed", node="node03")
    ledger.set_phase("deploy", "failed", node="node04")
    return ledger


def test_format_run_status_golden_block(tmp_path: Path) -> None:
    ledger = _golden_in_progress_ledger(tmp_path)
    run_id = ledger.state["run_id"]
    expected = "\n".join(
        [
            f"run {run_id}  tool=autobench  kind=release  source={SOURCE_SHA[:7]}  created={CREATED_AT}",
            "  verify:        passed",
            f"  publish:       passed (snapshot {SNAPSHOT_SHA[:7]})",
            "  deploy:        node03=passed node04=failed",
            "  tag_bitbucket: pending",
            "  tag_github:    pending",
            f"next: python -m edge_deploy deploy --run {run_id} --nodes node04   [posture: both-vpns]",
        ]
    )
    assert format_run_status(ledger) == expected


def test_complete_run_renders_next_none_complete(tmp_path: Path) -> None:
    ledger = _golden_in_progress_ledger(tmp_path)
    ledger.set_phase("deploy", "passed", node="node04")
    ledger.set_phase("tag_github", "passed", evidence={"tag": "release-1.0.0"})
    ledger.set_phase("tag_bitbucket", "passed", evidence={"tag": "release-1.0.0"})
    ledger.complete()
    output = format_run_status(ledger)
    assert "next: none (complete)" in output
    assert output.endswith("next: none (complete)")


def test_abandoned_run_renders_next_none_abandoned(tmp_path: Path) -> None:
    ledger = _golden_in_progress_ledger(tmp_path)
    ledger.abandon("operator cancelled")
    output = format_run_status(ledger)
    assert output.endswith("next: none (abandoned)")


def test_print_run_statuses_no_open_runs(tmp_path: Path, capsys) -> None:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    assert print_run_statuses([], runs_root=runs_root) == 0
    assert capsys.readouterr().out == f"no open runs under {runs_root}\n"


def test_print_run_statuses_newest_first(tmp_path: Path, capsys) -> None:
    runs_root = _runs_root(tmp_path)
    older = RunLedger.create(
        runs_root,
        tool="autobench",
        source_sha="a" * 40,
        nodes=["node03"],
        operator="op@example.com",
    )
    older.state["created_at"] = "2026-07-03T10:00:00+00:00"
    older._persist_state()

    newer = RunLedger.create(
        runs_root,
        tool="autobench",
        source_sha="b" * 40,
        nodes=["node03"],
        operator="op@example.com",
    )
    newer.state["created_at"] = "2026-07-03T12:00:00+00:00"
    newer._persist_state()

    print_run_statuses([newer, older])
    out = capsys.readouterr().out
    assert out.index(newer.state["run_id"]) < out.index(older.state["run_id"])


def test_cmd_status_run_filter(tmp_path: Path, monkeypatch, capsys) -> None:
    ledger = _golden_in_progress_ledger(tmp_path)
    complete = RunLedger.create(
        _runs_root(tmp_path),
        tool="autobench",
        source_sha="c" * 40,
        nodes=["node03"],
        operator="op@example.com",
    )
    complete.set_phase("verify", "passed")
    complete.set_phase("publish", "passed", evidence={"snapshot_sha": "d" * 40})
    complete.set_phase("deploy", "passed", node="node03")
    complete.set_phase("tag_github", "passed")
    complete.set_phase("tag_bitbucket", "passed")
    complete.complete()

    monkeypatch.chdir(tmp_path)
    from edge_deploy.cli import main

    assert main(["status", "--run", ledger.state["run_id"]]) == 0
    out = capsys.readouterr().out
    assert ledger.state["run_id"] in out
    assert complete.state["run_id"] not in out
    assert "node04=failed" in out


def test_cmd_status_default_open_runs_only(tmp_path: Path, monkeypatch, capsys) -> None:
    open_ledger = _golden_in_progress_ledger(tmp_path)
    complete = RunLedger.create(
        _runs_root(tmp_path),
        tool="autobench",
        source_sha="c" * 40,
        nodes=["node03"],
        operator="op@example.com",
    )
    complete.complete()

    monkeypatch.chdir(tmp_path)
    from edge_deploy.cli import main

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert open_ledger.state["run_id"] in out
    assert complete.state["run_id"] not in out


def test_cmd_status_missing_run_exits_two(tmp_path: Path, monkeypatch, capsys) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    runs_root.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    from edge_deploy.cli import main

    assert main(["status", "--run", "run-missing"]) == 2
    err = capsys.readouterr().err
    assert f"no such run: run-missing under {runs_root}" in err


def test_next_skips_skipped_verify_on_rollback_run(tmp_path: Path) -> None:
    """A rollback run's skipped verify must not strand `next:` on verify forever."""
    ledger = _create_ledger(tmp_path)
    ledger.state["kind"] = "rollback"
    ledger.state["rollback_tag"] = "release-20260630T221900Z-5335a65"
    ledger._persist_state()
    ledger.set_phase("verify", "skipped", evidence={"reason": "rollback tag"})
    ledger.set_phase(
        "publish",
        "passed",
        evidence={"snapshot_sha": SNAPSHOT_SHA, "source_commit": SOURCE_SHA},
    )
    run_id = ledger.state["run_id"]

    output = format_run_status(ledger)

    assert (
        f"next: python -m edge_deploy deploy --run {run_id} --nodes node03,node04"
        f"   [posture: both-vpns]"
    ) in output
