"""End-to-end onboarding integration coverage via ``edge_deploy.cli.main``.

Uses temporary real filesystem/git checkouts plus injected command/network/auth
adapters. Never contacts real GitHub, Bitbucket, or Edge.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import paramiko
import pytest

import edge_deploy.onboarding.runner as runner_mod
import edge_deploy.posture as posture_mod
from edge_console import collect_runs
from edge_deploy.cli import main
from edge_deploy.config import default_operator_config_path
from edge_deploy.ledger import RunLedger, engine_identity, is_training_ledger
from edge_deploy.onboarding.checks import CheckResult, NodeSessionRegistry
from edge_deploy.onboarding.config_import import load_private_onboarding_source
from edge_deploy.onboarding.manifest import (
    CORE_GITHUB_URL,
    TOOL_MANIFESTS,
    approved_engine_tag,
)
from edge_deploy.onboarding.repositories import (
    ProvisionResult,
    bootstrap_core_root,
    fingerprint_remote_url,
)
from edge_deploy.onboarding.state import OnboardingState, default_state_path
from edge_deploy.phases import deploy as phases_deploy
from edge_deploy.phases import publish as phases_publish
from edge_deploy.phases import tag as phases_tag
from edge_deploy.phases import verify as phases_verify

_BASELINE_OPERATOR_CONFIG = Path.home() / ".config" / "edge-deploy" / "config.yaml"

_PRIVATE_HOST = "edge-node-03.example"
_BB_CORE = "https://bitbucket.example/core.git"
_BB_AB = "https://bitbucket.example/ab.git"
_BB_RC = "https://bitbucket.example/rc.git"
_SECRET = "token=should-be-redacted-in-reports"
_FORBIDDEN_SNIPPETS = (
    "should-be-redacted-in-reports",
    "bitbucket.example",
    _PRIVATE_HOST,
    "op@example.com",
    "operator@",
)


def _host_key_line(hostname: str, port: int, key: paramiko.PKey) -> str:
    lookup = hostname if port == 22 else f"[{hostname}]:{port}"
    return f"{lookup} {key.get_name()} {key.get_base64()}\n"


def _git(cwd: Path, *args: str) -> str:
    # Isolate from controller/cloud insteadOf credential rewriting so origin
    # URLs stay exactly the manifest values under test.
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "onboard-it",
            "GIT_AUTHOR_EMAIL": "onboard-it@example.com",
            "GIT_COMMITTER_NAME": "onboard-it",
            "GIT_COMMITTER_EMAIL": "onboard-it@example.com",
        }
    )
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return completed.stdout


def _write_tool_tree(dest: Path, tool: str, *, deep: bool = False) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    deep_cmds = '["kinit -l 1h"]' if deep else "[]"
    (dest / "edge_deploy.yaml").write_text(
        "\n".join(
            [
                f"tool: {tool}",
                f"github_url: {TOOL_MANIFESTS[tool].github_url}",
                f"bitbucket_url: https://bitbucket.example/{tool}.git",
                "smoke:",
                "  standard: []",
                f"  deep: {deep_cmds}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (dest / "tools" / "dev").mkdir(parents=True, exist_ok=True)
    (dest / "tools" / "dev" / "local_check.ps1").write_text("# ok\n", encoding="utf-8")
    (dest / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                f'name = "{tool}"',
                f'dependencies = ["edge-deploy-core @ git+https://example/@{approved_engine_tag()}"]',
                "",
                "[project.optional-dependencies]",
                'dev = ["pytest"]',
                'release = ["build"]',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _init_real_git_checkout(dest: Path, *, origin_url: str, tool: str, deep: bool) -> None:
    """Create a real git checkout whose origin matches the tool manifest URL."""
    if dest.exists():
        raise AssertionError(f"clone dest already exists: {dest}")
    dest.mkdir(parents=True)
    _git(dest, "init")
    _git(dest, "checkout", "-b", "main")
    _write_tool_tree(dest, tool, deep=deep)
    _git(dest, "add", "-A")
    _git(dest, "commit", "-m", f"seed {tool}")
    _git(dest, "remote", "add", "origin", origin_url)


class RepoCommandRecorder:
    """Injectable ``default_runner``: real git on disk, no network clone/pip."""

    def __init__(self, *, deep_tools: set[str] | None = None) -> None:
        self.deep_tools = deep_tools or set()
        self.clone_calls: list[list[str]] = []
        self.remote_add_calls: list[tuple[str, list[str]]] = []
        self.install_calls: list[str] = []
        self.push_calls: list[list[str]] = []
        self.all_commands: list[tuple[str, list[str]]] = []

    def __call__(self, root: Path):
        root = Path(root)

        def run(args: list[str] | tuple[str, ...]) -> str:
            command = list(args)
            self.all_commands.append((str(root), command))
            if command[:2] == ["git", "clone"]:
                self.clone_calls.append(command)
                url = command[-2]
                dest = Path(command[-1])
                assert "edge-deploy-core" not in url, "core must never be cloned"
                if "autobench" in url:
                    tool = "autobench"
                elif "robocop" in url:
                    tool = "robocop"
                else:
                    raise AssertionError(f"unexpected clone url: {url}")
                _init_real_git_checkout(
                    dest,
                    origin_url=url,
                    tool=tool,
                    deep=tool in self.deep_tools,
                )
                return ""
            if len(command) >= 3 and command[1] == "-m" and command[2] == "pip":
                self.install_calls.append(str(root))
                return ""
            if command[:2] == ["git", "push"]:
                self.push_calls.append(command)
                if "--dry-run" not in command:
                    raise RuntimeError("non-dry-run git push is forbidden in onboarding")
                return ""
            if command[:3] == ["git", "remote", "add"]:
                self.remote_add_calls.append((str(root), command))
            # Prefer real git for remotes/status/rev-parse against the temp checkout.
            try:
                return _git(root, *command[1:])
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"git failed (exit {exc.returncode})") from exc

        return run


class OnboardHarness:
    """Shared zero-state fixture wiring for CLI onboarding integration tests."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        deep_tools: set[str] | None = None,
        tools_for_session: bool = False,
    ) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.app = tmp_path / "app"
        self.root = tmp_path / "edge-deploy"
        self.known_hosts = tmp_path / "known_hosts"
        self.private = tmp_path / "private.yaml"
        self.recorder = RepoCommandRecorder(deep_tools=deep_tools)
        self.console_launches: list[list[Path]] = []
        self.stage_events: list[str] = []
        self.session_order: list[str] = []
        self.git_probes: list[list[str]] = []
        self.ack_messages: list[str] = []
        self._readiness_failures_remaining = 0
        self._baseline_existed = _BASELINE_OPERATOR_CONFIG.is_file()
        self._baseline_bytes = (
            _BASELINE_OPERATOR_CONFIG.read_bytes() if self._baseline_existed else None
        )

        monkeypatch.setenv("APPDATA", str(self.app))
        monkeypatch.setenv("BB_TOKEN", _SECRET)

        key = paramiko.RSAKey.generate(1024)
        self.known_hosts.write_text(
            _host_key_line(_PRIVATE_HOST, 2222, key),
            encoding="utf-8",
        )
        self._write_private()

        monkeypatch.setattr(
            "edge_deploy.onboarding.runner._platform_name", lambda: "win32"
        )
        monkeypatch.setattr(
            "edge_deploy.onboarding.runner._command_available", lambda _n: True
        )
        monkeypatch.setattr(
            "edge_deploy.onboarding.runner._powershell_compatible", lambda: True
        )
        monkeypatch.setattr(
            "edge_deploy.onboarding.repositories.default_runner", self.recorder
        )
        monkeypatch.setattr(
            "edge_deploy.onboarding.runner.validate_bootstrap_core",
            self._fake_validate_core,
        )
        monkeypatch.setattr(
            "edge_deploy.onboarding.runner.inspect_bootstrap_core",
            self._fake_inspect_core,
        )
        monkeypatch.setattr(
            "edge_deploy.onboarding.runner._launch_console",
            lambda roots, github_write_roots=None: self.console_launches.append(list(roots)),
        )
        monkeypatch.setattr(
            "edge_deploy.onboarding.runner._training_acknowledge",
            lambda message: self.ack_messages.append(message),
        )
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: "")
        monkeypatch.setattr(
            "edge_deploy.onboarding.runner._local_check_runner", lambda _root: 0
        )
        monkeypatch.setattr(
            "edge_deploy.onboarding.runner.default_git_runner",
            self._recording_git_probe,
        )

        # Network-facing readiness builders stay offline.
        for name, check_id in (
            ("build_default_gh_auth_runner", "gh_auth"),
            ("build_default_github_read_runner", "github_read"),
            ("build_default_tool_clean_main_runner", "tool_clean_main"),
        ):
            monkeypatch.setattr(
                f"edge_deploy.onboarding.runner.{name}",
                lambda check_id=check_id, **k: (
                    lambda: CheckResult(check_id, "passed", "ok", "")
                ),
            )
        monkeypatch.setattr(
            "edge_deploy.onboarding.runner.build_default_edge_tcp_runner",
            self._edge_tcp_factory,
        )
        # known_hosts uses the real installed operator config + fixture file.

        self._wrap_pin_install_ordering()
        self._guard_production_executors()
        self._guard_posture_switch()

        if tools_for_session:
            self._wire_live_session_runners()
        else:
            self._wire_stub_session_runners()

    def _write_private(self, *, email: str = "op@example.com") -> None:
        self.private.write_text(
            "\n".join(
                [
                    f"operator_email: {email}",
                    f"checkout_root: {self.root.as_posix()}",
                    "nodes:",
                    "  node03:",
                    f"    host: operator@{_PRIVATE_HOST}",
                    (
                        "    ssh_options: -p 2222 "
                        f"-o UserKnownHostsFile={self.known_hosts.as_posix()}"
                    ),
                    "    session: edge-node03",
                    "    transport: ssh",
                    "bitbucket_remotes:",
                    f"  core: {_BB_CORE}",
                    f"  autobench: {_BB_AB}",
                    f"  robocop: {_BB_RC}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _core_evidence(self) -> dict:
        return {
            "head_sha": "a" * 40,
            "exact_tag": approved_engine_tag(),
            "origin_fingerprint": fingerprint_remote_url(CORE_GITHUB_URL),
            "bitbucket_fingerprint": fingerprint_remote_url(_BB_CORE),
            "dirty": False,
            "evidence_fingerprint": "c" * 64,
        }

    def _fake_validate_core(self, *args, **kwargs):
        del args, kwargs
        return ProvisionResult(
            tool_id="core",
            path=bootstrap_core_root(),
            action="validated",
            message="validated bootstrap core",
        )

    def _fake_inspect_core(self, *args, **kwargs):
        del args, kwargs
        return self._core_evidence()

    def _recording_git_probe(self, command: list[str], repo_root: Path) -> int:
        del repo_root
        cmd = list(command)
        self.git_probes.append(cmd)
        if cmd[:2] == ["git", "push"]:
            if "--dry-run" not in cmd:
                raise AssertionError(f"non-dry-run push in readiness: {cmd}")
            return 0
        return 0

    def _edge_tcp_factory(self, operator, **kwargs):
        del kwargs

        def check() -> CheckResult:
            if self._readiness_failures_remaining > 0:
                self._readiness_failures_remaining -= 1
                return CheckResult(
                    "edge_tcp",
                    "failed",
                    "Edge TCP failed for: node03",
                    "Restore Edge VPN (both-vpns), then re-run.",
                )
            return CheckResult("edge_tcp", "passed", "Edge TCP ok for all nodes", "")

        del operator
        return check

    def _wrap_pin_install_ordering(self) -> None:
        real_assert = runner_mod.assert_engine_pins_compatible
        real_install = runner_mod.install_tool_dependencies
        real_provision = runner_mod.provision_tool_checkout

        def assert_pins(tool_roots, *, expected_tag):
            self.stage_events.append("assert_pins")
            return real_assert(tool_roots, expected_tag=expected_tag)

        def install(tool_root, **kwargs):
            self.stage_events.append(f"install:{Path(tool_root).name}")
            return real_install(tool_root, **kwargs)

        def provision(dest, manifest, **kwargs):
            self.stage_events.append(f"provision:{manifest.tool_id}")
            return real_provision(dest, manifest, **kwargs)

        self.monkeypatch.setattr(runner_mod, "assert_engine_pins_compatible", assert_pins)
        self.monkeypatch.setattr(runner_mod, "install_tool_dependencies", install)
        self.monkeypatch.setattr(runner_mod, "provision_tool_checkout", provision)

    def _guard_production_executors(self) -> None:
        def boom(name: str):
            def _inner(*_a, **_k):
                raise AssertionError(f"production executor invoked: {name}")

            return _inner

        self.monkeypatch.setattr(
            phases_publish, "run_publish_phase", boom("phases.publish.run_publish_phase")
        )
        self.monkeypatch.setattr(phases_deploy, "run_deploy", boom("phases.deploy.run_deploy"))
        self.monkeypatch.setattr(
            phases_tag, "_cmd_tag_github", boom("phases.tag._cmd_tag_github")
        )
        self.monkeypatch.setattr(
            phases_tag, "_cmd_tag_bitbucket", boom("phases.tag._cmd_tag_bitbucket")
        )
        self.monkeypatch.setattr(
            phases_verify, "ensure_verified", boom("phases.verify.ensure_verified")
        )

    def _guard_posture_switch(self) -> None:
        for name in dir(posture_mod):
            if "switch" in name.lower() or name.startswith("set_"):
                self.monkeypatch.setattr(
                    posture_mod,
                    name,
                    lambda *a, **k: (_ for _ in ()).throw(
                        AssertionError(f"posture mutation via {name}")
                    ),
                )

    def _wire_stub_session_runners(self) -> None:
        def factory(operator):
            del operator
            registry = NodeSessionRegistry()
            return (
                (
                    lambda node: CheckResult(
                        f"rsa_auth:{node}", "passed", "RSA authenticated", ""
                    )
                ),
                (
                    lambda node: CheckResult(
                        f"transport_smoke:{node}", "passed", "transport smoke ok", ""
                    )
                ),
                (
                    lambda node: CheckResult(
                        f"kerberos:{node}", "passed", "Kerberos ticket ok", ""
                    )
                ),
                registry,
            )

        self.monkeypatch.setattr(
            "edge_deploy.onboarding.runner._session_runners_for_readiness",
            factory,
        )

    def _wire_live_session_runners(self) -> None:
        """Keep ``_session_runners_for_readiness``; inject lower transport/auth seams."""
        drivers: dict[str, object] = {}
        harness = self

        class FakeDriver:
            def __init__(self, node):
                self.node = node
                self.stopped = False

            def stop_session(self):
                self.stopped = True
                # Record on the harness list (nested-class ``self`` is the driver).
                harness.session_order.append(
                    f"stop:{getattr(self.node, 'name', '?')}"
                )

        def transport_factory(node):
            harness.session_order.append(f"transport_factory:{node.name}")
            return FakeDriver(node)

        def authenticate(driver, node_name):
            harness.session_order.append(f"auth:{node_name}")
            drivers[node_name] = driver
            return driver

        def smoke(driver, *, node_label):
            harness.session_order.append(f"smoke:{node_label}")
            assert drivers[node_label] is driver
            return SimpleNamespace(passed=True)

        def ensure_kerb(driver, node_name):
            harness.session_order.append(f"kerberos:{node_name}")
            assert drivers[node_name] is driver
            return SimpleNamespace(passed=True)

        self.monkeypatch.setattr(
            "edge_deploy.onboarding.runner._transport_factory_for_node",
            transport_factory,
        )
        self.monkeypatch.setattr(
            "edge_deploy.onboarding.runner._authenticate_node",
            authenticate,
        )
        self.monkeypatch.setattr(
            "edge_deploy.onboarding.runner._run_transport_smoke",
            smoke,
        )
        self.monkeypatch.setattr(
            "edge_deploy.onboarding.runner._ensure_kerberos",
            ensure_kerb,
        )

    def onboard(self, *tool_flags: str, extra: list[str] | None = None) -> int:
        argv = ["onboard", "--config", str(self.private), "--root", str(self.root)]
        for tool in tool_flags:
            argv.extend(["--tool", tool])
        if extra:
            argv.extend(extra)
        return main(argv)

    def state(self) -> OnboardingState:
        return OnboardingState.load(default_state_path())

    def report_path(self) -> Path:
        return self.app / "edge-deploy" / "onboarding-report.json"

    def training_root(self, tool: str) -> Path:
        return self.app / "edge-deploy" / "training" / tool

    def assert_no_secrets(self, text: str) -> None:
        for snippet in _FORBIDDEN_SNIPPETS:
            assert snippet not in text, f"leaked {snippet!r}"

    def assert_config_under_test_appdata(self) -> None:
        installed = default_operator_config_path()
        assert installed == self.app / "edge-deploy" / "config.yaml"
        assert installed.is_file()
        if self._baseline_existed:
            assert _BASELINE_OPERATOR_CONFIG.read_bytes() == self._baseline_bytes
        else:
            assert not _BASELINE_OPERATOR_CONFIG.is_file()

    def assert_complete(self, tools: list[str]) -> None:
        state = self.state()
        assert state.data["tools"] == tools
        for stage in (
            "prerequisites",
            "config",
            "repositories",
            "readiness",
            "practice",
            "complete",
        ):
            assert state.data["stages"][stage]["outcome"] == "passed", stage
        report = json.loads(self.report_path().read_text(encoding="utf-8"))
        assert report["tools"] == tools
        assert report["practice_completed"] is True
        assert report["config_fingerprint"] == state.data["config_fingerprint"]
        imported = load_private_onboarding_source(self.private)
        assert report["config_fingerprint"] == imported.fingerprint
        self.assert_no_secrets(self.report_path().read_text(encoding="utf-8"))
        self.assert_no_secrets(default_state_path().read_text(encoding="utf-8"))
        self.assert_config_under_test_appdata()


def test_empty_zero_state_autobench_via_cli(tmp_path: Path, monkeypatch) -> None:
    h = OnboardHarness(tmp_path, monkeypatch)
    assert h.onboard("autobench") == 0
    h.assert_complete(["autobench"])
    assert len(h.recorder.clone_calls) == 1
    assert "autobench" in h.recorder.clone_calls[0][-2]
    assert bootstrap_core_root() == Path(engine_identity()["package_dir"]).resolve().parent
    assert not any("edge-deploy-core" in " ".join(c) for c in h.recorder.clone_calls)


def test_empty_zero_state_dispatch_alias_via_cli(tmp_path: Path, monkeypatch) -> None:
    h = OnboardHarness(tmp_path, monkeypatch)
    assert h.onboard("dispatch") == 0
    h.assert_complete(["robocop"])
    assert (h.root / "robocop" / "edge_deploy.yaml").is_file()
    assert not (h.root / "autobench").exists()


def test_empty_both_tools_complete_pin_order_training_redaction(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    h = OnboardHarness(tmp_path, monkeypatch, deep_tools={"autobench"})
    assert h.onboard("autobench", "dispatch") == 0
    h.assert_complete(["autobench", "robocop"])

    # Exact pin-before-install ordering across all selected repos.
    assert h.stage_events.count("assert_pins") == 1
    pin_idx = h.stage_events.index("assert_pins")
    provision_idxs = [
        i for i, e in enumerate(h.stage_events) if e.startswith("provision:")
    ]
    install_idxs = [i for i, e in enumerate(h.stage_events) if e.startswith("install:")]
    assert provision_idxs
    assert install_idxs
    assert max(provision_idxs) < pin_idx < min(install_idxs)
    assert {e.split(":", 1)[1] for e in h.stage_events if e.startswith("provision:")} == {
        "autobench",
        "robocop",
    }

    # Training ledgers under APPDATA, outside real roots, console-readable.
    for tool in ("autobench", "robocop"):
        runs = h.training_root(tool) / "edge-deploy" / "runs"
        assert runs.is_dir()
        states = list(runs.glob("*/state.json"))
        assert states
        real_runs = h.root / tool / "edge-deploy" / "runs"
        assert not real_runs.exists() or not list(real_runs.glob("*/state.json"))
        collected = collect_runs(runs)
        assert collected
        ledgers = [RunLedger.load(p.parent) for p in runs.glob("*/state.json")]
        assert ledgers
        assert all(is_training_ledger(ledger) for ledger in ledgers)
        assert any(
            run["state"].get("kind") == "training"
            or run["state"].get("training") is True
            for run in collected
        )

    assert h.console_launches and len(h.console_launches[0]) == 2
    assert any("Do not change the workstation posture" in msg for msg in h.ack_messages)

    # Only dry-run write path observed in readiness probes.
    pushes = [c for c in h.git_probes if c[:2] == ["git", "push"]]
    assert pushes
    assert all("--dry-run" in c for c in pushes)

    out = capsys.readouterr().out + capsys.readouterr().err
    h.assert_no_secrets(out)
    assert "py -m edge_deploy release --guided --tool autobench" in out
    assert "py -m edge_deploy release --guided --tool robocop" in out


def test_one_session_auth_conditional_kerberos_smoke(
    tmp_path: Path, monkeypatch
) -> None:
    h = OnboardHarness(
        tmp_path, monkeypatch, deep_tools={"autobench"}, tools_for_session=True
    )
    assert h.onboard("autobench") == 0
    assert h.session_order.count("auth:node03") == 1
    assert "kerberos:node03" in h.session_order
    auth_i = h.session_order.index("auth:node03")
    kerb_i = h.session_order.index("kerberos:node03")
    smoke_i = h.session_order.index("smoke:node03")
    assert auth_i < kerb_i < smoke_i
    assert "stop:node03" in h.session_order
    assert h.session_order.index("stop:node03") > smoke_i
    readiness = h.state().data["stages"]["readiness"]
    assert any(c["id"] == "kerberos:node03" for c in readiness["checks"])
    assert any(c["id"] == "rsa_auth:node03" for c in readiness["checks"])
    assert any(c["id"] == "transport_smoke:node03" for c in readiness["checks"])


def test_interrupt_and_same_command_resume(tmp_path: Path, monkeypatch) -> None:
    h = OnboardHarness(tmp_path, monkeypatch)
    real_provision = runner_mod.provision_tool_checkout
    calls = {"n": 0}

    def flaky_provision(dest, manifest, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated interrupt during first provision")
        return real_provision(dest, manifest, **kwargs)

    monkeypatch.setattr(runner_mod, "provision_tool_checkout", flaky_provision)
    assert h.onboard("autobench") == 1
    state = h.state()
    assert state.data["stages"]["prerequisites"]["outcome"] == "passed"
    assert state.data["stages"]["config"]["outcome"] == "passed"
    assert state.data["stages"]["repositories"]["outcome"] == "failed"
    assert state.data["stages"]["complete"]["outcome"] == "pending"

    # Resume with the same CLI command after the seam recovers.
    monkeypatch.setattr(runner_mod, "provision_tool_checkout", real_provision)
    # Re-wrap ordering on the restored provision.
    h._wrap_pin_install_ordering()
    assert h.onboard("autobench") == 0
    h.assert_complete(["autobench"])


def test_temporary_readiness_failure_then_success(tmp_path: Path, monkeypatch) -> None:
    h = OnboardHarness(
        tmp_path, monkeypatch, deep_tools={"autobench"}, tools_for_session=True
    )
    h._readiness_failures_remaining = 1
    assert h.onboard("autobench") == 1
    state = h.state()
    assert state.data["stages"]["repositories"]["outcome"] == "passed"
    assert state.data["stages"]["readiness"]["outcome"] == "failed"
    assert state.data["stages"]["practice"]["outcome"] == "pending"
    checks = {c["id"]: c for c in state.data["stages"]["readiness"]["checks"]}
    assert checks["edge_tcp"]["outcome"] == "failed"
    assert checks["rsa_auth:node03"]["outcome"] == "blocked"
    assert checks["kerberos:node03"]["outcome"] == "blocked"
    assert checks["transport_smoke:node03"]["outcome"] == "blocked"
    # Dependents must not have executed while edge_tcp was failed.
    assert "auth:node03" not in h.session_order
    assert "kerberos:node03" not in h.session_order
    assert "smoke:node03" not in h.session_order
    assert "stop:node03" not in h.session_order

    assert h.onboard("autobench") == 0
    h.assert_complete(["autobench"])
    assert "auth:node03" in h.session_order
    assert "kerberos:node03" in h.session_order
    assert "smoke:node03" in h.session_order
    assert "stop:node03" in h.session_order
    assert h.session_order.index("auth:node03") < h.session_order.index(
        "kerberos:node03"
    ) < h.session_order.index("smoke:node03")


def test_second_identical_invocation_zero_duplicates(
    tmp_path: Path, monkeypatch
) -> None:
    h = OnboardHarness(tmp_path, monkeypatch)
    assert h.onboard("autobench", "dispatch") == 0
    clones = len(h.recorder.clone_calls)
    remotes = len(h.recorder.remote_add_calls)
    installs = len(h.recorder.install_calls)
    consoles = len(h.console_launches)
    training_states = {
        tool: sorted(
            p.read_text(encoding="utf-8")
            for p in (h.training_root(tool) / "edge-deploy" / "runs").glob("*/state.json")
        )
        for tool in ("autobench", "robocop")
    }
    assert clones >= 2
    assert remotes >= 2
    assert installs >= 2
    assert consoles == 1

    assert h.onboard("autobench", "dispatch") == 0
    assert len(h.recorder.clone_calls) == clones
    assert len(h.recorder.remote_add_calls) == remotes
    assert len(h.recorder.install_calls) == installs
    assert len(h.console_launches) == consoles
    for tool in ("autobench", "robocop"):
        after = sorted(
            p.read_text(encoding="utf-8")
            for p in (h.training_root(tool) / "edge-deploy" / "runs").glob("*/state.json")
        )
        assert after == training_states[tool]
        assert len(after) == 1


def test_changed_config_tool_root_repo_drift_invalidation(
    tmp_path: Path, monkeypatch
) -> None:
    h = OnboardHarness(tmp_path, monkeypatch)
    assert h.onboard("autobench") == 0
    first_fp = h.state().data["config_fingerprint"]
    first_clones = len(h.recorder.clone_calls)

    # Config fingerprint drift (email change) invalidates config-onward.
    h._write_private(email="other@example.com")
    assert h.onboard("autobench") == 0
    assert h.state().data["config_fingerprint"] != first_fp
    assert h.state().data["config_fingerprint"] == load_private_onboarding_source(
        h.private
    ).fingerprint

    # Tool selection drift: add dispatch/robocop.
    clones_before_tool = len(h.recorder.clone_calls)
    assert h.onboard("autobench", "dispatch") == 0
    assert h.state().data["tools"] == ["autobench", "robocop"]
    assert len(h.recorder.clone_calls) > clones_before_tool

    # Root drift: new checkout root requires fresh clones.
    new_root = tmp_path / "edge-deploy-b"
    clones_before_root = len(h.recorder.clone_calls)
    h.root = new_root
    assert (
        main(
            [
                "onboard",
                "--config",
                str(h.private),
                "--root",
                str(new_root),
                "--tool",
                "autobench",
            ]
        )
        == 0
    )
    assert h.state().data["root"] == str(new_root)
    assert len(h.recorder.clone_calls) > clones_before_root
    assert (new_root / "autobench" / ".git").exists()

    # Repo evidence drift: mutate HEAD in checkout → repositories revalidation invalidates.
    tool_root = new_root / "autobench"
    (tool_root / "drift.txt").write_text("changed\n", encoding="utf-8")
    _git(tool_root, "add", "drift.txt")
    _git(tool_root, "commit", "-m", "drift")
    # Same selection; revalidate should notice evidence drift and re-enter repositories.
    # Existing checkout is reused (no new clone) but stage must leave pending/complete cycle.
    before = h.state().data["stages"]["repositories"].get("inputs", {})
    assert h.onboard("autobench") == 0
    after = h.state().data["stages"]["repositories"].get("inputs", {})
    assert after.get("checkout_evidence", {}).get("autobench", {}).get(
        "head_sha"
    ) != before.get("checkout_evidence", {}).get("autobench", {}).get("head_sha")
    assert len(h.recorder.clone_calls) >= first_clones


def test_check_mode_no_mutation(tmp_path: Path, monkeypatch) -> None:
    h = OnboardHarness(tmp_path, monkeypatch)
    assert h.onboard("autobench") == 0
    clones = len(h.recorder.clone_calls)
    remotes = len(h.recorder.remote_add_calls)
    installs = len(h.recorder.install_calls)
    consoles = len(h.console_launches)
    ack_count = len(h.ack_messages)
    config_path = default_operator_config_path()
    config_bytes = config_path.read_bytes()
    report_bytes = h.report_path().read_bytes()
    training_bytes = {
        p: p.read_bytes()
        for p in (h.training_root("autobench") / "edge-deploy" / "runs").glob("*/state.json")
    }
    repo_head = _git(h.root / "autobench", "rev-parse", "HEAD").strip()
    tool_tree = sorted(
        (p.relative_to(h.root / "autobench").as_posix(), p.read_bytes())
        for p in (h.root / "autobench").rglob("*")
        if p.is_file() and ".git" not in p.parts
    )
    state_before = h.state().data
    practice_before = json.dumps(state_before["stages"]["practice"], sort_keys=True)
    complete_before = json.dumps(state_before["stages"]["complete"], sort_keys=True)
    readiness_before = json.dumps(state_before["stages"]["readiness"], sort_keys=True)

    code = h.onboard("autobench", extra=["--check"])
    assert code == 0

    # Provisioning / install / console / training ack must not re-run.
    assert len(h.recorder.clone_calls) == clones
    assert len(h.recorder.remote_add_calls) == remotes
    assert len(h.recorder.install_calls) == installs
    assert len(h.console_launches) == consoles
    assert len(h.ack_messages) == ack_count
    # Installed operator config, report, training ledgers, and repo bytes unchanged.
    assert config_path.read_bytes() == config_bytes
    assert h.report_path().read_bytes() == report_bytes
    assert {
        p: p.read_bytes()
        for p in (h.training_root("autobench") / "edge-deploy" / "runs").glob("*/state.json")
    } == training_bytes
    assert _git(h.root / "autobench", "rev-parse", "HEAD").strip() == repo_head
    assert (
        sorted(
            (p.relative_to(h.root / "autobench").as_posix(), p.read_bytes())
            for p in (h.root / "autobench").rglob("*")
            if p.is_file() and ".git" not in p.parts
        )
        == tool_tree
    )
    state = h.state()
    assert state.data["stages"]["practice"]["outcome"] == "passed"
    assert state.data["stages"]["complete"]["outcome"] == "passed"
    assert json.dumps(state.data["stages"]["practice"], sort_keys=True) == practice_before
    assert json.dumps(state.data["stages"]["complete"], sort_keys=True) == complete_before
    # Readiness evidence in onboarding state may refresh under --check.
    assert state.data["stages"]["readiness"]["outcome"] == "passed"
    assert "readiness" in state.data["stages"]
    del readiness_before  # may differ; refresh is allowed


def test_restart_bad_config_preserves_evidence(tmp_path: Path, monkeypatch) -> None:
    h = OnboardHarness(tmp_path, monkeypatch)
    assert h.onboard("autobench") == 0
    before = default_state_path().read_bytes()
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "bb_token: leaked\noperator_email: x@y.com\nnodes: {}\n",
        encoding="utf-8",
    )
    code = main(
        [
            "onboard",
            "--config",
            str(bad),
            "--root",
            str(h.root),
            "--tool",
            "autobench",
            "--restart",
            "--yes",
        ]
    )
    assert code == 2
    assert default_state_path().read_bytes() == before
    loaded = OnboardingState.load(default_state_path())
    assert loaded.data["stages"]["complete"]["outcome"] == "passed"
    assert loaded.data["practice"]["completed"] is True
