from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from edge_deploy.config import DependencyBundleConfig, ToolProfile
from edge_deploy.dependencies import BundleError, create_dependency_bundle, deliver_dependency_bundle
from tests.conftest import FakeTmuxDriver


def _config() -> DependencyBundleConfig:
    return DependencyBundleConfig(
        requirements_file="requirements.txt",
        constraints_file="constraints.txt",
        python_version="3.10",
        implementation="cp",
        abi="cp310",
        platform="manylinux2014_x86_64",
    )


def test_dependency_bundle_identity_canonicalizes_line_endings(tmp_path: Path) -> None:
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    first = create_dependency_bundle(
        tool="demo",
        source_sha="a" * 40,
        dependency_files={
            "requirements.txt": b"demo==1.0\r\n",
            "constraints.txt": b"demo==1.0\r\n",
        },
        wheels=[wheel],
        config=_config(),
        output_dir=tmp_path / "first",
    )
    second = create_dependency_bundle(
        tool="demo",
        source_sha="a" * 40,
        dependency_files={
            "requirements.txt": b"demo==1.0\n",
            "constraints.txt": b"demo==1.0\n",
        },
        wheels=[wheel],
        config=_config(),
        output_dir=tmp_path / "second",
    )

    assert first.digest == second.digest
    assert first.archive_sha256 == second.archive_sha256


def test_dependency_bundle_rejects_unexpected_non_wheel_files(tmp_path: Path) -> None:
    unexpected = tmp_path / "README.txt"
    unexpected.write_text("stale", encoding="utf-8")

    with pytest.raises(BundleError, match="unexpected bundle file"):
        create_dependency_bundle(
            tool="demo",
            source_sha="a" * 40,
            dependency_files={"requirements.txt": b"demo==1.0\n"},
            wheels=[unexpected],
            config=_config(),
            output_dir=tmp_path / "bundle",
        )


def test_dependency_bundle_rejects_stale_duplicate_wheel_versions(tmp_path: Path) -> None:
    first = tmp_path / "demo-1.0-py3-none-any.whl"
    stale = tmp_path / "demo-0.9-py3-none-any.whl"
    first.write_bytes(b"new")
    stale.write_bytes(b"stale")

    with pytest.raises(BundleError, match="multiple versions for demo"):
        create_dependency_bundle(
            tool="demo",
            source_sha="a" * 40,
            dependency_files={"requirements.txt": b"demo==1.0\n"},
            wheels=[first, stale],
            config=_config(),
            output_dir=tmp_path / "bundle",
        )


def test_dependency_bundle_manifest_and_archive_are_deterministic(tmp_path: Path) -> None:
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    bundle = create_dependency_bundle(
        tool="demo",
        source_sha="b" * 40,
        dependency_files={
            "requirements.txt": b"demo==1.0\n",
            "constraints.txt": b"demo==1.0\n",
        },
        wheels=[wheel],
        config=_config(),
        output_dir=tmp_path / "bundle",
    )

    manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_sha"] == "b" * 40
    assert manifest["target"] == {
        "python": "3.10",
        "implementation": "cp",
        "abi": "cp310",
        "platform": "manylinux2014_x86_64",
    }
    assert manifest["bundle_digest"] == bundle.digest
    with zipfile.ZipFile(bundle.archive_path) as archive:
        assert sorted(archive.namelist()) == [
            "manifest.json",
            "requirements/constraints.txt",
            "requirements/requirements.txt",
            "wheels/demo-1.0-py3-none-any.whl",
        ]


