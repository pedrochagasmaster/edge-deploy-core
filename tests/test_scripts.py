"""Run the real ``update.sh`` scripts under POSIX ``sh`` to verify the ADR-0004 interface.

A fake ``git`` (and ``chmod``) shim is placed first on PATH and the script is copied into a
throwaway working dir, so no real repository, remote, fetch or hard-reset is ever touched —
only the *variable resolution* and the optional positional ref are exercised.

Skips gracefully when no POSIX ``sh`` is available (e.g. CI without MSYS/Git-bash).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SH = shutil.which("sh")
PROJECTS_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(SH is None, reason="no POSIX sh found on PATH")

# A fake git that answers every subcommand both update.sh scripts use, harmlessly.
GIT_SHIM = """#!/bin/sh
case "$1" in
  rev-parse)
    case "$2" in
      --is-inside-work-tree) exit 0 ;;
      --short) echo 1111111 ;;
      *) echo 1111111111111111111111111111111111111111 ;;
    esac ;;
  fetch) exit 0 ;;
  diff) echo "" ;;
  reset) echo "RESET $3" ;;
  log) echo "1111111 fake subject" ;;
  update-ref) exit 0 ;;
  *) exit 0 ;;
esac
"""

CHMOD_SHIM = "#!/bin/sh\nexit 0\n"

# Cleared before each run so host environment cannot leak into the resolution test.
RESOLUTION_VARS = (
    "EDGE_DEPLOY_REMOTE",
    "EDGE_DEPLOY_BRANCH",
    "AUTOBENCH_GIT_REMOTE",
    "AUTOBENCH_GIT_BRANCH",
    "DISPATCH_UPDATE_REMOTE",
    "DISPATCH_UPDATE_BRANCH",
    "GIT_REMOTE",
    "GIT_BRANCH",
)


@pytest.fixture
def run_update_sh(tmp_path_factory):
    def _run(tool: str, args=(), env_extra=None):
        script_src = PROJECTS_ROOT / tool / "update.sh"
        if not script_src.exists():
            pytest.skip(f"{tool}/update.sh not found")

        root = tmp_path_factory.mktemp(f"{tool}_update")
        shim = root / "shimbin"
        shim.mkdir()
        (shim / "git").write_text(GIT_SHIM, encoding="utf-8", newline="\n")
        (shim / "chmod").write_text(CHMOD_SHIM, encoding="utf-8", newline="\n")
        work = root / "work"
        work.mkdir()
        shutil.copyfile(script_src, work / "update.sh")

        if tool == "autobench":
            helper_src = PROJECTS_ROOT / tool / "scripts" / "provision_telemetry_dirs.sh"
            helper_dir = work / "scripts"
            helper_dir.mkdir()
            shutil.copyfile(helper_src, helper_dir / helper_src.name)

        env = dict(os.environ)
        env["PATH"] = str(shim) + os.pathsep + env["PATH"]
        for name in RESOLUTION_VARS:
            env.pop(name, None)
        env.update(env_extra or {})

        if tool == "autobench":
            posix_work = subprocess.run(
                [SH, "-c", "pwd"],
                cwd=str(work),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            env["AUTOBENCH_TELEMETRY_DIR"] = f"{posix_work}/telemetry"

        result = subprocess.run(
            [SH, "update.sh", *args],
            cwd=str(work),
            env=env,
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout + result.stderr

    return _run


# ---------------------------------------------------------------------------
# autobench/update.sh
# ---------------------------------------------------------------------------


def test_autobench_default_remote_branch(run_update_sh) -> None:
    rc, out = run_update_sh("autobench")

    assert rc == 0
    assert "Fetching bitbucket/main" in out
    assert "Resetting working tree to bitbucket/main" in out


def test_autobench_edge_deploy_overrides_alias(run_update_sh) -> None:
    rc, out = run_update_sh(
        "autobench",
        env_extra={
            "EDGE_DEPLOY_REMOTE": "origin",
            "EDGE_DEPLOY_BRANCH": "develop",
            "AUTOBENCH_GIT_REMOTE": "alias-remote",
            "AUTOBENCH_GIT_BRANCH": "alias-branch",
        },
    )

    assert rc == 0
    assert "Fetching origin/develop" in out
    assert "Resetting working tree to origin/develop" in out
    assert "alias-remote" not in out


def test_autobench_alias_used_when_no_edge_deploy(run_update_sh) -> None:
    rc, out = run_update_sh(
        "autobench",
        env_extra={"AUTOBENCH_GIT_REMOTE": "mirror", "AUTOBENCH_GIT_BRANCH": "stable"},
    )

    assert rc == 0
    assert "Fetching mirror/stable" in out


def test_autobench_positional_ref_overrides_target(run_update_sh) -> None:
    rc, out = run_update_sh("autobench", args=("deadbeefcafe",))

    assert rc == 0
    # Default remote/branch still resolve, but the reset target is the positional ref.
    assert "Fetching bitbucket/main" in out
    assert "Resetting working tree to deadbeefcafe" in out


# ---------------------------------------------------------------------------
# robocop/update.sh
# ---------------------------------------------------------------------------


def test_robocop_default_remote_branch(run_update_sh) -> None:
    rc, out = run_update_sh("robocop")

    assert rc == 0
    assert "remote: bitbucket" in out
    assert "branch: main" in out


def test_robocop_edge_deploy_overrides_alias(run_update_sh) -> None:
    rc, out = run_update_sh(
        "robocop",
        env_extra={
            "EDGE_DEPLOY_REMOTE": "origin",
            "EDGE_DEPLOY_BRANCH": "develop",
            "DISPATCH_UPDATE_REMOTE": "alias-remote",
            "DISPATCH_UPDATE_BRANCH": "alias-branch",
            "GIT_REMOTE": "git-remote",
            "GIT_BRANCH": "git-branch",
        },
    )

    assert rc == 0
    assert "remote: origin" in out
    assert "branch: develop" in out
    assert "alias-remote" not in out


def test_robocop_alias_used_when_no_edge_deploy(run_update_sh) -> None:
    rc, out = run_update_sh(
        "robocop",
        env_extra={"DISPATCH_UPDATE_REMOTE": "mirror", "DISPATCH_UPDATE_BRANCH": "stable"},
    )

    assert rc == 0
    assert "remote: mirror" in out
    assert "branch: stable" in out
