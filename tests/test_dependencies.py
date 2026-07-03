from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from edge_deploy.config import DependencyBundleConfig, ToolProfile
from edge_deploy.dependencies import BundleError, create_dependency_bundle, deliver_dependency_bundle


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

    class Driver:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.uploads: list[tuple[Path, str]] = []

        def run_remote(self, command: str, *, timeout: float = 30) -> tuple[str, int]:
            self.calls.append(command)
            if "base64 -d" in command:
                return (
                    "DEPENDENCY_STAGE_START\n"
                    f'{{"remote_dir": "/ads_storage/test/.edge-deploy/bundles/demo/{bundle.digest}", '
                    f'"reused": false, "bundle_digest": "{bundle.digest}"}}\n'
                    "DEPENDENCY_STAGE_END\n",
                    0,
                )
            return "", 0

        def upload_file(self, source: Path, remote_path: str) -> str:
            self.uploads.append((source, remote_path))
            return hashlib.sha256(source.read_bytes()).hexdigest()

    driver = Driver()
    delivered = deliver_dependency_bundle(
        driver,
        ToolProfile(tool="demo", dependency_bundle=_config()),
        bundle,
    )

    assert driver.uploads == [
        (
            bundle.archive_path,
            f"/ads_storage/$USER/.edge-deploy/bundles/demo/.incoming/{bundle.digest}.zip",
        )
    ]
    assert delivered.reused is False
    assert delivered.remote_dir.endswith(bundle.digest)
