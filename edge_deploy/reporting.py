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
_REDACTED = "***REDACTED***"


def redact(text: str) -> str:
    """Mask ``passcode=`` / ``password=`` / ``token=`` values in a string."""
    return _SECRET_RE.sub(rf"\1={_REDACTED}", text)


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
