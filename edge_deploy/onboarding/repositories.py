"""Provision and validate core/tool checkouts behind injectable runners."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from edge_deploy.ledger import engine_identity
from edge_deploy.onboarding.manifest import CORE_GITHUB_URL, ToolManifest

CommandRunner = Callable[[Sequence[str]], str]

_ENGINE_PIN_RE = re.compile(r"@(v\d+\.\d+\.\d+)")
_REQUIRED_OPERATOR_EXTRAS = ("dev", "release")
GIT_COMMAND_TIMEOUT_S = 20.0
CLONE_COMMAND_TIMEOUT_S = 600.0
PIP_COMMAND_TIMEOUT_S = 600.0


@dataclass(frozen=True)
class ProvisionResult:
    tool_id: str
    path: Path
    action: str
    message: str


@dataclass(frozen=True)
class CheckoutEvidence:
    """Non-secret checkout snapshot for resume/revalidation."""

    tool_id: str
    head_sha: str
    dirty: bool
    origin_fingerprint: str
    bitbucket_fingerprint: str | None
    engine_pin: str
    required_files_ok: bool

    def fingerprint(self) -> str:
        payload = "|".join(
            [
                self.tool_id,
                self.head_sha,
                "1" if self.dirty else "0",
                self.origin_fingerprint,
                self.bitbucket_fingerprint or "",
                self.engine_pin,
                "1" if self.required_files_ok else "0",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_stored(self) -> dict:
        return {
            "tool_id": self.tool_id,
            "head_sha": self.head_sha,
            "dirty": self.dirty,
            "origin_fingerprint": self.origin_fingerprint,
            "bitbucket_fingerprint": self.bitbucket_fingerprint,
            "engine_pin": self.engine_pin,
            "required_files_ok": self.required_files_ok,
            "evidence_fingerprint": self.fingerprint(),
        }

    def matches_stored(self, stored: Mapping[str, object]) -> bool:
        live = self.to_stored()
        keys = (
            "tool_id",
            "head_sha",
            "dirty",
            "origin_fingerprint",
            "bitbucket_fingerprint",
            "engine_pin",
            "required_files_ok",
        )
        return all(live.get(key) == stored.get(key) for key in keys)


def fingerprint_remote_url(url: str) -> str:
    return hashlib.sha256(_normalize_url(url).encode("utf-8")).hexdigest()


def _prompt_free_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_ASKPASS"] = ""
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def _timeout_for(args: Sequence[str]) -> float:
    if not args:
        return GIT_COMMAND_TIMEOUT_S
    name = Path(args[0]).name.lower()
    if name.startswith("pip"):
        return PIP_COMMAND_TIMEOUT_S
    if len(args) >= 3 and args[1] == "-m" and args[2] == "pip":
        return PIP_COMMAND_TIMEOUT_S
    if name == "git" and len(args) >= 2 and args[1] == "clone":
        return CLONE_COMMAND_TIMEOUT_S
    return GIT_COMMAND_TIMEOUT_S


def default_runner(root: Path) -> CommandRunner:
    root = Path(root)

    def run(args: Sequence[str]) -> str:
        command = list(args)
        label = Path(command[0]).name if command else "command"
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=_timeout_for(command),
                env=_prompt_free_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{label} timed out") from exc
        if completed.returncode:
            # Host-safe: never interpolate stderr/stdout (may contain private URLs/tokens).
            raise RuntimeError(f"{label} failed (exit {completed.returncode})")
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


def _is_dirty(run: CommandRunner) -> bool:
    status = run(["git", "status", "--porcelain", "--untracked-files=all"])
    return any(line.strip() for line in status.splitlines())


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
        raise RuntimeError("origin points to unexpected repository")
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


def inspect_bootstrap_core(
    core_root: Path,
    *,
    expected_tag: str,
    expected_bitbucket_fingerprint: str,
    runner: CommandRunner | None = None,
) -> dict:
    """Read-only core validation. Never clones or adds remotes."""
    core_root = Path(core_root)
    if not (core_root / ".git").exists():
        raise RuntimeError("bootstrap core checkout missing")
    run = runner if runner is not None else default_runner(core_root)
    origin = _remote_url(run, "origin")
    if origin is None or _normalize_url(origin) != _normalize_url(CORE_GITHUB_URL):
        raise RuntimeError("origin points to unexpected repository")
    if not _head_matches_tag(run, expected_tag):
        raise RuntimeError(f"bootstrap core HEAD is not exactly tag {expected_tag}")
    bitbucket = _remote_url(run, "bitbucket")
    if bitbucket is None:
        raise RuntimeError("bitbucket remote missing on bootstrap core")
    bb_fp = fingerprint_remote_url(bitbucket)
    if bb_fp != expected_bitbucket_fingerprint:
        raise RuntimeError("bitbucket remote fingerprint mismatch on bootstrap core")
    head_sha = run(["git", "rev-parse", "HEAD"]).strip()
    payload = {
        "head_sha": head_sha,
        "exact_tag": expected_tag,
        "origin_fingerprint": fingerprint_remote_url(origin),
        "bitbucket_fingerprint": bb_fp,
        "dirty": _is_dirty(run),
    }
    digest = hashlib.sha256(
        "|".join(
            [
                payload["head_sha"],
                payload["exact_tag"],
                payload["origin_fingerprint"],
                payload["bitbucket_fingerprint"],
                "1" if payload["dirty"] else "0",
            ]
        ).encode("utf-8")
    ).hexdigest()
    payload["evidence_fingerprint"] = digest
    return payload


def collect_checkout_evidence(
    dest: Path,
    manifest: ToolManifest,
    *,
    runner: CommandRunner | None = None,
) -> CheckoutEvidence:
    """Read-only checkout snapshot. Never clones or adds remotes."""
    dest = Path(dest)
    if not (dest / ".git").exists():
        raise RuntimeError(f"checkout missing at {dest}")
    run = runner if runner is not None else default_runner(dest)
    head_sha = run(["git", "rev-parse", "HEAD"]).strip()
    dirty = _is_dirty(run)
    origin = _remote_url(run, "origin")
    if origin is None:
        raise RuntimeError("origin remote missing")
    bitbucket = _remote_url(run, "bitbucket")
    profile = dest / manifest.profile_filename
    local_check = dest / manifest.local_check_relative
    required_files_ok = profile.is_file() and local_check.is_file()
    try:
        pin = read_engine_pin(dest)
    except RuntimeError:
        pin = ""
    return CheckoutEvidence(
        tool_id=manifest.tool_id,
        head_sha=head_sha,
        dirty=dirty,
        origin_fingerprint=fingerprint_remote_url(origin),
        bitbucket_fingerprint=fingerprint_remote_url(bitbucket) if bitbucket else None,
        engine_pin=pin,
        required_files_ok=required_files_ok,
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
        raise RuntimeError("origin points to unexpected repository")
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
