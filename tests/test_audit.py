import json
from datetime import datetime, timezone

import pytest

from edge_deploy.audit import AuditAttempt, AuditSyncError, _copy_redacted, _relative_path, check_audit_remote


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
