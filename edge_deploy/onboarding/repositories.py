"""Provision and validate core/tool checkouts behind injectable runners."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from edge_deploy.ledger import engine_identity
from edge_deploy.onboarding.manifest import CORE_GITHUB_URL, ToolManifest

CommandRunner = Callable[[Sequence[str]], str]

_ENGINE_PIN_RE = re.compile(r"@(v\d+\.\d+\.\d+)")
_REQUIRED_OPERATOR_EXTRAS = ("dev", "release")


@dataclass(frozen=True)
class ProvisionResult:
    tool_id: str
    path: Path
    action: str
    message: str


def default_runner(root: Path) -> CommandRunner:
    root = Path(root)

    def run(args: Sequence[str]) -> str:
        completed = subprocess.run(list(args), cwd=root, capture_output=True, text=True)
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"{args[0]} failed: {detail}")
        return completed.stdout

    return run


def bootstrap_core_root() -> Path:
    return Path(engine_identity()["package_dir"]).resolve().parent


def _normalize_url(value: str) -> str:
    return value.strip().removesuffix("/").removesuffix(".git").lower()


def _remote_names(run: CommandRunner) -> set[str]:
    output = run(["git", "remote"])
    return {line.strip() for line in output.splitlines() if line.strip()}


def _remote_url(run: CommandRunner, name: str) -> str | None:
    if name not in _remote_names(run):
        return None
    return run(["git", "remote", "get-url", name]).strip()


def _ensure_bitbucket_remote(run: CommandRunner, *, bitbucket_url: str) -> None:
    existing = _remote_url(run, "bitbucket")
    if existing is None:
        run(["git", "remote", "add", "bitbucket", bitbucket_url])
        return
    if _normalize_url(existing) != _normalize_url(bitbucket_url):
        raise RuntimeError(
            "bitbucket remote points to unexpected repository; "
            "fix or remove it before re-running onboarding"
        )


def _head_matches_tag(run: CommandRunner, expected_tag: str) -> bool:
    try:
        described = run(["git", "describe", "--tags", "--exact-match"]).strip()
        if described == expected_tag:
            return True
    except RuntimeError:
        pass
    head = run(["git", "rev-parse", "HEAD"]).strip()
    tagged = run(["git", "rev-parse", f"{expected_tag}^{{}}"]).strip()
    return head == tagged and bool(head)


def validate_bootstrap_core(
    core_root: Path,
    *,
    bitbucket_url: str,
    expected_tag: str,
    runner: CommandRunner | None = None,
) -> ProvisionResult:
    core_root = Path(core_root)
    if not (core_root / ".git").exists():
        raise RuntimeError(
            f"bootstrap core checkout missing at {core_root}; "
            "clone and install the approved engine tag before onboarding"
        )
    run = runner if runner is not None else default_runner(core_root)
    origin = _remote_url(run, "origin")
    if origin is None or _normalize_url(origin) != _normalize_url(CORE_GITHUB_URL):
        raise RuntimeError(f"origin points to unexpected repository: {origin or '(missing)'}")
    if not _head_matches_tag(run, expected_tag):
        raise RuntimeError(
            f"bootstrap core HEAD is not exactly tag {expected_tag}; "
            "check out the approved immutable engine tag"
        )
    _ensure_bitbucket_remote(run, bitbucket_url=bitbucket_url)
    return ProvisionResult(
        tool_id="core",
        path=core_root.resolve(),
        action="validated",
        message=f"validated bootstrap core at {core_root}",
    )


def provision_tool_checkout(
    dest: Path,
    manifest: ToolManifest,
    *,
    bitbucket_url: str,
    runner: CommandRunner | None = None,
) -> ProvisionResult:
    dest = Path(dest)
    action = "reused"
    if dest.exists():
        if not (dest / ".git").exists():
            raise RuntimeError(f"unexpected existing directory at {dest}")
    else:
        clone_runner = runner if runner is not None else default_runner(dest.parent)
        clone_runner(["git", "clone", manifest.github_url, str(dest)])
        action = "cloned"

    run = runner if runner is not None else default_runner(dest)
    origin = _remote_url(run, "origin")
    if origin is None or _normalize_url(origin) != _normalize_url(manifest.github_url):
        raise RuntimeError(f"origin points to unexpected repository: {origin or '(missing)'}")
    _ensure_bitbucket_remote(run, bitbucket_url=bitbucket_url)

    profile = dest / manifest.profile_filename
    local_check = dest / manifest.local_check_relative
    if not profile.is_file():
        raise RuntimeError(f"missing tool profile {manifest.profile_filename} in {dest}")
    if not local_check.is_file():
        raise RuntimeError(f"missing local check {manifest.local_check_relative} in {dest}")

    return ProvisionResult(
        tool_id=manifest.tool_id,
        path=dest.resolve(),
        action=action,
        message=f"{action} {manifest.tool_id} at {dest}",
    )


def read_engine_pin(tool_root: Path) -> str:
    pyproject = Path(tool_root) / "pyproject.toml"
    if not pyproject.is_file():
        raise RuntimeError(f"engine pin: missing pyproject.toml in {tool_root}")
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        if "edge-deploy-core" not in line:
            continue
        match = _ENGINE_PIN_RE.search(line)
        if match:
            return match.group(1)
    raise RuntimeError(f"engine pin not found in {pyproject}")


def assert_engine_pins_compatible(tool_roots: list[Path], *, expected_tag: str) -> None:
    for root in tool_roots:
        pin = read_engine_pin(Path(root))
        if pin != expected_tag:
            raise RuntimeError(
                f"engine pin mismatch in {root}: found {pin}, expected {expected_tag}"
            )


def _declared_optional_extras(tool_root: Path) -> set[str]:
    pyproject = Path(tool_root) / "pyproject.toml"
    if not pyproject.is_file():
        return set()
    extras: set[str] = set()
    in_section = False
    for raw in pyproject.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_section = line == "[project.optional-dependencies]"
            continue
        if not in_section or not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            extras.add(key)
    return extras


def install_tool_dependencies(tool_root: Path, *, runner: CommandRunner | None = None) -> None:
    tool_root = Path(tool_root)
    declared = _declared_optional_extras(tool_root)
    missing = [name for name in _REQUIRED_OPERATOR_EXTRAS if name not in declared]
    if missing:
        raise RuntimeError(
            f"tool at {tool_root} is missing required optional-dependencies "
            f"{missing}; declare {list(_REQUIRED_OPERATOR_EXTRAS)} before install"
        )
    run = runner if runner is not None else default_runner(tool_root)
    extras = ",".join(_REQUIRED_OPERATOR_EXTRAS)
    run([sys.executable, "-m", "pip", "install", "-e", f".[{extras}]"])
