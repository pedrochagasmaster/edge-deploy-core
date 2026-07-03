from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from edge_deploy.config import DependencyBundleConfig, ToolProfile
from edge_deploy.dependencies import (
    BundleError,
    _parse_stage_evidence,
    create_dependency_bundle,
    deliver_dependency_bundle,
    target_filtered_dependency_bytes,
)


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


def test_target_filter_drops_python311_constraint_for_cp310_bundle() -> None:
    data = (
        b"scipy==1.16.3; python_version >= \"3.11\"\n"
        b"scipy==1.15.3; python_version < \"3.11\"\n"
    )

    filtered = target_filtered_dependency_bytes(data, _config()).decode("utf-8")

    assert "scipy==1.16.3" not in filtered
    assert "scipy==1.15.3" in filtered


def test_parse_stage_evidence_tolerates_tmux_wrapped_json_string() -> None:
    screen = (
        "noise\n"
        "DEPENDENCY_STAGE_START\n"
        '{"remote_dir": "/ads_storage/test/.edge-deploy/bundles/demo/f56af0dd44289019b7ac7e5\n'
        '648d2ccb8078e7e7103af34e289b90b696974e362", "reused": false, '
        '"bundle_digest": "f56af0dd44289019b7ac7e5648d2ccb8078e7e7103af34e289b90b696974e362"}\n'
        "DEPENDENCY_STAGE_END\n"
    )

    evidence = _parse_stage_evidence(screen)

    assert evidence == {
        "remote_dir": (
            "/ads_storage/test/.edge-deploy/bundles/demo/"
            "f56af0dd44289019b7ac7e5648d2ccb8078e7e7103af34e289b90b696974e362"
        ),
        "reused": False,
        "bundle_digest": "f56af0dd44289019b7ac7e5648d2ccb8078e7e7103af34e289b90b696974e362",
    }


def test_parse_stage_evidence_missing_markers_returns_none() -> None:
    assert _parse_stage_evidence('{"remote_dir": "/tmp"}') is None


def test_parse_stage_evidence_invalid_json_still_raises() -> None:
    with pytest.raises(json.JSONDecodeError):
        _parse_stage_evidence(
            "DEPENDENCY_STAGE_START\n"
            '{"remote_dir": "/tmp", nope}\n'
            "DEPENDENCY_STAGE_END\n"
        )


def test_dependency_bundle_archives_original_dependency_markers(tmp_path: Path) -> None:
    wheel = tmp_path / "scipy-1.15.3-cp310-cp310-manylinux2014_x86_64.whl"
    wheel.write_bytes(b"wheel")
    constraints = (
        b"scipy==1.16.3; python_version >= \"3.11\"\n"
        b"scipy==1.15.3; python_version < \"3.11\"\n"
    )

    bundle = create_dependency_bundle(
        tool="demo",
        source_sha="a" * 40,
        dependency_files={
            "requirements.txt": b"scipy>=1.10,<2\n",
            "constraints.txt": constraints,
        },
        wheels=[wheel],
        config=_config(),
        output_dir=tmp_path / "bundle",
    )

    with zipfile.ZipFile(bundle.archive_path) as archive:
        assert archive.read("requirements/constraints.txt") == constraints


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

    class Driver:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.uploads: list[tuple[Path, str]] = []

        def run_remote(
            self, command: str, *, timeout: float = 30, ensure_shell: bool = True
        ) -> tuple[str, int]:
            self.calls.append(command)
            if "EDGE_DEPLOY_REUSE_ONLY=1" in command:
                return "dependency stage missing", 1
            if command.endswith(".stage.py"):
                return (
                    "DEPENDENCY_STAGE_START\n"
                    f'{{"remote_dir": "/ads_storage/test/.edge-deploy/bundles/demo/{bundle.digest}", '
                    f'"reused": false, "bundle_digest": "{bundle.digest}"}}\n'
                    "DEPENDENCY_STAGE_END\n",
                    0,
                )
            return "", 0

        def upload_file(self, source: Path, remote_path: str) -> None:
            self.uploads.append((source, remote_path))

    driver = Driver()
    delivered = deliver_dependency_bundle(
        driver,
        ToolProfile(tool="demo", dependency_bundle=_config()),
        bundle,
    )

    assert len(driver.uploads) == 2
    assert driver.uploads[0][1].endswith(f"/.incoming/{bundle.digest}.stage.py")
    assert driver.uploads[1] == (
        bundle.archive_path,
        f"/ads_storage/$USER/.edge-deploy/bundles/demo/.incoming/{bundle.digest}.zip",
    )
    assert delivered.reused is False
    assert delivered.remote_dir.endswith(bundle.digest)


def test_delivery_reuses_verified_remote_stage_without_upload(tmp_path: Path) -> None:
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

    class Driver:
        def __init__(self) -> None:
            self.uploads: list[tuple[Path, str]] = []

        def run_remote(
            self, command: str, *, timeout: float = 30, ensure_shell: bool = True
        ) -> tuple[str, int]:
            if "EDGE_DEPLOY_REUSE_ONLY=1" in command:
                return (
                    "DEPENDENCY_STAGE_START\n"
                    f'{{"remote_dir": "/ads_storage/test/.edge-deploy/bundles/demo/{bundle.digest}", '
                    f'"reused": true, "bundle_digest": "{bundle.digest}"}}\n'
                    "DEPENDENCY_STAGE_END\n",
                    0,
                )
            return "", 0

        def upload_file(self, source: Path, remote_path: str) -> None:
            self.uploads.append((source, remote_path))

    driver = Driver()
    delivered = deliver_dependency_bundle(
        driver,
        ToolProfile(tool="demo", dependency_bundle=_config()),
        bundle,
    )

    assert len(driver.uploads) == 1
    assert driver.uploads[0][1].endswith(f"/.incoming/{bundle.digest}.stage.py")
    assert delivered.reused is True
    assert delivered.remote_dir.endswith(bundle.digest)
