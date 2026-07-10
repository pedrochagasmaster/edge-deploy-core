"""Rollout engine: bring one Edge Node to an exact Snapshot via the standardized
``EDGE_DEPLOY_*`` on-node interface, then verify.

Generalized from robocop's ``deploy.py``:

* install-trigger detection is driven by ``ToolProfile.install_trigger_paths``;
* ``update.sh`` is invoked with ``EDGE_DEPLOY_REMOTE`` / ``EDGE_DEPLOY_BRANCH`` and the
  Snapshot SHA as the positional ref; ``install.sh`` with ``EDGE_DEPLOY_EMAIL`` /
  ``EDGE_DEPLOY_PYTHON_BIN`` (ADR-0004);
* a Snapshot whose changed paths touch ``dependency_paths`` is **refused** before any
  ``update.sh`` runs (ADR-0005);
* ``sensitive_changed`` (paths intersecting ``sensitive_paths``) is flagged, never blocked
  (ADR-0003 / Round 12).

Rollout ``status`` is one of ``rolled_out | failed | skipped | refused``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Callable

from edge_deploy.config import ToolProfile
from edge_deploy.dependencies import BundleError, DependencyBundle, deliver_dependency_bundle
from edge_deploy.remote_paths import edge_deploy_path, shell_remote_path
from edge_deploy.remote_python import REMOTE_PYTHON_EXPR
from edge_deploy.reporting import OperationReport, ReportCheck, report_node_name
from edge_deploy.runner import bootstrap_runner, read_remote_json, read_remote_text, run_step
from edge_deploy.tmux_driver import TmuxDriver

# The status values a single Rollout can report.
ROLLOUT_STATUSES = ("rolled_out", "failed", "skipped", "refused")

_TRANSIENT_GIT_MARKERS = (
    "unable to access",
    "could not resolve host",
    "timed out",
    "rpc failed",
    "remote end hung up",
    "connection reset",
)
_PERMANENT_GIT_MARKERS = (
    "not a git repository",
    "bad object",
    "unknown revision",
    "invalid ref",
)


@dataclass(frozen=True)
class RemoteGitPreflightFailure:
    """Structured evidence when remote verify/fetch/diff preflight fails."""

    step: str
    command: str
    exit_code: int
    output_tail: str
    transient: bool
    attempts: int
    suggested_action: str
    repair_attempted: bool = False
    repair_succeeded: bool = False


class RemoteGitPreflightError(RuntimeError):
    """Raised when remote git preflight fails; carries structured evidence."""

    def __init__(self, failure: RemoteGitPreflightFailure) -> None:
        self.failure = failure
        super().__init__(failure.suggested_action)


@dataclass(frozen=True)
class RemoteGitPreflightResult:
    changed_paths: list[str]
    fetch_attempts: int
    repair_attempted: bool
    repair_succeeded: bool


@dataclass(frozen=True)
class InstallDecision:
    action: str
    reason: str


def _path_matches(path: str, pattern: str) -> bool:
    """Match a changed path against a profile pattern.

    Supports exact paths, directory prefixes (``scr/``), ``dir/**`` subtrees and general
    ``fnmatch`` globs (``core/**/*.py``).
    """
    norm = path.replace("\\", "/")
    pat = pattern.replace("\\", "/")
    if norm == pat:
        return True
    if pat.endswith("/") and norm.startswith(pat):
        return True
    if pat.endswith("/**"):
        prefix = pat[:-3]
        if norm == prefix or norm.startswith(prefix + "/"):
            return True
    return fnmatch.fnmatch(norm, pat)


def matching_paths(changed_paths: list[str], patterns: list[str]) -> list[str]:
    """Return the sorted, de-duplicated changed paths matching any of ``patterns``."""
    matched = {
        path
        for path in changed_paths
        for pattern in patterns
        if _path_matches(path, pattern)
    }
    return sorted(matched)


def decide_install_action(profile: ToolProfile, *, mode: str, changed_paths: list[str]) -> InstallDecision:
    if mode == "always":
        return InstallDecision("run", "Install forced by --install always")
    if mode == "never":
        return InstallDecision("skip", "Install skipped by --install never")
    triggered = matching_paths(changed_paths, profile.install_trigger_paths)
    if triggered:
        return InstallDecision("run", f"Install-trigger files changed: {', '.join(triggered[:6])}")
    return InstallDecision("skip", "No install-trigger files changed between the deployed and target commits")


def build_update_command(profile: ToolProfile, target_commit: str, *, remote: str = "bitbucket") -> str:
    """``update.sh <snapshot>`` with the standardized remote/branch env (ADR-0004)."""
    branch = profile.release_branch or "main"
    return (
        f"cd {profile.repo_path} && "
        f"EDGE_DEPLOY_REMOTE={remote} EDGE_DEPLOY_BRANCH={branch} "
        f"./update.sh {target_commit}"
    )


def _install_python_expr() -> str:
    """Shell expression resolving the preferred Edge Python for install.sh."""
    return (
        "$(dsw=$(command -v dswpython310 2>/dev/null); "
        "case \"$dsw\" in \"alias dswpython310='\"*) "
        "printf %s \"$dsw\" | sed \"s/^alias dswpython310='//;s/'$//\";; "
        "?*) printf %s \"$dsw\";; "
        "*) command -v python3.10 || "
        "if [ -x /sys_apps_01/python/python310/bin/python3.10 ]; then "
        "printf %s /sys_apps_01/python/python310/bin/python3.10; "
        "else command -v python3.11; fi;; "
        "esac)"
    )


def build_install_command(
    profile: ToolProfile, *, operator_email: str = ""
) -> str:
    """``install.sh`` with the standardized python-bin/email env (ADR-0004)."""
    py = _install_python_expr()
    parts: list[str] = []
    if operator_email:
        parts.append(f"EDGE_DEPLOY_EMAIL={operator_email}")
    parts.append(f"EDGE_DEPLOY_PYTHON_BIN={py}")
    parts.append("./install.sh")
    return " ".join(parts)


def build_install_preflight_command(
    profile: ToolProfile, *, operator_email: str = "", bundle_dir: str = ""
) -> str:
    """Dry-run offline wheel resolution before running install.sh."""
    py = _install_python_expr()
    parts: list[str] = []
    if operator_email:
        parts.append(f"EDGE_DEPLOY_EMAIL={operator_email}")
    if bundle_dir:
        parts.append(f"EDGE_DEPLOY_BUNDLE_DIR={bundle_dir}")
    parts.append(f"EDGE_DEPLOY_PYTHON_BIN={py}")
    parts.append(
        "sh -u -c '"
        "bundle=${EDGE_DEPLOY_BUNDLE_DIR:-}; "
        "if [ -n \"$bundle\" ] && [ -d \"$bundle/wheels\" ]; then "
        "tmp=$(mktemp -d) || exit 1; "
        "\"$EDGE_DEPLOY_PYTHON_BIN\" -m venv \"$tmp/venv\" && "
        "\"$tmp/venv/bin/pip\" install --dry-run --no-index "
        "--find-links=\"$bundle/wheels\" "
        "-r \"$bundle/requirements/requirements.txt\"; "
        "rc=$?; rm -rf \"$tmp\"; exit \"$rc\"; "
        "fi"
        "'"
    )
    return " ".join(parts)


def _step_data_path(run_id: str, step_name: str) -> str:
    return f"~/.edge-deploy/runs/{run_id}/steps/{step_name}-data.txt"


def _run_repo_step(
    driver: TmuxDriver,
    runner_path: str,
    run_id: str,
    step_name: str,
    repo_path: str,
    command: str,
    *,
    timeout: float,
) -> dict:
    return run_step(
        driver,
        runner_path,
        run_id,
        step_name,
        f"cd {repo_path} && {command}",
        timeout=timeout,
    )


def _is_transient_git_error(output: str) -> bool:
    lowered = output.lower()
    if any(marker in lowered for marker in _PERMANENT_GIT_MARKERS):
        return False
    return any(marker in lowered for marker in _TRANSIENT_GIT_MARKERS)


def _is_repairable_tracking_ref_error(output: str, remote_ref: str) -> bool:
    lowered = output.lower()
    expected_ref = remote_ref.lower()
    unresolved_ref = (
        f"cannot lock ref '{expected_ref}'" in lowered
        and f"unable to resolve reference '{expected_ref}'" in lowered
    )
    bad_tracking_ref = f"fatal: bad object {expected_ref}" in lowered
    return unresolved_ref or bad_tracking_ref


def _repair_tracking_ref_command(remote_ref: str) -> str:
    loose_ref = f".git/{remote_ref}"
    reflog = f".git/logs/{remote_ref}"
    return (
        f"{{ git update-ref -d {remote_ref} 2>/dev/null || true; }} && "
        f"rm -f {loose_ref} {reflog}"
    )


def _suggested_action_for_step(step: str, *, transient: bool) -> str:
    if step == "verify":
        return "confirm the tool repo path exists on the node and is a git checkout"
    if step == "fetch":
        if transient:
            return "retry the release; if fetch keeps failing, check network and remote access from the node"
        return "inspect fetch stderr on the node and confirm the remote branch/ref exists"
    if step == "diff":
        return "confirm previous and target SHAs exist on the node after fetch"
    return "inspect remote git preflight evidence and retry when resolved"


def _remote_git_preflight(
    driver: TmuxDriver,
    repo_path: str,
    previous: str,
    target: str,
    *,
    runner_path: str,
    run_id: str,
    remote: str = "bitbucket",
    branch: str = "main",
) -> RemoteGitPreflightResult:
    """Verify repo, fetch target branch, and diff SHAs as separate runner steps."""
    verify_command = "git rev-parse --is-inside-work-tree"
    verify_result = _run_repo_step(
        driver, runner_path, run_id, "git-verify", repo_path, verify_command, timeout=30
    )
    verify_code = int(verify_result.get("exit_code", 1))
    verify_tail = str(verify_result.get("stdout_tail", ""))
    if verify_code != 0 or "true" not in verify_tail:
        raise RemoteGitPreflightError(
            RemoteGitPreflightFailure(
                step="verify",
                command=verify_command,
                exit_code=verify_code,
                output_tail=verify_tail,
                transient=False,
                attempts=1,
                suggested_action=_suggested_action_for_step("verify", transient=False),
            )
        )

    remote_ref = f"refs/remotes/{remote}/{branch}"
    fetch_command = f"git fetch --prune {remote} {branch}"
    attempts = 0
    last_tail = ""
    last_code = 0
    repair_attempted = False
    repair_succeeded = False
    while attempts < 2:
        attempts += 1
        fetch_result = _run_repo_step(
            driver, runner_path, run_id, "git-fetch", repo_path, fetch_command, timeout=90
        )
        last_code = int(fetch_result.get("exit_code", 1))
        last_tail = str(fetch_result.get("stdout_tail", ""))
        if last_code == 0:
            break
        if attempts == 1 and _is_repairable_tracking_ref_error(last_tail, remote_ref):
            repair_attempted = True
            repair_command = _repair_tracking_ref_command(remote_ref)
            _repair_screen, repair_code = driver.run_remote(
                f"cd {repo_path} && {repair_command}", timeout=30
            )
            repair_succeeded = repair_code == 0
            if repair_code == 0:
                continue
        if attempts == 1 and _is_transient_git_error(last_tail):
            continue
        break
    if last_code != 0:
        transient = _is_transient_git_error(last_tail)
        raise RemoteGitPreflightError(
            RemoteGitPreflightFailure(
                step="fetch",
                command=fetch_command,
                exit_code=last_code,
                output_tail=last_tail,
                transient=transient,
                attempts=attempts,
                suggested_action=_suggested_action_for_step("fetch", transient=transient),
                repair_attempted=repair_attempted,
                repair_succeeded=repair_succeeded,
            )
        )

    diff_data_path = f"$HOME/.edge-deploy/runs/{run_id}/steps/git-diff-data.txt"
    diff_command = f"git --no-pager diff --name-only {previous} {target} > {diff_data_path} 2>&1"
    diff_result = _run_repo_step(
        driver, runner_path, run_id, "git-diff", repo_path, diff_command, timeout=60
    )
    diff_code = int(diff_result.get("exit_code", 1))
    if diff_code != 0:
        diff_tail = str(diff_result.get("stdout_tail", ""))
        transient = _is_transient_git_error(diff_tail)
        raise RemoteGitPreflightError(
            RemoteGitPreflightFailure(
                step="diff",
                command=diff_command,
                exit_code=diff_code,
                output_tail=diff_tail,
                transient=transient,
                attempts=1,
                suggested_action=_suggested_action_for_step("diff", transient=transient),
            )
        )
    diff_text = read_remote_text(driver, _step_data_path(run_id, "git-diff"))
    changed_paths = [line.strip() for line in diff_text.splitlines() if line.strip()]
    return RemoteGitPreflightResult(
        changed_paths=changed_paths,
        fetch_attempts=attempts,
        repair_attempted=repair_attempted,
        repair_succeeded=repair_succeeded,
    )


def _preflight_failure_report(
    *,
    node_name: str,
    host: str,
    repo_path: str,
    target_commit: str,
    previous_commit: str,
    failure: RemoteGitPreflightFailure,
) -> OperationReport:
    evidence = {
        "step": failure.step,
        "command": failure.command,
        "exit_code": failure.exit_code,
        "output_tail": failure.output_tail,
        "transient": failure.transient,
        "attempts": failure.attempts,
        "suggested_action": failure.suggested_action,
        "repair_attempted": failure.repair_attempted,
        "repair_succeeded": failure.repair_succeeded,
    }
    check = ReportCheck(
        name="remote_git_preflight",
        passed=False,
        message=f"Remote git preflight failed at {failure.step}: {failure.suggested_action}",
        evidence=evidence,
    )
    return OperationReport(
        operation="rollout",
        status="failed",
        node=node_name,
        host=host,
        repo_path=repo_path,
        deployment_commit=target_commit,
        previous_remote_commit=previous_commit,
        install_decision="not_applicable",
        checks=[check],
    )


def _output_tail(text: str, *, limit: int = 20) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def _remote_rev_parse(
    driver: TmuxDriver,
    repo_path: str,
    ref: str,
    *,
    runner_path: str,
    run_id: str,
) -> str:
    data_path = f"$HOME/.edge-deploy/runs/{run_id}/steps/git-rev-parse-data.txt"
    command = f"git rev-parse --verify {ref} > {data_path} 2>&1"
    result = _run_repo_step(
        driver, runner_path, run_id, "git-rev-parse", repo_path, command, timeout=30
    )
    code = int(result.get("exit_code", 1))
    if code != 0:
        raise RuntimeError(f"Remote git rev-parse failed ({code}) for {ref!r}")
    text = read_remote_text(driver, _step_data_path(run_id, "git-rev-parse"))
    for line in reversed([ln.strip() for ln in text.splitlines() if ln.strip()]):
        if re.fullmatch(r"[0-9a-f]{40}", line):
            return line
    raise RuntimeError(f"Could not parse git rev-parse output for {ref!r}")


def remote_changed_paths(
    driver: TmuxDriver,
    repo_path: str,
    previous: str,
    target: str,
    *,
    remote: str = "bitbucket",
    branch: str = "main",
    run_id: str = "edge-deploy",
) -> list[str]:
    """Changed paths between the node's current HEAD and the target Snapshot."""
    runner_path = bootstrap_runner(driver, run_id)
    return _remote_git_preflight(
        driver,
        repo_path,
        previous,
        target,
        runner_path=runner_path,
        run_id=run_id,
        remote=remote,
        branch=branch,
    ).changed_paths


