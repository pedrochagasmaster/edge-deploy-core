from __future__ import annotations

from pathlib import Path

import paramiko
import pytest

from edge_deploy.config import TOOL_PROFILE_FILENAME, NodeConfig, OperatorConfig
from edge_deploy.onboarding.checks import (
    CheckResult,
    CheckSpec,
    NodeSessionRegistry,
    ReadinessContext,
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
    make_rsa_and_transport_runners,
    run_checks,
)
from edge_deploy.posture import git_probe_command
from edge_deploy.reporting import ReportCheck, redact


def _host_key_line(hostname: str, port: int, key: paramiko.PKey) -> str:
    lookup = hostname if port == 22 else f"[{hostname}]:{port}"
    return f"{lookup} {key.get_name()} {key.get_base64()}\n"


def test_dependency_block_and_independent_failure_continue() -> None:
    calls: list[str] = []

    def ok(cid: str) -> CheckResult:
        calls.append(cid)
        return CheckResult(cid, "passed", "ok", "")

    def fail(cid: str) -> CheckResult:
        calls.append(cid)
        return CheckResult(cid, "failed", "boom", "fix it")

    specs = [
        CheckSpec("a", (), lambda: fail("a")),
        CheckSpec("b", ("a",), lambda: ok("b")),  # should block
        CheckSpec("c", (), lambda: ok("c")),  # independent, still runs
    ]
    results = run_checks(specs, max_workers=1)
    assert [r.id for r in results] == ["a", "b", "c"]
    assert results[0].outcome == "failed"
    assert results[1].outcome == "blocked"
    assert results[2].outcome == "passed"
    assert "b" not in calls
    assert calls == ["a", "c"]


def test_redacted_summary_never_embeds_token_assignment() -> None:
    result = CheckResult(
        "bb_token",
        "failed",
        redact("token=supersecret missing"),
        "Set BB_TOKEN in the environment",
    )
    assert "supersecret" not in result.summary
    assert "***REDACTED***" in result.summary


