import json
from datetime import datetime, timezone

import pytest

from edge_deploy.audit import (
    AuditAttempt,
    AuditSyncError,
    _attempt_requires_resolution,
    _copy_redacted,
    _relative_path,
    check_audit_remote,
)


def attempt(tmp_path):
    return AuditAttempt(
        tool="autobench",
        source_sha="a" * 40,
        started_at=datetime(2026, 7, 1, 14, 30, tzinfo=timezone.utc),
        report_dir=tmp_path,
        core_version="1.0.0",
        operator="operator@example.com",
        status="passed",
    )


def test_relative_path_is_stable_and_partitioned(tmp_path):
    assert str(_relative_path(attempt(tmp_path))).replace("\\", "/") == (
        "releases/autobench/2026/07/20260701T143000Z-aaaaaaa"
    )


def test_copy_redacted_masks_secrets(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "release.json").write_text(
        json.dumps({"message": "token=secret password=hunter2 passcode=12345678"}),
        encoding="utf-8",
    )
    _copy_redacted(source, target)
    text = (target / "release.json").read_text(encoding="utf-8")
    assert "secret" not in text
    assert "hunter2" not in text
    assert "12345678" not in text


def test_copy_redacted_refuses_existing_attempt(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    with pytest.raises(FileExistsError):
        _copy_redacted(source, target)


def test_check_audit_remote_blocks_pending_outbox(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "pending").mkdir()
    with pytest.raises(AuditSyncError, match="unsynchronized"):
        check_audit_remote(tmp_path, outbox=outbox)


def test_failed_pre_publish_attempt_does_not_require_resolution():
    metadata = {"status": "failed", "source_sha": "a" * 40}
    release = {
        "publishes": [{"status": "failed", "snapshot": None}],
        "rollouts": [
            {"status": "skipped", "deployment_commit": None, "previous_remote_commit": None},
            {"status": "skipped", "deployment_commit": None, "previous_remote_commit": None},
        ],
    }

    assert _attempt_requires_resolution(metadata, release) is False


def test_failed_post_publish_attempt_requires_resolution():
    metadata = {"status": "failed", "source_sha": "a" * 40}
    release = {
        "publishes": [{"status": "published", "snapshot": "a" * 40}],
        "rollouts": [{"status": "failed", "deployment_commit": "a" * 40}],
    }

    assert _attempt_requires_resolution(metadata, release) is True


def test_failed_attempt_without_report_requires_resolution():
    metadata = {"status": "failed", "source_sha": "a" * 40}

    assert _attempt_requires_resolution(metadata, None) is True