def _permission_evidence(
    driver: TmuxDriver,
    profile: ToolProfile,
    *,
    runner_path: str,
    run_id: str,
) -> dict[str, object]:
    # Expanded by Python's Path.expanduser() on the node, which only understands
    # ``~`` (not ``$HOME``).
    evidence_path = f"~/.edge-deploy/runs/{run_id}/steps/permission-check-data.json"
    script = f"""
import json
import os
from pathlib import Path

root = Path({profile.repo_path!r})
patterns = {profile.runtime_paths!r}
unreadable = []
checked = 0
seen = set()
for pattern in patterns:
    for path in root.glob(pattern):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            checked += 1
            if not os.access(path, os.R_OK):
                unreadable.append(rel)
payload = {{
    "root_traversable": root.is_dir() and os.access(root, os.X_OK),
    "update_executable": os.access(root / "update.sh", os.X_OK),
    "install_executable": os.access(root / "install.sh", os.X_OK),
    "runtime_files_checked": checked,
    "unreadable_runtime_files": sorted(unreadable),
}}
out = Path({evidence_path!r}).expanduser()
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(payload, sort_keys=True))
"""
    result = run_step(
        driver,
        runner_path,
        run_id,
        "permission-check",
        f"{REMOTE_PYTHON_EXPR} - <<'PY'\n{script}\nPY",
        timeout=120,
    )
    if int(result.get("exit_code", 1)) != 0:
        raise RuntimeError(
            f"Permission verification failed with exit code {result.get('exit_code')}"
        )
    remote_json = f"~/.edge-deploy/runs/{run_id}/steps/permission-check-data.json"
    payload = read_remote_json(driver, remote_json)
    return payload


