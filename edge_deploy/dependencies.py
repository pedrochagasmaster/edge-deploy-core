"""Deterministic, content-addressed offline dependency bundles."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from edge_deploy.config import DependencyBundleConfig, ToolProfile
from edge_deploy.remote_paths import edge_deploy_path, shell_remote_path
from edge_deploy.runner import bootstrap_runner, read_remote_json, run_step

BUNDLE_SCHEMA = "edge-deploy/dependency-bundle/1"
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_WHEEL_RE = re.compile(r"^(?P<name>.+?)-(?P<version>[^-]+)-[^-]+-[^-]+-[^-]+\.whl$", re.IGNORECASE)


class BundleError(RuntimeError):
    """Raised when a dependency bundle cannot be built or trusted."""


@dataclass(frozen=True)
class DependencyBundle:
    tool: str
    source_sha: str
    digest: str
    archive_sha256: str
    archive_path: Path
    manifest_path: Path
    manifest: dict[str, object]


@dataclass(frozen=True)
class DeliveredBundle:
    remote_dir: str
    reused: bool
    evidence: dict[str, object]


def canonical_dependency_bytes(data: bytes) -> bytes:
    """Normalize Git text inputs to LF so Windows and Linux produce one identity."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _validate_wheels(wheels: Sequence[Path]) -> list[Path]:
    validated: list[Path] = []
    distributions: dict[str, str] = {}
    for path in sorted((Path(item) for item in wheels), key=lambda item: item.name.lower()):
        if path.suffix.lower() != ".whl" or not path.is_file():
            raise BundleError(f"unexpected bundle file: {path.name}")
        match = _WHEEL_RE.match(path.name)
        if not match:
            raise BundleError(f"invalid wheel filename: {path.name}")
        name = re.sub(r"[-_.]+", "-", match.group("name")).lower()
        version = match.group("version")
        previous = distributions.setdefault(name, version)
        if previous != version:
            raise BundleError(f"multiple versions for {name}: {previous}, {version}")
        validated.append(path)
    if not validated:
        raise BundleError("wheelhouse is empty")
    return validated


def create_dependency_bundle(
    *,
    tool: str,
    source_sha: str,
    dependency_files: Mapping[str, bytes],
    wheels: Sequence[Path],
    config: DependencyBundleConfig,
    output_dir: Path,
) -> DependencyBundle:
    """Create a deterministic manifest and ZIP from already-resolved wheels."""
    output_dir.mkdir(parents=True, exist_ok=True)
    wheel_paths = _validate_wheels(wheels)
    normalized = {
        name.replace("\\", "/"): canonical_dependency_bytes(data)
        for name, data in dependency_files.items()
    }
    files: list[dict[str, object]] = []
    archive_entries: dict[str, bytes] = {}
    for name, data in sorted(normalized.items()):
        archive_name = f"requirements/{name}"
        archive_entries[archive_name] = data
        files.append({"path": archive_name, "sha256": _sha256(data), "size": len(data), "kind": "dependency"})
    for wheel in wheel_paths:
        data = wheel.read_bytes()
        archive_name = f"wheels/{wheel.name}"
        archive_entries[archive_name] = data
        files.append({"path": archive_name, "sha256": _sha256(data), "size": len(data), "kind": "wheel"})

    identity = {
        "schema": BUNDLE_SCHEMA,
        "tool": tool,
        "source_sha": source_sha,
        "target": {
            "python": config.python_version,
            "implementation": config.implementation,
            "abi": config.abi,
            "platform": config.platform,
        },
        "files": sorted(files, key=lambda item: str(item["path"])),
    }
    digest = _sha256(_json_bytes(identity))
    manifest: dict[str, object] = {**identity, "bundle_digest": digest}
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    archive_entries["manifest.json"] = manifest_bytes
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    archive_path = output_dir / f"{tool}-{digest}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(archive_entries.items()):
            info = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return DependencyBundle(
        tool=tool,
        source_sha=source_sha,
        digest=digest,
        archive_sha256=_file_sha256(archive_path),
        archive_path=archive_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )


GitReader = Callable[[str], bytes]
CommandRunner = Callable[[Sequence[str], Path], None]


