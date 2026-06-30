"""Shared reporting helpers for edge-deploy-core operations.

Reuses robocop's per-operation ``OperationReport`` / ``ReportCheck`` schema, adds a
``sensitive_changed`` field (ADR-0003 / Round 12) and mandatory secret redaction of
``passcode=`` / ``password=`` / ``token=`` values in all written output (ADR-0002).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Secrets are forwarded transiently (RSA passcode, Kerberos password, BB token); they
# must never reach a report or log. Match ``key=value`` up to the next whitespace/quote.
_SECRET_RE = re.compile(r"(?i)\b(passcode|password|token)=([^\s'\"]+)")
_BEARER_RE = re.compile(r"(?i)Authorization:\s*Bearer\s+\S+")
_REDACTED = "***REDACTED***"


def redact(text: str) -> str:
    """Mask secret assignments and Bearer auth headers in a string."""
    masked = _SECRET_RE.sub(rf"\1={_REDACTED}", text)
    return _BEARER_RE.sub(f"Authorization: Bearer {_REDACTED}", masked)


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {key: _redact_obj(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact_obj(value) for value in obj]
    return obj


def utc_iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def node_name_from_host(host: str) -> str:
    hostname = host.rsplit("@", 1)[-1]
    if hostname.endswith("0004.mastercard.int"):
        return "node04"
    if hostname.endswith("0003.mastercard.int"):
        return "node03"
    return hostname.split(".", 1)[0]


def report_node_name(node: Any) -> str:
    """Prefer a NodeConfig's own ``name``; fall back to deriving it from ``host``."""
    name = getattr(node, "name", "") or ""
    return name or node_name_from_host(getattr(node, "host", ""))


@dataclass(frozen=True)
class ReportCheck:
    name: str
    passed: bool
    message: str
    evidence: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "passed": self.passed,
            "message": self.message,
        }
        if self.evidence:
            payload["evidence"] = self.evidence
        return payload


@dataclass
class OperationReport:
    operation: str
    status: str
    node: str
    host: str
    repo_path: str
    deployment_commit: str
    timestamp: str = field(default_factory=utc_iso_timestamp)
    reviewed_commit: str | None = None
    previous_remote_commit: str | None = None
    install_decision: str = "not_applicable"
    checks: list[ReportCheck] = field(default_factory=list)
    sensitive_changed: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": self.timestamp,
            "node": self.node,
            "host": self.host,
            "repo_path": self.repo_path,
            "operation": self.operation,
            "deployment_commit": self.deployment_commit,
            "install_decision": self.install_decision,
            "status": self.status,
            "sensitive_changed": list(self.sensitive_changed),
            "checks": [check.to_payload() for check in self.checks],
        }
        if self.reviewed_commit is not None:
            payload["reviewed_commit"] = self.reviewed_commit
        if self.previous_remote_commit is not None:
            payload["previous_remote_commit"] = self.previous_remote_commit
        payload.update(self.extra)
        return payload


def write_report(path: str | Path, report: OperationReport) -> Path:
    """Write a report as redacted JSON (secrets masked everywhere — ADR-0002)."""
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _redact_obj(report.to_payload())
    report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# Consolidated release report (edge-deploy/release/1)
# ---------------------------------------------------------------------------

# The schema identifier embedded in every consolidated release report.
RELEASE_SCHEMA = "edge-deploy/release/1"

# Rollout pair statuses that count as actionable failures for the exit code (ADR-0003).
_FAILED_ROLLOUT_STATUSES = ("failed", "refused")


def _node_suffix(node: Any) -> str:
    """``"node04"`` -> ``"04"`` for a copy-pasteable ``--nodes`` argument in handoffs."""
    name = str(node or "")
    return name[len("node"):] if name.startswith("node") else name