def test_delivery_transfers_then_records_verified_remote_stage(tmp_path: Path) -> None:
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    bundle = create_dependency_bundle(
        tool="demo",
        source_sha="c" * 40,
        dependency_files={"requirements.txt": b"demo==1.0\n"},
        wheels=[wheel],
        config=_config(),
        output_dir=tmp_path / "bundle",
    )
    run_id = "run-test-delivery"
    remote_dir = f"/ads_storage/test/.edge-deploy/bundles/demo/{bundle.digest}"
    evidence = {
        "remote_dir": remote_dir,
        "reused": False,
        "bundle_digest": bundle.digest,
    }
    driver = FakeTmuxDriver(
        runner_step_results={
            "dependency-stage": {
                "schema": "edge-deploy/step/1",
                "step": "dependency-stage",
                "exit_code": 0,
                "started_at": "2026-07-03T12:00:00Z",
                "finished_at": "2026-07-03T12:00:01Z",
                "stdout_tail": "",
            },
            "dependency-stage-evidence": evidence,
        }
    )

    delivered = deliver_dependency_bundle(
        driver,
        ToolProfile(tool="demo", dependency_bundle=_config()),
        bundle,
        run_id=run_id,
    )

    assert any(
        remote == f"~/.edge-deploy/bundles/demo/.incoming/{bundle.digest}.zip"
        for _, remote in driver.uploads
    )
    assert any("stage-" in remote and remote.endswith(".py") for _, remote in driver.uploads)
    assert any("runner-" in remote for _, remote in driver.uploads)
    assert driver.runner_step_commands
    assert driver.runner_step_commands[0][1] == run_id
    assert driver.runner_step_commands[0][2] == "dependency-stage"
    assert any("__EDGE_RESULT_START__" in command for command in driver.commands)
    assert delivered.reused is False
    assert delivered.remote_dir == remote_dir
    assert delivered.evidence == evidence


def test_delivery_reuse_path_unchanged(tmp_path: Path) -> None:
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    bundle = create_dependency_bundle(
        tool="demo",
        source_sha="d" * 40,
        dependency_files={"requirements.txt": b"demo==1.0\n"},
        wheels=[wheel],
        config=_config(),
        output_dir=tmp_path / "bundle",
    )
    remote_dir = f"/ads_storage/test/.edge-deploy/bundles/demo/{bundle.digest}"
    evidence = {
        "remote_dir": remote_dir,
        "reused": True,
        "bundle_digest": bundle.digest,
    }
    driver = FakeTmuxDriver(
        runner_step_results={
            "dependency-stage": {
                "schema": "edge-deploy/step/1",
                "step": "dependency-stage",
                "exit_code": 0,
                "started_at": "2026-07-03T12:00:00Z",
                "finished_at": "2026-07-03T12:00:01Z",
                "stdout_tail": "",
            },
            "dependency-stage-evidence": evidence,
        }
    )

    delivered = deliver_dependency_bundle(
        driver,
        ToolProfile(tool="demo", dependency_bundle=_config()),
        bundle,
        run_id="run-reuse",
    )

    assert delivered.reused is True
    assert delivered.remote_dir.endswith(bundle.digest)
    assert set(delivered.evidence.keys()) >= {"remote_dir", "reused", "bundle_digest"}


def test_delivery_paths_are_variable_free_and_stage_script_expands_user_home(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    bundle = create_dependency_bundle(
        tool="demo",
        source_sha="e" * 40,
        dependency_files={"requirements.txt": b"demo==1.0\n"},
        wheels=[wheel],
        config=_config(),
        output_dir=tmp_path / "bundle",
    )
    remote_dir = f"/ads_storage/test/.edge-deploy/bundles/demo/{bundle.digest}"
    evidence = {
        "remote_dir": remote_dir,
        "reused": False,
        "bundle_digest": bundle.digest,
    }
    driver = FakeTmuxDriver(
        runner_step_results={
            "dependency-stage": {
                "schema": "edge-deploy/step/1",
                "step": "dependency-stage",
                "exit_code": 0,
                "started_at": "2026-07-03T12:00:00Z",
                "finished_at": "2026-07-03T12:00:01Z",
                "stdout_tail": "",
            },
            "dependency-stage-evidence": evidence,
        }
    )

    deliver_dependency_bundle(
        driver,
        ToolProfile(tool="demo", dependency_bundle=_config()),
        bundle,
        run_id="run-variable-free",
    )

    for _, remote in driver.uploads:
        assert "/ads_storage/$USER" not in remote
        assert "$USER" not in remote
    for command in driver.commands:
        assert "/ads_storage/$USER" not in command
        assert "$USER" not in command

    stage_script = next(
        content for _, remote, content in driver.uploaded_contents if "stage-" in remote
    )
    assert "Path(os.path.expanduser(" in stage_script
    assert "os.path.expandvars(" not in stage_script
    assert "/ads_storage/$USER" not in stage_script
    assert "$USER" not in stage_script
