"""Progress tracker: event ordering, release-progress.json, heartbeat, stall warnings, release.log."""

from __future__ import annotations

import json

from edge_deploy.progress import PROGRESS_SCHEMA, ReleaseProgressTracker


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
