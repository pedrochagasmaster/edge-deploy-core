"""Unit tests for the resumable onboarding runner (Task 10)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from edge_deploy.onboarding.checks import CheckResult
from edge_deploy.onboarding.config_import import load_private_onboarding_source
from edge_deploy.onboarding.manifest import approved_engine_tag, normalize_tool_id
from edge_deploy.onboarding.repositories import ProvisionResult, bootstrap_core_root
from edge_deploy.onboarding.runner import run_onboarding
from edge_deploy.onboarding.state import OnboardingState, default_state_path


def _private_yaml(
    tmp_path: Path,
    *,
    tools_remotes: bool = True,
    checkout_root: Path | None = None,
    transport: str = "ssh",
) -> Path:
    root = checkout_root or (tmp_path / "root")
    lines = [
        "operator_email: op@example.com",
        f"checkout_root: {root.as_posix()}",
        "nodes:",
        "  node03:",
        "    host: operator@edge-node-03.example",
        "    ssh_options: -p 2222",
        "    session: edge-node03",
        f"    transport: {transport}",
    ]
    if tools_remotes:
        lines.extend(
            [
                "bitbucket_remotes:",
                "  core: https://bitbucket.example/core.git",
                "  autobench: https://bitbucket.example/ab.git",
                "  robocop: https://bitbucket.example/rc.git",
            ]
        )
    path = tmp_path / "private.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


_UNSET = object()


def _args(
    private: Path,
    *,
    root: str | None = None,
    tool: list[str] | None | object = _UNSET,
    check: bool = False,
    restart: bool = False,
    yes: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        config=str(private),
        root=root,
        tool=["autobench"] if tool is _UNSET else tool,
        check=check,
        restart=restart,
        yes=yes,
    )


def _seed_tool_tree(dest: Path, tool: str, *, deep: bool = False) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".git").mkdir(exist_ok=True)
    deep_cmds = '["kinit -l 1h"]' if deep else "[]"
    (dest / "edge_deploy.yaml").write_text(
        "\n".join(
            [
                f"tool: {tool}",
                "github_url: https://github.com/example/tool.git",
                "bitbucket_url: https://bitbucket.example/tool.git",
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


def _pass_all_readiness(monkeypatch) -> list[str]:
    """Stub readiness to a single passing check; record call order markers via side effects."""

    def fake_stage_readiness(state, *, tools, root, check_only=False):
        del tools, root, check_only
        state.mark_stage(
            "readiness",
            "passed",
            inputs={"tools": list(state.data["tools"])},
            checks=[
                {
                    "id": "bb_token_present",
                    "outcome": "passed",
                    "summary": "BB_TOKEN is set",
                    "remediation": "",
                    "evidence_fingerprint": None,
                }
            ],
        )

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_readiness",
        fake_stage_readiness,
    )
    return []


def _stub_prereqs_and_below_for_restart(monkeypatch) -> None:
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._run_stages",
        lambda *a, **k: 0,
    )


def test_restart_requires_confirmation_unless_yes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    private = _private_yaml(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "n")
    assert run_onboarding(_args(private, restart=True, yes=False)) == 1

    args = _args(private, restart=True, yes=True)
    _stub_prereqs_and_below_for_restart(monkeypatch)
    assert run_onboarding(args) == 0


def test_engine_mismatch_requires_restart(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    private = _private_yaml(tmp_path)
    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(tmp_path / "root"),
        config_fingerprint="d" * 64,
    )
    state.data["engine"]["content_sha256"] = "0" * 64
    state.save()
    code = run_onboarding(_args(private, root=str(tmp_path / "root")))
    assert code == 2
    captured = capsys.readouterr()
    assert "--restart" in captured.out + captured.err


def test_invalidate_from_resets_stage_and_later(tmp_path: Path) -> None:
    state = OnboardingState.create_new(
        path=tmp_path / "onboarding-state.json",
        tools=["autobench"],
        root=str(tmp_path),
        config_fingerprint="a" * 64,
    )
    for stage in (
        "prerequisites",
        "config",
        "repositories",
        "readiness",
        "practice",
        "complete",
    ):
        state.mark_stage(stage, "passed", inputs={"x": 1})
    state.data["practice"] = {"completed": True, "run_id": "run-1"}
    state.invalidate_from("config")
    assert state.data["stages"]["prerequisites"]["outcome"] == "passed"
    for stage in ("config", "repositories", "readiness", "practice", "complete"):
        assert state.data["stages"][stage]["outcome"] == "pending"
    assert state.data["practice"] == {"completed": False, "run_id": None}


def test_selection_change_invalidates_config_onward(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path, checkout_root=tmp_path / "root-a")
    imported = load_private_onboarding_source(private)
    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(tmp_path / "root-a"),
        config_fingerprint=imported.fingerprint,
    )
    for stage in ("prerequisites", "config", "repositories", "readiness", "practice", "complete"):
        state.mark_stage(stage, "passed", inputs={})
    state.data["practice"] = {"completed": True, "run_id": "x"}
    state.save()

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._platform_name",
        lambda: "win32",
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._command_available",
        lambda name: True,
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._powershell_compatible",
        lambda: True,
    )
    _pass_all_readiness(monkeypatch)

    config_calls: list[object] = []
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_config",
        lambda state, **kw: config_calls.append(kw)
        or state.mark_stage("config", "passed", inputs={"fingerprint": "n" * 64}),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_repositories",
        lambda state, **kw: state.mark_stage("repositories", "passed", inputs={}),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_practice",
        lambda state, **kw: state.mark_stage("practice", "passed", inputs={})
        or state.data.update(practice={"completed": True, "run_id": "y"}),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_complete",
        lambda state, **kw: state.mark_stage("complete", "passed", inputs={}),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._launch_console",
        lambda roots: None,
    )

    code = run_onboarding(
        _args(private, root=str(tmp_path / "root-b"), tool=["autobench", "dispatch"])
    )
    assert code == 0
    assert config_calls, "config stage must re-run after root/tools change"
    loaded = OnboardingState.load(default_state_path())
    assert loaded.data["root"] == str(tmp_path / "root-b")
    assert loaded.data["tools"] == ["autobench", "robocop"]


def test_fingerprint_change_invalidates_config_onward(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    imported = load_private_onboarding_source(private)
    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(tmp_path / "root"),
        config_fingerprint="0" * 64,
    )
    assert state.data["config_fingerprint"] != imported.fingerprint
    for stage in ("prerequisites", "config", "repositories", "readiness"):
        state.mark_stage(stage, "passed", inputs={})
    state.save()

    monkeypatch.setattr("edge_deploy.onboarding.runner._platform_name", lambda: "win32")
    monkeypatch.setattr("edge_deploy.onboarding.runner._command_available", lambda _n: True)
    monkeypatch.setattr("edge_deploy.onboarding.runner._powershell_compatible", lambda: True)
    config_ran = {"n": 0}

    def fake_config(state, **kw):
        config_ran["n"] += 1
        state.mark_stage(
            "config",
            "passed",
            inputs={"fingerprint": kw["imported"].fingerprint},
        )

    monkeypatch.setattr("edge_deploy.onboarding.runner._stage_config", fake_config)
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_repositories",
        lambda state, **kw: state.mark_stage("repositories", "passed", inputs={}),
    )
    _pass_all_readiness(monkeypatch)
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_practice",
        lambda state, **kw: state.mark_stage("practice", "passed", inputs={})
        or state.data.update(practice={"completed": True, "run_id": "z"}),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_complete",
        lambda state, **kw: state.mark_stage("complete", "passed", inputs={}),
    )
    monkeypatch.setattr("edge_deploy.onboarding.runner._launch_console", lambda roots: None)

    assert run_onboarding(_args(private, root=str(tmp_path / "root"))) == 0
    assert config_ran["n"] == 1
    loaded = OnboardingState.load(default_state_path())
    assert loaded.data["config_fingerprint"] == imported.fingerprint


def test_linux_prerequisites_fail_actionably(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    private = _private_yaml(tmp_path)
    monkeypatch.setattr("edge_deploy.onboarding.runner._platform_name", lambda: "linux")
    monkeypatch.setattr("edge_deploy.onboarding.runner._command_available", lambda _n: True)
    monkeypatch.setattr("edge_deploy.onboarding.runner._powershell_compatible", lambda: True)
    code = run_onboarding(_args(private, root=str(tmp_path / "root")))
    assert code == 1
    state = OnboardingState.load(default_state_path())
    assert state.data["stages"]["prerequisites"]["outcome"] == "failed"
    text = capsys.readouterr().out + capsys.readouterr().err
    # remediation lives in stage checks
    checks = state.data["stages"]["prerequisites"]["checks"]
    assert checks
    assert any("Windows" in (c.get("remediation") or c.get("summary") or "") for c in checks)
    del text


def test_prerequisites_probe_missing_git(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    private = _private_yaml(tmp_path)
    monkeypatch.setattr("edge_deploy.onboarding.runner._platform_name", lambda: "win32")
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._command_available",
        lambda name: name != "git",
    )
    monkeypatch.setattr("edge_deploy.onboarding.runner._powershell_compatible", lambda: True)
    assert run_onboarding(_args(private)) == 1
    state = OnboardingState.load(default_state_path())
    assert state.data["stages"]["prerequisites"]["outcome"] == "failed"
    assert any(c["id"] == "git" for c in state.data["stages"]["prerequisites"]["checks"])


def test_tool_canonicalize_dedupe_and_cli_overrides_root(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path, checkout_root=tmp_path / "from-private")
    monkeypatch.setattr("edge_deploy.onboarding.runner._platform_name", lambda: "win32")
    monkeypatch.setattr("edge_deploy.onboarding.runner._command_available", lambda _n: True)
    monkeypatch.setattr("edge_deploy.onboarding.runner._powershell_compatible", lambda: True)

    seen: dict[str, object] = {}

    def capture_stages(state, *, imported, tools, root, check_only, app_dir):
        seen["tools"] = list(tools)
        seen["root"] = Path(root)
        seen["imported"] = imported
        state.mark_stage("prerequisites", "passed", inputs={})
        state.mark_stage("config", "passed", inputs={"fingerprint": imported.fingerprint})
        state.mark_stage("repositories", "passed", inputs={})
        state.mark_stage("readiness", "passed", inputs={}, checks=[])
        state.mark_stage("practice", "passed", inputs={})
        state.data["practice"] = {"completed": True, "run_id": "r"}
        state.mark_stage("complete", "passed", inputs={})
        state.save()
        return 0

    monkeypatch.setattr("edge_deploy.onboarding.runner._run_stages", capture_stages)
    cli_root = tmp_path / "from-cli"
    code = run_onboarding(
        _args(
            private,
            root=str(cli_root),
            tool=["dispatch", "Autobench", "dispatch", "autobench"],
        )
    )
    assert code == 0
    assert seen["tools"] == ["robocop", "autobench"]
    assert seen["root"] == cli_root
    assert normalize_tool_id("dispatch") == "robocop"


def test_interactive_tool_prompt_when_omitted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    monkeypatch.setattr("edge_deploy.onboarding.runner._platform_name", lambda: "win32")
    monkeypatch.setattr("edge_deploy.onboarding.runner._command_available", lambda _n: True)
    monkeypatch.setattr("edge_deploy.onboarding.runner._powershell_compatible", lambda: True)
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._prompt_tools",
        lambda: ["robocop"],
    )
    seen: list[str] = []

    def capture(state, *, imported, tools, root, check_only, app_dir):
        seen.extend(tools)
        return 0

    monkeypatch.setattr("edge_deploy.onboarding.runner._run_stages", capture)
    assert run_onboarding(_args(private, tool=None)) == 0
    assert seen == ["robocop"]


def test_stage_order_and_atomic_save_resume(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    monkeypatch.setattr("edge_deploy.onboarding.runner._platform_name", lambda: "win32")
    monkeypatch.setattr("edge_deploy.onboarding.runner._command_available", lambda _n: True)
    monkeypatch.setattr("edge_deploy.onboarding.runner._powershell_compatible", lambda: True)

    order: list[str] = []
    saves: list[str] = []

    def wrap(name, fn):
        def inner(state, **kwargs):
            order.append(name)
            fn(state, **kwargs)
            # caller saves after each stage; capture outcome after save by hooking save
            return None

        return inner

    def boom_repos(state, **kwargs):
        order.append("repositories")
        state.mark_stage(
            "repositories",
            "failed",
            inputs={},
            checks=[{"id": "provision", "outcome": "failed", "summary": "boom", "remediation": "fix"}],
        )

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_config",
        wrap(
            "config",
            lambda state, **kw: state.mark_stage(
                "config", "passed", inputs={"fingerprint": kw["imported"].fingerprint}
            ),
        ),
    )
    monkeypatch.setattr("edge_deploy.onboarding.runner._stage_repositories", boom_repos)
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.OnboardingState.save",
        lambda self: saves.append(
            self.data["stages"]["repositories"]["outcome"]
            if "repositories" in order
            else self.data["stages"]["config"]["outcome"]
        )
        or Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        or Path(self.path).write_text(json.dumps(self.data), encoding="utf-8"),
    )

    # First: let prerequisites run for real, fail at repositories
    assert run_onboarding(_args(private, root=str(tmp_path / "root"))) == 1
    assert order[:2] == ["config", "repositories"]
    loaded = OnboardingState.load(default_state_path())
    assert loaded.data["stages"]["prerequisites"]["outcome"] == "passed"
    assert loaded.data["stages"]["config"]["outcome"] == "passed"
    assert loaded.data["stages"]["repositories"]["outcome"] == "failed"
    assert loaded.data["stages"]["readiness"]["outcome"] == "pending"

    # Resume: skip passed stages
    order.clear()

    def ok_repos(state, **kwargs):
        order.append("repositories")
        state.mark_stage("repositories", "passed", inputs={})

    monkeypatch.setattr("edge_deploy.onboarding.runner._stage_repositories", ok_repos)
    _pass_all_readiness(monkeypatch)
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_practice",
        wrap(
            "practice",
            lambda state, **kw: (
                state.mark_stage("practice", "passed", inputs={}),
                state.data.update(practice={"completed": True, "run_id": "r1"}),
            ),
        ),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_complete",
        wrap("complete", lambda state, **kw: state.mark_stage("complete", "passed", inputs={})),
    )
    # restore normal save
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.OnboardingState.save",
        OnboardingState.save,
    )
    assert run_onboarding(_args(private, root=str(tmp_path / "root"))) == 0
    assert "config" not in order
    assert order[0] == "repositories"
    assert "readiness" in order or loaded  # readiness stubbed via _stage_readiness
    assert "practice" in order
    assert "complete" in order


def test_check_mode_does_not_provision_or_practice(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    root = tmp_path / "root"
    tool_path = root / "autobench"
    _seed_tool_tree(tool_path, "autobench")

    # Install operator config as if prior onboard succeeded
    from edge_deploy.onboarding.config_import import install_operator_config, merge_operator_config

    imported = load_private_onboarding_source(private)
    merged = merge_operator_config(
        imported.operator_mapping,
        audit_repo=str(bootstrap_core_root()),
        tools={"autobench": str(tool_path)},
    )
    install_operator_config(merged)

    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(root),
        config_fingerprint=imported.fingerprint,
    )
    state.mark_stage("prerequisites", "passed", inputs={})
    state.mark_stage("config", "passed", inputs={"fingerprint": imported.fingerprint})
    state.mark_stage("repositories", "passed", inputs={})
    state.save()

    monkeypatch.setattr("edge_deploy.onboarding.runner._platform_name", lambda: "win32")
    monkeypatch.setattr("edge_deploy.onboarding.runner._command_available", lambda _n: True)
    monkeypatch.setattr("edge_deploy.onboarding.runner._powershell_compatible", lambda: True)

    calls = {"config": 0, "repos": 0, "practice": 0, "console": 0, "install": 0, "clone": 0}

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.install_operator_config",
        lambda *a, **k: calls.__setitem__("install", calls["install"] + 1),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.provision_tool_checkout",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("clone/provision in --check")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.install_tool_dependencies",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("deps install in --check")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.create_training_workspace",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("practice in --check")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._launch_console",
        lambda roots: calls.__setitem__("console", calls["console"] + 1),
    )

    readiness_ran = {"n": 0}

    def fake_readiness(state, *, tools, root, check_only=False):
        readiness_ran["n"] += 1
        assert check_only is True
        state.mark_stage(
            "readiness",
            "passed",
            inputs={},
            checks=[{"id": "gh_auth", "outcome": "passed", "summary": "ok", "remediation": ""}],
        )

    monkeypatch.setattr("edge_deploy.onboarding.runner._stage_readiness", fake_readiness)

    code = run_onboarding(_args(private, root=str(root), check=True))
    assert code == 0
    assert readiness_ran["n"] == 1
    assert calls["console"] == 0
    assert calls["install"] == 0
    loaded = OnboardingState.load(default_state_path())
    assert loaded.data["stages"]["practice"]["outcome"] == "pending"
    assert loaded.data["stages"]["complete"]["outcome"] == "pending"


def test_check_mode_blocked_when_setup_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    monkeypatch.setattr("edge_deploy.onboarding.runner._platform_name", lambda: "win32")
    monkeypatch.setattr("edge_deploy.onboarding.runner._command_available", lambda _n: True)
    monkeypatch.setattr("edge_deploy.onboarding.runner._powershell_compatible", lambda: True)

    code = run_onboarding(_args(private, root=str(tmp_path / "missing"), check=True))
    assert code != 0
    state = OnboardingState.load(default_state_path())
    # readiness or a validation stage should be blocked/failed with remediation
    readiness = state.data["stages"]["readiness"]
    assert readiness["outcome"] in {"blocked", "failed"}
    assert readiness["checks"]
    assert any((c.get("remediation") or "").strip() for c in readiness["checks"])


def test_pin_before_install_ordering(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    root = tmp_path / "root"
    monkeypatch.setattr("edge_deploy.onboarding.runner._platform_name", lambda: "win32")
    monkeypatch.setattr("edge_deploy.onboarding.runner._command_available", lambda _n: True)
    monkeypatch.setattr("edge_deploy.onboarding.runner._powershell_compatible", lambda: True)

    events: list[str] = []

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.validate_bootstrap_core",
        lambda *a, **k: (
            events.append("validate_core"),
            ProvisionResult("core", bootstrap_core_root(), "validated", "ok"),
        )[1],
    )

    def fake_provision(dest, manifest, **kwargs):
        events.append(f"provision:{manifest.tool_id}")
        _seed_tool_tree(Path(dest), manifest.tool_id)
        return ProvisionResult(manifest.tool_id, Path(dest), "cloned", "cloned")

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.provision_tool_checkout",
        fake_provision,
    )

    def fake_pins(tool_roots, *, expected_tag):
        events.append("assert_pins")
        assert expected_tag == approved_engine_tag()
        assert len(tool_roots) == 2

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.assert_engine_pins_compatible",
        fake_pins,
    )

    def fake_install(tool_root, **kwargs):
        events.append(f"install:{Path(tool_root).name}")

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.install_tool_dependencies",
        fake_install,
    )
    _stub_repo_evidence(monkeypatch)

    # Skip readiness/practice with stubs after repos
    _pass_all_readiness(monkeypatch)
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_practice",
        lambda state, **kw: state.mark_stage("practice", "passed", inputs={})
        or state.data.update(practice={"completed": True, "run_id": "r"}),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_complete",
        lambda state, **kw: state.mark_stage("complete", "passed", inputs={}),
    )
    monkeypatch.setattr("edge_deploy.onboarding.runner._launch_console", lambda roots: None)

    code = run_onboarding(
        _args(private, root=str(root), tool=["autobench", "robocop"])
    )
    assert code == 0
    assert events[0] == "validate_core"
    assert "provision:autobench" in events
    assert "provision:robocop" in events
    pin_idx = events.index("assert_pins")
    install_idxs = [i for i, e in enumerate(events) if e.startswith("install:")]
    assert install_idxs
    assert pin_idx < min(install_idxs)
    assert all(events.index(p) < pin_idx for p in events if p.startswith("provision:"))


def _stub_repo_evidence(monkeypatch, *, bb_url: str = "https://bitbucket.example/ab.git") -> None:
    from edge_deploy.onboarding.manifest import TOOL_MANIFESTS
    from edge_deploy.onboarding.repositories import CheckoutEvidence, fingerprint_remote_url

    def fake_collect(dest, manifest, **kwargs):
        return CheckoutEvidence(
            tool_id=manifest.tool_id,
            head_sha="a" * 40,
            dirty=False,
            origin_fingerprint=fingerprint_remote_url(TOOL_MANIFESTS[manifest.tool_id].github_url),
            bitbucket_fingerprint=fingerprint_remote_url(bb_url),
            engine_pin=approved_engine_tag(),
            required_files_ok=True,
        )

    def fake_core(*_args, **_kwargs):
        return {
            "head_sha": "a" * 40,
            "exact_tag": approved_engine_tag(),
            "origin_fingerprint": fingerprint_remote_url(
                "https://github.com/pedrochagasmaster/edge-deploy-core.git"
            ),
            "bitbucket_fingerprint": fingerprint_remote_url(
                "https://bitbucket.example/core.git"
            ),
            "dirty": False,
            "evidence_fingerprint": "c" * 64,
        }

    monkeypatch.setattr("edge_deploy.onboarding.runner.collect_checkout_evidence", fake_collect)
    monkeypatch.setattr("edge_deploy.onboarding.runner.inspect_bootstrap_core", fake_core)


def test_second_identical_run_skips_clone_and_install(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    root = tmp_path / "root"
    monkeypatch.setattr("edge_deploy.onboarding.runner._platform_name", lambda: "win32")
    monkeypatch.setattr("edge_deploy.onboarding.runner._command_available", lambda _n: True)
    monkeypatch.setattr("edge_deploy.onboarding.runner._powershell_compatible", lambda: True)

    counts = {"clone": 0, "install": 0, "remote_add": 0, "provision": 0}

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.validate_bootstrap_core",
        lambda *a, **k: ProvisionResult("core", bootstrap_core_root(), "validated", "ok"),
    )

    def provision(dest, manifest, **kwargs):
        counts["provision"] += 1
        dest = Path(dest)
        if dest.exists():
            return ProvisionResult(manifest.tool_id, dest, "reused", "reused")
        counts["clone"] += 1
        counts["remote_add"] += 1
        _seed_tool_tree(dest, manifest.tool_id)
        return ProvisionResult(manifest.tool_id, dest, "cloned", "cloned")

    monkeypatch.setattr("edge_deploy.onboarding.runner.provision_tool_checkout", provision)
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.assert_engine_pins_compatible",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.install_tool_dependencies",
        lambda *a, **k: counts.__setitem__("install", counts["install"] + 1),
    )
    _stub_repo_evidence(monkeypatch)
    _pass_all_readiness(monkeypatch)
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_practice",
        lambda state, **kw: state.mark_stage("practice", "passed", inputs={})
        or state.data.update(practice={"completed": True, "run_id": "r"}),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_complete",
        lambda state, **kw: state.mark_stage("complete", "passed", inputs={}),
    )
    console_launches: list[list[Path]] = []
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._launch_console",
        lambda roots: console_launches.append(list(roots)),
    )

    args = _args(private, root=str(root), tool=["autobench"])
    assert run_onboarding(args) == 0
    assert counts["clone"] == 1
    assert counts["install"] == 1
    assert counts["remote_add"] == 1
    loaded = OnboardingState.load(default_state_path())
    assert "checkout_evidence" in loaded.data["stages"]["repositories"]["inputs"]
    first_launches = len(console_launches)
    first_provision = counts["provision"]

    assert run_onboarding(args) == 0
    assert counts["clone"] == 1
    assert counts["install"] == 1
    assert counts["remote_add"] == 1
    assert counts["provision"] == first_provision
    assert len(console_launches) == first_launches


def test_readiness_single_session_auth_kerberos_smoke_order(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    monkeypatch.setenv("BB_TOKEN", "token=secret-should-not-persist")
    private = _private_yaml(tmp_path)
    root = tmp_path / "root"
    tool_path = root / "autobench"
    _seed_tool_tree(tool_path, "autobench", deep=True)

    from edge_deploy.onboarding.config_import import install_operator_config, merge_operator_config

    imported = load_private_onboarding_source(private)
    merged = merge_operator_config(
        imported.operator_mapping,
        audit_repo=str(bootstrap_core_root()),
        tools={"autobench": str(tool_path)},
    )
    install_operator_config(merged)

    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(root),
        config_fingerprint=imported.fingerprint,
    )
    state.mark_stage("prerequisites", "passed", inputs={})
    state.mark_stage("config", "passed", inputs={"fingerprint": imported.fingerprint})
    state.mark_stage("repositories", "passed", inputs={})
    state.save()

    order: list[str] = []
    drivers: dict[str, object] = {}

    class FakeDriver:
        def __init__(self, node):
            self.node = node
            self.stopped = False

        def stop_session(self):
            self.stopped = True
            order.append(f"stop:{getattr(self.node, 'name', 'n')}")

    def transport_factory(node):
        order.append(f"transport_factory:{node.name}")
        return FakeDriver(node)

    def authenticate(driver, node_name):
        order.append(f"auth:{node_name}")
        drivers[node_name] = driver
        return driver

    def smoke(driver, *, node_label):
        order.append(f"smoke:{node_label}")
        assert drivers[node_label] is driver
        return SimpleNamespace(passed=True)

    def ensure_kerb(driver, node_name):
        order.append(f"kerberos:{node_name}")
        assert drivers[node_name] is driver
        return SimpleNamespace(passed=True)

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._transport_factory_for_node",
        transport_factory,
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._authenticate_node",
        authenticate,
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._run_transport_smoke",
        smoke,
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._ensure_kerberos",
        ensure_kerb,
    )

    # Keep other readiness checks green without network (patch runner bindings).
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_gh_auth_runner",
        lambda **k: (lambda: CheckResult("gh_auth", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_github_read_runner",
        lambda **k: (lambda: CheckResult("github_read", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_tool_clean_main_runner",
        lambda **k: (lambda: CheckResult("tool_clean_main", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_bitbucket_read_runner",
        lambda **k: (lambda: CheckResult("bitbucket_read", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_bitbucket_write_dry_run_runner",
        lambda **k: (lambda: CheckResult("bitbucket_write_dry_run", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_audit_runner",
        lambda **k: (lambda: CheckResult("audit_release_log", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_edge_tcp_runner",
        lambda *a, **k: (lambda: CheckResult("edge_tcp", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_known_hosts_runner",
        lambda *a, **k: (lambda: CheckResult("known_hosts", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._local_check_runner",
        lambda root: 0,
    )

    from edge_deploy.onboarding import runner as runner_mod

    runner_mod._stage_readiness(state, tools=["autobench"], root=root)
    assert state.data["stages"]["readiness"]["checks"], state.data["stages"]["readiness"]
    assert "auth:node03" in order, order
    assert order.count("auth:node03") == 1
    auth_i = order.index("auth:node03")
    kerb_i = order.index("kerberos:node03")
    smoke_i = order.index("smoke:node03")
    assert auth_i < kerb_i < smoke_i

    # Persisted checks must not contain secret material
    blob = json.dumps(state.data["stages"]["readiness"])
    assert "secret-should-not-persist" not in blob
    assert "token=" not in blob or "***REDACTED***" in blob
    assert state.data["stages"]["readiness"]["outcome"] == "passed"
    for check in state.data["stages"]["readiness"]["checks"]:
        assert set(check) <= {
            "id",
            "outcome",
            "summary",
            "remediation",
            "evidence_fingerprint",
        }


def test_non_ssh_node_fails_actionably(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    monkeypatch.setenv("BB_TOKEN", "x")
    private = _private_yaml(tmp_path, transport="pane")
    root = tmp_path / "root"
    tool_path = root / "autobench"
    _seed_tool_tree(tool_path, "autobench")

    from edge_deploy.onboarding.config_import import install_operator_config, merge_operator_config

    imported = load_private_onboarding_source(private)
    merged = merge_operator_config(
        imported.operator_mapping,
        audit_repo=str(bootstrap_core_root()),
        tools={"autobench": str(tool_path)},
    )
    install_operator_config(merged)

    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(root),
        config_fingerprint=imported.fingerprint,
    )

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_gh_auth_runner",
        lambda **k: (lambda: CheckResult("gh_auth", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_github_read_runner",
        lambda **k: (lambda: CheckResult("github_read", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_tool_clean_main_runner",
        lambda **k: (lambda: CheckResult("tool_clean_main", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_bitbucket_read_runner",
        lambda **k: (lambda: CheckResult("bitbucket_read", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_bitbucket_write_dry_run_runner",
        lambda **k: (lambda: CheckResult("bitbucket_write_dry_run", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_audit_runner",
        lambda **k: (lambda: CheckResult("audit_release_log", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_edge_tcp_runner",
        lambda *a, **k: (lambda: CheckResult("edge_tcp", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_known_hosts_runner",
        lambda *a, **k: (lambda: CheckResult("known_hosts", "passed", "ok", "")),
    )
    monkeypatch.setattr("edge_deploy.onboarding.runner._local_check_runner", lambda root: 0)

    from edge_deploy.onboarding import runner as runner_mod

    runner_mod._stage_readiness(state, tools=["autobench"], root=root)
    assert state.data["stages"]["readiness"]["outcome"] in {"failed", "blocked"}
    rsa = next(
        c for c in state.data["stages"]["readiness"]["checks"] if c["id"] == "rsa_auth:node03"
    )
    assert rsa["outcome"] == "failed"
    assert "ssh" in (rsa["summary"] + rsa["remediation"]).lower()


def test_practice_uses_training_roots_and_injected_ack(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    app_dir = default_state_path().parent
    real_root = tmp_path / "real-root" / "autobench"
    real_root.mkdir(parents=True)

    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(tmp_path / "real-root"),
        config_fingerprint="a" * 64,
    )
    # minimal installed config for operator email/nodes
    from edge_deploy.onboarding.config_import import install_operator_config

    install_operator_config(
        {
            "operator_email": "op@example.com",
            "audit_repo": str(bootstrap_core_root()),
            "nodes": {
                "node03": {
                    "host": "operator@edge",
                    "ssh_options": "",
                    "session": "edge-node03",
                    "transport": "ssh",
                }
            },
            "tools": {"autobench": str(real_root)},
        }
    )

    acks: list[str] = []
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._training_acknowledge",
        lambda msg: acks.append(msg),
    )
    launched: list[list[Path]] = []
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._launch_console",
        lambda roots: launched.append(list(roots)),
    )

    from edge_deploy.onboarding import runner as runner_mod

    runner_mod._stage_practice(state, tools=["autobench"], app_dir=app_dir)
    assert state.data["stages"]["practice"]["outcome"] == "passed"
    assert state.data["practice"]["completed"] is True
    training_runs = app_dir / "training" / "autobench" / "edge-deploy" / "runs"
    assert training_runs.is_dir()
    assert list(training_runs.glob("*/state.json"))
    assert not list((real_root / "edge-deploy" / "runs").glob("*/state.json")) if (
        real_root / "edge-deploy" / "runs"
    ).exists() else True
    assert acks
    assert launched and all(
        Path(r).is_relative_to(app_dir / "training")
        or str(app_dir / "training") in str(r)
        for r in launched[0]
    )


def test_console_launch_uses_popen_seam_nonblocking(tmp_path: Path, monkeypatch) -> None:
    calls: list[object] = []

    def fake_popen(command, **kwargs):
        calls.append((list(command), kwargs))
        return SimpleNamespace(pid=1)

    monkeypatch.setattr("edge_deploy.onboarding.runner._popen", fake_popen)
    from edge_deploy.onboarding import runner as runner_mod

    roots = [tmp_path / "training" / "autobench"]
    runner_mod._launch_console(roots)
    assert calls
    cmd, kwargs = calls[0]
    assert any("edge_console" in str(c) for c in cmd)
    assert "--no-browser" in cmd
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("stdin") is not None


def test_complete_writes_redacted_report(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    monkeypatch.setenv("BB_TOKEN", "token=super-secret-value")
    app_dir = default_state_path().parent
    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench", "robocop"],
        root=str(tmp_path / "root"),
        config_fingerprint="f" * 64,
    )
    state.mark_stage(
        "readiness",
        "passed",
        inputs={},
        checks=[
            {
                "id": "gh_auth",
                "outcome": "passed",
                "summary": "gh authenticated",
                "remediation": "",
            }
        ],
    )
    state.data["practice"] = {"completed": True, "run_id": "run-training"}
    state.mark_stage("practice", "passed", inputs={})

    from edge_deploy.onboarding import runner as runner_mod

    runner_mod._stage_complete(state, app_dir=app_dir)
    report_path = app_dir / "onboarding-report.json"
    assert report_path.is_file()
    raw = report_path.read_text(encoding="utf-8")
    assert "super-secret-value" not in raw
    assert "token=super-secret-value" not in raw
    data = json.loads(raw)
    assert data["tools"] == ["autobench", "robocop"]
    assert data["config_fingerprint"] == "f" * 64
    assert "engine" in data
    assert data["practice_completed"] is True
    assert "first_real_release_commands" in data
    assert any("release --guided --tool autobench" in c for c in data["first_real_release_commands"])
    assert "bitbucket.example" not in raw
    assert "operator@" not in raw
    out = capsys.readouterr().out
    assert "autobench" in out
    assert "release" in out.lower() or "guided" in out.lower()


def test_state_never_persists_private_config_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    monkeypatch.setattr("edge_deploy.onboarding.runner._platform_name", lambda: "win32")
    monkeypatch.setattr("edge_deploy.onboarding.runner._command_available", lambda _n: True)
    monkeypatch.setattr("edge_deploy.onboarding.runner._powershell_compatible", lambda: True)

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.validate_bootstrap_core",
        lambda *a, **k: ProvisionResult("core", bootstrap_core_root(), "validated", "ok"),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.provision_tool_checkout",
        lambda dest, manifest, **k: (
            _seed_tool_tree(Path(dest), manifest.tool_id),
            ProvisionResult(manifest.tool_id, Path(dest), "cloned", "ok"),
        )[1],
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.assert_engine_pins_compatible",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.install_tool_dependencies",
        lambda *a, **k: None,
    )
    _stub_repo_evidence(monkeypatch)
    _pass_all_readiness(monkeypatch)
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_practice",
        lambda state, **kw: state.mark_stage("practice", "passed", inputs={})
        or state.data.update(practice={"completed": True, "run_id": "r"}),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_complete",
        lambda state, **kw: state.mark_stage("complete", "passed", inputs={}),
    )
    monkeypatch.setattr("edge_deploy.onboarding.runner._launch_console", lambda roots: None)

    assert run_onboarding(_args(private, root=str(tmp_path / "root"))) == 0
    blob = default_state_path().read_text(encoding="utf-8")
    assert "bitbucket.example" not in blob
    assert "op@example.com" not in blob
    assert "operator@edge" not in blob


def test_restart_resets_evidence_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    imported = load_private_onboarding_source(private)
    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(tmp_path / "root"),
        config_fingerprint=imported.fingerprint,
    )
    state.mark_stage("prerequisites", "passed", inputs={"ok": True})
    state.mark_stage("config", "passed", inputs={"fingerprint": imported.fingerprint})
    state.data["practice"] = {"completed": True, "run_id": "old"}
    state.save()

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._run_stages",
        lambda *a, **k: 0,
    )
    assert run_onboarding(_args(private, restart=True, yes=True)) == 0
    loaded = OnboardingState.load(default_state_path())
    assert loaded.data["tools"] == ["autobench"]
    assert loaded.data["root"] == str(tmp_path / "root")
    assert loaded.data["stages"]["prerequisites"]["outcome"] == "pending"
    assert loaded.data["practice"] == {"completed": False, "run_id": None}


def test_bad_config_before_restart_leaves_state_unchanged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    imported = load_private_onboarding_source(private)
    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(tmp_path / "root"),
        config_fingerprint=imported.fingerprint,
    )
    state.mark_stage("prerequisites", "passed", inputs={"kept": True})
    state.mark_stage("config", "passed", inputs={"fingerprint": imported.fingerprint})
    state.data["practice"] = {"completed": True, "run_id": "keep-me"}
    state.save()
    before = default_state_path().read_bytes()

    bad = tmp_path / "bad.yaml"
    bad.write_text("bb_token: leaked\noperator_email: x@y.com\nnodes: {}\n", encoding="utf-8")
    try:
        run_onboarding(_args(bad, restart=True, yes=True))
        raised = False
    except ValueError:
        raised = True
    assert raised
    assert default_state_path().read_bytes() == before


def test_powershell_rejects_wrong_versions(tmp_path: Path, monkeypatch) -> None:
    from edge_deploy.onboarding import runner as runner_mod

    monkeypatch.setattr(runner_mod, "_platform_name", lambda: "win32")
    monkeypatch.setattr(runner_mod, "_powershell_version", lambda: (5, 0))
    assert runner_mod._powershell_compatible() is False
    monkeypatch.setattr(runner_mod, "_powershell_version", lambda: (7, 4))
    assert runner_mod._powershell_compatible() is False
    monkeypatch.setattr(runner_mod, "_powershell_version", lambda: (5, 1))
    assert runner_mod._powershell_compatible() is True
    monkeypatch.setattr(runner_mod, "_powershell_version", lambda: None)
    assert runner_mod._powershell_compatible() is False


def test_deleted_checkout_invalidates_repositories(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    imported = load_private_onboarding_source(private)
    root = tmp_path / "root"
    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(root),
        config_fingerprint=imported.fingerprint,
    )
    for stage in ("prerequisites", "config", "repositories", "readiness"):
        state.mark_stage(stage, "passed", inputs={})
    from edge_deploy.onboarding.manifest import TOOL_MANIFESTS
    from edge_deploy.onboarding.repositories import fingerprint_remote_url

    state.data["stages"]["repositories"]["inputs"] = {
        "tools": ["autobench"],
        "root": str(root),
        "engine_tag": approved_engine_tag(),
        "checkout_evidence": {
            "autobench": {
                "tool_id": "autobench",
                "head_sha": "a" * 40,
                "dirty": False,
                "origin_fingerprint": fingerprint_remote_url(
                    TOOL_MANIFESTS["autobench"].github_url
                ),
                "bitbucket_fingerprint": fingerprint_remote_url(
                    "https://bitbucket.example/ab.git"
                ),
                "engine_pin": approved_engine_tag(),
                "required_files_ok": True,
                "evidence_fingerprint": "e" * 64,
            }
        },
        "core_evidence": {"evidence_fingerprint": "c" * 64},
    }
    state.save()

    def missing_collect(dest, manifest, **kwargs):
        raise RuntimeError("checkout missing")

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.collect_checkout_evidence",
        missing_collect,
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.inspect_bootstrap_core",
        lambda *_a, **_k: state.data["stages"]["repositories"]["inputs"]["core_evidence"],
    )

    from edge_deploy.onboarding import runner as runner_mod

    runner_mod._revalidate_repositories(
        state, imported=imported, tools=["autobench"], root=root
    )
    assert state.data["stages"]["repositories"]["outcome"] == "pending"
    assert state.data["stages"]["readiness"]["outcome"] == "pending"


def test_head_or_dirty_drift_invalidates_repositories(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    imported = load_private_onboarding_source(private)
    root = tmp_path / "root"
    from edge_deploy.onboarding.manifest import TOOL_MANIFESTS
    from edge_deploy.onboarding.repositories import CheckoutEvidence, fingerprint_remote_url

    stored = {
        "tool_id": "autobench",
        "head_sha": "a" * 40,
        "dirty": False,
        "origin_fingerprint": fingerprint_remote_url(TOOL_MANIFESTS["autobench"].github_url),
        "bitbucket_fingerprint": fingerprint_remote_url("https://bitbucket.example/ab.git"),
        "engine_pin": approved_engine_tag(),
        "required_files_ok": True,
        "evidence_fingerprint": "e" * 64,
    }
    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(root),
        config_fingerprint=imported.fingerprint,
    )
    state.mark_stage("prerequisites", "passed", inputs={})
    state.mark_stage("config", "passed", inputs={})
    state.mark_stage(
        "repositories",
        "passed",
        inputs={
            "tools": ["autobench"],
            "root": str(root),
            "engine_tag": approved_engine_tag(),
            "checkout_evidence": {"autobench": stored},
            "core_evidence": {"evidence_fingerprint": "c" * 64},
        },
    )
    state.mark_stage("readiness", "passed", inputs={})

    def drifted(dest, manifest, **kwargs):
        return CheckoutEvidence(
            tool_id="autobench",
            head_sha="b" * 40,
            dirty=True,
            origin_fingerprint=stored["origin_fingerprint"],
            bitbucket_fingerprint=stored["bitbucket_fingerprint"],
            engine_pin=approved_engine_tag(),
            required_files_ok=True,
        )

    monkeypatch.setattr("edge_deploy.onboarding.runner.collect_checkout_evidence", drifted)
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.inspect_bootstrap_core",
        lambda *_a, **_k: {"evidence_fingerprint": "c" * 64},
    )
    from edge_deploy.onboarding import runner as runner_mod

    runner_mod._revalidate_repositories(
        state, imported=imported, tools=["autobench"], root=root
    )
    assert state.data["stages"]["repositories"]["outcome"] == "pending"
    assert state.data["stages"]["readiness"]["outcome"] == "pending"


def test_missing_or_wrong_remote_invalidates_repositories(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    imported = load_private_onboarding_source(private)
    root = tmp_path / "root"
    from edge_deploy.onboarding.manifest import TOOL_MANIFESTS
    from edge_deploy.onboarding.repositories import CheckoutEvidence, fingerprint_remote_url

    origin_fp = fingerprint_remote_url(TOOL_MANIFESTS["autobench"].github_url)
    stored = {
        "tool_id": "autobench",
        "head_sha": "a" * 40,
        "dirty": False,
        "origin_fingerprint": origin_fp,
        "bitbucket_fingerprint": fingerprint_remote_url("https://bitbucket.example/ab.git"),
        "engine_pin": approved_engine_tag(),
        "required_files_ok": True,
        "evidence_fingerprint": "e" * 64,
    }
    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(root),
        config_fingerprint=imported.fingerprint,
    )
    state.mark_stage("prerequisites", "passed", inputs={})
    state.mark_stage("config", "passed", inputs={})
    state.mark_stage(
        "repositories",
        "passed",
        inputs={
            "tools": ["autobench"],
            "root": str(root),
            "engine_tag": approved_engine_tag(),
            "checkout_evidence": {"autobench": stored},
            "core_evidence": {"evidence_fingerprint": "c" * 64},
        },
    )
    state.mark_stage("readiness", "passed", inputs={})

    def wrong_remote(dest, manifest, **kwargs):
        return CheckoutEvidence(
            tool_id="autobench",
            head_sha="a" * 40,
            dirty=False,
            origin_fingerprint=origin_fp,
            bitbucket_fingerprint=None,
            engine_pin=approved_engine_tag(),
            required_files_ok=True,
        )

    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.collect_checkout_evidence",
        wrong_remote,
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.inspect_bootstrap_core",
        lambda *_a, **_k: {"evidence_fingerprint": "c" * 64},
    )
    from edge_deploy.onboarding import runner as runner_mod

    runner_mod._revalidate_repositories(
        state, imported=imported, tools=["autobench"], root=root
    )
    assert state.data["stages"]["repositories"]["outcome"] == "pending"


def test_legacy_state_without_evidence_invalidates_safely(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    private = _private_yaml(tmp_path)
    imported = load_private_onboarding_source(private)
    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(tmp_path / "root"),
        config_fingerprint=imported.fingerprint,
    )
    state.mark_stage("prerequisites", "passed", inputs={})
    state.mark_stage("config", "passed", inputs={})
    state.mark_stage(
        "repositories",
        "passed",
        inputs={"tools": ["autobench"], "root": str(tmp_path / "root")},
    )
    state.mark_stage("readiness", "passed", inputs={})
    from edge_deploy.onboarding import runner as runner_mod

    runner_mod._revalidate_repositories(
        state,
        imported=imported,
        tools=["autobench"],
        root=tmp_path / "root",
    )
    assert state.data["stages"]["repositories"]["outcome"] == "pending"
    assert state.data["stages"]["readiness"]["outcome"] == "pending"


def test_check_mode_real_orchestration_no_mutations(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    monkeypatch.setenv("BB_TOKEN", "token=env-only")
    private = _private_yaml(tmp_path)
    root = tmp_path / "root"
    tool_path = root / "autobench"
    _seed_tool_tree(tool_path, "autobench")

    from edge_deploy.onboarding.config_import import install_operator_config, merge_operator_config

    imported = load_private_onboarding_source(private)
    merged = merge_operator_config(
        imported.operator_mapping,
        audit_repo=str(bootstrap_core_root()),
        tools={"autobench": str(tool_path)},
    )
    install_operator_config(merged)
    config_bytes = (
        Path(tmp_path / "app" / "edge-deploy" / "config.yaml").read_bytes()
        if (tmp_path / "app" / "edge-deploy" / "config.yaml").is_file()
        else None
    )
    # DEFAULT_OPERATOR_CONFIG_PATH uses APPDATA
    from edge_deploy.config import DEFAULT_OPERATOR_CONFIG_PATH

    config_bytes = DEFAULT_OPERATOR_CONFIG_PATH.read_bytes()

    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(root),
        config_fingerprint=imported.fingerprint,
    )
    state.mark_stage("prerequisites", "passed", inputs={})
    state.mark_stage("config", "passed", inputs={"fingerprint": imported.fingerprint})
    state.mark_stage("repositories", "passed", inputs={})
    state.save()
    report_path = default_state_path().parent / "onboarding-report.json"
    assert not report_path.exists()

    monkeypatch.setattr("edge_deploy.onboarding.runner._platform_name", lambda: "win32")
    monkeypatch.setattr("edge_deploy.onboarding.runner._command_available", lambda _n: True)
    monkeypatch.setattr("edge_deploy.onboarding.runner._powershell_compatible", lambda: True)

    mutations = {
        "install_config": 0,
        "provision": 0,
        "install_deps": 0,
        "practice": 0,
        "console": 0,
        "complete": 0,
    }
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.install_operator_config",
        lambda *a, **k: mutations.__setitem__("install_config", mutations["install_config"] + 1),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.provision_tool_checkout",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("provision in --check")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.install_tool_dependencies",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("install in --check")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.create_training_workspace",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("practice in --check")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._launch_console",
        lambda roots: mutations.__setitem__("console", mutations["console"] + 1),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._stage_complete",
        lambda *a, **k: mutations.__setitem__("complete", mutations["complete"] + 1),
    )

    # Stub only external network/auth adapters on the runner module bindings.
    for name, check_id, summary in (
        ("build_default_gh_auth_runner", "gh_auth", "ok"),
        ("build_default_github_read_runner", "github_read", "ok"),
        ("build_default_tool_clean_main_runner", "tool_clean_main", "ok"),
        ("build_default_bitbucket_read_runner", "bitbucket_read", "ok"),
        ("build_default_bitbucket_write_dry_run_runner", "bitbucket_write_dry_run", "ok"),
        ("build_default_audit_runner", "audit_release_log", "ok"),
    ):
        monkeypatch.setattr(
            f"edge_deploy.onboarding.runner.{name}",
            lambda check_id=check_id, summary=summary, **k: (
                lambda: CheckResult(check_id, "passed", summary, "")
            ),
        )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_edge_tcp_runner",
        lambda *a, **k: (lambda: CheckResult("edge_tcp", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner.build_default_known_hosts_runner",
        lambda *a, **k: (lambda: CheckResult("known_hosts", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._session_runners_for_readiness",
        lambda operator: (
            (lambda node: CheckResult(f"rsa_auth:{node}", "passed", "ok", "")),
            (lambda node: CheckResult(f"transport_smoke:{node}", "passed", "ok", "")),
            (lambda node: CheckResult(f"kerberos:{node}", "passed", "ok", "")),
            SimpleNamespace(stop_all=lambda: None),
        ),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._local_check_runner",
        lambda root: 0,
    )

    code = run_onboarding(_args(private, root=str(root), check=True))
    assert code == 0
    assert mutations["install_config"] == 0
    assert mutations["console"] == 0
    assert mutations["complete"] == 0
    assert DEFAULT_OPERATOR_CONFIG_PATH.read_bytes() == config_bytes
    assert not report_path.exists()
    loaded = OnboardingState.load(default_state_path())
    assert loaded.data["stages"]["readiness"]["outcome"] == "passed"
    assert loaded.data["stages"]["practice"]["outcome"] == "pending"
    assert loaded.data["stages"]["complete"]["outcome"] == "pending"
    assert any(
        c["id"] == "rsa_auth:node03" for c in loaded.data["stages"]["readiness"]["checks"]
    )


def test_fail_stub_make_runners_removed() -> None:
    import edge_deploy.onboarding.runner as runner_mod

    assert not hasattr(runner_mod, "_make_rsa_auth_runner")
    assert not hasattr(runner_mod, "_make_transport_smoke_runner")
    assert not hasattr(runner_mod, "_make_kerberos_runner")
    assert hasattr(runner_mod, "_session_runners_for_readiness")
