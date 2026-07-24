"""Resumable release-operator onboarding orchestrator."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from edge_deploy.auth import AuthBroker, ensure_kerberos
from edge_deploy.config import (
    DEFAULT_OPERATOR_CONFIG_PATH,
    NodeConfig,
    OperatorConfig,
    ToolProfile,
)
from edge_deploy.ledger import RunLedger, engine_identity, is_training_ledger
from edge_deploy.local_check import run_local_check
from edge_deploy.onboarding.checks import (
    AuthRunner,
    CheckResult,
    KerberosRunner,
    NodeSessionRegistry,
    ReadinessContext,
    TransportSmokeRunner,
    build_default_audit_runner,
    build_default_bitbucket_read_runner,
    build_default_bitbucket_write_dry_run_runner,
    build_default_edge_tcp_runner,
    build_default_gh_auth_runner,
    build_default_github_read_runner,
    build_default_kerberos_runner,
    build_default_known_hosts_runner,
    build_default_tool_clean_main_runner,
    build_readiness_specs,
    default_git_runner,
    make_rsa_and_transport_runners,
    run_checks,
)
from edge_deploy.onboarding.config_import import (
    ImportedPrivateConfig,
    install_operator_config,
    load_private_onboarding_source,
    merge_operator_config,
)
from edge_deploy.onboarding.manifest import (
    CANONICAL_TOOLS,
    DISPLAY_NAMES,
    TOOL_MANIFESTS,
    approved_engine_tag,
    normalize_tool_id,
)
from edge_deploy.onboarding.repositories import (
    assert_engine_pins_compatible,
    bootstrap_core_root,
    collect_checkout_evidence,
    fingerprint_remote_url,
    inspect_bootstrap_core,
    install_tool_dependencies,
    provision_tool_checkout,
    validate_bootstrap_core,
)
from edge_deploy.onboarding.state import OnboardingState, default_state_path
from edge_deploy.onboarding.training import (
    create_training_workspace,
    run_guided_training,
    start_training_ledger,
)
from edge_deploy.reporting import redact
from edge_deploy.ssh_transport import ParamikoSshTransport
from edge_deploy.transport_smoke import run_transport_smoke

_SUBPROCESS_TIMEOUT_S = 20.0
_PREREQUISITE_COMMANDS = ("py", "git", "gh")


class _OnboardAuthProgress:
    """Minimal AuthProgress adapter; never echoes secrets."""

    def emit(self, message: str, **_kwargs: object) -> None:
        print(redact(message))

    def set_waiting(self, _waiting_on: str | None) -> None:
        return None


def _platform_name() -> str:
    return sys.platform


def _command_available(name: str) -> bool:
    return shutil.which(name) is not None


def _powershell_version() -> tuple[int, int] | None:
    """Return Windows PowerShell (major, minor), or None if unavailable."""
    if _platform_name() != "win32":
        return None
    shell = shutil.which("powershell") or shutil.which("powershell.exe")
    if not shell:
        return None
    try:
        completed = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-Command",
                "$v = $PSVersionTable.PSVersion; Write-Output \"$($v.Major).$($v.Minor)\"",
            ],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    text = (completed.stdout or "").strip().splitlines()
    if not text:
        return None
    try:
        major_s, minor_s = text[-1].strip().split(".", 1)
        return int(major_s), int(minor_s)
    except (TypeError, ValueError):
        return None


def _powershell_compatible() -> bool:
    """True only for Windows PowerShell 5.1+ (not 4.x, not 7+)."""
    version = _powershell_version()
    if version is None:
        return False
    major, minor = version
    return major == 5 and minor >= 1


def _confirm_restart() -> bool:
    try:
        answer = input("Discard onboarding evidence only? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def _prompt_tools() -> list[str]:
    print("Select tools to onboard:")
    for index, tool_id in enumerate(CANONICAL_TOOLS, start=1):
        print(f"  {index}) {DISPLAY_NAMES[tool_id]} ({tool_id})")
    print("Enter numbers and/or ids separated by space/comma (e.g. 1 2 or autobench,dispatch):")
    try:
        raw = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        raw = ""
    if not raw:
        raise ValueError("tool selection is required; pass --tool or choose interactively")
    tokens = [part.strip() for part in raw.replace(",", " ").split() if part.strip()]
    selected: list[str] = []
    for token in tokens:
        if token.isdigit():
            index = int(token)
            if index < 1 or index > len(CANONICAL_TOOLS):
                raise ValueError(f"unknown tool selection {token!r}")
            selected.append(CANONICAL_TOOLS[index - 1])
        else:
            selected.append(normalize_tool_id(token))
    return _canonicalize_tools(selected)


def _canonicalize_tools(tools: Sequence[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in tools:
        tool_id = normalize_tool_id(raw)
        if tool_id in seen:
            continue
        seen.add(tool_id)
        ordered.append(tool_id)
    if not ordered:
        raise ValueError("at least one tool is required")
    return ordered


def _default_checkout_root(imported: ImportedPrivateConfig) -> Path:
    if imported.checkout_root:
        return Path(imported.checkout_root)
    return Path.home() / "edge-deploy"


def _tool_dest(root: Path, tool_id: str) -> Path:
    return Path(root) / TOOL_MANIFESTS[tool_id].default_dirname


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _check_payload(result: CheckResult) -> dict[str, Any]:
    return {
        "id": result.id,
        "outcome": result.outcome,
        "summary": redact(result.summary),
        "remediation": redact(result.remediation),
        "evidence_fingerprint": result.evidence_fingerprint,
    }


def _aggregate_outcome(checks: list[dict[str, Any]]) -> str:
    outcomes = {str(item.get("outcome") or "") for item in checks}
    if checks and outcomes <= {"passed"}:
        return "passed"
    if "failed" in outcomes:
        return "failed"
    if "blocked" in outcomes:
        return "blocked"
    return "failed"


def _training_acknowledge(message: str) -> None:
    input(f"{message}\n")


def _popen(command: Sequence[str], **kwargs: Any) -> Any:
    return subprocess.Popen(list(command), **kwargs)


def _launch_console(roots: list[Path]) -> None:
    """Start Edge Console against training roots without blocking onboarding."""
    script = Path(__file__).resolve().parents[2] / "edge_console.py"
    command: list[str] = [sys.executable, str(script), "--no-browser"]
    for root in roots:
        command.extend(["--root", str(root)])
    _popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _local_check_runner(repo_root: Path) -> int:
    try:
        return run_local_check(Path(repo_root)).exit_code
    except Exception:
        return 1


def _transport_factory_for_node(node: NodeConfig) -> Any:
    transport = getattr(node, "transport", "ssh") or "ssh"
    if transport != "ssh":
        raise RuntimeError(
            f"node {node.name!r} uses transport {transport!r}; "
            "onboarding readiness requires transport: ssh"
        )
    return ParamikoSshTransport.from_node_and_profile(node, ToolProfile())


def _authenticate_node(driver: Any, node_name: str) -> Any:
    broker = AuthBroker(_OnboardAuthProgress(), "prompt", 300.0, 3)
    broker.ensure_authenticated(driver, node_name)
    return driver


def _run_transport_smoke(driver: Any, *, node_label: str) -> Any:
    return run_transport_smoke(driver, node_label=node_label)


def _ensure_kerberos(driver: Any, node_name: str) -> Any:
    return ensure_kerberos(driver, node_name)


def _session_runners_for_readiness(
    operator: OperatorConfig,
) -> tuple[AuthRunner, TransportSmokeRunner, KerberosRunner, NodeSessionRegistry]:
    """Primary readiness auth seam (Task 13 / tests monkeypatch this).

    Builds one ``NodeSessionRegistry`` so RSA → optional Kerberos → transport smoke
    share a single per-node authenticated driver.
    """
    registry = NodeSessionRegistry()
    rsa_inner, transport_runner = make_rsa_and_transport_runners(
        operator,
        registry=registry,
        transport_factory=_transport_factory_for_node,
        authenticate=_authenticate_node,
        smoke=_run_transport_smoke,
    )

    def rsa_runner(node_name: str) -> CheckResult:
        node = operator.nodes.get(node_name)
        transport = getattr(node, "transport", "ssh") if node is not None else "ssh"
        if transport != "ssh":
            return CheckResult(
                f"rsa_auth:{node_name}",
                "failed",
                f"node {node_name} uses transport {transport!r}; onboarding requires transport: ssh",
                "Configure the node with transport: ssh for onboarding readiness.",
            )
        return rsa_inner(node_name)

    kerberos_runner = build_default_kerberos_runner(registry, ensure_fn=_ensure_kerberos)
    return rsa_runner, transport_runner, kerberos_runner, registry


def _first_real_release_commands(tools: Sequence[str]) -> list[str]:
    return [f"py -m edge_deploy release --guided --tool {tool}" for tool in tools]


def _require_deep_smoke(tools: Sequence[str], tool_roots: dict[str, Path]) -> bool:
    for tool in tools:
        root = tool_roots.get(tool)
        if root is None:
            continue
        profile_path = Path(root) / "edge_deploy.yaml"
        if not profile_path.is_file():
            continue
        try:
            profile = ToolProfile.load(profile_path)
        except Exception:
            continue
        if profile.smoke.deep:
            return True
    return False


def _stage_prerequisites(state: OnboardingState) -> None:
    checks: list[dict[str, Any]] = []
    platform = _platform_name()
    if platform != "win32":
        checks.append(
            {
                "id": "platform",
                "outcome": "failed",
                "summary": f"unsupported platform {platform}",
                "remediation": (
                    "Run onboarding on a Windows controller with PowerShell 5.1, "
                    "the py launcher, Git, and gh."
                ),
                "evidence_fingerprint": None,
            }
        )
    else:
        checks.append(
            {
                "id": "platform",
                "outcome": "passed",
                "summary": "Windows platform ok",
                "remediation": "",
                "evidence_fingerprint": None,
            }
        )
        if _powershell_compatible():
            checks.append(
                {
                    "id": "powershell",
                    "outcome": "passed",
                    "summary": "PowerShell 5.1-compatible shell present",
                    "remediation": "",
                    "evidence_fingerprint": None,
                }
            )
        else:
            checks.append(
                {
                    "id": "powershell",
                    "outcome": "failed",
                    "summary": "PowerShell 5.1-compatible shell missing",
                    "remediation": "Install Windows PowerShell 5.1 and ensure powershell is on PATH.",
                    "evidence_fingerprint": None,
                }
            )
        for name in _PREREQUISITE_COMMANDS:
            if _command_available(name):
                checks.append(
                    {
                        "id": name,
                        "outcome": "passed",
                        "summary": f"{name} available",
                        "remediation": "",
                        "evidence_fingerprint": None,
                    }
                )
            else:
                checks.append(
                    {
                        "id": name,
                        "outcome": "failed",
                        "summary": f"{name} not found on PATH",
                        "remediation": f"Install {name} and ensure it is on PATH, then re-run onboard.",
                        "evidence_fingerprint": None,
                    }
                )
    outcome = _aggregate_outcome(checks)
    state.mark_stage(
        "prerequisites",
        outcome,
        inputs={"platform": platform},
        checks=checks,
    )


def _stage_config(
    state: OnboardingState,
    *,
    imported: ImportedPrivateConfig,
    tools: list[str],
    root: Path,
) -> None:
    try:
        tool_paths = {tool: str(_tool_dest(root, tool)) for tool in tools}
        merged = merge_operator_config(
            imported.operator_mapping,
            audit_repo=str(bootstrap_core_root()),
            tools=tool_paths,
        )
        install_operator_config(merged)
        state.mark_stage(
            "config",
            "passed",
            inputs={"fingerprint": imported.fingerprint, "tools": list(tools), "root": str(root)},
            checks=[
                {
                    "id": "operator_config_install",
                    "outcome": "passed",
                    "summary": "operator config installed",
                    "remediation": "",
                    "evidence_fingerprint": imported.fingerprint,
                }
            ],
        )
    except Exception as exc:
        state.mark_stage(
            "config",
            "failed",
            inputs={"fingerprint": imported.fingerprint},
            checks=[
                {
                    "id": "operator_config_install",
                    "outcome": "failed",
                    "summary": redact(f"config install failed ({type(exc).__name__})"),
                    "remediation": "Correct the private onboarding source and re-run onboard.",
                    "evidence_fingerprint": imported.fingerprint,
                }
            ],
        )


def _stage_repositories(
    state: OnboardingState,
    *,
    imported: ImportedPrivateConfig,
    tools: list[str],
    root: Path,
) -> None:
    checks: list[dict[str, Any]] = []
    try:
        core_url = imported.bitbucket_remotes.get("core")
        if not core_url:
            raise RuntimeError(
                "private source is missing bitbucket_remotes.core; "
                "add the core Bitbucket URL and re-run"
            )
        validate_bootstrap_core(
            bootstrap_core_root(),
            bitbucket_url=core_url,
            expected_tag=approved_engine_tag(),
        )
        checks.append(
            {
                "id": "bootstrap_core",
                "outcome": "passed",
                "summary": "bootstrap core validated at approved tag",
                "remediation": "",
                "evidence_fingerprint": None,
            }
        )

        tool_roots: list[Path] = []
        actions: dict[str, str] = {}
        for tool in tools:
            remote = imported.bitbucket_remotes.get(tool)
            if not remote:
                raise RuntimeError(
                    f"private source is missing bitbucket_remotes.{tool}; "
                    "add the Bitbucket URL and re-run"
                )
            result = provision_tool_checkout(
                _tool_dest(root, tool),
                TOOL_MANIFESTS[tool],
                bitbucket_url=remote,
            )
            tool_roots.append(Path(result.path))
            actions[tool] = result.action
            checks.append(
                {
                    "id": f"provision:{tool}",
                    "outcome": "passed",
                    "summary": redact(result.message),
                    "remediation": "",
                    "evidence_fingerprint": None,
                }
            )

        expected = approved_engine_tag()
        assert_engine_pins_compatible(tool_roots, expected_tag=expected)
        checks.append(
            {
                "id": "engine_pins",
                "outcome": "passed",
                "summary": f"all selected tools pin {expected}",
                "remediation": "",
                "evidence_fingerprint": None,
            }
        )

        for tool_root in tool_roots:
            install_tool_dependencies(tool_root)
            tool_id = Path(tool_root).name
            checks.append(
                {
                    "id": f"deps:{tool_id}",
                    "outcome": "passed",
                    "summary": f"operator dependencies installed for {tool_id}",
                    "remediation": "",
                    "evidence_fingerprint": None,
                }
            )

        checkout_evidence: dict[str, dict[str, Any]] = {}
        for tool, tool_root in zip(tools, tool_roots):
            evidence = collect_checkout_evidence(tool_root, TOOL_MANIFESTS[tool])
            stored = evidence.to_stored()
            checkout_evidence[tool] = stored

        core_evidence = inspect_bootstrap_core(
            bootstrap_core_root(),
            expected_tag=expected,
            expected_bitbucket_fingerprint=fingerprint_remote_url(core_url),
        )

        state.mark_stage(
            "repositories",
            "passed",
            inputs={
                "tools": list(tools),
                "root": str(root),
                "engine_tag": expected,
                "actions": actions,
                "checkout_evidence": checkout_evidence,
                "core_evidence": {
                    key: core_evidence[key]
                    for key in (
                        "head_sha",
                        "exact_tag",
                        "origin_fingerprint",
                        "bitbucket_fingerprint",
                        "dirty",
                        "evidence_fingerprint",
                    )
                    if key in core_evidence
                },
            },
            checks=checks,
        )
    except Exception as exc:
        checks.append(
            {
                "id": "repositories",
                "outcome": "failed",
                "summary": redact(f"repository stage failed ({type(exc).__name__})"),
                "remediation": redact(
                    "Fix checkout/remote/pin issues described by the failure, then re-run onboard."
                ),
                "evidence_fingerprint": None,
            }
        )
        state.mark_stage(
            "repositories",
            "failed",
            inputs={"tools": list(tools), "root": str(root)},
            checks=checks,
        )


def _revalidate_repositories(
    state: OnboardingState,
    *,
    imported: ImportedPrivateConfig,
    tools: list[str],
    root: Path,
) -> None:
    """Read-only evidence compare. Never clones, adds remotes, or installs.

    Missing evidence (legacy state), missing checkouts, or fingerprint drift
    invalidates repositories+readiness so the full repository stage can provision
    or fail explicitly on the next pass.
    """
    previous = state.data["stages"]["repositories"].get("inputs") or {}
    stored_tools = previous.get("checkout_evidence")
    stored_core = previous.get("core_evidence")
    if not isinstance(stored_tools, dict) or not isinstance(stored_core, dict):
        state.invalidate_from("repositories")
        return

    try:
        core_url = imported.bitbucket_remotes.get("core")
        if not core_url:
            raise RuntimeError("missing bitbucket_remotes.core")
        live_core = inspect_bootstrap_core(
            bootstrap_core_root(),
            expected_tag=approved_engine_tag(),
            expected_bitbucket_fingerprint=fingerprint_remote_url(core_url),
        )
        if live_core.get("evidence_fingerprint") != stored_core.get("evidence_fingerprint"):
            raise RuntimeError("bootstrap core evidence drifted")

        if (
            list(previous.get("tools") or []) != list(tools)
            or str(previous.get("root") or "") != str(root)
            or str(previous.get("engine_tag") or "") != approved_engine_tag()
        ):
            raise RuntimeError("repository selection inputs changed")

        for tool in tools:
            stored = stored_tools.get(tool)
            if not isinstance(stored, dict):
                raise RuntimeError(f"missing stored evidence for {tool}")
            expected_bb = imported.bitbucket_remotes.get(tool)
            if not expected_bb:
                raise RuntimeError(f"missing bitbucket_remotes.{tool}")
            live = collect_checkout_evidence(_tool_dest(root, tool), TOOL_MANIFESTS[tool])
            if live.bitbucket_fingerprint != fingerprint_remote_url(expected_bb):
                raise RuntimeError(f"bitbucket remote fingerprint mismatch for {tool}")
            if live.origin_fingerprint != fingerprint_remote_url(
                TOOL_MANIFESTS[tool].github_url
            ):
                raise RuntimeError(f"origin fingerprint mismatch for {tool}")
            if live.engine_pin != approved_engine_tag() or not live.required_files_ok:
                raise RuntimeError(f"pin or required files invalid for {tool}")
            if not live.matches_stored(stored):
                raise RuntimeError(f"checkout evidence drifted for {tool}")
    except Exception:
        state.invalidate_from("repositories")


def _validate_existing_for_check(
    *,
    tools: list[str],
    root: Path,
) -> list[dict[str, Any]]:
    """Non-mutating presence checks for --check; never installs or clones."""
    checks: list[dict[str, Any]] = []
    config_path = DEFAULT_OPERATOR_CONFIG_PATH
    if not config_path.is_file():
        checks.append(
            {
                "id": "operator_config_present",
                "outcome": "blocked",
                "summary": "installed operator config is missing",
                "remediation": "Run onboard without --check to install operator config.",
                "evidence_fingerprint": None,
            }
        )
    else:
        checks.append(
            {
                "id": "operator_config_present",
                "outcome": "passed",
                "summary": "installed operator config present",
                "remediation": "",
                "evidence_fingerprint": None,
            }
        )
    for tool in tools:
        dest = _tool_dest(root, tool)
        if not (dest / ".git").exists() or not (dest / "edge_deploy.yaml").is_file():
            checks.append(
                {
                    "id": f"checkout_present:{tool}",
                    "outcome": "blocked",
                    "summary": f"tool checkout missing for {tool}",
                    "remediation": (
                        f"Run onboard without --check to provision {tool} under {dest}."
                    ),
                    "evidence_fingerprint": None,
                }
            )
        else:
            checks.append(
                {
                    "id": f"checkout_present:{tool}",
                    "outcome": "passed",
                    "summary": f"tool checkout present for {tool}",
                    "remediation": "",
                    "evidence_fingerprint": None,
                }
            )
    return checks


def _stage_readiness(
    state: OnboardingState,
    *,
    tools: list[str],
    root: Path,
    check_only: bool = False,
) -> None:
    if check_only:
        presence = _validate_existing_for_check(tools=tools, root=root)
        if any(item["outcome"] != "passed" for item in presence):
            state.mark_stage(
                "readiness",
                _aggregate_outcome(presence),
                inputs={"tools": list(tools), "root": str(root), "check_only": True},
                checks=presence,
            )
            return

    registry: NodeSessionRegistry | None = None
    try:
        if not DEFAULT_OPERATOR_CONFIG_PATH.is_file():
            raise RuntimeError(
                "operator config is not installed; run onboard without --check first"
            )
        operator = OperatorConfig.load(DEFAULT_OPERATOR_CONFIG_PATH)
        tool_roots = {tool: _tool_dest(root, tool) for tool in tools}
        for tool, path in tool_roots.items():
            if not path.is_dir():
                raise RuntimeError(f"missing tool checkout for {tool} at {path}")

        rsa_runner, transport_runner, kerberos_runner, registry = (
            _session_runners_for_readiness(operator)
        )

        ctx = ReadinessContext(
            tools=list(tools),
            tool_roots=tool_roots,
            core_root=bootstrap_core_root(),
            operator=operator,
            git_runner=default_git_runner,
            local_check_runner=_local_check_runner,
            gh_auth_runner=build_default_gh_auth_runner(),
            github_read_runner=build_default_github_read_runner(
                tools=tools, tool_roots=tool_roots, git_runner=default_git_runner
            ),
            tool_clean_main_runner=build_default_tool_clean_main_runner(
                tools=tools, tool_roots=tool_roots, git_runner=default_git_runner
            ),
            bitbucket_read_runner=build_default_bitbucket_read_runner(
                tools=tools,
                tool_roots=tool_roots,
                core_root=bootstrap_core_root(),
                git_runner=default_git_runner,
            ),
            bitbucket_write_dry_run_runner=build_default_bitbucket_write_dry_run_runner(
                tools=tools,
                tool_roots=tool_roots,
                core_root=bootstrap_core_root(),
                git_runner=default_git_runner,
            ),
            audit_runner=build_default_audit_runner(
                core_root=bootstrap_core_root(), git_runner=default_git_runner
            ),
            edge_tcp_runner=build_default_edge_tcp_runner(operator),
            known_hosts_runner=build_default_known_hosts_runner(operator),
            rsa_auth_runner=rsa_runner,
            transport_smoke_runner=transport_runner,
            kerberos_runner=kerberos_runner,
            require_deep_smoke=_require_deep_smoke(tools, tool_roots),
        )
        results = run_checks(build_readiness_specs(ctx))
        checks = [_check_payload(result) for result in results]
        state.mark_stage(
            "readiness",
            _aggregate_outcome(checks),
            inputs={
                "tools": list(tools),
                "root": str(root),
                "require_deep_smoke": ctx.require_deep_smoke,
            },
            checks=checks,
        )
    except Exception as exc:
        state.mark_stage(
            "readiness",
            "blocked" if check_only else "failed",
            inputs={"tools": list(tools), "root": str(root)},
            checks=[
                {
                    "id": "readiness",
                    "outcome": "blocked" if check_only else "failed",
                    "summary": redact(f"readiness failed ({type(exc).__name__})"),
                    "remediation": (
                        "Install config and provision repositories with a normal onboard run, "
                        "then re-run readiness."
                        if check_only
                        else "Resolve readiness failures, then re-run onboard."
                    ),
                    "evidence_fingerprint": None,
                }
            ],
        )
    finally:
        if registry is not None:
            registry.stop_all()


def _resume_or_start_training_ledger(
    workspace: Path,
    *,
    tool: str,
    operator: str,
    nodes: list[str],
) -> RunLedger:
    runs_root = workspace / "edge-deploy" / "runs"
    for ledger in RunLedger.find_open(runs_root):
        if is_training_ledger(ledger) and ledger.state.get("tool") == tool:
            return ledger
    return start_training_ledger(workspace, tool=tool, operator=operator, nodes=nodes)


def _stage_practice(
    state: OnboardingState,
    *,
    tools: list[str],
    app_dir: Path,
) -> None:
    try:
        operator = OperatorConfig.load(DEFAULT_OPERATOR_CONFIG_PATH)
        nodes = sorted(operator.nodes)
        if not nodes:
            nodes = ["node03"]
        run_ids: list[str] = []
        training_roots: list[Path] = []
        training_base = (Path(app_dir) / "training").resolve()
        for tool in tools:
            workspace = create_training_workspace(app_dir, tool)
            resolved = workspace.resolve()
            if training_base not in resolved.parents and resolved != training_base:
                raise RuntimeError("training workspace escaped app_dir/training")
            real_root = Path(str(state.data.get("root") or "")).resolve()
            if real_root != Path(".") and (
                real_root == resolved or real_root in resolved.parents
            ):
                raise RuntimeError("refusing to create training under the real checkout root")
            ledger = _resume_or_start_training_ledger(
                workspace,
                tool=tool,
                operator=operator.operator_email or "operator",
                nodes=nodes,
            )
            run_guided_training(ledger, acknowledge=_training_acknowledge)
            run_ids.append(str(ledger.state["run_id"]))
            training_roots.append(workspace)
        _launch_console(training_roots)
        state.data["practice"] = {
            "completed": True,
            "run_id": run_ids[0] if len(run_ids) == 1 else ",".join(run_ids),
        }
        state.mark_stage(
            "practice",
            "passed",
            inputs={"tools": list(tools), "training_root": str(app_dir / "training")},
            checks=[
                {
                    "id": f"practice:{tool}",
                    "outcome": "passed",
                    "summary": f"guided training completed for {tool}",
                    "remediation": "",
                    "evidence_fingerprint": None,
                }
                for tool in tools
            ],
        )
    except Exception as exc:
        state.mark_stage(
            "practice",
            "failed",
            inputs={"tools": list(tools)},
            checks=[
                {
                    "id": "practice",
                    "outcome": "failed",
                    "summary": redact(f"practice failed ({type(exc).__name__})"),
                    "remediation": "Inspect the training workspace under APPDATA, then re-run onboard.",
                    "evidence_fingerprint": None,
                }
            ],
        )


def _stage_complete(state: OnboardingState, *, app_dir: Path) -> None:
    commands = _first_real_release_commands(state.data.get("tools") or [])
    readiness = state.data.get("stages", {}).get("readiness", {})
    report = {
        "schema": "edge-deploy/onboarding-report/1",
        "tools": list(state.data.get("tools") or []),
        "root": str(state.data.get("root") or ""),
        "engine": {
            "version": engine_identity().get("version"),
            "tag": approved_engine_tag(),
            "content_sha256": engine_identity().get("content_sha256"),
        },
        "config_fingerprint": str(state.data.get("config_fingerprint") or ""),
        "readiness_outcome": readiness.get("outcome"),
        "readiness_checks": [
            {
                "id": item.get("id"),
                "outcome": item.get("outcome"),
                "summary": redact(str(item.get("summary") or "")),
                "remediation": redact(str(item.get("remediation") or "")),
            }
            for item in (readiness.get("checks") or [])
        ],
        "practice_completed": bool((state.data.get("practice") or {}).get("completed")),
        "first_real_release_commands": commands,
    }
    report_path = Path(app_dir) / "onboarding-report.json"
    _write_json_atomic(report_path, report)
    state.mark_stage(
        "complete",
        "passed",
        inputs={"report": str(report_path)},
        checks=[
            {
                "id": "onboarding_report",
                "outcome": "passed",
                "summary": "redacted onboarding report written",
                "remediation": "",
                "evidence_fingerprint": None,
            }
        ],
    )
    tools = ", ".join(state.data.get("tools") or [])
    print(f"onboard: complete for {tools}")
    print("onboard: first real guided release command(s):")
    for command in commands:
        print(f"  {command}")


def _run_stages(
    state: OnboardingState,
    *,
    imported: ImportedPrivateConfig,
    tools: list[str],
    root: Path,
    check_only: bool,
    app_dir: Path,
) -> int:
    if state.data["stages"]["prerequisites"]["outcome"] != "passed":
        _stage_prerequisites(state)
        state.save()
        if state.data["stages"]["prerequisites"]["outcome"] != "passed":
            return 1

    if check_only:
        # Strictly non-provisioning: never install config, clone/remotes/deps,
        # create practice ledgers, or launch the console.
        _stage_readiness(state, tools=tools, root=root, check_only=True)
        state.save()
        return 0 if state.data["stages"]["readiness"]["outcome"] == "passed" else 1

    if state.data["stages"]["config"]["outcome"] != "passed":
        _stage_config(state, imported=imported, tools=tools, root=root)
        state.save()
        if state.data["stages"]["config"]["outcome"] != "passed":
            return 1

    if state.data["stages"]["repositories"]["outcome"] == "passed":
        _revalidate_repositories(state, imported=imported, tools=tools, root=root)
        state.save()
    if state.data["stages"]["repositories"]["outcome"] != "passed":
        _stage_repositories(state, imported=imported, tools=tools, root=root)
        state.save()
        if state.data["stages"]["repositories"]["outcome"] != "passed":
            return 1

    if state.data["stages"]["readiness"]["outcome"] != "passed":
        _stage_readiness(state, tools=tools, root=root, check_only=False)
        state.save()
        if state.data["stages"]["readiness"]["outcome"] != "passed":
            return 1

    if state.data["stages"]["practice"]["outcome"] != "passed":
        _stage_practice(state, tools=tools, app_dir=app_dir)
        state.save()
        if state.data["stages"]["practice"]["outcome"] != "passed":
            return 1

    if state.data["stages"]["complete"]["outcome"] != "passed":
        _stage_complete(state, app_dir=app_dir)
        state.save()
        if state.data["stages"]["complete"]["outcome"] != "passed":
            return 1
    else:
        # Idempotent completed re-run: refresh report without re-practice/console.
        _stage_complete(state, app_dir=app_dir)
        state.save()
    return 0


def run_onboarding(args: argparse.Namespace) -> int:
    state_path = default_state_path()
    app_dir = state_path.parent

    # Validate private source before any evidence reset so bad --restart cannot
    # discard a good state when the new config is unusable.
    imported = load_private_onboarding_source(Path(args.config))

    if args.restart:
        if not (args.yes or _confirm_restart()):
            print("restart cancelled")
            return 1
        if state_path.is_file():
            state = OnboardingState.load(state_path)
            state.reset_evidence()
            state.save()

    raw_tools = args.tool if args.tool is not None else _prompt_tools()
    tools = _canonicalize_tools(raw_tools)
    root = Path(args.root) if args.root else _default_checkout_root(imported)

    if state_path.is_file():
        state = OnboardingState.load(state_path)
        if not state.engine_matches() and not args.restart:
            print(
                "engine identity changed; re-run with --restart to discard onboarding evidence"
            )
            return 2
        state.apply_selection(
            tools=tools,
            root=str(root),
            config_fingerprint=imported.fingerprint,
        )
        state.save()
    else:
        state = OnboardingState.create_new(
            path=state_path,
            tools=tools,
            root=str(root),
            config_fingerprint=imported.fingerprint,
        )
        state.save()

    return _run_stages(
        state,
        imported=imported,
        tools=tools,
        root=root,
        check_only=bool(args.check),
        app_dir=app_dir,
    )
