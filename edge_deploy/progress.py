"""Structured progress, heartbeat, stall detection, and durable release logging."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from edge_deploy.reporting import redact, utc_iso_timestamp

PROGRESS_SCHEMA = "edge-deploy/release-progress/1"


@dataclass
class ActiveOperation:
    phase: str
    label: str
    started_at: float
    last_activity_at: float
    last_meaningful_output_at: str
    tmux_session: str | None = None
    tool: str | None = None
    node: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ReleaseProgressTracker:
    """Emit console progress, heartbeats, stall warnings, release.log, and release-progress.json."""

    def __init__(
        self,
        report_dir: str | Path,
        *,
        heartbeat_interval_s: float = 30.0,
        stall_threshold_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        notify_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.report_dir = Path(report_dir)
        self.heartbeat_interval_s = heartbeat_interval_s
        self.stall_threshold_s = stall_threshold_s
        self._clock = clock
        self._notify = notify_fn or (lambda _message: None)
        self._events: list[str] = []
        self._active: ActiveOperation | None = None
        self._stall_warned = False
        self._log_path = self.report_dir / "release.log"
        self._progress_path = self.report_dir / "release-progress.json"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        if not self._log_path.exists():
            self._append_log("release", "release run started")

    @property
    def events(self) -> list[str]:
        return list(self._events)

    def emit(self, message: str, *, phase: str = "release", log: bool = True, mark_activity: bool = True) -> None:
        redacted = redact(message)
        self._events.append(redacted)
        self._notify(redacted)
        if mark_activity:
            self.mark_activity()
        if log:
            self._append_log(phase, redacted)

    def mark_activity(self) -> None:
        if self._active is not None:
            self._active.last_activity_at = self._clock()
            self._active.last_meaningful_output_at = utc_iso_timestamp()
            self._stall_warned = False

    def start(self, label: str, *, phase: str = "release", tmux_session: str | None = None, **meta: Any) -> None:
        now = self._clock()
        self._active = ActiveOperation(
            phase=phase,
            label=label,
            started_at=now,
            last_activity_at=now,
            last_meaningful_output_at=utc_iso_timestamp(),
            tmux_session=tmux_session,
            tool=meta.get("tool"),
            node=meta.get("node"),
            extra=dict(meta),
        )
        self._stall_warned = False
        self.emit(f"starting: {label}", phase=phase)
        self._write_progress_json()

    def complete(self, message: str, *, phase: str | None = None) -> None:
        active_phase = phase or (self._active.phase if self._active else "release")
        self.emit(f"completed: {message}", phase=active_phase)
        self._active = None
        self._stall_warned = False
        self._write_progress_json()

    def log(self, phase: str, message: str) -> None:
        self.mark_activity()
        self._append_log(phase, message)

    def retry(self, message: str) -> None:
        self.emit(f"retry: {message}", phase="release")

    def final_reports(self, *paths: str | Path) -> None:
        joined = ", ".join(str(path) for path in paths)
        self.emit(f"final reports: {joined}", phase="report")

    @contextmanager
    def tracked(
        self,
        label: str,
        *,
        phase: str = "release",
        tmux_session: str | None = None,
        **meta: Any,
    ) -> Iterator[None]:
        self.start(label, phase=phase, tmux_session=tmux_session, **meta)
        stop = threading.Event()

        def _heartbeat_loop() -> None:
            while not stop.wait(self.heartbeat_interval_s):
                self._maybe_heartbeat()

        thread = threading.Thread(target=_heartbeat_loop, name="release-heartbeat", daemon=True)
        thread.start()
        try:
            yield
            self.complete(label, phase=phase)
        finally:
            stop.set()
            thread.join(timeout=1.0)

    def _elapsed_s(self) -> float:
        if self._active is None:
            return 0.0
        return max(0.0, self._clock() - self._active.started_at)

    def _inactive_s(self) -> float:
        if self._active is None:
            return 0.0
        return max(0.0, self._clock() - self._active.last_activity_at)

    def _maybe_heartbeat(self) -> None:
        if self._active is None:
            return
        elapsed = int(self._elapsed_s())
        inactive = int(self._inactive_s())
        label = self._active.label
        message = f"still running: {label} ({elapsed}s elapsed)"
        self._events.append(redact(message))
        self._notify(redact(message))
        self._write_progress_json()
        if inactive >= self.stall_threshold_s and not self._stall_warned:
            self._stall_warned = True
            session = self._active.tmux_session or "(unknown)"
            safe_action = (
                "check the tmux pane for prompts or hung output; "
                "if idle, interrupt safely and resume with --resume"
            )
            warning = (
                f"stall warning: {label} has had no activity for {inactive}s; "
                f"tmux session {session!r}; next safe action: {safe_action}"
            )
            self.emit(warning, phase=self._active.phase, mark_activity=False)

    def _append_log(self, phase: str, message: str) -> None:
        line = f"{utc_iso_timestamp()} [{phase}] {redact(message)}\n"
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _write_progress_json(self) -> None:
        payload: dict[str, Any] = {
            "schema": PROGRESS_SCHEMA,
            "updated_at": utc_iso_timestamp(),
            "elapsed_s": round(self._elapsed_s(), 1),
        }
        if self._active is not None:
            payload["active"] = {
                "phase": self._active.phase,
                "label": self._active.label,
                "tool": self._active.tool,
                "node": self._active.node,
                "tmux_session": self._active.tmux_session,
                "last_meaningful_output_at": self._active.last_meaningful_output_at,
            }
            payload["inactive_s"] = round(self._inactive_s(), 1)
            if self._inactive_s() >= self.stall_threshold_s:
                payload["stall_warning"] = True
        else:
            payload["active"] = None
        text = json.dumps(payload, indent=2) + "\n"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.report_dir / f".{self._progress_path.name}.tmp"
        tmp_path.write_text(text, encoding="utf-8")
        try:
            tmp_path.replace(self._progress_path)
        except OSError:
            self._progress_path.write_text(text, encoding="utf-8")
            tmp_path.unlink(missing_ok=True)
