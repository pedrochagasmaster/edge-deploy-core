"""Reporting contract + mandatory secret redaction (ADR-0002 / ADR-0003)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from edge_deploy import reporting
from edge_deploy.reporting import (
    RELEASE_SCHEMA,
    OperationReport,
    ReleaseReport,
    ReportCheck,
    redact,
    write_release_report,
    write_report,
)

BASE_KEYS = {
    "timestamp",
    "node",
    "host",
    "repo_path",
    "operation",
    "deployment_commit",
    "install_decision",
    "status",
    "sensitive_changed",
    "checks",
}


def test_operation_report_to_payload_full_contract() -> None:
    report = OperationReport(
        operation="rollout",
        status="rolled_out",
        node="node03",
        host="user@hde2stl020003.mastercard.int",
        repo_path="/ads_storage/dispatch",
        deployment_commit="abc",
        reviewed_commit="rev1",
        previous_remote_commit="prev1",
        install_decision="run",
        checks=[ReportCheck("update", True, "ok"), ReportCheck("permissions", False, "bad", {"k": "v"})],
        sensitive_changed=["scr/secret.py"],
        extra={"changed_paths": ["a", "b"], "refused_paths": ["a"]},
    )

    payload = report.to_payload()

    assert BASE_KEYS <= set(payload)
    assert payload["timestamp"].endswith("Z")
    assert payload["reviewed_commit"] == "rev1"
    assert payload["previous_remote_commit"] == "prev1"
    assert payload["sensitive_changed"] == ["scr/secret.py"]
    # extra is merged into the top-level payload (rollout adds changed_paths/refused_paths).
    assert payload["changed_paths"] == ["a", "b"]
    assert payload["refused_paths"] == ["a"]
    assert payload["checks"][0] == {"name": "update", "passed": True, "message": "ok"}
    assert payload["checks"][1] == {"name": "permissions", "passed": False, "message": "bad", "evidence": {"k": "v"}}


def test_operation_report_omits_unset_optional_fields() -> None:
    report = OperationReport(
        operation="preflight",
        status="passed",
        node="edge",
        host="user@edge.example",
        repo_path="not_applicable",
        deployment_commit="not_applicable",
    )

    payload = report.to_payload()

    assert "reviewed_commit" not in payload
    assert "previous_remote_commit" not in payload
    assert payload["sensitive_changed"] == []
    assert payload["install_decision"] == "not_applicable"


def test_report_check_to_payload_omits_empty_evidence() -> None:
    assert ReportCheck("a", True, "m").to_payload() == {"name": "a", "passed": True, "message": "m"}
    assert ReportCheck("a", True, "m", {"x": 1}).to_payload() == {
        "name": "a",
        "passed": True,
        "message": "m",
        "evidence": {"x": 1},
    }


def test_redact_masks_secret_assignments_case_insensitively() -> None:
    text = "ssh PASSCODE=12345678 token=abcDEF password=hunter2 keep=this"

    masked = redact(text)

    assert "12345678" not in masked
    assert "abcDEF" not in masked
    assert "hunter2" not in masked
    assert "PASSCODE=***REDACTED***" in masked
    assert "token=***REDACTED***" in masked
    assert "password=***REDACTED***" in masked
    assert "keep=this" in masked  # non-secret assignments are untouched


def test_write_report_redacts_secrets_everywhere(tmp_path) -> None:
    report = OperationReport(
        operation="rollout",
        status="failed",
        node="node03",
        host="user@edge",
        repo_path="/repo",
        deployment_commit="abc",
        checks=[ReportCheck("auth", False, "sent token=supersecret", {"cmd": "login password=pw123"})],
        extra={"note": "passcode=000111"},
    )

    path = write_report(tmp_path / "report.json", report)
    text = path.read_text(encoding="utf-8")

    assert "supersecret" not in text
    assert "pw123" not in text
    assert "000111" not in text

    data = json.loads(text)
    assert data["checks"][0]["message"] == "sent token=***REDACTED***"
    assert data["checks"][0]["evidence"]["cmd"] == "login password=***REDACTED***"
    assert data["note"] == "passcode=***REDACTED***"


def test_shared_report_contract_contains_minimum_fields(tmp_path) -> None:
    report = OperationReport(
        operation="rollout",
        status="rolled_out",
        node="node03",
        host="user@hde2stl020003.mastercard.int",
        repo_path="/ads_storage/dispatch",
        deployment_commit="abc123",
        previous_remote_commit="def456",
        install_decision="run",
        checks=[ReportCheck(name="update", passed=True, message="update ok")],
    )
    report_path = tmp_path / "deploy-report.json"
    write_report(report_path, report)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["operation"] == "rollout"
    assert payload["status"] == "rolled_out"
    assert payload["node"] == "node03"
    assert payload["host"] == "user@hde2stl020003.mastercard.int"
    assert payload["repo_path"] == "/ads_storage/dispatch"
    assert payload["deployment_commit"] == "abc123"
    assert payload["previous_remote_commit"] == "def456"
    assert payload["install_decision"] == "run"
    assert payload["sensitive_changed"] == []
    assert payload["checks"] == [{"name": "update", "passed": True, "message": "update ok"}]


def test_utc_timestamp_is_iso_z() -> None:
    assert reporting.utc_iso_timestamp().endswith("Z")


def test_report_node_name_prefers_name_then_derives_from_host() -> None:
    assert reporting.report_node_name(SimpleNamespace(name="node07", host="ignored")) == "node07"
    assert reporting.report_node_name(SimpleNamespace(name="", host="user@hde2stl020003.mastercard.int")) == "node03"
    assert reporting.report_node_name(SimpleNamespace(name="", host="user@hde2stl020004.mastercard.int")) == "node04"
    assert reporting.report_node_name(SimpleNamespace(name="", host="user@edge.example")) == "edge"


# ---------------------------------------------------------------------------
# Consolidated release report (edge-deploy/release/1)
# ---------------------------------------------------------------------------


def _sample_release_report() -> ReleaseReport:
    return ReleaseReport(
        selection={
            "tools": ["autobench", "robocop"],
            "nodes": ["node03", "node04"],
            "smoke": "standard",
            "fail_fast": False,
            "snapshot_override": None,
        },
        publishes=[
            {
                "tool": "autobench",
                "status": "published",
                "snapshot": "a1b2c3d4",
                "source_short": "a1b2c3d",
                "branch": "main",
                "previous_remote_commit": "9999",
                "message": "Deploy snapshot: autobench a1b2c3d on main (2026-06-29 23:00) [edge-deploy]",
                "report_path": "edge-deploy/reports/release-X/publish-autobench.json",
            },
            {
                "tool": "robocop", "status": "failed", "snapshot": None,
                "error": "local_check.ps1 failed with exit code 1",
            },
        ],
        rollouts=[
            {
                "tool": "autobench", "node": "node03", "status": "rolled_out", "state_left": "",
                "deployment_commit": "a1b2c3d4", "drift": "passed", "smoke": "passed",
                "report_path": "edge-deploy/reports/release-X/rollout-autobench-node03.json",
            },
            {
                "tool": "autobench", "node": "node04", "status": "failed",
                "state_left": "rolled out but verification failed: runtime_drift",
                "deployment_commit": "a1b2c3d4", "drift": "failed", "smoke": "not_run",
                "report_path": "edge-deploy/reports/release-X/rollout-autobench-node04.json",
            },
            {
                "tool": "robocop", "node": "node03", "status": "skipped",
                "state_left": "publish failed; rollout not attempted",
                "drift": "not_run", "smoke": "not_run", "report_path": None,
            },
            {
                "tool": "robocop", "node": "node04", "status": "skipped",
                "state_left": "publish failed; rollout not attempted",
                "drift": "not_run", "smoke": "not_run", "report_path": None,
            },
        ],
        operator_email="e176097@mastercard.com",
    )


def test_release_report_payload_schema_and_keys() -> None:
    payload = _sample_release_report().to_payload()

    assert payload["schema"] == RELEASE_SCHEMA == "edge-deploy/release/1"
    assert payload["timestamp"].endswith("Z")
    assert payload["operator_email"] == "e176097@mastercard.com"
    assert set(payload) == {
        "schema", "timestamp", "operator_email", "selection", "publishes", "rollouts", "summary", "exit_code"
    }
    assert payload["selection"]["tools"] == ["autobench", "robocop"]


def test_release_report_counts_cover_all_pair_statuses_and_publishes() -> None:
    counts = _sample_release_report().summary()["counts"]

    assert counts == {
        "rolled_out": 1, "failed": 1, "skipped": 2, "refused": 0, "published": 1, "publish_failed": 1
    }


def test_release_report_exit_code_and_overall_failed() -> None:
    report = _sample_release_report()

    assert report.exit_code() == 1
    assert report.to_payload()["exit_code"] == 1
    assert report.summary()["overall"] == "failed"


def test_release_report_handoffs_enumerate_followups() -> None:
    handoffs = _sample_release_report().summary()["handoffs"]
    by_kind = {handoff["kind"]: handoff for handoff in handoffs}

    assert "publish" in by_kind
    assert by_kind["publish"]["tool"] == "robocop"
    assert "publish --tool robocop" in by_kind["publish"]["action"]
    assert "mid_state" in by_kind
    assert by_kind["mid_state"]["node"] == "node04"
    assert "--snapshot a1b2c3d4" in by_kind["mid_state"]["action"]


def test_release_report_clean_run_exit_zero() -> None:
    report = ReleaseReport(
        selection={"tools": ["autobench"], "nodes": ["node03"]},
        publishes=[{"tool": "autobench", "status": "published", "snapshot": "abc"}],
        rollouts=[{"tool": "autobench", "node": "node03", "status": "rolled_out", "state_left": ""}],
    )

    assert report.exit_code() == 0
    assert report.summary()["overall"] == "passed"
    assert report.summary()["handoffs"] == []


def test_release_report_refused_and_snapshot_handoffs() -> None:
    report = ReleaseReport(
        selection={},
        rollouts=[
            {"tool": "robocop", "node": "node03", "status": "refused",
             "state_left": "not started; dependency change refused", "deployment_commit": "abc"},
            {"tool": "autobench", "node": "node04", "status": "skipped",
             "state_left": "snapshot abc not available locally; run git fetch", "deployment_commit": "abc"},
        ],
    )

    kinds = {handoff["kind"] for handoff in report.summary()["handoffs"]}
    assert "refused" in kinds  # a refused rollout is actionable
    assert "snapshot" in kinds  # a snapshot-unavailable skip is actionable (Risk #1)
    assert report.exit_code() == 1  # refused forces non-zero


def test_write_release_report_redacts_secrets(tmp_path) -> None:
    report = ReleaseReport(
        selection={},
        publishes=[{"tool": "robocop", "status": "failed", "error": "boom token=leakedtoken"}],
        rollouts=[{"tool": "robocop", "node": "node03", "status": "failed", "state_left": "auth: passcode=00112233"}],
    )

    path = write_release_report(tmp_path / "release.json", report)
    text = path.read_text(encoding="utf-8")

    assert "leakedtoken" not in text
    assert "00112233" not in text
    data = json.loads(text)
    assert data["schema"] == RELEASE_SCHEMA
