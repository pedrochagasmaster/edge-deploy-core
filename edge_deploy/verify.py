"""Verify: the DESIGN §6 VERIFY work that runs *after* a Rollout, beyond what
``run_rollout`` already does (it already verifies the final commit and permissions).

Adds two things over the authenticated pane:

* **Drift == 0** over the Tool's ``runtime_paths`` — reuses :func:`edge_deploy.drift.check_drift`
  unchanged (its single ``runtime_drift`` check).
* **Smoke** — the profile's ``smoke.standard`` (no auth) or ``smoke.deep`` (Kerberos)
  commands, one :class:`~edge_deploy.reporting.ReportCheck` per command.

The orchestrator merges these checks into the pair's rollout :class:`OperationReport`, so
there is one detailed file (and one pointer) per (tool × node). ``autobench``'s
``smoke.deep`` is ``[]``, so ``--smoke deep`` is a no-op there and needs no Kerberos — the
kinit seam is only paid when a *selected* tool actually has deep commands.
"""

from __future__ import annotations

from edge_deploy.config import ToolProfile
from edge_deploy.drift import check_drift
from edge_deploy.reporting import ReportCheck
from edge_deploy.tmux_driver import TmuxDriver


def run_smoke(driver: TmuxDriver, profile: ToolProfile, *, level: str) -> list[ReportCheck]:
    """Run the profile's smoke commands at ``level`` over the pane; one check per command.

    ``level`` is ``"deep"`` (uses ``smoke.deep``) or anything else (uses ``smoke.standard``).
    Every command is run with an explicit ``cd <repo_path>`` so the landing dir is cosmetic
    even when the pane is reused across tools (Plan §1.4 / Risk #7).
    """
    commands = profile.smoke.deep if level == "deep" else profile.smoke.standard
    checks: list[ReportCheck] = []
    for command in commands:
        _screen, code = driver.run_remote(f"cd {profile.repo_path} && {command}", timeout=120)
        checks.append(ReportCheck(f"smoke:{command}", code == 0, f"exit {code}"))
    return checks


def verify_after_rollout(
    driver: TmuxDriver,
    profile: ToolProfile,
    node: object,
    *,
    commit: str,
    local_root: str,
    smoke_level: str,
) -> list[ReportCheck]:
    """Return the post-rollout verify checks: ``runtime_drift`` followed by the smoke checks."""
    drift_report = check_drift(driver, profile, node, commit=commit, local_root=local_root)
    checks = list(drift_report.checks)  # the single "runtime_drift" check
    checks.extend(run_smoke(driver, profile, level=smoke_level))
    return checks