def _git_reader(repo_root: Path, source_sha: str) -> GitReader:
    def read(path: str) -> bytes:
        completed = subprocess.run(
            ["git", "show", f"{source_sha}:{path}"],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        if completed.returncode:
            raise BundleError(f"dependency input unavailable at {source_sha}: {path}")
        return completed.stdout

    return read


def _run_command(argv: Sequence[str], cwd: Path) -> None:
    completed = subprocess.run(list(argv), cwd=cwd, check=False, capture_output=True, text=True)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise BundleError(f"bundle resolution failed: {detail}")


def build_dependency_bundle(
    profile: ToolProfile,
    *,
    repo_root: Path,
    source_sha: str,
    output_root: Path,
    command_runner: CommandRunner = _run_command,
) -> DependencyBundle:
    """Resolve a clean target wheelhouse and package it for node delivery."""
    config = profile.dependency_bundle
    if config is None:
        raise BundleError(f"{profile.tool} has dependency_paths but no dependency_bundle configuration")
    work = output_root / profile.tool / source_sha
    inputs = work / "inputs"
    wheels = work / "wheels"
    if work.exists():
        import shutil

        shutil.rmtree(work)
    inputs.mkdir(parents=True)
    wheels.mkdir()
    read = _git_reader(repo_root, source_sha)
    dependency_names = [config.requirements_file]
    if config.constraints_file:
        dependency_names.append(config.constraints_file)
    dependency_files: dict[str, bytes] = {}
    for name in dependency_names:
        data = canonical_dependency_bytes(read(name))
        dependency_files[name] = data
        target = inputs / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "-r",
        str(inputs / config.requirements_file),
        "--dest",
        str(wheels),
        "--platform",
        config.platform,
        "--python-version",
        config.python_version,
        "--implementation",
        config.implementation,
        "--abi",
        config.abi,
        "--only-binary=:all:",
    ]
    if config.constraints_file:
        command.extend(["-c", str(inputs / config.constraints_file)])
    command_runner(command, repo_root)
    return create_dependency_bundle(
        tool=profile.tool,
        source_sha=source_sha,
        dependency_files=dependency_files,
        wheels=list(wheels.iterdir()),
        config=config,
        output_dir=work / "bundle",
    )


def _stage_script(
    bundle: DependencyBundle,
    config: DependencyBundleConfig,
    *,
    remote_archive: str,
    run_id: str,
) -> str:
    expected_files = {
        str(item["path"]): str(item["sha256"])
        for item in bundle.manifest["files"]  # type: ignore[index]
    }
    # Expanded by os.path.expanduser on the node, which only understands ``~``
    # (not ``$HOME``).
    evidence_path = f"~/.edge-deploy/runs/{run_id}/steps/dependency-stage-evidence.json"
    return f"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path

archive = Path(os.path.expanduser({remote_archive!r}))
root = Path(os.path.expanduser({edge_deploy_path("bundles", bundle.tool)!r}))
final = root / {bundle.digest!r}
expected_source = {bundle.source_sha!r}
expected_digest = {bundle.digest!r}
expected_archive = {bundle.archive_sha256!r}
expected_files = {expected_files!r}
target_python = {config.python_version!r}
evidence_path = Path(os.path.expanduser({evidence_path!r}))

def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def emit(reused):
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps({{"remote_dir": str(final), "reused": reused, "bundle_digest": expected_digest}}),
        encoding="utf-8",
    )

if final.exists():
    manifest = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("bundle_digest") != expected_digest or manifest.get("source_sha") != expected_source:
        raise SystemExit("existing stage provenance mismatch")
    staged_files = {{
        path.relative_to(final).as_posix()
        for path in final.rglob("*")
        if path.is_file()
    }}
    if staged_files != set(expected_files) | {{"manifest.json"}}:
        raise SystemExit("existing stage file set mismatch")
    for name, digest in expected_files.items():
        if sha(final / name) != digest:
            raise SystemExit("existing stage checksum mismatch: " + name)
    archive.unlink(missing_ok=True)
    emit(True)
    raise SystemExit(0)

size = archive.stat().st_size
if shutil.disk_usage(root).free < max(size * 3, size + 512 * 1024 * 1024):
    raise SystemExit("insufficient disk space for dependency staging")
if sha(archive) != expected_archive:
    raise SystemExit("dependency archive checksum mismatch")
