"""Progress tracker: event ordering, release-progress.json, heartbeat, stall warnings, release.log."""

from __future__ import annotations

import json

from edge_deploy.progress import PROGRESS_SCHEMA, ReleaseProgressTracker
from edge_deploy.transport import TransferProgress


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def test_progress_event_ordering_and_release_progress_json_lifecycle(tmp_path) -> None:
    clock = FakeClock()
    messages: list[str] = []
    tracker = ReleaseProgressTracker(
        tmp_path,
        heartbeat_interval_s=5.0,
        stall_threshold_s=100.0,
        clock=clock,
        notify_fn=messages.append,
    )

    tracker.start("publish autobench", phase="publish", tool="autobench")
    progress_path = tmp_path / "release-progress.json"
    payload = json.loads(progress_path.read_text(encoding="utf-8"))
    assert payload["schema"] == PROGRESS_SCHEMA
    assert payload["active"]["label"] == "publish autobench"
    assert payload["active"]["phase"] == "publish"
    assert payload["active"]["waiting_on"] is None

    tracker.complete("publish autobench", phase="publish")
    payload = json.loads(progress_path.read_text(encoding="utf-8"))
    assert payload["active"] is None
    assert any("starting: publish autobench" in message for message in messages)
    assert any("completed: publish autobench" in message for message in messages)


def test_heartbeat_and_stall_warning_include_active_step_and_safe_action(tmp_path) -> None:
    clock = FakeClock()
    messages: list[str] = []
    tracker = ReleaseProgressTracker(
        tmp_path,
        heartbeat_interval_s=1.0,
        stall_threshold_s=3.0,
        clock=clock,
        notify_fn=messages.append,
    )

    with tracker.tracked(
        "robocop/node04 smoke dispatch --help",
        phase="verify",
        tool="robocop",
        node="node04",
        tmux_session="edge-node04",
    ):
        clock.advance(1.1)
        tracker._maybe_heartbeat()
        clock.advance(3.0)
        tracker._maybe_heartbeat()

    assert any("still running: robocop/node04 smoke dispatch --help" in message for message in messages)
    stall_messages = [
        message
        for message in messages
        if "stall warning:" in message and "robocop/node04 smoke dispatch --help" in message
    ]
    assert stall_messages
    assert any("no activity for" in message for message in stall_messages)
    assert any("next safe action:" in message for message in messages)
    assert any("edge-node04" in message for message in messages)


def test_activity_delays_stall_warning(tmp_path) -> None:
    clock = FakeClock()
    messages: list[str] = []
    tracker = ReleaseProgressTracker(
        tmp_path,
        heartbeat_interval_s=1.0,
        stall_threshold_s=3.0,
        clock=clock,
        notify_fn=messages.append,
    )

    with tracker.tracked("publish autobench", phase="publish", tool="autobench"):
        clock.advance(2.5)
        tracker.mark_activity()
        clock.advance(2.5)
        tracker._maybe_heartbeat()

    stall_messages = [message for message in messages if "stall warning:" in message]
    assert not stall_messages


def test_heartbeat_does_not_change_last_meaningful_output_timestamp(tmp_path) -> None:
    clock = FakeClock()
    tracker = ReleaseProgressTracker(
        tmp_path,
        heartbeat_interval_s=1.0,
        stall_threshold_s=10.0,
        clock=clock,
        notify_fn=lambda _message: None,
    )
    tracker.start("dependency transfer", phase="dependency", tool="autobench", node="node03")
    before = json.loads((tmp_path / "release-progress.json").read_text(encoding="utf-8"))
    clock.advance(2)

    tracker._maybe_heartbeat()

    after = json.loads((tmp_path / "release-progress.json").read_text(encoding="utf-8"))
    assert (
        after["active"]["last_meaningful_output_at"]
        == before["active"]["last_meaningful_output_at"]
    )
    assert after["inactive_s"] == 2.0


def test_transfer_progress_is_durable_and_marks_activity(tmp_path) -> None:
    clock = FakeClock()
    tracker = ReleaseProgressTracker(tmp_path, clock=clock, stall_threshold_s=3.0)
    tracker.start("rollout autobench/node03", phase="rollout", tool="autobench", node="node03")
    clock.advance(2.0)
    tracker.update_transfer(
        artifact="dependency bundle",
        progress=TransferProgress(bytes_sent=25, total_bytes=100, elapsed_s=2.0),
    )
    payload = json.loads((tmp_path / "release-progress.json").read_text(encoding="utf-8"))
    assert payload["active"]["transfer"]["percent"] == 25.0
    assert payload["active"]["transfer"]["bytes_sent"] == 25
    assert payload["inactive_s"] == 0.0
    assert "stall_warning" not in payload