def _resume_action(rollout: dict[str, Any]) -> str:
    """A ready-to-paste resume command for a mid-state/refused/unavailable pair."""
    tool = rollout.get("tool", "")
    nodes = _node_suffix(rollout.get("node"))
    command = f"py -m edge_deploy release --tool {tool} --nodes {nodes}"
    snapshot = rollout.get("deployment_commit")
    if snapshot:
        command += f" --snapshot {snapshot}"
    return f"investigate {rollout.get('node')}; re-run: {command}"


@dataclass
class ReleaseReport:
    """The consolidated ``edge-deploy/release/1`` report for one Release run.

    Embeds compact per-Publish and per-Rollout summaries (the detailed per-(tool×node)
    :class:`OperationReport` files are referenced via each rollout's ``report_path``).
    ``summary`` carries counts + ``handoffs[]`` + ``overall``; ``exit_code`` is non-zero
    when any Rollout failed/refused or any Publish failed (ADR-0003).
    """

    selection: dict[str, Any]
    publishes: list[dict[str, Any]] = field(default_factory=list)
    rollouts: list[dict[str, Any]] = field(default_factory=list)
    operator_email: str = ""
    timestamp: str = field(default_factory=utc_iso_timestamp)

    def exit_code(self) -> int:
        if any(rollout.get("status") in _FAILED_ROLLOUT_STATUSES for rollout in self.rollouts):
            return 1
        if any(publish.get("status") != "published" for publish in self.publishes):
            return 1
        return 0

    def _counts(self) -> dict[str, int]:
        counts = {"rolled_out": 0, "failed": 0, "skipped": 0, "refused": 0, "published": 0, "publish_failed": 0}
        for rollout in self.rollouts:
            status = rollout.get("status", "")
            if status in counts:
                counts[status] += 1
        for publish in self.publishes:
            counts["published" if publish.get("status") == "published" else "publish_failed"] += 1
        return counts

    def _handoffs(self) -> list[dict[str, Any]]:
        handoffs: list[dict[str, Any]] = []
        for publish in self.publishes:
            if publish.get("status") != "published":
                tool = publish.get("tool", "")
                handoffs.append({
                    "kind": "publish",
                    "tool": tool,
                    "node": None,
                    "message": publish.get("error", "publish failed"),
                    "action": f"fix the publish gate, then re-run: py -m edge_deploy publish --tool {tool}",
                })
        for rollout in self.rollouts:
            status = rollout.get("status")
            state_left = rollout.get("state_left", "")
            if status == "failed":
                # A failed --snapshot resume (tree not local) is a snapshot handoff (Risk #1);
                # any other failure is a node left mid-state.
                kind = "snapshot" if state_left.startswith("snapshot ") else "mid_state"
                handoffs.append({
                    "kind": kind,
                    "tool": rollout.get("tool", ""),
                    "node": rollout.get("node"),
                    "message": state_left,
                    "action": _resume_action(rollout),
                })
            elif status == "refused":
                handoffs.append({
                    "kind": "refused",
                    "tool": rollout.get("tool", ""),
                    "node": rollout.get("node"),
                    "message": rollout.get("state_left", ""),
                    "action": "run the offline bundle refresh first, then re-run the Release",
                })
            elif status == "skipped" and "snapshot" in state_left:
                handoffs.append({
                    "kind": "snapshot",
                    "tool": rollout.get("tool", ""),
                    "node": rollout.get("node"),
                    "message": state_left,
                    "action": _resume_action(rollout),
                })
        return handoffs

    def summary(self) -> dict[str, Any]:
        return {
            "counts": self._counts(),
            "handoffs": self._handoffs(),
            "overall": "failed" if self.exit_code() else "passed",
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": RELEASE_SCHEMA,
            "timestamp": self.timestamp,
            "operator_email": self.operator_email,
            "selection": self.selection,
            "publishes": self.publishes,
            "rollouts": self.rollouts,
            "summary": self.summary(),
            "exit_code": self.exit_code(),
        }


def write_release_report(path: str | Path, report: ReleaseReport) -> Path:
    """Write the consolidated release report as redacted JSON (same path as ``write_report``)."""
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _redact_obj(report.to_payload())
    report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return report_path
