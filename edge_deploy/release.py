"""Low-level Deploy orchestration and consolidated reporting.

The public CLI supplies the Tool inferred from the current checkout and completes all
hard preflight and authentication gates before calling this engine.

Pair statuses (ADR-0003): ``rolled_out | failed | refused | skipped``. ``run_rollout``
never returns ``skipped`` (Risk #9), so the orchestrator synthesizes it for
publish-failed tools, snapshot-unavailable resumes (Risk #1), and pairs left untouched by
``--fail-fast``.
"""

from __future__ import annotations

import getpass
import inspect
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from edge_deploy.auth import authenticate_node, authenticate_node_via_pane, ensure_kerberos
from edge_deploy.config import OperatorConfig, load_tool_profile
from edge_deploy.dependencies import BundleError, DependencyBundle, build_dependency_bundle
from edge_deploy.progress import ReleaseProgressTracker
from edge_deploy.publish import PublishError, PublishResult, publish_snapshot
from edge_deploy.reporting import (
    OperationReport,
    ReleaseReport,
    ReportCheck,
    report_node_name,
    write_release_report,
    write_report,
)
from edge_deploy.rollout import run_rollout
from edge_deploy.tmux_driver import AuthenticationError, SessionGoneError, TmuxDriver
from edge_deploy.verify import verify_after_rollout


@dataclass(frozen=True)
class ReleaseSelection:
    """A resolved Release request: which tools, which nodes, and how."""

    tools: list[str]  # public CLI supplies exactly the current checkout's tool
    nodes: list[str]  # resolved node names (default: all configured)
    snapshot: str | None = None  # --snapshot <sha> -> skip Publish, roll out an existing Snapshot
    snapshot_by_tool: dict[str, str] | None = None  # per-tool resume map, e.g. {"autobench": "..."}
    smoke: str = "standard"  # "standard" | "deep"
    fail_fast: bool = False
    run_local_check: bool = True


# ---------------------------------------------------------------------------
# Node selection resolution (shared with the CLI)
# ---------------------------------------------------------------------------


def normalize_node_name(raw: str) -> str:
    """Accept ``03`` / ``3`` / ``node03`` and normalize to the operator-config key ``node03``."""
    raw = raw.strip()
    if not raw:
        return raw
    if raw.startswith("node"):
        return raw
    if raw.isdigit():
        return f"node{int(raw):02d}"
    return raw