def test_transfer_progress_completion_reports_full_percent_and_console_message(tmp_path) -> None:
    clock = FakeClock()
    messages: list[str] = []
    tracker = ReleaseProgressTracker(tmp_path, clock=clock, notify_fn=messages.append)
    tracker.start("rollout autobench/node03", phase="rollout", tool="autobench", node="node03")
    clock.advance(5.0)
    tracker.update_transfer(
        artifact="dependency bundle",
        progress=TransferProgress(bytes_sent=10_485_760, total_bytes=10_485_760, elapsed_s=5.0),
    )
    payload = json.loads((tmp_path / "release-progress.json").read_text(encoding="utf-8"))
    assert payload["active"]["transfer"]["percent"] == 100.0
    assert payload["active"]["transfer"]["bytes_sent"] == 10_485_760
    console_message = messages[-1]
    assert "MiB" in console_message
    assert "%" in console_message
    assert "MiB/s" in console_message


def test_release_log_created_with_phase_transitions_and_redacts_secrets(tmp_path) -> None:
    tracker = ReleaseProgressTracker(tmp_path, notify_fn=lambda _message: None)
    secret = "s3cr3t-bearer-token"
    tracker.emit(f"publish complete Authorization: Bearer {secret}", phase="publish")
    tracker.log("auth", "submitted passcode=11112222 for node03")
    tracker.final_reports(tmp_path / "release.json")

    log_text = (tmp_path / "release.log").read_text(encoding="utf-8")
    assert "release run started" in log_text
    assert "publish complete" in log_text
    assert secret not in log_text
    assert "11112222" not in log_text
    assert "Authorization: Bearer ***REDACTED***" in log_text
    assert "passcode=***REDACTED***" in log_text
    assert "final reports:" in log_text


def test_progress_json_contains_waiting_on(tmp_path) -> None:
    clock = FakeClock()
    tracker = ReleaseProgressTracker(tmp_path, clock=clock, notify_fn=lambda _message: None)
    progress_path = tmp_path / "release-progress.json"

    tracker.start("auth node03", phase="auth", node="node03")
    payload = json.loads(progress_path.read_text(encoding="utf-8"))
    assert payload["active"]["waiting_on"] is None

    tracker.set_waiting("operator")
    payload = json.loads(progress_path.read_text(encoding="utf-8"))
    assert payload["active"]["waiting_on"] == "operator"


def test_heartbeat_switches_format_when_waiting_on_operator(tmp_path) -> None:
    clock = FakeClock()
    messages: list[str] = []
    tracker = ReleaseProgressTracker(
        tmp_path,
        heartbeat_interval_s=1.0,
        stall_threshold_s=100.0,
        clock=clock,
        notify_fn=messages.append,
    )

    tracker.start("auth node03", phase="auth", node="node03")
    clock.advance(5)
    tracker._maybe_heartbeat()
    assert any("still running: auth node03 (5s elapsed)" in message for message in messages)

    tracker.set_waiting("operator")
    messages.clear()
    clock.advance(10)
    tracker._maybe_heartbeat()
    assert any(">>> WAITING FOR OPERATOR - auth node03 (15s) <<<" in message for message in messages)
    assert not any("still running:" in message for message in messages)


def test_set_waiting_none_reverts_heartbeat_format(tmp_path) -> None:
    clock = FakeClock()
    messages: list[str] = []
    tracker = ReleaseProgressTracker(
        tmp_path,
        heartbeat_interval_s=1.0,
        stall_threshold_s=100.0,
        clock=clock,
        notify_fn=messages.append,
    )

    tracker.start("auth node03", phase="auth", node="node03")
    tracker.set_waiting("operator")
    clock.advance(5)
    tracker._maybe_heartbeat()
    assert any(">>> WAITING FOR OPERATOR - auth node03 (5s) <<<" in message for message in messages)

    tracker.set_waiting(None)
    payload = json.loads((tmp_path / "release-progress.json").read_text(encoding="utf-8"))
    assert payload["active"]["waiting_on"] is None

    messages.clear()
    clock.advance(3)
    tracker._maybe_heartbeat()
    assert any("still running: auth node03 (8s elapsed)" in message for message in messages)
    assert not any("WAITING FOR OPERATOR" in message for message in messages)