if "%d.%d" % sys.version_info[:2] != target_python:
    raise SystemExit("target Python compatibility mismatch")

tmp = Path(tempfile.mkdtemp(prefix=f".{{expected_digest}}.", dir=root))
try:
    with zipfile.ZipFile(archive) as bundle_zip:
        names = set(bundle_zip.namelist())
        wanted = set(expected_files) | {{"manifest.json"}}
        if names != wanted or any(Path(name).is_absolute() or ".." in Path(name).parts for name in names):
            raise SystemExit("unexpected files in dependency archive")
        bundle_zip.extractall(tmp)
    manifest = json.loads((tmp / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("bundle_digest") != expected_digest or manifest.get("source_sha") != expected_source:
        raise SystemExit("dependency manifest provenance mismatch")
    for name, digest in expected_files.items():
        if sha(tmp / name) != digest:
            raise SystemExit("dependency file checksum mismatch: " + name)
    verify = Path(tempfile.mkdtemp(prefix=".resolve.", dir=root))
    try:
        subprocess.run([sys.executable, "-m", "venv", str(verify / "venv")], check=True)
        pip = verify / "venv" / "bin" / "pip"
        command = [str(pip), "install", "--dry-run", "--no-index", "--find-links", str(tmp / "wheels"),
                   "-r", str(tmp / "requirements" / {config.requirements_file!r})]
        constraints = {config.constraints_file!r}
        if constraints:
            command.extend(["-c", str(tmp / "requirements" / constraints)])
        subprocess.run(command, check=True)
    finally:
        shutil.rmtree(verify, ignore_errors=True)
    os.replace(tmp, final)
    archive.unlink(missing_ok=True)
    emit(False)
finally:
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
"""


def deliver_dependency_bundle(
    driver: object,
    profile: ToolProfile,
    bundle: DependencyBundle,
    *,
    run_id: str,
) -> DeliveredBundle:
    """Transfer and verify one bundle through an authenticated transport."""
    config = profile.dependency_bundle
    if config is None:
        raise BundleError(f"{profile.tool} has no dependency bundle configuration")
    incoming = edge_deploy_path("bundles", profile.tool, ".incoming")
    remote_archive = f"{incoming}/{bundle.digest}.zip"
    stage_script_remote = f"{incoming}/stage-{bundle.digest}.py"
    runner_path = bootstrap_runner(driver, run_id)  # type: ignore[arg-type]
    screen, code = driver.run_remote(  # type: ignore[attr-defined]
        f"mkdir -p {shell_remote_path(incoming)}",
        timeout=30,
    )
    if code:
        raise BundleError(f"could not create remote bundle staging directory: {screen.strip()}")
    _archive_digest = driver.upload_file(bundle.archive_path, remote_archive)  # type: ignore[attr-defined]
    script = _stage_script(bundle, config, remote_archive=remote_archive, run_id=run_id)
    # newline="\n" prevents Windows CRLF translation; the script runs on Linux.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(script)
        script_tmp = handle.name
    try:
        driver.upload_file(script_tmp, stage_script_remote)  # type: ignore[attr-defined]
    finally:
        Path(script_tmp).unlink(missing_ok=True)
    fallback = (
        "/sys_apps_01/python/python310/bin/python3.10"
        if config.python_version == "3.10"
        else f"python{config.python_version}"
    )
    python_expr = f"$(command -v python{config.python_version} || printf %s {fallback})"
    stage_command = f"{python_expr} {stage_script_remote}"
    step_result = run_step(
        driver,  # type: ignore[arg-type]
        runner_path,
        run_id,
        "dependency-stage",
        stage_command,
        timeout=900,
    )
    if step_result.get("exit_code") != 0:
        tail = step_result.get("stdout_tail", "")
        raise BundleError(f"remote dependency verification failed: {tail}")
    evidence_path = f"~/.edge-deploy/runs/{run_id}/steps/dependency-stage-evidence.json"
    evidence = read_remote_json(driver, evidence_path)  # type: ignore[arg-type]
    if "remote_dir" not in evidence or "reused" not in evidence:
        raise BundleError("remote dependency verification returned no provenance")
    return DeliveredBundle(
        remote_dir=str(evidence["remote_dir"]),
        reused=bool(evidence["reused"]),
        evidence=evidence,
    )