def resolve_nodes(operator: OperatorConfig, nodes_arg: str | None) -> list[str]:
    """Resolve ``--nodes 03,04`` (or ``None`` -> all configured nodes) to validated names."""
    if not nodes_arg:
        return sorted(operator.nodes)
    names = [normalize_node_name(part) for part in nodes_arg.split(",") if part.strip()]
    for name in names:
        operator.node(name)  # validate; raises KeyError with the configured set
    return names


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Snapshot-availability guard for --snapshot resumes (Risk #1)
# ---------------------------------------------------------------------------


def ensure_snapshot_available(repo_root: str | Path, sha: str, *, remote: str = "bitbucket") -> bool:
    """Return True if ``sha`` is present locally (so Drift can ``git show`` its tree).

    ``check_drift`` reads the Snapshot's blobs from the operator's working copy, so a resume
    against a remote-only SHA would fail. This makes a best-effort ``git fetch`` and reports
    whether the object is now available; ``run_release`` turns a ``False`` into a clear
    handoff rather than a cryptic git error. Monkeypatched in tests.
    """
    root = str(repo_root)

    def have() -> bool:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    if have():
        return True
    subprocess.run(["git", "fetch", remote], cwd=root, capture_output=True, text=True)
    return have()


# ---------------------------------------------------------------------------
# Compact-summary + state-derivation helpers
# ---------------------------------------------------------------------------


def _drift_result(checks: list[ReportCheck]) -> str:
    for check in checks:
        if check.name == "runtime_drift":
            return "passed" if check.passed else "failed"
    return "not_run"


def _smoke_result(checks: list[ReportCheck]) -> str:
    smoke = [check for check in checks if check.name.startswith("smoke:")]
    if not smoke:
        return "not_run"
    return "passed" if all(check.passed for check in smoke) else "failed"


def _resolve_auth_mode(auth_mode: str) -> str:
    """Resolve ``auto`` to prompt (interactive stdin) or pane (non-interactive)."""
    if auth_mode != "auto":
        return auth_mode
    if sys.stdin.isatty():
        return "prompt"
    return "pane"


def _is_transient_preflight_failure(report: OperationReport) -> bool:
    for check in report.checks:
        if check.name != "remote_git_preflight" or check.evidence is None:
            continue
        return bool(check.evidence.get("transient"))
    return False


def _preflight_state_left(report: OperationReport) -> str:
    for check in report.checks:
        if check.name == "remote_git_preflight":
            evidence = check.evidence or {}
            action = evidence.get("suggested_action", check.message)
            step = evidence.get("step", "preflight")
            return f"remote git preflight ({step}): {action}"
    return _engine_state_left(report)


def _engine_state_left(report: OperationReport) -> str:
    if report.status in {"failed", "refused"}:
        failed = [check.message for check in report.checks if not check.passed]
        return "; ".join(failed) or "rollout failed"
    return ""


def _verify_state_left(report: OperationReport) -> str:
    failed = [check.name for check in report.checks if not check.passed]
    return "rolled out but verification failed: " + ", ".join(failed)


def _compact_rollout(
    *,
    tool: str,
    node: str,
    status: str,
    state_left: str = "",
    deployment_commit: str | None = None,
    previous_remote_commit: str | None = None,
    sensitive_changed: list[str] | None = None,
    drift: str = "not_run",
    smoke: str = "not_run",
    report_path: Any = None,
    dependency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "tool": tool,
        "node": node,
        "status": status,
        "state_left": state_left,
        "deployment_commit": deployment_commit,
        "previous_remote_commit": previous_remote_commit,
        "sensitive_changed": list(sensitive_changed or []),
        "drift": drift,
        "smoke": smoke,
        "report_path": str(report_path) if report_path else None,
        "dependency": dependency,
    }


def _synthetic_report(
    status: str, node: object, repo_path: str, *, deployment_commit: str, check: ReportCheck
) -> OperationReport:
    return OperationReport(
        operation="rollout",
        status=status,
        node=report_node_name(node),
        host=getattr(node, "host", ""),
        repo_path=repo_path,
        deployment_commit=deployment_commit,
        checks=[check],
    )


def _write_publish_report(report_dir: Path, tool: str, result: PublishResult, repo_root: str) -> Path:
    gate_checks = [
        ReportCheck(f"gate:{name}", passed, "publish gate") for name, passed in result.gate.items()
    ]
    report = OperationReport(
        operation="publish",
        status="published",
        node="local",
        host="",
        repo_path=str(repo_root),
        deployment_commit=result.snapshot,
        previous_remote_commit=result.previous_remote_commit,
        checks=gate_checks,
        extra={
            "tool": result.tool,
            "message": result.message,
            "source_commit": result.source_commit,
            "source_short": result.source_short,
            "branch": result.branch,
        },
    )
    return write_report(report_dir / f"publish-{tool}.json", report)


def _load_publishes_from_disk(report_dir: Path, tools: list[str]) -> list[dict[str, Any]]:
    """Load prior publish summaries from ``publish-<tool>.json`` for resume continuity."""
    publishes: list[dict[str, Any]] = []
    for tool in tools:
        path = report_dir / f"publish-{tool}.json"
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        snapshot = payload.get("deployment_commit") or payload.get("snapshot")
        publishes.append(
            {
                "tool": payload.get("tool", tool),
                "status": payload.get("status", "failed"),
                "snapshot": snapshot,
                "source_commit": payload.get("source_commit")
                or payload.get("reviewed_commit")
                or payload.get("extra", {}).get("source_commit"),
                "source_short": payload.get("source_short") or payload.get("extra", {}).get("source_short"),
                "branch": payload.get("branch") or payload.get("extra", {}).get("branch"),
                "previous_remote_commit": payload.get("previous_remote_commit"),
                "message": payload.get("message") or payload.get("extra", {}).get("message", ""),
                "report_path": str(path),
            }
        )
    return publishes


def _log_local_check_output(tracker: ReleaseProgressTracker, tool: str, output_tail: str) -> None:
    if not output_tail:
        return
    tracker.log("publish", f"local_check {tool} output tail:\n{output_tail}")


def _log_preflight_evidence(
    tracker: ReleaseProgressTracker,
    *,
    tool: str,
    node: str,
    report: OperationReport,
    retry: bool = False,
) -> None:
    for check in report.checks:
        if check.name != "remote_git_preflight" or check.evidence is None:
            continue
        evidence = check.evidence
        prefix = "remote preflight retry decision" if retry else "remote preflight"
        tracker.log(
            "rollout",
            (
                f"{prefix} {tool}/{node} step={evidence.get('step')} "
                f"exit={evidence.get('exit_code')} attempts={evidence.get('attempts')} "
                f"transient={evidence.get('transient')}: {evidence.get('suggested_action')}\n"
                f"output tail:\n{evidence.get('output_tail', '')}"
            ),
        )
        return


def _log_successful_preflight_repair(
    tracker: ReleaseProgressTracker,
    *,
    tool: str,
    node: str,
    report: OperationReport,
) -> None:
    for check in report.checks:
        if check.name != "remote_git_preflight" or not check.passed or check.evidence is None:
            continue
        evidence = check.evidence
        if not evidence.get("repair_attempted"):
            return
        tracker.log(
            "rollout",
            (
                f"repaired remote tracking ref for {tool}/{node}; "
                f"succeeded={evidence.get('repair_succeeded')} "
                f"fetch_attempts={evidence.get('fetch_attempts')}"
            ),
        )
        return


def _safe_stop(driver: TmuxDriver) -> None:
    try:
        driver.stop_session()
    except Exception:  # noqa: BLE001 - teardown must never mask the real outcome
        pass


def _driver_factory_with_pane_log(
    driver_factory: Callable[..., TmuxDriver],
    pane_log_dir: Path | None,
) -> Callable[..., TmuxDriver]:
    if pane_log_dir is None:
        return driver_factory
    accepts_pane = "pane_log_path" in inspect.signature(driver_factory).parameters

    def factory(node: object, profile: object, **kwargs: Any) -> TmuxDriver:
        extra: dict[str, Any] = {}
        if accepts_pane:
            node_name = getattr(node, "name", "node")
            extra["pane_log_path"] = pane_log_dir / f"pane-{node_name}.log"
        return driver_factory(node, profile, **kwargs, **extra)

    return factory


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


def run_release(
    operator: OperatorConfig,
    selection: ReleaseSelection,
    *,
    report_dir: str | Path,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    publish_fn: Callable[..., PublishResult] = publish_snapshot,
    driver_factory: Callable[..., TmuxDriver] = TmuxDriver.from_node_and_profile,
    clock: Callable[[], datetime] = _utc_now,
    remote: str = "bitbucket",
    max_auth_attempts: int = 3,
    auth_mode: str = "auto",
    auth_wait_seconds: float = 300.0,
    heartbeat_interval_s: float = 30.0,
    stall_threshold_s: float = 300.0,
    progress_fn: Callable[[str], None] | None = None,
    progress_tracker: ReleaseProgressTracker | None = None,
    dependency_builder: Callable[..., DependencyBundle] = build_dependency_bundle,
    pane_log_dir: str | Path | None = None,
) -> ReleaseReport:
    """Run one Release and return the consolidated :class:`ReleaseReport`.

    Side effects: writes a detailed per-pair :class:`OperationReport` JSON (and a
    ``publish-<tool>.json`` per Publish) under ``report_dir``; the caller writes the
    consolidated report and uses :meth:`ReleaseReport.exit_code` as the process exit code.
    """
    report_dir = Path(report_dir)
    effective_driver_factory = _driver_factory_with_pane_log(
        driver_factory,
        Path(pane_log_dir) if pane_log_dir is not None else None,
    )
    tools = selection.tools
    node_names = selection.nodes
    profiles = {tool: load_tool_profile(operator.tool_path(tool)) for tool in tools}
    local_roots = {tool: operator.tool_path(tool) for tool in tools}
    effective_auth_mode = _resolve_auth_mode(auth_mode)

    tracker = progress_tracker or ReleaseProgressTracker(
        report_dir,
        heartbeat_interval_s=heartbeat_interval_s,
        stall_threshold_s=stall_threshold_s,
        notify_fn=progress_fn,
    )

    publishes: list[dict[str, Any]] = []
    source_commits: dict[str, str] = {}
    dependency_bundles: dict[str, DependencyBundle] = {}
    snapshots: dict[str, str] = {}
    pairs = [(node_name, tool) for node_name in node_names for tool in tools]
    recorded: dict[tuple[str, str], dict[str, Any]] = {}
    stop = False  # set by --fail-fast on the first non-success

    def progress(message: str) -> None:
        tracker.emit(message)

    # ---- PUBLISH (skipped entirely when --snapshot reuses an existing Snapshot) ----
    if selection.snapshot_by_tool:
        publishes = _load_publishes_from_disk(report_dir, tools)
        source_commits.update(
            {
                str(item["tool"]): str(item["source_commit"])
                for item in publishes
                if item.get("source_commit")
            }
        )
        for tool in tools:
            snapshot = selection.snapshot_by_tool.get(tool)
            if not snapshot:
                for node_name in node_names:
                    recorded[(node_name, tool)] = _compact_rollout(
                        tool=tool,
                        node=node_name,
                        status="skipped",
                        state_left="no snapshot supplied for this tool",
                    )
                continue
            progress(f"checking local availability of {tool} snapshot {snapshot}")
            if ensure_snapshot_available(local_roots[tool], snapshot, remote=remote):
                snapshots[tool] = snapshot
            else:
                state_left = (
                    f"snapshot {snapshot} not available locally for drift; "
                    f"run: git fetch {remote} (in {local_roots[tool]}) then re-run"
                )
                for node_name in node_names:
                    node = operator.node(node_name)
                    synthetic = _synthetic_report(
                        "failed",
                        node,
                        profiles[tool].repo_path,
                        deployment_commit=snapshot,
                        check=ReportCheck("snapshot_unavailable", False, state_left),
                    )
                    path = write_report(report_dir / f"rollout-{tool}-{node_name}.json", synthetic)
                    recorded[(node_name, tool)] = _compact_rollout(
                        tool=tool,
                        node=node_name,
                        status="failed",
                        state_left=state_left,
                        deployment_commit=snapshot,
                        report_path=path,
                    )
    elif selection.snapshot:
        for tool in tools:
            progress(f"checking local availability of {tool} snapshot {selection.snapshot}")
            if ensure_snapshot_available(local_roots[tool], selection.snapshot, remote=remote):
                snapshots[tool] = selection.snapshot
            else:
                # Risk #1: the Snapshot tree is not local, so Drift cannot run. Surface a
                # clear snapshot handoff (status failed -> non-zero exit) rather than a
                # cryptic git error or a silently unverified rollout.
                state_left = (
                    f"snapshot {selection.snapshot} not available locally for drift; "
                    f"run: git fetch {remote} (in {local_roots[tool]}) then re-run"
                )
                for node_name in node_names:
                    node = operator.node(node_name)
                    synthetic = _synthetic_report(
                        "failed",
                        node,
                        profiles[tool].repo_path,
                        deployment_commit=selection.snapshot,
                        check=ReportCheck("snapshot_unavailable", False, state_left),
                    )
                    path = write_report(report_dir / f"rollout-{tool}-{node_name}.json", synthetic)
                    recorded[(node_name, tool)] = _compact_rollout(
                        tool=tool,
                        node=node_name,
                        status="failed",
                        state_left=state_left,
                        deployment_commit=selection.snapshot,
                        report_path=path,
                    )
    else:
        for tool in tools:
            if stop:
                break
            try:
                progress(f"publishing {tool}")
                with tracker.tracked(f"publish {tool}", phase="publish", tool=tool):
                    result = publish_fn(
                        profiles[tool],
                        repo_root=local_roots[tool],
                        remote=remote,
                        clock=clock,
                        run_local_check=selection.run_local_check,
                    )
            except PublishError as exc:
                progress(f"publish failed for {tool}: {exc}")
                publishes.append({"tool": tool, "status": "failed", "snapshot": None, "error": str(exc)})
                for node_name in node_names:
                    recorded[(node_name, tool)] = _compact_rollout(
                        tool=tool,
                        node=node_name,
                        status="skipped",
                        state_left="publish failed; rollout not attempted",
                    )
                if selection.fail_fast:
                    stop = True
                continue
            snapshots[tool] = result.snapshot
            source_commits[tool] = result.source_commit
            publish_path = _write_publish_report(report_dir, tool, result, local_roots[tool])
            publishes.append({**result.to_payload(), "report_path": str(publish_path)})
            _log_local_check_output(tracker, tool, result.local_check_output_tail)
            progress(f"published {tool}: {result.snapshot}")

    # ---- ROLLOUT fan-out: nodes outer, tools inner (one reused pane per node) ----
    if snapshots and not stop:
        for node_name in node_names:
            if stop:
                break
            node = operator.node(node_name)
            driver = effective_driver_factory(node, profiles[tools[0]])  # chrome/tui_exit from the first tool (Risk #7)

            try:
                if effective_auth_mode == "pane":
                    if auth_mode == "auto" and not sys.stdin.isatty():
                        progress(
                            "non-interactive terminal detected; using tmux pane auth — "
                            "attach to the node session and enter the current PASSCODE"
                        )
                    with tracker.tracked(
                        f"auth {node_name}",
                        phase="auth",
                        node=node_name,
                        tmux_session=getattr(driver, "session", None),
                    ):
                        authenticate_node_via_pane(
                            driver,
                            node_name,
                            notify_fn=progress,
                            wait_timeout=auth_wait_seconds,
                        )
                else:
                    progress(f"waiting for {node_name} RSA prompt")
                    with tracker.tracked(
                        f"auth {node_name}",
                        phase="auth",
                        node=node_name,
                        tmux_session=getattr(driver, "session", None),
                    ):
                        authenticate_node(
                            driver,
                            node_name,
                            getpass_fn=getpass_fn,
                            max_attempts=max_auth_attempts,
                            wait_timeout=auth_wait_seconds,
                        )
            except (AuthenticationError, SessionGoneError, TimeoutError) as exc:
                # Never block other nodes (ADR-0003): record every still-open pair as failed.
                for tool in tools:
                    if (node_name, tool) in recorded:
                        continue
                    synthetic = _synthetic_report(
                        "failed",
                        node,
                        profiles[tool].repo_path,
                        deployment_commit=snapshots.get(tool, "not_applicable"),
                        check=ReportCheck("auth", False, f"authentication failed: {exc}"),
                    )
                    path = write_report(report_dir / f"rollout-{tool}-{node_name}.json", synthetic)
                    recorded[(node_name, tool)] = _compact_rollout(
                        tool=tool,
                        node=node_name,
                        status="failed",
                        state_left=f"auth: {exc}",
                        deployment_commit=snapshots.get(tool),
                        report_path=path,
                    )
                _safe_stop(driver)
                if selection.fail_fast:
                    stop = True
                continue

            # Kerberos is paid once per node, and only when a selected tool has deep smoke.
            kerb_check: ReportCheck | None = None
            if selection.smoke == "deep" and any(profiles[tool].smoke.deep for tool in tools):
                kerb_check = ensure_kerberos(driver, node_name, getpass_fn=getpass_fn)

            for tool in tools:
                if (node_name, tool) in recorded or tool not in snapshots:
                    continue
                snapshot = snapshots[tool]
                profile = profiles[tool]

                def bundle_for_tool(
                    current_tool: str = tool,
                    current_profile: Any = profile,
                ) -> DependencyBundle:
                    if current_tool in dependency_bundles:
                        return dependency_bundles[current_tool]
                    source_commit = source_commits.get(current_tool)
                    if not source_commit:
                        raise BundleError(
                            "Dependency delivery requires reviewed-source provenance; "
                            "resume from the original release report"
                        )
                    dependency_bundles[current_tool] = dependency_builder(
                        current_profile,
                        repo_root=Path(local_roots[current_tool]),
                        source_sha=source_commit,
                        output_root=report_dir / "bundles",
                    )
                    return dependency_bundles[current_tool]

                try:
                    progress(f"rolling out {tool}/{node_name} -> {snapshot}")
                    with tracker.tracked(
                        f"rollout {tool}/{node_name}",
                        phase="rollout",
                        tool=tool,
                        node=node_name,
                        tmux_session=getattr(driver, "session", None),
                    ):
                        report = run_rollout(
                            driver,
                            profile,
                            node,
                            target_commit=snapshot,
                            operator_email=operator.operator_email,
                            remote=remote,
                            dependency_bundle_factory=bundle_for_tool,
                        )
                    _log_successful_preflight_repair(
                        tracker, tool=tool, node=node_name, report=report
                    )
                    if report.status == "failed" and _is_transient_preflight_failure(report):
                        _log_preflight_evidence(
                            tracker, tool=tool, node=node_name, report=report, retry=True
                        )
                        tracker.retry(
                            f"transient remote git preflight for {tool}/{node_name}; retrying rollout once"
                        )
                        with tracker.tracked(
                            f"rollout retry {tool}/{node_name}",
                            phase="rollout",
                            tool=tool,
                            node=node_name,
                            tmux_session=getattr(driver, "session", None),
                        ):
                            report = run_rollout(
                                driver,
                                profile,
                                node,
                                target_commit=snapshot,
                                operator_email=operator.operator_email,
                                remote=remote,
                                dependency_bundle_factory=bundle_for_tool,
                            )
                        _log_successful_preflight_repair(
                            tracker, tool=tool, node=node_name, report=report
                        )
                except (RuntimeError, SessionGoneError, AuthenticationError) as exc:
                    report = _synthetic_report(
                        "failed",
                        node,
                        profile.repo_path,
                        deployment_commit=snapshot,
                        check=ReportCheck("rollout_error", False, f"rollout raised: {exc}"),
                    )

                state_left = ""
                if report.status == "rolled_out":
                    with tracker.tracked(
                        f"verify {tool}/{node_name}",
                        phase="verify",
                        tool=tool,
                        node=node_name,
                        tmux_session=getattr(driver, "session", None),
                    ):
                        report.checks.extend(
                            verify_after_rollout(
                                driver,
                                profile,
                                node,
                                commit=snapshot,
                                local_root=local_roots[tool],
                                smoke_level=selection.smoke,
                            )
                        )
                    if selection.smoke == "deep" and profile.smoke.deep and kerb_check is not None:
                        report.checks.append(kerb_check)
                    if not all(check.passed for check in report.checks):
                        report.status = "failed"
                        state_left = _verify_state_left(report)
                else:
                    state_left = _preflight_state_left(report) if any(
                        check.name == "remote_git_preflight" and not check.passed
                        for check in report.checks
                    ) else _engine_state_left(report)
                    _log_preflight_evidence(tracker, tool=tool, node=node_name, report=report)

                path = write_report(report_dir / f"rollout-{tool}-{node_name}.json", report)
                progress(f"{tool}/{node_name}: {report.status}")
                recorded[(node_name, tool)] = _compact_rollout(
                    tool=tool,
                    node=node_name,
                    status=report.status,
                    state_left=state_left,
                    deployment_commit=report.deployment_commit,
                    previous_remote_commit=report.previous_remote_commit,
                    sensitive_changed=list(report.sensitive_changed),
                    drift=_drift_result(report.checks),
                    smoke=_smoke_result(report.checks),
                    report_path=path,
                    dependency=report.extra.get("dependency"),
                )
                if report.status != "rolled_out" and selection.fail_fast:
                    stop = True
                    break

            _safe_stop(driver)

    # ---- assemble rollouts in pair order; any unreached pair was halted by --fail-fast ----
    rollouts: list[dict[str, Any]] = []
    for pair in pairs:
        if pair in recorded:
            rollouts.append(recorded[pair])
            continue
        node_name, tool = pair
        rollouts.append(
            _compact_rollout(
                tool=tool,
                node=node_name,
                status="skipped",
                state_left="run halted by --fail-fast before this pair",
                deployment_commit=snapshots.get(tool),
            )
        )

    consolidated = ReleaseReport(
        selection={
            "tools": tools,
            "nodes": node_names,
            "smoke": selection.smoke,
            "fail_fast": selection.fail_fast,
            "snapshot_override": selection.snapshot,
            "snapshot_by_tool": dict(selection.snapshot_by_tool or {}),
        },
        publishes=publishes,
        rollouts=rollouts,
        operator_email=operator.operator_email,
    )
    release_json = write_release_report(report_dir / "release.json", consolidated)
    tracker.final_reports(release_json)
    return consolidated