def run_rollout(
    driver: TmuxDriver,
    profile: ToolProfile,
    node: "object",
    *,
    target_commit: str,
    run_id: str,
    install_mode: str = "auto",
    operator_email: str = "",
    remote: str = "bitbucket",
    dependency_bundle: DependencyBundle | None = None,
    dependency_bundle_factory: Callable[[], DependencyBundle] | None = None,
) -> OperationReport:
    """Roll one Edge Node to ``target_commit`` and return an :class:`OperationReport`.

    The driver is expected to already hold an authenticated pane for ``node``.
    """
    repo_path = profile.repo_path
    branch = profile.release_branch or "main"
    node_name = report_node_name(node)
    host = getattr(node, "host", "")

    runner_path = bootstrap_runner(driver, run_id)

    previous_commit = _remote_rev_parse(
        driver, repo_path, "HEAD", runner_path=runner_path, run_id=run_id
    )
    try:
        preflight = _remote_git_preflight(
            driver,
            repo_path,
            previous_commit,
            target_commit,
            runner_path=runner_path,
            run_id=run_id,
            remote=remote,
            branch=branch,
        )
    except RemoteGitPreflightError as exc:
        return _preflight_failure_report(
            node_name=node_name,
            host=host,
            repo_path=repo_path,
            target_commit=target_commit,
            previous_commit=previous_commit,
            failure=exc.failure,
        )
    changed_paths = preflight.changed_paths
    preflight_check = ReportCheck(
        "remote_git_preflight",
        True,
        "Remote Git preflight passed",
        {
            "fetch_attempts": preflight.fetch_attempts,
            "repair_attempted": preflight.repair_attempted,
            "repair_succeeded": preflight.repair_succeeded,
        },
    )
    sensitive_changed = matching_paths(changed_paths, profile.sensitive_paths)
    refused = matching_paths(changed_paths, profile.dependency_paths)
    bundle_dir = ""
    dependency_evidence: dict[str, object] | None = None

    if refused:
        if dependency_bundle is None and dependency_bundle_factory is not None:
            try:
                dependency_bundle = dependency_bundle_factory()
            except BundleError as exc:
                check = ReportCheck("dependency_build", False, str(exc))
                return OperationReport(
                    operation="rollout",
                    status="failed",
                    node=node_name,
                    host=host,
                    repo_path=repo_path,
                    deployment_commit=target_commit,
                    previous_remote_commit=previous_commit,
                    install_decision="not_applicable",
                    checks=[preflight_check, check],
                    sensitive_changed=sensitive_changed,
                    extra={"changed_paths": changed_paths, "dependency_paths": refused},
                )
        if dependency_bundle is None:
            check = ReportCheck(
                name="dependency_bundle_unavailable",
                passed=False,
                message="Dependency files changed but no reviewed-source bundle was supplied",
                evidence={"dependency_paths": refused},
            )
            return OperationReport(
                operation="rollout",
                status="refused",
                node=node_name,
                host=host,
                repo_path=repo_path,
                deployment_commit=target_commit,
                previous_remote_commit=previous_commit,
                install_decision="not_applicable",
                checks=[preflight_check, check],
                sensitive_changed=sensitive_changed,
                extra={"changed_paths": changed_paths, "dependency_paths": refused},
            )
        try:
            delivered = deliver_dependency_bundle(
                driver, profile, dependency_bundle, run_id=run_id
            )
        except BundleError as exc:
            check = ReportCheck("dependency_delivery", False, str(exc))
            return OperationReport(
                operation="rollout",
                status="failed",
                node=node_name,
                host=host,
                repo_path=repo_path,
                deployment_commit=target_commit,
                previous_remote_commit=previous_commit,
                install_decision="not_applicable",
                checks=[preflight_check, check],
                sensitive_changed=sensitive_changed,
                extra={"changed_paths": changed_paths, "dependency_paths": refused},
            )
        bundle_dir = delivered.remote_dir
        dependency_evidence = {
            **delivered.evidence,
            "source_sha": dependency_bundle.source_sha,
            "archive_sha256": dependency_bundle.archive_sha256,
            "manifest": dependency_bundle.manifest,
        }

    install = decide_install_action(profile, mode=install_mode, changed_paths=changed_paths)
    if not bundle_dir and install.action == "run" and profile.dependency_bundle is not None:
        current_bundle = edge_deploy_path("bundles", profile.tool, "current")
        _screen, current_code = driver.run_remote(
            f"test -f {shell_remote_path(current_bundle)}/manifest.json",
            timeout=30,
        )
        if current_code == 0:
            bundle_dir = current_bundle
        elif dependency_bundle_factory is None:
            return OperationReport(
                operation="rollout",
                status="refused",
                node=node_name,
                host=host,
                repo_path=repo_path,
                deployment_commit=target_commit,
                previous_remote_commit=previous_commit,
                install_decision="not_applicable",
                checks=[
                    preflight_check,
                    ReportCheck(
                        "dependency_bootstrap",
                        False,
                        "Installation requires a verified bundle, but the node has no active bundle",
                    ),
                ],
                sensitive_changed=sensitive_changed,
                extra={"changed_paths": changed_paths},
            )
        else:
            try:
                dependency_bundle = dependency_bundle or dependency_bundle_factory()
                delivered = deliver_dependency_bundle(
                    driver, profile, dependency_bundle, run_id=run_id
                )
            except BundleError as exc:
                return OperationReport(
                    operation="rollout",
                    status="failed",
                    node=node_name,
                    host=host,
                    repo_path=repo_path,
                    deployment_commit=target_commit,
                    previous_remote_commit=previous_commit,
                    install_decision="not_applicable",
                    checks=[preflight_check, ReportCheck("dependency_bootstrap", False, str(exc))],
                    sensitive_changed=sensitive_changed,
                    extra={"changed_paths": changed_paths},
                )
            bundle_dir = delivered.remote_dir
            dependency_evidence = {
                **delivered.evidence,
                "source_sha": dependency_bundle.source_sha,
                "archive_sha256": dependency_bundle.archive_sha256,
                "manifest": dependency_bundle.manifest,
                "bootstrap": True,
            }
    checks: list[ReportCheck] = [preflight_check]
    if dependency_evidence is not None:
        checks.append(
            ReportCheck(
                "dependency_delivery",
                True,
                "Verified dependency bundle staged before checkout update",
                dependency_evidence,
            )
        )

    update_result = run_step(
        driver,
        runner_path,
        run_id,
        "update",
        f"cd {repo_path} && {build_update_command(profile, target_commit, remote=remote)}",
        timeout=180,
    )
    update_code = int(update_result.get("exit_code", 1))
    checks.append(ReportCheck("update", update_code == 0, f"update.sh exit {update_code}"))

    final_commit = _remote_rev_parse(
        driver, repo_path, "HEAD", runner_path=runner_path, run_id=run_id
    )
    checks.append(
        ReportCheck(
            "final_commit",
            final_commit == target_commit,
            f"Remote HEAD is {final_commit}",
            {"expected_commit": target_commit},
        )
    )

    if install.action == "run":
        preflight_result = run_step(
            driver,
            runner_path,
            run_id,
            "install-preflight",
            f"cd {repo_path} && "
            f"{build_install_preflight_command(profile, operator_email=operator_email, bundle_dir=bundle_dir)}",
            timeout=240,
        )
        preflight_code = int(preflight_result.get("exit_code", 1))
        preflight_tail = str(preflight_result.get("stdout_tail", ""))
        checks.append(
            ReportCheck(
                "install_preflight",
                preflight_code == 0,
                f"offline install dry-run exit {preflight_code}",
                {"exit_code": preflight_code, "output_tail": preflight_tail},
            )
        )
        if preflight_code == 0:
            install_result = run_step(
                driver,
                runner_path,
                run_id,
                "install",
                f"cd {repo_path} && "
                f"{build_install_command(profile, operator_email=operator_email)}",
                timeout=240,
                bundle_dir=bundle_dir or None,
            )
            install_code = int(install_result.get("exit_code", 1))
            install_tail = str(install_result.get("stdout_tail", ""))
            checks.append(
                ReportCheck(
                    "install",
                    install_code == 0,
                    install.reason,
                    {"exit_code": install_code, "output_tail": install_tail},
                )
            )
            if install_code == 0 and bundle_dir and dependency_evidence is not None:
                bundles_dir = edge_deploy_path("bundles", profile.tool)
                current_link = edge_deploy_path("bundles", profile.tool, "current")
                _activate_screen, activate_code = driver.run_remote(
                    f"mkdir -p {shell_remote_path(bundles_dir)} && "
                    f"ln -sfn {bundle_dir} "
                    f"{shell_remote_path(current_link)}",
                    timeout=30,
                )
                checks.append(
                    ReportCheck(
                        "dependency_activate",
                        activate_code == 0,
                        f"active dependency bundle link exit {activate_code}",
                    )
                )
    else:
        checks.append(ReportCheck("install", True, install.reason))

    permissions = _permission_evidence(driver, profile, runner_path=runner_path, run_id=run_id)
    permissions_ok = (
        bool(permissions["root_traversable"])
        and bool(permissions["update_executable"])
        and bool(permissions["install_executable"])
        and not permissions["unreadable_runtime_files"]
    )
    checks.append(ReportCheck("permissions", permissions_ok, "Permission evidence collected", permissions))

    status = "rolled_out" if all(check.passed for check in checks) else "failed"
    return OperationReport(
        operation="rollout",
        status=status,
        node=node_name,
        host=host,
        repo_path=repo_path,
        deployment_commit=target_commit,
        previous_remote_commit=previous_commit,
        install_decision=install.action,
        checks=checks,
        sensitive_changed=sensitive_changed,
        extra={
            "changed_paths": changed_paths,
            **({"dependency": dependency_evidence} if dependency_evidence is not None else {}),
        },
    )
