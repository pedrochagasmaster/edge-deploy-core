"""Dependency-aware readiness check runner (passed / failed / blocked).

Task 7 adds the concrete both-vpns readiness suite: ``ReadinessContext`` and
``build_readiness_specs``. Network/auth/transport/audit work goes through injected
high-level runners so unit tests never touch real services. Production / Task 10
wire those runners with the factories in this module (``build_default_*``,
``make_rsa_and_transport_runners``), reusing existing engine APIs.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import paramiko

from edge_deploy.audit import AuditSyncError, check_audit_remote
from edge_deploy.config import (
    TOOL_PROFILE_FILENAME,
    NodeConfig,
    OperatorConfig,
    ToolProfile,
)
from edge_deploy.posture import git_probe_command
from edge_deploy.preflight import run_tcp_preflight
from edge_deploy.reporting import redact
from edge_deploy.ssh_transport import settings_from_node
from edge_deploy.transport_smoke import run_transport_smoke

_VALID_OUTCOMES = frozenset({"passed", "failed", "blocked"})
_SUBPROCESS_TIMEOUT_S = 20.0
_GH_AUTH_TIMEOUT_S = 20.0

GitRunner = Callable[[list[str], Path], int]
LocalCheckRunner = Callable[[Path], int]
SocketConnector = Callable[[tuple[str, int], float], object]


@dataclass(frozen=True)
class CheckResult:
    id: str
    outcome: str
    summary: str
    remediation: str
    evidence_fingerprint: str | None = None


@dataclass(frozen=True)
class CheckSpec:
    id: str
    depends_on: tuple[str, ...]
    run: Callable[[], CheckResult]


AuthRunner = Callable[[str], CheckResult]
TransportSmokeRunner = Callable[[str], CheckResult]
KerberosRunner = Callable[[str], CheckResult]
SimpleCheckRunner = Callable[[], CheckResult]


@dataclass
class NodeSessionRegistry:
    """Memory-only authenticated transports keyed by node label.

    Task 10 should create one registry per readiness pass, authenticate via the
    RSA runner into this registry, then reuse the same driver for transport smoke
    (and Kerberos when deep smoke is required) so the operator is not prompted
    twice. Never persist drivers or secrets.
    """

    _drivers: dict[str, Any] = field(default_factory=dict)

    def get(self, node: str) -> Any | None:
        return self._drivers.get(node)

    def put(self, node: str, driver: Any) -> None:
        self._drivers[node] = driver

    def clear(self) -> None:
        self._drivers.clear()

    def stop_all(self) -> None:
        for driver in list(self._drivers.values()):
            stop = getattr(driver, "stop_session", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
        self.clear()


@dataclass(frozen=True)
class ReadinessContext:
    """Inputs for the both-vpns readiness suite.

    ``operator`` carries real :class:`NodeConfig` entries so known-hosts and Edge
    checks resolve each node's ``settings_from_node(...).known_hosts_path`` (not one
    global path). Network/auth/transport/audit checks are injected runners; tests
    supply fakes and Task 10 supplies production adapters from the factories below.
    """

    tools: list[str]
    tool_roots: Mapping[str, Path]
    core_root: Path
    operator: OperatorConfig
    git_runner: GitRunner
    local_check_runner: LocalCheckRunner
    gh_auth_runner: SimpleCheckRunner
    github_read_runner: SimpleCheckRunner
    tool_clean_main_runner: SimpleCheckRunner
    bitbucket_read_runner: SimpleCheckRunner
    bitbucket_write_dry_run_runner: SimpleCheckRunner
    audit_runner: SimpleCheckRunner
    edge_tcp_runner: SimpleCheckRunner
    rsa_auth_runner: AuthRunner
    transport_smoke_runner: TransportSmokeRunner
    kerberos_runner: KerberosRunner
    known_hosts_runner: SimpleCheckRunner | None = None
    require_deep_smoke: bool = False


def run_checks(specs: list[CheckSpec], *, max_workers: int = 4) -> list[CheckResult]:
    """Run readiness checks respecting dependencies; emit results in spec order.

    ``max_workers`` is accepted for API compatibility. Execution is serial and
    deterministic so report order always matches input order.
    """
    del max_workers  # serial scheduler; concurrency left for a later hardening pass
    _validate_graph(specs)

    outcomes: dict[str, str] = {}
    results: list[CheckResult] = []

    for spec in specs:
        unmet = [dep for dep in spec.depends_on if outcomes.get(dep) != "passed"]
        if unmet:
            result = CheckResult(
                spec.id,
                "blocked",
                f"blocked by unmet dependencies: {', '.join(unmet)}",
                "Resolve failed or blocked dependencies, then re-run readiness.",
            )
        else:
            result = _execute(spec)
        results.append(_redact_result(result))
        outcomes[spec.id] = result.outcome

    return results


def build_readiness_specs(ctx: ReadinessContext) -> list[CheckSpec]:
    """Build the concrete both-vpns readiness DAG for routine onboarding.

    Never includes a GitHub-write success gate (write is expected red in both-vpns).
    Kerberos checks appear only when ``ctx.require_deep_smoke`` is true, and are
    ordered before transport smoke so both can share the RSA-authenticated session.
    """
    known_hosts_run = ctx.known_hosts_runner or build_default_known_hosts_runner(
        ctx.operator
    )
    specs: list[CheckSpec] = [
        CheckSpec("bb_token_present", (), _check_bb_token),
        CheckSpec("operator_config", (), lambda: _check_operator_config(ctx)),
        CheckSpec("gh_auth", (), ctx.gh_auth_runner),
        CheckSpec("github_read", ("gh_auth",), ctx.github_read_runner),
        CheckSpec("tool_clean_main", (), ctx.tool_clean_main_runner),
        CheckSpec("bitbucket_read", (), ctx.bitbucket_read_runner),
        CheckSpec(
            "bitbucket_write_dry_run",
            ("bitbucket_read",),
            ctx.bitbucket_write_dry_run_runner,
        ),
        CheckSpec("audit_release_log", ("bitbucket_read",), ctx.audit_runner),
        CheckSpec("known_hosts", (), known_hosts_run),
        CheckSpec("edge_tcp", ("known_hosts",), ctx.edge_tcp_runner),
    ]
    for node in sorted(ctx.operator.nodes):
        specs.append(
            CheckSpec(
                f"rsa_auth:{node}",
                ("edge_tcp",),
                lambda node=node: ctx.rsa_auth_runner(node),
            )
        )
        if ctx.require_deep_smoke:
            # Before transport smoke so Kerberos can reuse the RSA session.
            specs.append(
                CheckSpec(
                    f"kerberos:{node}",
                    (f"rsa_auth:{node}",),
                    lambda node=node: ctx.kerberos_runner(node),
                )
            )
        specs.append(
            CheckSpec(
                f"transport_smoke:{node}",
                (f"rsa_auth:{node}",),
                lambda node=node: ctx.transport_smoke_runner(node),
            )
        )
    for tool in ctx.tools:
        specs.append(
            CheckSpec(
                f"local_check:{tool}",
                ("tool_clean_main",),
                lambda tool=tool: _check_local_check(ctx, tool),
            )
        )
    return specs


# ---------------------------------------------------------------------------
# Default / production adapters (Task 10 construction)
# ---------------------------------------------------------------------------


def _prompt_free_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env.setdefault("GH_PROMPT_DISABLED", "1")
    env.setdefault("GIT_ASKPASS", "")
    return env


def default_git_runner(command: list[str], repo_root: Path) -> int:
    """Run a git probe with prompts disabled and a hard deadline."""
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            env=_prompt_free_env(),
        )
    except subprocess.TimeoutExpired:
        return -1
    return completed.returncode


def build_default_gh_auth_runner(
    *,
    runner: Callable[[list[str]], int] | None = None,
) -> SimpleCheckRunner:
    def _run_gh(command: list[str]) -> int:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=_GH_AUTH_TIMEOUT_S,
                env=_prompt_free_env(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return 1
        return completed.returncode

    run = runner or _run_gh

    def check() -> CheckResult:
        code = run(["gh", "auth", "status"])
        if code == 0:
            return CheckResult("gh_auth", "passed", "gh authenticated", "")
        return CheckResult(
            "gh_auth",
            "failed",
            "gh is not authenticated",
            "Run: gh auth login",
        )

    return check


def build_default_github_read_runner(
    *,
    tools: Sequence[str],
    tool_roots: Mapping[str, Path],
    git_runner: GitRunner,
) -> SimpleCheckRunner:
    def check() -> CheckResult:
        failed: list[str] = []
        command = git_probe_command("origin", "read")
        for tool in tools:
            root = tool_roots.get(tool)
            if root is None:
                failed.append(tool)
                continue
            if git_runner(list(command), Path(root)) != 0:
                failed.append(tool)
        if failed:
            return CheckResult(
                "github_read",
                "failed",
                f"GitHub read failed for: {', '.join(failed)}",
                "Restore GitHub read access in the current posture, then re-run.",
            )
        return CheckResult("github_read", "passed", "GitHub read ok for selected tools", "")

    return check


def build_default_tool_clean_main_runner(
    *,
    tools: Sequence[str],
    tool_roots: Mapping[str, Path],
    git_runner: GitRunner,
    output_runner: Callable[[list[str], Path], str] | None = None,
) -> SimpleCheckRunner:
    """Require each selected tool on clean exact origin/main (label-only failures)."""

    def _output(command: list[str], root: Path) -> str:
        if output_runner is not None:
            return output_runner(command, root)
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
                env=_prompt_free_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(str(exc)) from exc
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "git failed")
        return completed.stdout

    def check() -> CheckResult:
        failed: list[str] = []
        for tool in tools:
            root = Path(tool_roots[tool]) if tool in tool_roots else None
            if root is None:
                failed.append(tool)
                continue
            try:
                if git_runner(["git", "fetch", "origin", "main"], root) != 0:
                    failed.append(tool)
                    continue
                branch = _output(["git", "branch", "--show-current"], root).strip()
                if branch != "main":
                    failed.append(tool)
                    continue
                status = _output(
                    ["git", "status", "--porcelain", "--untracked-files=all"], root
                )
                if any(
                    line.strip() and not line.startswith("?? edge-deploy/reports/")
                    for line in status.splitlines()
                ):
                    failed.append(tool)
                    continue
                head = _output(["git", "rev-parse", "HEAD"], root).strip()
                origin_main = _output(
                    ["git", "rev-parse", "refs/remotes/origin/main"], root
                ).strip()
                if head != origin_main:
                    failed.append(tool)
            except Exception:
                failed.append(tool)
        if failed:
            return CheckResult(
                "tool_clean_main",
                "failed",
                f"clean exact origin/main failed for: {', '.join(failed)}",
                "Checkout main, discard local changes, and match origin/main exactly.",
            )
        return CheckResult(
            "tool_clean_main",
            "passed",
            "selected tools are clean exact origin/main",
            "",
        )

    return check


def build_default_bitbucket_read_runner(
    *,
    tools: Sequence[str],
    tool_roots: Mapping[str, Path],
    core_root: Path,
    git_runner: GitRunner,
) -> SimpleCheckRunner:
    def check() -> CheckResult:
        failed: list[str] = []
        command = git_probe_command("bitbucket", "read")
        targets: list[tuple[str, Path]] = [("core", Path(core_root))]
        targets.extend((tool, Path(tool_roots[tool])) for tool in tools if tool in tool_roots)
        for label, root in targets:
            if git_runner(list(command), root) != 0:
                failed.append(label)
        if failed:
            return CheckResult(
                "bitbucket_read",
                "failed",
                f"Bitbucket read failed for: {', '.join(failed)}",
                "Confirm Bitbucket VPN and remotes, then re-run.",
            )
        return CheckResult(
            "bitbucket_read",
            "passed",
            "Bitbucket read ok for selected tools and core",
            "",
        )

    return check


def build_default_bitbucket_write_dry_run_runner(
    *,
    tools: Sequence[str],
    tool_roots: Mapping[str, Path],
    core_root: Path,
    git_runner: GitRunner,
) -> SimpleCheckRunner:
    """Dry-run Bitbucket write via ``git_probe_command('bitbucket', 'write')`` only."""

    def check() -> CheckResult:
        failed: list[str] = []
        command = git_probe_command("bitbucket", "write")
        targets: list[tuple[str, Path]] = [("core", Path(core_root))]
        targets.extend((tool, Path(tool_roots[tool])) for tool in tools if tool in tool_roots)
        for label, root in targets:
            if git_runner(list(command), root) != 0:
                failed.append(label)
        if failed:
            return CheckResult(
                "bitbucket_write_dry_run",
                "failed",
                f"Bitbucket dry-run write failed for: {', '.join(failed)}",
                "Confirm BB_TOKEN and Bitbucket write access; only dry-run push is used.",
            )
        return CheckResult(
            "bitbucket_write_dry_run",
            "passed",
            "Bitbucket dry-run write ok for selected tools and core",
            "",
        )

    return check


def build_default_audit_runner(
    *,
    core_root: Path,
    tools: Sequence[str],
    audit_check: Callable[..., None] | None = None,
) -> SimpleCheckRunner:
    check_fn = audit_check or check_audit_remote

    def check() -> CheckResult:
        try:
            # Reachability / sync first (no tool filter).
            check_fn(Path(core_root))
            for tool in tools:
                check_fn(Path(core_root), tool=tool, allow_unresolved=True)
        except AuditSyncError as exc:
            return CheckResult(
                "audit_release_log",
                "failed",
                f"release-log audit not synchronized ({type(exc).__name__})",
                "Clear the audit outbox and synchronize the Bitbucket release-log branch.",
            )
        except Exception as exc:
            return CheckResult(
                "audit_release_log",
                "failed",
                f"release-log audit check failed ({type(exc).__name__})",
                "Confirm Bitbucket release-log access, then re-run.",
            )
        return CheckResult(
            "audit_release_log",
            "passed",
            "release-log audit synchronized",
            "",
        )

    return check


def build_default_known_hosts_runner(operator: OperatorConfig) -> SimpleCheckRunner:
    """Require a strict known_hosts entry per node; never enroll or append keys.

    Uses each node's ``settings_from_node(...).known_hosts_path`` (honoring
    ``UserKnownHostsFile``). Failure summaries name node labels only.
    """

    def check() -> CheckResult:
        missing: list[str] = []
        for name in sorted(operator.nodes):
            node = operator.nodes[name]
            try:
                settings = settings_from_node(node)
            except Exception:
                missing.append(name)
                continue
            path = settings.known_hosts_path
            if not path.is_file():
                missing.append(name)
                continue
            host_keys = paramiko.HostKeys()
            try:
                host_keys.load(str(path))
            except (OSError, paramiko.hostkeys.InvalidHostKey):
                missing.append(name)
                continue
            lookup = (
                settings.hostname
                if settings.port == 22
                else f"[{settings.hostname}]:{settings.port}"
            )
            if host_keys.lookup(lookup) is None:
                missing.append(name)
        if missing:
            return CheckResult(
                "known_hosts",
                "failed",
                f"missing known_hosts entry for: {', '.join(missing)}",
                "Add the exact host key for each listed node to that node's "
                "UserKnownHostsFile; auto-enrollment is disabled.",
            )
        return CheckResult(
            "known_hosts",
            "passed",
            "strict known_hosts entries present for all nodes",
            "",
        )

    return check


def build_default_edge_tcp_runner(
    operator: OperatorConfig,
    *,
    connect: SocketConnector | None = None,
    timeout: float = 15.0,
) -> SimpleCheckRunner:
    def check() -> CheckResult:
        failed: list[str] = []
        for name in sorted(operator.nodes):
            kwargs: dict[str, Any] = {"timeout": timeout}
            if connect is not None:
                kwargs["connector"] = connect
            result = run_tcp_preflight(operator.nodes[name], **kwargs)
            if not result.connected:
                failed.append(name)
        if failed:
            return CheckResult(
                "edge_tcp",
                "failed",
                f"Edge TCP failed for: {', '.join(failed)}",
                "Restore Edge VPN (both-vpns), then re-run.",
            )
        return CheckResult("edge_tcp", "passed", "Edge TCP ok for all nodes", "")

    return check


def make_rsa_and_transport_runners(
    operator: OperatorConfig,
    *,
    registry: NodeSessionRegistry | None = None,
    transport_factory: Callable[[NodeConfig], Any] | None = None,
    authenticate: Callable[[Any, str], None] | None = None,
    smoke: Callable[..., Any] | None = None,
) -> tuple[AuthRunner, TransportSmokeRunner]:
    """Build RSA + transport runners that share one per-node authenticated session.

    ``authenticate(driver, node_name)`` should leave ``driver`` authenticated and
    registered. ``smoke`` defaults to :func:`run_transport_smoke` and receives that
    same driver — avoiding a second RSA prompt. Kerberos (when required) should be
    scheduled before transport smoke so it can use the live session first.
    """
    active = registry if registry is not None else NodeSessionRegistry()
    smoke_fn = smoke or run_transport_smoke

    def rsa_runner(node_name: str) -> CheckResult:
        node = operator.nodes.get(node_name)
        if node is None:
            return CheckResult(
                f"rsa_auth:{node_name}",
                "failed",
                f"unknown node {node_name}",
                "Configure the node in OperatorConfig, then re-run.",
            )
        try:
            if transport_factory is None:
                raise RuntimeError("transport_factory is required for RSA auth")
            driver = transport_factory(node)
            if authenticate is None:
                raise RuntimeError("authenticate is required for RSA auth")
            authenticate(driver, node_name)
            active.put(node_name, driver)
        except Exception as exc:
            summary = redact(f"{type(exc).__name__}: {exc}")
            return CheckResult(
                f"rsa_auth:{node_name}",
                "failed",
                f"RSA authentication failed for {node_name}: {summary}",
                "Re-enter a fresh RSA passcode; it is never stored.",
            )
        return CheckResult(
            f"rsa_auth:{node_name}",
            "passed",
            "RSA authenticated",
            "",
        )

    def transport_runner(node_name: str) -> CheckResult:
        driver = active.get(node_name)
        if driver is None:
            return CheckResult(
                f"transport_smoke:{node_name}",
                "failed",
                f"no authenticated session for {node_name}",
                "RSA auth must succeed before transport smoke.",
            )
        try:
            result = smoke_fn(driver, node_label=node_name)
        except Exception as exc:
            summary = redact(f"{type(exc).__name__}: {exc}")
            return CheckResult(
                f"transport_smoke:{node_name}",
                "failed",
                f"transport smoke failed for {node_name}: {summary}",
                "Inspect transport smoke failures, then re-run readiness.",
            )
        passed = bool(getattr(result, "passed", False))
        if passed:
            return CheckResult(
                f"transport_smoke:{node_name}",
                "passed",
                "transport smoke ok",
                "",
            )
        return CheckResult(
            f"transport_smoke:{node_name}",
            "failed",
            f"transport smoke failed for {node_name}",
            "Inspect transport smoke failures, then re-run readiness.",
        )

    return rsa_runner, transport_runner


# ---------------------------------------------------------------------------
# Built-in helpers (local / env only)
# ---------------------------------------------------------------------------


def _check_bb_token() -> CheckResult:
    if os.environ.get("BB_TOKEN"):
        return CheckResult("bb_token_present", "passed", "BB_TOKEN is set", "")
    return CheckResult(
        "bb_token_present",
        "failed",
        "BB_TOKEN is not set",
        "Set BB_TOKEN in the environment (value is never displayed or stored).",
    )


def _check_operator_config(ctx: ReadinessContext) -> CheckResult:
    problems: list[str] = []
    operator = ctx.operator
    if not (operator.operator_email or "").strip():
        problems.append("operator_email")
    if not (operator.audit_repo or "").strip():
        problems.append("audit_repo")
    if not operator.nodes:
        problems.append("nodes")
    else:
        for name, node in operator.nodes.items():
            if not (node.host or "").strip():
                problems.append(f"node:{name}:host")
    for tool in ctx.tools:
        root = ctx.tool_roots.get(tool)
        if root is None:
            problems.append(f"tool_root:{tool}")
            continue
        profile_path = Path(root) / TOOL_PROFILE_FILENAME
        if not profile_path.is_file():
            problems.append(tool)
            continue
        try:
            profile = ToolProfile.load(profile_path)
        except Exception:
            problems.append(tool)
            continue
        if not (profile.tool and profile.github_url and profile.bitbucket_url):
            problems.append(tool)
    if problems:
        return CheckResult(
            "operator_config",
            "failed",
            f"operator/profile inputs incomplete: {', '.join(problems)}",
            "Complete OperatorConfig and each selected tool's edge_deploy.yaml, then re-run.",
        )
    return CheckResult(
        "operator_config",
        "passed",
        "OperatorConfig and tool profiles are complete",
        "",
    )


def _check_local_check(ctx: ReadinessContext, tool: str) -> CheckResult:
    root = ctx.tool_roots.get(tool)
    if root is None:
        return CheckResult(
            f"local_check:{tool}",
            "failed",
            f"local_check missing tool root for {tool}",
            "Provision the tool checkout, then re-run.",
        )
    try:
        code = ctx.local_check_runner(Path(root))
    except Exception as exc:
        return CheckResult(
            f"local_check:{tool}",
            "failed",
            f"local_check raised for {tool}: {type(exc).__name__}",
            "Restore tools/dev/local_check.ps1 execution, then re-run.",
        )
    if code == 0:
        return CheckResult(
            f"local_check:{tool}",
            "passed",
            f"local_check passed for {tool}",
            "",
        )
    return CheckResult(
        f"local_check:{tool}",
        "failed",
        f"local_check failed for {tool} (exit {code})",
        "Fix the tool's tools/dev/local_check.ps1 failures, then re-run.",
    )


def _validate_graph(specs: list[CheckSpec]) -> None:
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise ValueError(f"duplicate check id: {spec.id}")
        seen.add(spec.id)

    known = {spec.id for spec in specs}
    for spec in specs:
        for dep in spec.depends_on:
            if dep not in known:
                raise ValueError(
                    f"unknown dependency {dep!r} required by check {spec.id!r}"
                )


def _execute(spec: CheckSpec) -> CheckResult:
    try:
        result = spec.run()
    except Exception as exc:
        return CheckResult(
            spec.id,
            "failed",
            f"check raised {type(exc).__name__}: {exc}",
            "Inspect the check error, correct the environment, then re-run.",
        )

    if result.id != spec.id:
        raise ValueError(
            f"result id {result.id!r} does not match check id {spec.id!r}"
        )
    if result.outcome not in _VALID_OUTCOMES:
        raise ValueError(
            f"invalid outcome {result.outcome!r} for check {spec.id!r}; "
            f"expected one of {sorted(_VALID_OUTCOMES)}"
        )
    return result


def _redact_result(result: CheckResult) -> CheckResult:
    return CheckResult(
        result.id,
        result.outcome,
        redact(result.summary),
        redact(result.remediation),
        result.evidence_fingerprint,
    )
