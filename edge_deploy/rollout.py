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
import json
import re
from dataclasses import dataclass

from edge_deploy.config import ToolProfile
from edge_deploy.drift import _extract_payload, _remote_python
from edge_deploy.reporting import OperationReport, ReportCheck, report_node_name
from edge_deploy.tmux_driver import TmuxDriver

# The status values a single Rollout can report.
ROLLOUT_STATUSES = ("rolled_out", "failed", "skipped", "refused")


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


def build_install_command(profile: ToolProfile, *, operator_email: str = "") -> str:
    """``install.sh`` with the standardized python-bin/email env (ADR-0004)."""
    py = _install_python_expr()
    parts: list[str] = []
    if operator_email:
        parts.append(f"EDGE_DEPLOY_EMAIL={operator_email}")
    parts.append(f"EDGE_DEPLOY_PYTHON_BIN={py}")
    parts.append("./install.sh")
    return " ".join(parts)


def build_install_preflight_command(profile: ToolProfile, *, operator_email: str = "") -> str:
    """Dry-run offline wheel resolution before running install.sh."""
    py = _install_python_expr()
    parts: list[str] = []
    if operator_email:
        parts.append(f"EDGE_DEPLOY_EMAIL={operator_email}")
    parts.append(f"EDGE_DEPLOY_PYTHON_BIN={py}")
    parts.append(
        "sh -u -c '"
        "if [ -d offline_packages ] || [ -d vendor ]; then "
        "tmp=$(mktemp -d) || exit 1; "
        "\"$EDGE_DEPLOY_PYTHON_BIN\" -m venv \"$tmp/venv\" && "
        "\"$tmp/venv/bin/pip\" install --dry-run --no-index "
        "--find-links=\"$PWD/offline_packages\" --find-links=\"$PWD/vendor\" "
        "-r \"$PWD/requirements.txt\"; "
        "rc=$?; rm -rf \"$tmp\"; exit \"$rc\"; "
        "fi"
        "'"
    )
    return " ".join(parts)


def _remote_git_output(driver: TmuxDriver, repo_path: str, command: str, *, timeout: float = 60.0) -> str:
    screen, code = driver.run_remote(f"cd {repo_path} && {command}", timeout=timeout)
    if code != 0:
        raise RuntimeError(f"Remote command failed ({code}): {command}")
    return screen


def _remote_payload_lines(screen: str) -> list[str]:
    """Return command payload lines, excluding echoed prompts and run_remote sentinels."""
    lines: list[str] = []
    for raw in screen.splitlines():
        line = raw.strip()
        if not line or "__RC_" in line or "__START_" in line:
            continue
        if re.search(r"[\$#]\s*$", line):
            continue
        if re.search(r"[\$#]\s*cd\s+", line):
            continue
        if "git fetch --prune" in line or "git diff --name-only" in line or "printf " in line:
            continue
        lines.append(line)
    return lines


def _output_tail(screen: str, *, limit: int = 20) -> str:
    lines = _remote_payload_lines(screen)
    return "\n".join(lines[-limit:])


def _remote_rev_parse(driver: TmuxDriver, repo_path: str, ref: str) -> str:
    screen = _remote_git_output(driver, repo_path, f"git rev-parse --verify {ref}", timeout=30)
    for line in reversed(_remote_payload_lines(screen)):
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
) -> list[str]:
    """Changed paths between the node's current HEAD and the target Snapshot."""
    screen = _remote_git_output(
        driver,
        repo_path,
        (
            f"git fetch --prune {remote} {branch}:refs/remotes/{remote}/{branch} >/dev/null 2>&1 && "
            f"git --no-pager diff --name-only {previous} {target}"
        ),
        timeout=90,
    )
    return _remote_payload_lines(screen)


def _permission_evidence(driver: TmuxDriver, profile: ToolProfile) -> dict[str, object]:
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
print("PERMISSION_PAYLOAD_START")
print(json.dumps(payload, sort_keys=True))
print("PERMISSION_PAYLOAD_END")
"""
    screen, code = _remote_python(driver, script, timeout=120)
    if code != 0:
        raise RuntimeError(f"Permission verification failed with exit code {code}")
    payload = "".join(
        _extract_payload(screen, "PERMISSION_PAYLOAD_START", "PERMISSION_PAYLOAD_END").splitlines()
    )
    return json.loads(payload)


def run_rollout(
    driver: TmuxDriver,
    profile: ToolProfile,
    node: "object",
    *,
    target_commit: str,
    install_mode: str = "auto",
    operator_email: str = "",
    remote: str = "bitbucket",
) -> OperationReport:
    """Roll one Edge Node to ``target_commit`` and return an :class:`OperationReport`.

    The driver is expected to already hold an authenticated pane for ``node``.
    """
    repo_path = profile.repo_path
    branch = profile.release_branch or "main"
    node_name = report_node_name(node)
    host = getattr(node, "host", "")

    previous_commit = _remote_rev_parse(driver, repo_path, "HEAD")
    changed_paths = remote_changed_paths(
        driver, repo_path, previous_commit, target_commit, remote=remote, branch=branch
    )
    sensitive_changed = matching_paths(changed_paths, profile.sensitive_paths)
    refused = matching_paths(changed_paths, profile.dependency_paths)

    if refused:
        # ADR-0005: offline wheels do not travel in git, so a dependency change cannot be
        # delivered by update.sh. Refuse before running anything on the node.
        check = ReportCheck(
            name="dependency_refusal",
            passed=False,
            message=(
                "Refused: dependency files changed and cannot be delivered over the git path "
                f"({', '.join(refused)}). Run the offline bundle refresh first."
            ),
            evidence={"refused_paths": refused},
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
            checks=[check],
            sensitive_changed=sensitive_changed,
            extra={"changed_paths": changed_paths, "refused_paths": refused},
        )

    install = decide_install_action(profile, mode=install_mode, changed_paths=changed_paths)
    checks: list[ReportCheck] = []

    _update_screen, update_code = driver.run_remote(
        build_update_command(profile, target_commit, remote=remote), timeout=180
    )
    checks.append(ReportCheck("update", update_code == 0, f"update.sh exit {update_code}"))

    final_commit = _remote_rev_parse(driver, repo_path, "HEAD")
    checks.append(
        ReportCheck(
            "final_commit",
            final_commit == target_commit,
            f"Remote HEAD is {final_commit}",
            {"expected_commit": target_commit},
        )
    )

    if install.action == "run":
        preflight_screen, preflight_code = driver.run_remote(
            f"cd {repo_path} && {build_install_preflight_command(profile, operator_email=operator_email)}",
            timeout=240,
        )
        checks.append(
            ReportCheck(
                "install_preflight",
                preflight_code == 0,
                f"offline install dry-run exit {preflight_code}",
                {"exit_code": preflight_code, "output_tail": _output_tail(preflight_screen)},
            )
        )
        if preflight_code == 0:
            install_screen, install_code = driver.run_remote(
                f"cd {repo_path} && {build_install_command(profile, operator_email=operator_email)}",
                timeout=240,
            )
            checks.append(
                ReportCheck(
                    "install",
                    install_code == 0,
                    install.reason,
                    {"exit_code": install_code, "output_tail": _output_tail(install_screen)},
                )
            )
    else:
        checks.append(ReportCheck("install", True, install.reason))

    permissions = _permission_evidence(driver, profile)
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
        extra={"changed_paths": changed_paths},
    )