def test_duplicate_check_ids_rejected() -> None:
    specs = [
        CheckSpec("a", (), lambda: CheckResult("a", "passed", "ok", "")),
        CheckSpec("a", (), lambda: CheckResult("a", "passed", "ok", "")),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        run_checks(specs, max_workers=1)


def test_unknown_dependency_rejected() -> None:
    specs = [
        CheckSpec("a", ("missing",), lambda: CheckResult("a", "passed", "ok", "")),
    ]
    with pytest.raises(ValueError, match="unknown dependency"):
        run_checks(specs, max_workers=1)


def test_result_id_mismatch_rejected() -> None:
    specs = [
        CheckSpec("a", (), lambda: CheckResult("other", "passed", "ok", "")),
    ]
    with pytest.raises(ValueError, match="result id"):
        run_checks(specs, max_workers=1)


def test_invalid_outcome_rejected() -> None:
    specs = [
        CheckSpec("a", (), lambda: CheckResult("a", "skipped", "ok", "")),
    ]
    with pytest.raises(ValueError, match="outcome"):
        run_checks(specs, max_workers=1)


def test_check_exception_becomes_failed_and_continues() -> None:
    calls: list[str] = []

    def boom() -> CheckResult:
        calls.append("a")
        raise RuntimeError("token=leaked-secret exploded")

    def ok() -> CheckResult:
        calls.append("b")
        return CheckResult("b", "passed", "ok", "")

    results = run_checks(
        [
            CheckSpec("a", (), boom),
            CheckSpec("b", (), ok),
        ],
        max_workers=1,
    )
    assert [r.id for r in results] == ["a", "b"]
    assert results[0].outcome == "failed"
    assert "leaked-secret" not in results[0].summary
    assert "leaked-secret" not in results[0].remediation
    assert "token=" not in results[0].summary
    assert "RuntimeError" in results[0].summary
    assert results[1].outcome == "passed"
    assert calls == ["a", "b"]


def test_runner_redacts_summary_and_remediation() -> None:
    results = run_checks(
        [
            CheckSpec(
                "secret",
                (),
                lambda: CheckResult(
                    "secret",
                    "failed",
                    "missing token=supersecret value",
                    "export token=supersecret and retry",
                ),
            ),
        ],
        max_workers=1,
    )
    assert "supersecret" not in results[0].summary
    assert "supersecret" not in results[0].remediation
    assert "***REDACTED***" in results[0].summary
    assert "***REDACTED***" in results[0].remediation


def test_base_exception_is_not_caught() -> None:
    def raise_keyboard_interrupt() -> CheckResult:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_checks(
            [CheckSpec("a", (), raise_keyboard_interrupt)],
            max_workers=1,
        )


def test_blocked_summary_names_failed_dependency() -> None:
    results = run_checks(
        [
            CheckSpec(
                "a",
                (),
                lambda: CheckResult("a", "failed", "boom", "fix a"),
            ),
            CheckSpec(
                "b",
                ("a",),
                lambda: CheckResult("b", "passed", "should not run", ""),
            ),
        ],
        max_workers=1,
    )
    assert results[1].outcome == "blocked"
    assert "a" in results[1].summary


def _pass(cid: str, summary: str = "ok") -> CheckResult:
    return CheckResult(cid, "passed", summary, "")


def _operator(
    tmp_path: Path,
    *,
    nodes: dict[str, NodeConfig] | None = None,
    known_hosts: Path | None = None,
) -> OperatorConfig:
    kh = known_hosts or (tmp_path / "known_hosts")
    if nodes is None:
        nodes = {
            "node03": NodeConfig(
                host="operator@edge-private.example.internal",
                ssh_options=f"-p 2222 -o UserKnownHostsFile={kh.as_posix()}",
                session="edge-node03",
                name="node03",
            )
        }
    return OperatorConfig(
        operator_email="op@example.com",
        audit_repo=str(tmp_path / "edge-deploy-core"),
        nodes=nodes,
        tools={"autobench": str(tmp_path / "autobench")},
    )


def _ctx(
    tmp_path: Path,
    *,
    require_deep_smoke: bool = False,
    operator: OperatorConfig | None = None,
    rsa_auth_runner=None,
    transport_smoke_runner=None,
    kerberos_runner=None,
    gh_auth_runner=None,
    github_read_runner=None,
    tool_clean_main_runner=None,
    bitbucket_read_runner=None,
    bitbucket_write_dry_run_runner=None,
    audit_runner=None,
    edge_tcp_runner=None,
    known_hosts_runner=None,
    local_check_runner=None,
    git_runner=None,
) -> ReadinessContext:
    return ReadinessContext(
        tools=["autobench"],
        tool_roots={"autobench": tmp_path / "autobench"},
        core_root=tmp_path / "edge-deploy-core",
        operator=operator or _operator(tmp_path),
        git_runner=git_runner or (lambda command, root: 0),
        local_check_runner=local_check_runner or (lambda root: 0),
        gh_auth_runner=gh_auth_runner or (lambda: _pass("gh_auth", "gh authenticated")),
        github_read_runner=github_read_runner
        or (lambda: _pass("github_read", "GitHub read ok")),
        tool_clean_main_runner=tool_clean_main_runner
        or (lambda: _pass("tool_clean_main", "clean origin/main")),
        bitbucket_read_runner=bitbucket_read_runner
        or (lambda: _pass("bitbucket_read", "Bitbucket read ok")),
        bitbucket_write_dry_run_runner=bitbucket_write_dry_run_runner
        or (lambda: _pass("bitbucket_write_dry_run", "Bitbucket dry-run write ok")),
        audit_runner=audit_runner
        or (lambda: _pass("audit_release_log", "release-log synchronized")),
        edge_tcp_runner=edge_tcp_runner or (lambda: _pass("edge_tcp", "Edge TCP ok")),
        known_hosts_runner=known_hosts_runner
        if known_hosts_runner is not None
        else (lambda: _pass("known_hosts", "known_hosts ok")),
        rsa_auth_runner=rsa_auth_runner
        or (
            lambda node: CheckResult(
                f"rsa_auth:{node}", "passed", "RSA authenticated", ""
            )
        ),
        transport_smoke_runner=transport_smoke_runner
        or (
            lambda node: CheckResult(
                f"transport_smoke:{node}", "passed", "transport smoke ok", ""
            )
        ),
        kerberos_runner=kerberos_runner
        or (
            lambda node: CheckResult(
                f"kerberos:{node}", "passed", "Kerberos ticket ok", ""
            )
        ),
        require_deep_smoke=require_deep_smoke,
    )


def test_build_readiness_includes_rsa_transport_and_conditional_kerberos(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BB_TOKEN", "env-only-token")
    specs = build_readiness_specs(_ctx(tmp_path, require_deep_smoke=False))
    ids = [s.id for s in specs]
    assert "bb_token_present" in ids
    assert "operator_config" in ids
    assert "gh_auth" in ids
    assert "github_read" in ids
    assert "tool_clean_main" in ids
    assert "bitbucket_read" in ids
    assert "bitbucket_write_dry_run" in ids
    assert "audit_release_log" in ids
    assert "known_hosts" in ids
    assert "edge_tcp" in ids
    assert "rsa_auth:node03" in ids
    assert "transport_smoke:node03" in ids
    assert "kerberos:node03" not in ids
    assert "local_check:autobench" in ids
    assert "github_write_required" not in ids
    assert not any("github_write" in cid for cid in ids)

    deep_ids = [s.id for s in build_readiness_specs(_ctx(tmp_path, require_deep_smoke=True))]
    assert "kerberos:node03" in deep_ids
    # Kerberos must run before transport smoke so both can share the RSA session.
    assert deep_ids.index("kerberos:node03") < deep_ids.index("transport_smoke:node03")


def test_bb_token_check_presence_without_leaking(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BB_TOKEN", raising=False)
    specs = build_readiness_specs(_ctx(tmp_path))
    token_spec = next(s for s in specs if s.id == "bb_token_present")
    result = token_spec.run()
    assert result.outcome == "failed"
    assert "BB_TOKEN" in result.remediation
    assert "env-only" not in result.summary
    assert "token=" not in result.summary.lower()


def test_bb_token_present_passes_without_echoing_value(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BB_TOKEN", "env-only-token")
    specs = build_readiness_specs(_ctx(tmp_path))
    result = next(s for s in specs if s.id == "bb_token_present").run()
    assert result.outcome == "passed"
    assert result.summary == "BB_TOKEN is set"
    assert "env-only-token" not in result.summary
    assert "env-only-token" not in result.remediation


def test_rsa_runner_failure_is_failed_not_persisting_secret(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        rsa_auth_runner=lambda node: CheckResult(
            f"rsa_auth:{node}",
            "failed",
            redact("passcode=123456 RSA rejected"),
            "Re-enter a fresh RSA passcode; it is never stored",
        ),
        transport_smoke_runner=lambda node: CheckResult(
            f"transport_smoke:{node}", "blocked", "blocked on auth", ""
        ),
        kerberos_runner=lambda node: CheckResult(
            f"kerberos:{node}", "blocked", "blocked on auth", ""
        ),
    )
    rsa = next(s for s in build_readiness_specs(ctx) if s.id == "rsa_auth:node03")
    result = rsa.run()
    assert result.outcome == "failed"
    assert "123456" not in result.summary
    assert "***REDACTED***" in result.summary


def test_readiness_dependency_blocking_for_auth_chain(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        known_hosts_runner=lambda: _pass("known_hosts"),
        edge_tcp_runner=lambda: CheckResult(
            "edge_tcp", "failed", "Edge TCP failed for: node03", "Restore Edge VPN"
        ),
        rsa_auth_runner=lambda node: CheckResult(
            f"rsa_auth:{node}", "passed", "should not run", ""
        ),
    )
    results = run_checks(build_readiness_specs(ctx), max_workers=1)
    by_id = {r.id: r for r in results}
    assert by_id["edge_tcp"].outcome == "failed"
    assert by_id["rsa_auth:node03"].outcome == "blocked"
    assert by_id["transport_smoke:node03"].outcome == "blocked"


def test_stable_check_families_invoke_injected_runners(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BB_TOKEN", "x")
    calls: list[str] = []

    def track(cid: str):
        def _run() -> CheckResult:
            calls.append(cid)
            return _pass(cid)

        return _run

    def track_node(prefix: str):
        def _run(node: str) -> CheckResult:
            calls.append(f"{prefix}:{node}")
            return _pass(f"{prefix}:{node}")

        return _run

    (tmp_path / "autobench").mkdir()
    (tmp_path / "autobench" / TOOL_PROFILE_FILENAME).write_text(
        "tool: autobench\n"
        "github_url: https://github.com/example/autobench.git\n"
        "bitbucket_url: https://bitbucket.example/autobench.git\n"
        "repo_path: /opt/autobench\n"
        "smoke:\n  standard: [echo ok]\n",
        encoding="utf-8",
    )

    ctx = _ctx(
        tmp_path,
        gh_auth_runner=track("gh_auth"),
        github_read_runner=track("github_read"),
        tool_clean_main_runner=track("tool_clean_main"),
        bitbucket_read_runner=track("bitbucket_read"),
        bitbucket_write_dry_run_runner=track("bitbucket_write_dry_run"),
        audit_runner=track("audit_release_log"),
        known_hosts_runner=track("known_hosts"),
        edge_tcp_runner=track("edge_tcp"),
        rsa_auth_runner=track_node("rsa_auth"),
        transport_smoke_runner=track_node("transport_smoke"),
        local_check_runner=lambda root: calls.append(f"local:{root.name}") or 0,
    )
    results = run_checks(build_readiness_specs(ctx), max_workers=1)
    assert all(r.outcome == "passed" for r in results)
    assert "gh_auth" in calls
    assert "github_read" in calls
    assert "tool_clean_main" in calls
    assert "bitbucket_read" in calls
    assert "bitbucket_write_dry_run" in calls
    assert "audit_release_log" in calls
    assert "edge_tcp" in calls
    assert "rsa_auth:node03" in calls
    assert "transport_smoke:node03" in calls
    assert "local:autobench" in calls


def test_known_hosts_uses_per_node_settings_from_node_paths(tmp_path: Path) -> None:
    key = paramiko.RSAKey.generate(1024)
    kh_a = tmp_path / "kh-a"
    kh_b = tmp_path / "kh-b"
    kh_a.write_text(
        _host_key_line("edge-a.example.internal", 2222, key),
        encoding="utf-8",
    )
    # kh_b intentionally missing entry for node04
    kh_b.write_text("", encoding="utf-8")
    operator = OperatorConfig(
        operator_email="op@example.com",
        audit_repo=str(tmp_path / "core"),
        nodes={
            "node03": NodeConfig(
                host="operator@edge-a.example.internal",
                ssh_options=f"-p 2222 -o UserKnownHostsFile={kh_a.as_posix()}",
                name="node03",
            ),
            "node04": NodeConfig(
                host="operator@edge-b.example.internal",
                ssh_options=f"-p 2222 -o UserKnownHostsFile={kh_b.as_posix()}",
                name="node04",
            ),
        },
        tools={"autobench": str(tmp_path / "autobench")},
    )
    runner = build_default_known_hosts_runner(operator)
    result = runner()
    assert result.id == "known_hosts"
    assert result.outcome == "failed"
    assert "node04" in result.summary
    assert "node03" not in result.summary or "node04" in result.summary
    assert "edge-a.example.internal" not in result.summary
    assert "edge-b.example.internal" not in result.summary
    assert "edge-a.example.internal" not in result.remediation
    assert "edge-b.example.internal" not in result.remediation


def test_known_hosts_passes_when_each_node_file_has_entry(tmp_path: Path) -> None:
    key_a = paramiko.RSAKey.generate(1024)
    key_b = paramiko.RSAKey.generate(1024)
    kh_a = tmp_path / "kh-a"
    kh_b = tmp_path / "kh-b"
    kh_a.write_text(
        _host_key_line("edge-a.example.internal", 2222, key_a),
        encoding="utf-8",
    )
    kh_b.write_text(
        _host_key_line("edge-b.example.internal", 2222, key_b),
        encoding="utf-8",
    )
    operator = OperatorConfig(
        operator_email="op@example.com",
        audit_repo=str(tmp_path / "core"),
        nodes={
            "node03": NodeConfig(
                host="operator@edge-a.example.internal",
                ssh_options=f"-p 2222 -o UserKnownHostsFile={kh_a.as_posix()}",
                name="node03",
            ),
            "node04": NodeConfig(
                host="operator@edge-b.example.internal",
                ssh_options=f"-p 2222 -o UserKnownHostsFile={kh_b.as_posix()}",
                name="node04",
            ),
        },
    )
    result = build_default_known_hosts_runner(operator)()
    assert result.outcome == "passed"


def test_failures_never_leak_private_hosts_urls_or_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BB_TOKEN", "token=super-secret-bb")
    private_host = "hde2stl020003.mastercard.int"
    kh = tmp_path / "kh-empty"
    kh.write_text("", encoding="utf-8")
    operator = OperatorConfig(
        operator_email="op@example.com",
        audit_repo=str(tmp_path / "core"),
        nodes={
            "node03": NodeConfig(
                host=f"operator@{private_host}",
                ssh_options=f"-p 2222 -o UserKnownHostsFile={kh.as_posix()}",
                name="node03",
            )
        },
        tools={"autobench": str(tmp_path / "autobench")},
    )
    # Default known_hosts / bitbucket / edge helpers must name labels only.
    kh_result = build_default_known_hosts_runner(operator)()
    assert kh_result.outcome == "failed"
    assert "node03" in kh_result.summary
    assert private_host not in kh_result.summary
    assert private_host not in kh_result.remediation
    assert kh.as_posix() not in kh_result.summary

    bb_result = build_default_bitbucket_write_dry_run_runner(
        tools=["autobench"],
        tool_roots={"autobench": tmp_path / "autobench"},
        core_root=tmp_path / "core",
        git_runner=lambda command, root: 1,
    )()
    assert bb_result.outcome == "failed"
    assert "autobench" in bb_result.summary or "core" in bb_result.summary
    assert "scm.mastercard.int" not in bb_result.summary
    assert "https://" not in bb_result.summary

    ctx = _ctx(
        tmp_path,
        operator=operator,
        known_hosts_runner=lambda: _pass("known_hosts"),
        edge_tcp_runner=lambda: _pass("edge_tcp"),
        rsa_auth_runner=lambda node: CheckResult(
            f"rsa_auth:{node}",
            "failed",
            f"passcode=999999 RSA rejected for {node}",
            "Re-enter RSA passcode",
        ),
        bitbucket_write_dry_run_runner=lambda: bb_result,
    )
    results = run_checks(build_readiness_specs(ctx), max_workers=1)
    blob = "\n".join(f"{r.id}|{r.summary}|{r.remediation}" for r in results)
    assert private_host not in blob
    assert "scm.mastercard.int" not in blob
    assert "super-secret-bb" not in blob
    assert "999999" not in blob
    assert "***REDACTED***" in blob


def test_bitbucket_write_dry_run_uses_git_probe_command_write(
    tmp_path: Path,
) -> None:
    seen: list[tuple[tuple[str, ...], str]] = []

    def git_runner(command: list[str], root: Path) -> int:
        seen.append((tuple(command), root.name))
        return 0

    runner = build_default_bitbucket_write_dry_run_runner(
        tools=["autobench"],
        tool_roots={"autobench": tmp_path / "autobench"},
        core_root=tmp_path / "edge-deploy-core",
        git_runner=git_runner,
    )
    result = runner()
    assert result.outcome == "passed"
    expected = tuple(git_probe_command("bitbucket", "write"))
    assert expected[0:3] == ("git", "push", "--dry-run")
    assert "--force" in expected
    assert all(cmd == expected for cmd, _root in seen)
    assert {root for _cmd, root in seen} == {"autobench", "edge-deploy-core"}
    assert not any("origin" in cmd for cmd, _root in seen)
    # Never a non-dry-run push.
    assert all("--dry-run" in cmd for cmd, _root in seen)


def test_no_github_write_probe_in_default_bitbucket_write_runner(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def git_runner(command: list[str], root: Path) -> int:
        commands.append(list(command))
        return 0

    build_default_bitbucket_write_dry_run_runner(
        tools=["autobench"],
        tool_roots={"autobench": tmp_path / "autobench"},
        core_root=tmp_path / "core",
        git_runner=git_runner,
    )()
    github_write = git_probe_command("origin", "write")
    assert github_write not in commands


def test_operator_config_requires_complete_inputs(tmp_path: Path) -> None:
    incomplete = OperatorConfig(operator_email="", audit_repo="", nodes={}, tools={})
    ctx = _ctx(tmp_path, operator=incomplete)
    result = next(s for s in build_readiness_specs(ctx) if s.id == "operator_config").run()
    assert result.outcome == "failed"
    assert "operator" in result.summary.lower() or "config" in result.summary.lower()


def test_operator_config_requires_tool_profiles(tmp_path: Path) -> None:
    tool_root = tmp_path / "autobench"
    tool_root.mkdir()
    # Missing edge_deploy.yaml
    ctx = _ctx(tmp_path)
    result = next(s for s in build_readiness_specs(ctx) if s.id == "operator_config").run()
    assert result.outcome == "failed"
    assert "autobench" in result.summary


def test_local_check_reports_tool_label_on_failure(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, local_check_runner=lambda root: 7)
    result = next(
        s for s in build_readiness_specs(ctx) if s.id == "local_check:autobench"
    ).run()
    assert result.outcome == "failed"
    assert "autobench" in result.summary
    assert result.id == "local_check:autobench"


def test_rsa_transport_registry_reuses_same_driver(tmp_path: Path) -> None:
    class FakeDriver:
        def __init__(self) -> None:
            self.auth_calls = 0
            self.smoke_calls = 0
            self.stopped = False

        def ensure(self) -> None:
            self.auth_calls += 1

        def smoke(self) -> None:
            self.smoke_calls += 1

        def stop_session(self) -> None:
            self.stopped = True

    drivers = {"node03": FakeDriver()}
    registry = NodeSessionRegistry()

    def transport_factory(node: NodeConfig):
        return drivers[node.name]

    def authenticate(driver, node_name: str):
        del node_name
        driver.ensure()
        return driver  # builder alone registers

    def run_smoke(driver, *, node_label: str):
        driver.smoke()
        return type("R", (), {"passed": True, "checks": []})()

    rsa_runner, smoke_runner = make_rsa_and_transport_runners(
        _operator(tmp_path),
        registry=registry,
        transport_factory=transport_factory,
        authenticate=authenticate,
        smoke=run_smoke,
    )
    assert rsa_runner("node03").outcome == "passed"
    assert smoke_runner("node03").outcome == "passed"
    assert drivers["node03"].auth_calls == 1
    assert drivers["node03"].smoke_calls == 1
    assert registry.get("node03") is drivers["node03"]


def test_conditional_kerberos_only_when_require_deep_smoke(tmp_path: Path) -> None:
    kerberos_calls: list[str] = []
    ctx = _ctx(
        tmp_path,
        require_deep_smoke=True,
        kerberos_runner=lambda node: kerberos_calls.append(node)
        or CheckResult(f"kerberos:{node}", "passed", "Kerberos ticket ok", ""),
    )
    specs = build_readiness_specs(ctx)
    kerberos = next(s for s in specs if s.id == "kerberos:node03")
    assert kerberos.depends_on == ("rsa_auth:node03",)
    assert kerberos.run().outcome == "passed"
    assert kerberos_calls == ["node03"]

    shallow = build_readiness_specs(_ctx(tmp_path, require_deep_smoke=False))
    assert not any(s.id.startswith("kerberos:") for s in shallow)


# ---------------------------------------------------------------------------
# Review-fix coverage (host-safe errors, defaults, registry ownership)
# ---------------------------------------------------------------------------


_LEAK = "passcode=999999 https://scm.mastercard.int/x.git host=hde2stl020003.mastercard.int"


def test_execute_failure_summary_uses_exception_type_only() -> None:
    def boom() -> CheckResult:
        raise RuntimeError(_LEAK)

    results = run_checks([CheckSpec("leaky", (), boom)], max_workers=1)
    assert results[0].outcome == "failed"
    assert "RuntimeError" in results[0].summary
    assert "999999" not in results[0].summary
    assert "scm.mastercard.int" not in results[0].summary
    assert "hde2stl020003" not in results[0].summary
    assert _LEAK not in results[0].summary
    assert _LEAK not in results[0].remediation


def test_rsa_transport_kerberos_failures_never_interpolate_exc_text(
    tmp_path: Path,
) -> None:
    class FakeDriver:
        def stop_session(self) -> None:
            return None

    registry = NodeSessionRegistry()

    def transport_factory(node: NodeConfig):
        del node
        return FakeDriver()

    def authenticate(driver, node_name: str):
        del driver, node_name
        raise ConnectionError(_LEAK)

    def smoke(driver, *, node_label: str):
        del driver, node_label
        raise TimeoutError(_LEAK)

    rsa_runner, smoke_runner = make_rsa_and_transport_runners(
        _operator(tmp_path),
        registry=registry,
        transport_factory=transport_factory,
        authenticate=authenticate,
        smoke=smoke,
    )
    rsa = rsa_runner("node03")
    assert rsa.outcome == "failed"
    assert "ConnectionError" in rsa.summary
    assert "999999" not in rsa.summary
    assert "scm.mastercard.int" not in rsa.summary
    assert registry.get("node03") is None

    # Pre-register so transport/kerberos paths exercise exception handling.
    registry.put("node03", FakeDriver())
    smoke_result = smoke_runner("node03")
    assert smoke_result.outcome == "failed"
    assert "TimeoutError" in smoke_result.summary
    assert "999999" not in smoke_result.summary
    assert "scm.mastercard.int" not in smoke_result.summary

    kerberos = build_default_kerberos_runner(
        registry,
        ensure_fn=lambda driver, label, **kwargs: (_ for _ in ()).throw(
            RuntimeError(_LEAK)
        ),
    )
    kresult = kerberos("node03")
    assert kresult.outcome == "failed"
    assert "RuntimeError" in kresult.summary
    assert "999999" not in kresult.summary
    assert "scm.mastercard.int" not in kresult.summary
    assert "hde2stl020003" not in kresult.summary


def test_bitbucket_runners_fail_when_selected_tool_root_missing(tmp_path: Path) -> None:
    def git_runner(command: list[str], root: Path) -> int:
        del command, root
        return 0

    read = build_default_bitbucket_read_runner(
        tools=["autobench", "robocop"],
        tool_roots={"autobench": tmp_path / "autobench"},
        core_root=tmp_path / "core",
        git_runner=git_runner,
    )()
    write = build_default_bitbucket_write_dry_run_runner(
        tools=["autobench", "robocop"],
        tool_roots={"autobench": tmp_path / "autobench"},
        core_root=tmp_path / "core",
        git_runner=git_runner,
    )()
    assert read.outcome == "failed"
    assert write.outcome == "failed"
    assert "robocop" in read.summary
    assert "robocop" in write.summary
    assert "https://" not in read.summary
    assert "https://" not in write.summary


def test_whitespace_only_bb_token_fails(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BB_TOKEN", "   \t  ")
    result = next(
        s for s in build_readiness_specs(_ctx(tmp_path)) if s.id == "bb_token_present"
    ).run()
    assert result.outcome == "failed"
    assert "BB_TOKEN" in result.remediation


def test_default_gh_auth_runner_uses_injected_runner() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> int:
        calls.append(command)
        return 1

    result = build_default_gh_auth_runner(runner=runner)()
    assert result.outcome == "failed"
    assert calls == [["gh", "auth", "status"]]
    assert "gh" in result.summary.lower()


def test_default_github_read_runner_uses_git_probe_read(tmp_path: Path) -> None:
    seen: list[tuple[tuple[str, ...], str]] = []

    def git_runner(command: list[str], root: Path) -> int:
        seen.append((tuple(command), root.name))
        return 0

    result = build_default_github_read_runner(
        tools=["autobench"],
        tool_roots={"autobench": tmp_path / "autobench"},
        git_runner=git_runner,
    )()
    assert result.outcome == "passed"
    assert seen == [(tuple(git_probe_command("origin", "read")), "autobench")]


def test_default_tool_clean_main_runner_requires_exact_match(tmp_path: Path) -> None:
    outputs = {
        ("git", "branch", "--show-current"): "main\n",
        ("git", "status", "--porcelain", "--untracked-files=all"): "",
        ("git", "rev-parse", "HEAD"): "a" * 40 + "\n",
        ("git", "rev-parse", "refs/remotes/origin/main"): "b" * 40 + "\n",
    }

    def git_runner(command: list[str], root: Path) -> int:
        del root
        return 0

    def output_runner(command: list[str], root: Path) -> str:
        del root
        return outputs[tuple(command)]

    result = build_default_tool_clean_main_runner(
        tools=["autobench"],
        tool_roots={"autobench": tmp_path / "autobench"},
        git_runner=git_runner,
        output_runner=output_runner,
    )()
    assert result.outcome == "failed"
    assert "autobench" in result.summary
    assert "https://" not in result.summary


def test_default_bitbucket_read_and_write_use_probe_commands(tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []

    def git_runner(command: list[str], root: Path) -> int:
        del root
        seen.append(tuple(command))
        return 0

    assert (
        build_default_bitbucket_read_runner(
            tools=["autobench"],
            tool_roots={"autobench": tmp_path / "ab"},
            core_root=tmp_path / "core",
            git_runner=git_runner,
        )().outcome
        == "passed"
    )
    assert (
        build_default_bitbucket_write_dry_run_runner(
            tools=["autobench"],
            tool_roots={"autobench": tmp_path / "ab"},
            core_root=tmp_path / "core",
            git_runner=git_runner,
        )().outcome
        == "passed"
    )
    assert tuple(git_probe_command("bitbucket", "read")) in seen
    assert tuple(git_probe_command("bitbucket", "write")) in seen
    assert not any(cmd == tuple(git_probe_command("origin", "write")) for cmd in seen)


def test_default_edge_tcp_runner_names_nodes_only(tmp_path: Path) -> None:
    private = "hde2stl020003.mastercard.int"
    operator = OperatorConfig(
        operator_email="op@example.com",
        audit_repo=str(tmp_path / "core"),
        nodes={
            "node03": NodeConfig(
                host=f"operator@{private}",
                ssh_options="-p 2222",
                name="node03",
            )
        },
    )

    def connect(address: tuple[str, int], timeout: float) -> object:
        del timeout
        raise TimeoutError(f"timed out connecting to {address[0]}")

    result = build_default_edge_tcp_runner(operator, connect=connect, timeout=1.0)()
    assert result.outcome == "failed"
    assert "node03" in result.summary
    assert private not in result.summary
    assert private not in result.remediation


def test_default_audit_runner_probe_without_url_leak(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    outbox = tmp_path / "app" / "edge-deploy" / "outbox"
    outbox.mkdir(parents=True)
    # empty outbox ok
    calls: list[tuple[tuple[str, ...], str]] = []

    def git_runner(command: list[str], root: Path) -> int:
        calls.append((tuple(command), root.name))
        if command[:3] == ["git", "remote", "get-url"]:
            return 0
        if command[:2] == ["git", "ls-remote"]:
            return 0
        return 1

    result = build_default_audit_runner(
        core_root=tmp_path / "core",
        git_runner=git_runner,
        outbox=outbox,
    )()
    assert result.outcome == "passed"
    assert any(cmd[:3] == ("git", "remote", "get-url") for cmd, _ in calls)
    assert any(
        cmd[:2] == ("git", "ls-remote") and "release-log" in " ".join(cmd)
        for cmd, _ in calls
    )
    assert "https://" not in result.summary
    assert "scm." not in result.summary

    (outbox / "pending.json").write_text("{}", encoding="utf-8")
    dirty = build_default_audit_runner(
        core_root=tmp_path / "core",
        git_runner=git_runner,
        outbox=outbox,
    )()
    assert dirty.outcome == "failed"
    assert "outbox" in dirty.summary.lower() or "unsynchronized" in dirty.summary.lower()
    assert str(outbox) not in dirty.summary


def test_default_audit_runner_remote_or_lsremote_failure_is_label_safe(
    tmp_path: Path,
) -> None:
    def git_runner(command: list[str], root: Path) -> int:
        del root
        if command[:3] == ["git", "remote", "get-url"]:
            return 1
        return 0

    result = build_default_audit_runner(
        core_root=tmp_path / "core",
        git_runner=git_runner,
        outbox=tmp_path / "empty-outbox",
    )()
    assert result.outcome == "failed"
    assert "bitbucket" in result.summary.lower() or "remote" in result.summary.lower()
    assert "https://" not in result.summary
    assert "mastercard" not in result.summary


def test_default_kerberos_uses_registry_session_before_smoke(tmp_path: Path) -> None:
    class FakeDriver:
        def __init__(self) -> None:
            self.order: list[str] = []

        def ensure(self) -> None:
            self.order.append("rsa")

        def kerberos(self) -> None:
            self.order.append("kerberos")

        def smoke(self) -> None:
            self.order.append("smoke")

    driver = FakeDriver()
    registry = NodeSessionRegistry()

    def authenticate(d, node_name: str):
        del node_name
        d.ensure()
        return d

    def smoke(d, *, node_label: str):
        del node_label
        d.smoke()
        return type("R", (), {"passed": True, "checks": []})()

    def ensure_fn(d, label, **kwargs):
        del label, kwargs
        d.kerberos()
        return ReportCheck("kerberos", True, "Existing Kerberos ticket")

    rsa_runner, smoke_runner = make_rsa_and_transport_runners(
        _operator(tmp_path),
        registry=registry,
        transport_factory=lambda node: driver,
        authenticate=authenticate,
        smoke=smoke,
    )
    kerberos_runner = build_default_kerberos_runner(registry, ensure_fn=ensure_fn)
    assert rsa_runner("node03").outcome == "passed"
    assert kerberos_runner("node03").outcome == "passed"
    assert smoke_runner("node03").outcome == "passed"
    assert driver.order == ["rsa", "kerberos", "smoke"]
    assert registry.get("node03") is driver


def test_auth_failure_cleans_up_driver_and_leaves_no_stale_registry(
    tmp_path: Path,
) -> None:
    class FakeDriver:
        def __init__(self) -> None:
            self.stopped = False

        def stop_session(self) -> None:
            self.stopped = True

    driver = FakeDriver()
    registry = NodeSessionRegistry()
    # Stale entry that must be cleared on failure.
    registry.put("node03", FakeDriver())

    def authenticate(d, node_name: str):
        del d, node_name
        raise RuntimeError("passcode=123456 rejected at host=edge.example")

    rsa_runner, _smoke = make_rsa_and_transport_runners(
        _operator(tmp_path),
        registry=registry,
        transport_factory=lambda node: driver,
        authenticate=authenticate,
    )
    result = rsa_runner("node03")
    assert result.outcome == "failed"
    assert "RuntimeError" in result.summary
    assert "123456" not in result.summary
    assert "edge.example" not in result.summary
    assert registry.get("node03") is None
    assert driver.stopped is True
