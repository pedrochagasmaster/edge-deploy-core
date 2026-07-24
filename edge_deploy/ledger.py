"""Run ledger: durable release-run state, events, and locking."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import edge_deploy

__version__ = edge_deploy.__version__

_SCHEMA = "edge-deploy/run/1"
_VALID_PHASE_STATES = frozenset({"pending", "passed", "failed", "skipped"})
_VALID_STATUSES = frozenset({"open", "complete", "abandoned"})
_PHASES = frozenset({"verify", "publish", "deploy", "tag_github", "tag_bitbucket"})


class LedgerError(RuntimeError):
    """Invalid or inconsistent run ledger state."""


class RunLockError(LedgerError):
    """Another process holds the run lock."""


def is_training_ledger(ledger_or_state: RunLedger | dict) -> bool:
    """True when either training marker is present (kind or training flag)."""
    state = ledger_or_state.state if isinstance(ledger_or_state, RunLedger) else ledger_or_state
    return state.get("kind") == "training" or state.get("training") is True


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _content_sha256(package_dir: Path) -> str:
    py_files = sorted(
        (p for p in package_dir.rglob("*.py") if p.is_file()),
        key=lambda p: p.relative_to(package_dir).as_posix(),
    )
    parts: list[str] = []
    for path in py_files:
        relpath = path.relative_to(package_dir).as_posix()
        parts.append(f"{relpath}\n{_file_sha256(path)}\n")
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def engine_identity() -> dict:
    package_dir = Path(edge_deploy.__file__).resolve().parent
    return {
        "version": __version__,
        "package_dir": str(package_dir),
        "content_sha256": _content_sha256(package_dir),
    }


def _empty_phase() -> dict:
    return {"state": "pending", "updated_at": None, "evidence": {}}


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # Windows refuses os.replace while a concurrent reader (status, another
    # phase's load) holds the target open; retry briefly instead of crashing
    # a release mid-phase.
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _validate_state(state: dict) -> None:
    if state.get("schema") != _SCHEMA:
        raise LedgerError(f"invalid run schema: {state.get('schema')!r}")


@dataclass
class RunLedger:
    run_dir: Path
    state: dict
    _lock_depth: int = field(default=0, repr=False)
    _lock_borrowed: bool = field(default=False, repr=False)

    @classmethod
    def create(
        cls,
        runs_root: Path,
        *,
        tool: str,
        source_sha: str,
        nodes: list[str],
        operator: str,
        kind: str = "release",
        rollback_tag: str | None = None,
        training: bool = False,
    ) -> RunLedger:
        is_training = bool(training) or kind == "training"
        if is_training:
            kind = "training"
        now = _utc_now()
        run_id = f"run-{now.strftime('%Y%m%dT%H%M%SZ')}-{source_sha[:7]}"
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)

        engine = engine_identity()
        state = {
            "schema": _SCHEMA,
            "run_id": run_id,
            "tool": tool,
            "source_sha": source_sha,
            "operator": operator,
            "created_at": _utc_iso(now),
            "kind": kind,
            "rollback_tag": rollback_tag,
            "engine": engine,
            "nodes": list(nodes),
            "status": "open",
            "abandon_reason": None,
            "phases": {
                "verify": _empty_phase(),
                "publish": _empty_phase(),
                "deploy": {node: _empty_phase() for node in nodes},
                "tag_bitbucket": _empty_phase(),
                "tag_github": _empty_phase(),
            },
        }
        if is_training:
            state["training"] = True
        _write_json_atomic(run_dir / "state.json", state)
        ledger = cls(run_dir=run_dir, state=state)
        ledger.record_event("run_created")
        return ledger

    @classmethod
    def load(cls, run_dir: Path) -> RunLedger:
        state_path = run_dir / "state.json"
        if not state_path.is_file():
            raise LedgerError(f"missing run state: {state_path}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        _validate_state(state)
        return cls(run_dir=run_dir, state=state)

    @classmethod
    def find_open(cls, runs_root: Path) -> list[RunLedger]:
        if not runs_root.is_dir():
            return []
        ledgers: list[RunLedger] = []
        for entry in runs_root.iterdir():
            if not entry.is_dir():
                continue
            state_path = entry / "state.json"
            if not state_path.is_file():
                continue
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("schema") != _SCHEMA:
                continue
            if state.get("status") != "open":
                continue
            ledgers.append(cls(run_dir=entry, state=state))
        ledgers.sort(key=lambda ledger: ledger.state["created_at"])
        return ledgers

    def _persist_state(self) -> None:
        _write_json_atomic(self.run_dir / "state.json", self.state)

    def record_event(
        self,
        event: str,
        *,
        phase: str | None = None,
        node: str | None = None,
        **extra: object,
    ) -> None:
        entry: dict = {
            "ts": _utc_iso(),
            "event": event,
            "phase": phase,
            "node": node,
            **extra,
        }
        events_path = self.run_dir / "events.jsonl"
        with events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def set_phase(
        self,
        phase: str,
        state: str,
        *,
        node: str | None = None,
        evidence: dict | None = None,
    ) -> None:
        if phase not in _PHASES:
            raise LedgerError(f"unknown phase: {phase!r}")
        if state not in _VALID_PHASE_STATES:
            raise LedgerError(f"unknown phase state: {state!r}")
        if phase == "deploy":
            if node is None:
                raise LedgerError("deploy phase requires node")
            deploy = self.state["phases"]["deploy"]
            if node not in deploy:
                raise LedgerError(f"unknown deploy node: {node!r}")
            target = deploy[node]
        else:
            if node is not None:
                raise LedgerError(f"node must not be set for phase {phase!r}")
            target = self.state["phases"][phase]

        target["state"] = state
        target["updated_at"] = _utc_iso()
        if evidence is not None:
            target["evidence"] = evidence
        self._persist_state()

    def phase_state(self, phase: str, *, node: str | None = None) -> str:
        if phase not in _PHASES:
            raise LedgerError(f"unknown phase: {phase!r}")
        if phase == "deploy":
            if node is None:
                raise LedgerError("deploy phase requires node")
            deploy = self.state["phases"]["deploy"]
            if node not in deploy:
                raise LedgerError(f"unknown deploy node: {node!r}")
            return deploy[node]["state"]
        if node is not None:
            raise LedgerError(f"node must not be set for phase {phase!r}")
        return self.state["phases"][phase]["state"]

    def abandon(self, reason: str) -> None:
        self.state["status"] = "abandoned"
        self.state["abandon_reason"] = reason
        self._persist_state()
        self.record_event("run_abandoned", reason=reason)

    def complete(self) -> None:
        self.state["status"] = "complete"
        self._persist_state()
        self.record_event("run_completed")

    def _raise_foreign_lock(self, payload: dict) -> None:
        run_id = self.state["run_id"]
        pid = payload["pid"]
        hostname = payload["hostname"]
        acquired_at = payload["acquired_at"]
        raise RunLockError(
            f"run {run_id} is locked by PID {pid} on {hostname} "
            f"(acquired {acquired_at}); if that process is dead, "
            f"re-run with --force-lock"
        )

    def acquire_lock(self, *, force: bool = False) -> None:
        if self._lock_depth > 0:
            self._lock_depth += 1
            return

        lock_path = self.run_dir / "run.lock"
        if lock_path.is_file():
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            if (
                payload.get("pid") == os.getpid()
                and payload.get("hostname") == socket.gethostname()
            ):
                # Another RunLedger instance in this same process already holds the
                # lock (chained phases reload the ledger from disk). Borrow it:
                # reentry within one process is safe, and the owning instance stays
                # responsible for unlinking the lock file.
                self._lock_depth = 1
                self._lock_borrowed = True
                return
            if not force:
                self._raise_foreign_lock(payload)
            lock_path.unlink()
            self.record_event("lock_stolen")

        payload = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": _utc_iso(),
        }
        encoded = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
            self._raise_foreign_lock(existing)
        else:
            try:
                os.write(fd, encoded)
            finally:
                os.close(fd)
        self._lock_depth = 1

    def release_lock(self) -> None:
        if self._lock_depth <= 0:
            return
        self._lock_depth -= 1
        if self._lock_depth > 0:
            return
        if self._lock_borrowed:
            # The lock file belongs to the instance that created it; leave it.
            self._lock_borrowed = False
            return
        lock_path = self.run_dir / "run.lock"
        lock_path.unlink(missing_ok=True)

    @contextmanager
    def locked(self, *, force: bool = False) -> Iterator[None]:
        self.acquire_lock(force=force)
        try:
            yield
        finally:
            self.release_lock()
