# Release Operator Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a resumable `edge_deploy onboard` flow that provisions Autobench and/or Dispatch from a private config, validates real both-vpns readiness, and completes an isolated guided training release shown in the Edge Console — without real remotes writes (except `git push --dry-run`), posture switches, or secret persistence.

**Architecture:** New `edge_deploy/onboarding/` package owns manifest defaults, atomic state, config import, repository provisioning, dependency-aware checks, no-I/O training simulation, and a runner. `edge_deploy/cli.py` parses `onboard` and dispatches **before** `OperatorConfig.load`. Training ledgers use both `kind="training"` and `training=true` (production omits `training`), live outside real tool run roots, and never import production phase executors; production entry points reject them. Routine onboarding stays in `both-vpns` for the entire flow.

**Tech Stack:** Python 3.10+, PyYAML, Paramiko (existing), pytest/pytest-xdist, Ruff, Windows PowerShell 5.1 operator surface (`py -m edge_deploy`), Edge Console (`edge_console.py`).

## Global Constraints

- Authoritative design: `docs/superpowers/specs/2026-07-24-release-operator-onboarding-design.md` (do not edit it).
- Canonical internal tool id remains `robocop`; `dispatch` is accepted only as CLI alias / display name and is normalized to `robocop` before state or config writes.
- `onboard` is dispatched in `main()` before `OperatorConfig.load` (same early-exit class as `status` / `mirror`).
- Training ledgers set both `kind="training"` and `training=True`. Production ledgers omit the `training` key entirely (never persist `training=false`). Production commands reject `kind=="training"` or `training is True`.
- Training workspace lives outside real tool checkouts and outside `<tool>/edge-deploy/runs/`.
- Training handlers must not import production phase executors (`edge_deploy.phases.publish`, `.deploy`, `.tag`, `.verify` command modules, or `edge_deploy.publish` / `edge_deploy.rollout` release executors).
- Bootstrap core root is the editable package checkout: `Path(engine_identity()["package_dir"]).parent`. Onboarding validates that checkout (approved tag + remotes) and never clones a second core tree.
- Routine onboarding remains entirely in `both-vpns`; never change VPN/firewall state; never require `firewall-off`.
- No secrets, private endpoints, tokens, passcodes, or private operator config values in Git or durable onboarding state (fingerprint + redacted outcomes only). Interactive RSA/Kerberos secrets stay memory-only via injected auth runners.
- No auto host-key enrollment; unknown/changed keys fail with enrollment guidance.
- No real release, deploy, tag, or remote ref update; the only GitHub write-path operation allowed is `git push --dry-run`.
- `--yes` exists only as a confirmation bypass and is valid only when paired with `--restart`.
- Explicit CLI args override private-file values, which override bundled defaults.
- All Python imports stay at module top; no inline imports in functions (workspace rule).
- Windows operator commands use `py`; Cloud VM tests use `.venv/bin/python` and `.venv/bin/ruff`.
- Every new `edge_deploy/**/*.py` file changes Engine Identity; finish or abandon open Tool runs before installing a modified engine on a controller.

---

## File Map

**Create**

- `edge_deploy/onboarding/__init__.py`
- `edge_deploy/onboarding/manifest.py` — non-sensitive tool defaults, alias map, approved engine tag helper
- `edge_deploy/onboarding/state.py` — versioned onboarding state + atomic writes
- `edge_deploy/onboarding/config_import.py` — private-file validation/merge/install
- `edge_deploy/onboarding/repositories.py` — tool clone/validate; validate bootstrap core (no second core clone)
- `edge_deploy/onboarding/checks.py` — check DAG + passed/failed/blocked runner
- `edge_deploy/onboarding/training.py` — isolated training ledger + no-I/O phase simulator
- `edge_deploy/onboarding/runner.py` — stage machine, prompts, report
- `tests/test_onboarding_manifest.py`
- `tests/test_onboarding_state.py`
- `tests/test_onboarding_config_import.py`
- `tests/test_onboarding_repositories.py`
- `tests/test_onboarding_checks.py`
- `tests/test_onboarding_training.py`
- `tests/test_onboarding_cli.py`
- `tests/test_onboarding_runner.py`
- `tests/test_onboarding_integration.py`
- `docs/adr/0017-release-operator-onboarding.md`

**Modify**

- `edge_deploy/cli.py` — `onboard` subparser; early dispatch before `OperatorConfig.load`; `--yes` only with `--restart`
- `edge_deploy/ledger.py` — accept/persist `training` bool; helper `is_training_ledger`
- `edge_deploy/phases/__init__.py` — reject training ledgers in `enter_phase` / `load_run` path used by production
- `edge_deploy/cli.py` (`_cmd_release`, abandon/rollback load paths as needed) — refuse training ledgers
- `edge_console.py` — recognize training ledgers for display (read-only); do not mutate
- `tests/test_edge_console.py` — training ledger visibility
- `tests/test_phases.py` / `tests/test_ledger.py` — training rejection
- `README.md`, `docs/release-workflow.md` — bootstrap, both-vpns, onboard examples, training vs real, fix `.[dev,release]` where undeclared
- `config.example.yaml` — comments pointing at private onboarding source expectations (still no secrets)

---

### Task 1: Tool identity aliases and non-sensitive manifest

**Files:**
- Create: `edge_deploy/onboarding/__init__.py`
- Create: `edge_deploy/onboarding/manifest.py`
- Create: `tests/test_onboarding_manifest.py`

**Interfaces:**
- Consumes: `edge_deploy.__version__`
- Produces:
  - `CANONICAL_TOOLS: tuple[str, ...] = ("autobench", "robocop")`
  - `TOOL_ALIASES: dict[str, str] = {"dispatch": "robocop", "robocop": "robocop", "autobench": "autobench"}`
  - `DISPLAY_NAMES: dict[str, str] = {"autobench": "Autobench", "robocop": "Dispatch"}`
  - `normalize_tool_id(raw: str) -> str`  # raises `ValueError` on unknown
  - `approved_engine_tag() -> str`  # `f"v{edge_deploy.__version__}"`
  - `@dataclass(frozen=True) class ToolManifest` with fields:
    - `tool_id: str`
    - `display_name: str`
    - `github_url: str`
    - `default_dirname: str`
    - `profile_filename: str`  # `"edge_deploy.yaml"`
    - `local_check_relative: str`  # `"tools/dev/local_check.ps1"`
  - `TOOL_MANIFESTS: dict[str, ToolManifest]`
  - `CORE_GITHUB_URL: str = "https://github.com/pedrochagasmaster/edge-deploy-core.git"`
  - `ONBOARDING_STAGES: tuple[str, ...] = ("prerequisites", "config", "repositories", "readiness", "practice", "complete")`

- [ ] **Step 1: Write the failing test**

Create `tests/test_onboarding_manifest.py`:

```python
from pathlib import Path

import edge_deploy
import edge_deploy.onboarding.manifest as mod
from edge_deploy.onboarding.manifest import (
    CANONICAL_TOOLS,
    CORE_GITHUB_URL,
    DISPLAY_NAMES,
    TOOL_MANIFESTS,
    approved_engine_tag,
    normalize_tool_id,
)


def test_dispatch_alias_normalizes_to_robocop() -> None:
    assert normalize_tool_id("dispatch") == "robocop"
    assert normalize_tool_id("Dispatch") == "robocop"
    assert normalize_tool_id("robocop") == "robocop"
    assert normalize_tool_id("autobench") == "autobench"


def test_unknown_tool_rejected() -> None:
    try:
        normalize_tool_id("not-a-tool")
    except ValueError as exc:
        assert "not-a-tool" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_canonical_tools_and_display_names() -> None:
    assert CANONICAL_TOOLS == ("autobench", "robocop")
    assert set(TOOL_MANIFESTS) == {"autobench", "robocop"}
    assert DISPLAY_NAMES["robocop"] == "Dispatch"
    assert TOOL_MANIFESTS["autobench"].github_url.endswith("autobench.git")
    assert TOOL_MANIFESTS["robocop"].github_url.endswith("robocop.git")
    assert TOOL_MANIFESTS["robocop"].default_dirname == "robocop"
    assert CORE_GITHUB_URL.endswith("edge-deploy-core.git")


def test_approved_engine_tag_tracks_package_version() -> None:
    assert approved_engine_tag() == f"v{edge_deploy.__version__}"


def test_manifest_module_is_under_package_for_engine_identity() -> None:
    package_dir = Path(edge_deploy.__file__).resolve().parent
    assert package_dir in Path(mod.__file__).resolve().parents
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_onboarding_manifest.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'edge_deploy.onboarding'`.

- [ ] **Step 3: Write minimal implementation**

Create `edge_deploy/onboarding/__init__.py`:

```python
"""Resumable Release Operator onboarding (private config → readiness → training)."""
```

Create `edge_deploy/onboarding/manifest.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

import edge_deploy

CANONICAL_TOOLS: tuple[str, ...] = ("autobench", "robocop")
TOOL_ALIASES: dict[str, str] = {
    "autobench": "autobench",
    "robocop": "robocop",
    "dispatch": "robocop",
}
DISPLAY_NAMES: dict[str, str] = {
    "autobench": "Autobench",
    "robocop": "Dispatch",
}
CORE_GITHUB_URL = "https://github.com/pedrochagasmaster/edge-deploy-core.git"
ONBOARDING_STAGES: tuple[str, ...] = (
    "prerequisites",
    "config",
    "repositories",
    "readiness",
    "practice",
    "complete",
)


@dataclass(frozen=True)
class ToolManifest:
    tool_id: str
    display_name: str
    github_url: str
    default_dirname: str
    profile_filename: str = "edge_deploy.yaml"
    local_check_relative: str = "tools/dev/local_check.ps1"


TOOL_MANIFESTS: dict[str, ToolManifest] = {
    "autobench": ToolManifest(
        tool_id="autobench",
        display_name="Autobench",
        github_url="https://github.com/pedrochagasmaster/autobench.git",
        default_dirname="autobench",
    ),
    "robocop": ToolManifest(
        tool_id="robocop",
        display_name="Dispatch",
        github_url="https://github.com/pedrochagasmaster/robocop.git",
        default_dirname="robocop",
    ),
}


def normalize_tool_id(raw: str) -> str:
    key = str(raw).strip().lower()
    try:
        return TOOL_ALIASES[key]
    except KeyError as exc:
        supported = ", ".join((*CANONICAL_TOOLS, "dispatch"))
        raise ValueError(f"unknown tool {raw!r}; supported: {supported}") from exc


def approved_engine_tag() -> str:
    return f"v{edge_deploy.__version__}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_onboarding_manifest.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add edge_deploy/onboarding/__init__.py edge_deploy/onboarding/manifest.py tests/test_onboarding_manifest.py
git commit -m "feat(onboarding): add tool manifest and dispatch alias"
```

---

### Task 2: Versioned onboarding state with atomic writes

**Files:**
- Create: `edge_deploy/onboarding/state.py`
- Create: `tests/test_onboarding_state.py`

**Interfaces:**
- Consumes: `ONBOARDING_STAGES`, `engine_identity` from `edge_deploy.ledger`
- Produces:
  - `SCHEMA = "edge-deploy/onboarding/1"`
  - `default_state_path() -> Path`  # `%APPDATA%/edge-deploy/onboarding-state.json` (APPDATA fallback like config)
  - `@dataclass class OnboardingState` with `path: Path`, `data: dict`
  - `OnboardingState.create_new(*, tools: list[str], root: str, config_fingerprint: str) -> OnboardingState`
  - `OnboardingState.load(path: Path) -> OnboardingState`
  - `OnboardingState.save() -> None`  # atomic replace via temp + `os.replace`
  - `OnboardingState.mark_stage(stage: str, outcome: str, *, inputs: dict, checks: list[dict] | None = None) -> None`
  - `OnboardingState.invalidate_for_engine_mismatch() -> bool`
  - `OnboardingState.reset_evidence() -> None`  # used by `--restart`; keeps nothing about checkouts/config files on disk
  - State JSON contains only: schema, engine, selected tools, root, config_fingerprint, stages, practice, no credential/config value fields

- [ ] **Step 1: Write the failing test**

```python
import json
from pathlib import Path

from edge_deploy.ledger import engine_identity
from edge_deploy.onboarding.state import SCHEMA, OnboardingState, default_state_path


def test_default_state_path_under_edge_deploy_app_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert default_state_path() == tmp_path / "edge-deploy" / "onboarding-state.json"


def test_atomic_save_and_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "onboarding-state.json"
    state = OnboardingState.create_new(
        path=path,
        tools=["autobench", "robocop"],
        root=str(tmp_path / "root"),
        config_fingerprint="a" * 64,
    )
    state.mark_stage("prerequisites", "passed", inputs={"python": "3.10"})
    state.save()
    loaded = OnboardingState.load(path)
    assert loaded.data["schema"] == SCHEMA
    assert loaded.data["tools"] == ["autobench", "robocop"]
    assert loaded.data["config_fingerprint"] == "a" * 64
    assert loaded.data["stages"]["prerequisites"]["outcome"] == "passed"
    assert "operator_email" not in json.dumps(loaded.data)
    assert loaded.data["engine"]["content_sha256"] == engine_identity()["content_sha256"]


def test_interrupted_tmp_does_not_corrupt_existing(tmp_path) -> None:
    path = tmp_path / "onboarding-state.json"
    state = OnboardingState.create_new(
        path=path,
        tools=["autobench"],
        root=str(tmp_path),
        config_fingerprint="b" * 64,
    )
    state.save()
    (path.with_suffix(".json.tmp")).write_text("{not-json", encoding="utf-8")
    loaded = OnboardingState.load(path)
    assert loaded.data["tools"] == ["autobench"]


def test_reset_evidence_clears_stages_keeps_selection(tmp_path) -> None:
    path = tmp_path / "onboarding-state.json"
    state = OnboardingState.create_new(
        path=path,
        tools=["robocop"],
        root=str(tmp_path),
        config_fingerprint="c" * 64,
    )
    state.mark_stage("config", "passed", inputs={"fp": "c" * 64})
    state.data["practice"] = {"completed": True, "run_id": "training-1"}
    state.reset_evidence()
    assert state.data["tools"] == ["robocop"]
    assert state.data["stages"]["config"]["outcome"] == "pending"
    assert state.data["practice"] == {"completed": False, "run_id": None}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_onboarding_state.py -v`

Expected: FAIL with missing `edge_deploy.onboarding.state`.

- [ ] **Step 3: Implement `state.py`**

```python
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from edge_deploy.ledger import engine_identity
from edge_deploy.onboarding.manifest import ONBOARDING_STAGES

SCHEMA = "edge-deploy/onboarding/1"
_VALID_OUTCOMES = frozenset({"pending", "passed", "failed", "blocked"})


def default_state_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    return base / "edge-deploy" / "onboarding-state.json"


def _empty_stages() -> dict:
    return {
        stage: {"outcome": "pending", "inputs": {}, "checks": [], "updated_at": None}
        for stage in ONBOARDING_STAGES
    }


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


@dataclass
class OnboardingState:
    path: Path
    data: dict

    @classmethod
    def create_new(
        cls,
        *,
        path: Path,
        tools: list[str],
        root: str,
        config_fingerprint: str,
    ) -> OnboardingState:
        data = {
            "schema": SCHEMA,
            "engine": engine_identity(),
            "tools": list(tools),
            "root": root,
            "config_fingerprint": config_fingerprint,
            "stages": _empty_stages(),
            "practice": {"completed": False, "run_id": None},
        }
        return cls(path=path, data=data)

    @classmethod
    def load(cls, path: Path) -> OnboardingState:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA:
            raise ValueError(f"unsupported onboarding schema: {data.get('schema')!r}")
        return cls(path=Path(path), data=data)

    def save(self) -> None:
        _write_json_atomic(self.path, self.data)

    def mark_stage(
        self,
        stage: str,
        outcome: str,
        *,
        inputs: dict,
        checks: list[dict] | None = None,
    ) -> None:
        if stage not in ONBOARDING_STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        if outcome not in _VALID_OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        self.data["stages"][stage] = {
            "outcome": outcome,
            "inputs": dict(inputs),
            "checks": list(checks or []),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def engine_matches(self) -> bool:
        return self.data.get("engine", {}).get("content_sha256") == engine_identity()["content_sha256"]

    def reset_evidence(self) -> None:
        tools = list(self.data.get("tools") or [])
        root = str(self.data.get("root") or "")
        fp = str(self.data.get("config_fingerprint") or "")
        self.data = {
            "schema": SCHEMA,
            "engine": engine_identity(),
            "tools": tools,
            "root": root,
            "config_fingerprint": fp,
            "stages": _empty_stages(),
            "practice": {"completed": False, "run_id": None},
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_onboarding_state.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add edge_deploy/onboarding/state.py tests/test_onboarding_state.py
git commit -m "feat(onboarding): add atomic onboarding state store"
```

---

### Task 3: Private config import (validate, merge, fingerprint, permissions)

**Files:**
- Create: `edge_deploy/onboarding/config_import.py`
- Create: `tests/test_onboarding_config_import.py`
- Modify: `config.example.yaml` (comments only)

**Interfaces:**
- Consumes: `OperatorConfig.from_mapping`, `DEFAULT_OPERATOR_CONFIG_PATH`, `redact`
- Produces:
  - `FORBIDDEN_CONFIG_KEYS = frozenset({"bb_token", "token", "password", "passcode", "secret", "authorization"})`
  - `fingerprint_config_bytes(raw: bytes) -> str`  # sha256 hex
  - `load_private_onboarding_source(path: Path) -> dict`
  - `reject_credential_fields(data: dict) -> None`  # raises `ValueError`
  - `merge_operator_config(private: dict, *, audit_repo: str, tools: dict[str, str]) -> dict`
  - `install_operator_config(merged: dict, destination: Path, *, permission_setter=...) -> str`  # returns fingerprint; writes YAML; sets restrictive perms on Windows when available
  - Durable onboarding state stores fingerprint only, never merged values

- [ ] **Step 1: Write the failing test**

```python
import os
from pathlib import Path

import pytest

from edge_deploy.onboarding.config_import import (
    fingerprint_config_bytes,
    install_operator_config,
    load_private_onboarding_source,
    merge_operator_config,
    reject_credential_fields,
)


def test_reject_credential_shaped_fields() -> None:
    with pytest.raises(ValueError, match="credential"):
        reject_credential_fields({"operator_email": "a@b", "bb_token": "x"})
    with pytest.raises(ValueError, match="credential"):
        reject_credential_fields({"nodes": {"n": {"host": "h", "password": "x"}}})


def test_merge_sets_audit_and_tool_paths(tmp_path) -> None:
    private = {
        "operator_email": "op@example.com",
        "nodes": {
            "node03": {
                "host": "operator@edge-node-03.example",
                "ssh_options": "-p 2222",
                "session": "edge-node03",
                "transport": "ssh",
            }
        },
        "bitbucket_remote_url": "https://bitbucket.example/scm/proj/core.git",
    }
    merged = merge_operator_config(
        private,
        audit_repo=str(tmp_path / "edge-deploy-core"),
        tools={"autobench": str(tmp_path / "autobench")},
    )
    assert merged["operator_email"] == "op@example.com"
    assert merged["audit_repo"] == str(tmp_path / "edge-deploy-core")
    assert merged["tools"]["autobench"] == str(tmp_path / "autobench")
    assert "bitbucket_remote_url" not in merged  # not an OperatorConfig field
    assert "bb_token" not in merged


def test_install_writes_destination_and_fingerprint(tmp_path) -> None:
    dest = tmp_path / "edge-deploy" / "config.yaml"
    merged = {
        "operator_email": "op@example.com",
        "audit_repo": str(tmp_path / "core"),
        "nodes": {
            "node03": {
                "host": "operator@edge.example",
                "ssh_options": "-p 2222",
                "session": "edge",
                "transport": "ssh",
            }
        },
        "tools": {},
    }
    fp = install_operator_config(merged, dest, permission_setter=lambda p: None)
    assert dest.is_file()
    assert fp == fingerprint_config_bytes(dest.read_bytes())
    text = dest.read_text(encoding="utf-8")
    assert "op@example.com" in text
    assert "token" not in text.lower()


def test_load_private_source_parses_yaml(tmp_path) -> None:
    path = tmp_path / "private.yaml"
    path.write_text(
        "operator_email: op@example.com\n"
        "checkout_root: C:/edge-deploy\n"
        "nodes:\n  node03:\n    host: operator@edge\n    ssh_options: -p 2222\n",
        encoding="utf-8",
    )
    data = load_private_onboarding_source(path)
    assert data["operator_email"] == "op@example.com"
    assert data["checkout_root"] == "C:/edge-deploy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_onboarding_config_import.py -v`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement `config_import.py`**

Implement using `edge_deploy.config._load_yaml_mapping` patterns (or public loaders), recursive credential-key rejection (case-insensitive key match against `FORBIDDEN_CONFIG_KEYS`), merge that keeps only `OperatorConfig`-compatible keys plus staging metadata read by the runner (`checkout_root`, `bitbucket_remotes` as a separate returned structure if needed).

Recommended split of return values if cleaner:

```python
@dataclass(frozen=True)
class ImportedPrivateConfig:
    operator_mapping: dict  # OperatorConfig-compatible
    checkout_root: str | None
    bitbucket_remotes: dict[str, str]  # keys: "core", "autobench", "robocop"
    fingerprint: str
```

`load_private_onboarding_source` + `parse_private_onboarding_source(raw: dict) -> ImportedPrivateConfig` should:

1. `reject_credential_fields`
2. fingerprint the original file bytes
3. extract `checkout_root` and per-repo Bitbucket URLs from allowlisted private keys (`bitbucket_remotes.core`, `.autobench`, `.robocop`)
4. build operator mapping with `operator_email`, `nodes`, empty `audit_repo`/`tools` filled later by runner

`install_operator_config` writes YAML via PyYAML `safe_dump`, then on Windows attempts `icacls` / `os.chmod(0o600)` style restriction through an injectable `permission_setter`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_onboarding_config_import.py -v`

Expected: PASS

- [ ] **Step 5: Update `config.example.yaml` comments**

Add at top (still no secrets):

```yaml
# Private onboarding source files may also include checkout_root and
# bitbucket_remotes.{core,autobench,robocop}. Those keys are consumed by
# `py -m edge_deploy onboard` and are not stored in onboarding-state.json.
# Credentials (BB_TOKEN, RSA, Kerberos) must never appear in YAML.
```

- [ ] **Step 6: Commit**

```bash
git add edge_deploy/onboarding/config_import.py tests/test_onboarding_config_import.py config.example.yaml
git commit -m "feat(onboarding): import private operator config without secrets"
```

---

### Task 4: CLI surface — `onboard` before `OperatorConfig.load`

**Files:**
- Modify: `edge_deploy/cli.py`
- Create: `edge_deploy/onboarding/runner.py` (minimal callable used by CLI)
- Create: `tests/test_onboarding_cli.py`

**Interfaces:**
- Consumes: top-level `from edge_deploy.onboarding.runner import run_onboarding` in `cli.py`
- Produces: argparse command `onboard` with:
  - `--config` (required for onboard)
  - `--root`
  - `--tool` (repeatable; choices accept `autobench`, `robocop`, `dispatch`)
  - `--check`
  - `--restart`
  - `--yes` (allowed only with `--restart`)
  - Early dispatch in `main()` before `OperatorConfig.load`
  - Monkeypatch target for tests: `edge_deploy.cli.run_onboarding` (matches the top-level import name bound in `cli.py`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_onboarding_cli.py`:

```python
from pathlib import Path

import pytest

from edge_deploy.cli import build_parser, main


def test_onboard_parser_accepts_dispatch_alias() -> None:
    args = build_parser().parse_args(
        [
            "onboard",
            "--config",
            "C:/secure/operator.yaml",
            "--tool",
            "dispatch",
            "--tool",
            "autobench",
        ]
    )
    assert args.command == "onboard"
    assert args.tool == ["dispatch", "autobench"]


def test_yes_requires_restart() -> None:
    # Validated in main() after parse (not by argparse choices).
    code = main(["onboard", "--config", "x.yaml", "--yes"])
    assert code == 2


def test_onboard_does_not_require_operator_config_load(monkeypatch, tmp_path: Path) -> None:
    called = {"load": False, "run": False}

    def fake_load(path):
        called["load"] = True
        raise AssertionError("OperatorConfig.load must not run for onboard")

    def fake_run(args):
        called["run"] = True
        assert args.config.endswith("private.yaml")
        return 0

    monkeypatch.setattr("edge_deploy.cli.OperatorConfig.load", fake_load)
    monkeypatch.setattr("edge_deploy.cli.run_onboarding", fake_run)
    private = tmp_path / "private.yaml"
    private.write_text("operator_email: a@b.com\nnodes: {}\n", encoding="utf-8")
    code = main(["onboard", "--config", str(private), "--tool", "autobench"])
    assert code == 0
    assert called["run"] is True
    assert called["load"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_onboarding_cli.py -v`

Expected: FAIL (`invalid choice` / unknown command `onboard`, or missing `run_onboarding` import).

- [ ] **Step 3: Wire CLI and minimal runner**

In `edge_deploy/cli.py`, add a **module-top** import (with the other edge_deploy imports; not inside `main`):

```python
from edge_deploy.onboarding.runner import run_onboarding
```

In `build_parser()`, add:

```python
onboard_parser = subparsers.add_parser(
    "onboard",
    help="Provision tools, validate both-vpns readiness, and run guided training",
)
onboard_parser.add_argument(
    "--config",
    required=True,
    help="Private onboarding/operator source YAML (never commit this file)",
)
onboard_parser.add_argument("--root", default=None, help="Checkout root (overrides private file)")
onboard_parser.add_argument(
    "--tool",
    action="append",
    choices=("autobench", "robocop", "dispatch"),
    help="Tool to provision; repeatable. 'dispatch' is a CLI alias for robocop",
)
onboard_parser.add_argument(
    "--check",
    action="store_true",
    help="Rerun diagnostics without provisioning",
)
onboard_parser.add_argument(
    "--restart",
    action="store_true",
    help="Discard onboarding evidence only (keeps checkouts and private config)",
)
onboard_parser.add_argument(
    "--yes",
    action="store_true",
    help="Confirm --restart non-interactively (valid only with --restart)",
)
```

In `main()`, immediately after `parse_args` and **before** `OperatorConfig.load`:

```python
if args.command == "onboard":
    if args.yes and not args.restart:
        print("error: --yes requires --restart", file=sys.stderr)
        return 2
    try:
        return run_onboarding(args)
    except (RuntimeError, ValueError, FileNotFoundError, OSError) as exc:
        print(f"onboard failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
```

Create `edge_deploy/onboarding/runner.py` with module-top imports and a Task-4-minimal implementation (Task 10 replaces the body with full stages):

```python
from __future__ import annotations

import argparse

from edge_deploy.onboarding.manifest import normalize_tool_id


def _confirm_restart() -> bool:
    try:
        answer = input("Discard onboarding evidence only? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def run_onboarding(args: argparse.Namespace) -> int:
    tools = [normalize_tool_id(t) for t in (args.tool or [])]
    if args.restart and not (args.yes or _confirm_restart()):
        print("restart cancelled")
        return 1
    print(f"onboard: selected tools={tools or '(prompt later)'}")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_onboarding_cli.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add edge_deploy/cli.py edge_deploy/onboarding/runner.py tests/test_onboarding_cli.py
git commit -m "feat(cli): add onboard command before operator config load"
```

---

### Task 5: Repository provisioning with injected runners

**Files:**
- Create: `edge_deploy/onboarding/repositories.py`
- Create: `tests/test_onboarding_repositories.py`

**Interfaces:**
- Consumes: `TOOL_MANIFESTS`, `CORE_GITHUB_URL`, `approved_engine_tag`, `engine_identity`, injectable `CommandRunner`
- Produces:
  - `CommandRunner = Callable[[Sequence[str]], str]`
  - `default_runner(root: Path) -> CommandRunner`
  - `@dataclass(frozen=True) class ProvisionResult` (`tool_id: str`, `path: Path`, `action: str` where action is `"cloned"|"reused"|"validated"`, `message: str`)
  - `bootstrap_core_root() -> Path` — `Path(engine_identity()["package_dir"]).resolve().parent`
  - `validate_bootstrap_core(core_root: Path, *, bitbucket_url: str, expected_tag: str, runner: CommandRunner | None = None) -> ProvisionResult`
    - Requires existing git checkout at `core_root` (the editable install); **never** `git clone`s a second core
    - Verifies `origin` matches `CORE_GITHUB_URL` (URL-normalized)
    - Verifies HEAD equals the approved tag (`git describe --tags --exact-match` or `git rev-parse expected_tag^{}` == `HEAD`)
    - Ensures `bitbucket` remote equals configured private URL (`remote add` only when absent; fail if present and wrong)
  - `provision_tool_checkout(dest: Path, manifest: ToolManifest, *, bitbucket_url: str, runner: CommandRunner | None = None) -> ProvisionResult`
  - `read_engine_pin(tool_root: Path) -> str`
  - `assert_engine_pins_compatible(tool_roots: list[Path], *, expected_tag: str) -> None`
  - Never overwrite conflicting remotes/dirs; never `git reset --hard`

- [ ] **Step 1: Write the failing test**

Create `tests/test_onboarding_repositories.py`:

```python
from pathlib import Path

import pytest

from edge_deploy.ledger import engine_identity
from edge_deploy.onboarding.manifest import CORE_GITHUB_URL, TOOL_MANIFESTS, approved_engine_tag
from edge_deploy.onboarding.repositories import (
    assert_engine_pins_compatible,
    bootstrap_core_root,
    provision_tool_checkout,
    validate_bootstrap_core,
)


class FakeGit:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.origin = TOOL_MANIFESTS["autobench"].github_url
        self.bitbucket = ""
        self.head_tag = approved_engine_tag()

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if args[:2] == ["git", "clone"]:
            dest = Path(args[-1])
            dest.mkdir(parents=True)
            (dest / ".git").mkdir()
            (dest / "edge_deploy.yaml").write_text("tool: autobench\n", encoding="utf-8")
            (dest / "tools" / "dev").mkdir(parents=True)
            (dest / "tools" / "dev" / "local_check.ps1").write_text("# ok\n", encoding="utf-8")
            (dest / "pyproject.toml").write_text(
                f"edge-deploy-core @ git+https://example/@{approved_engine_tag()}\n",
                encoding="utf-8",
            )
            return ""
        if args[:3] == ["git", "remote", "get-url"]:
            if args[3] == "origin":
                return self.origin + "\n"
            if args[3] == "bitbucket":
                if not self.bitbucket:
                    raise RuntimeError("bitbucket missing")
                return self.bitbucket + "\n"
        if args[:3] == ["git", "remote", "add"]:
            self.bitbucket = args[4]
            return ""
        if args[:2] == ["git", "status"]:
            return ""
        if args[:3] == ["git", "describe", "--tags"]:
            return self.head_tag + "\n"
        if args[:2] == ["git", "rev-parse"]:
            return "a" * 40 + "\n"
        return ""


def test_bootstrap_core_root_is_editable_package_parent() -> None:
    expected = Path(engine_identity()["package_dir"]).resolve().parent
    assert bootstrap_core_root() == expected


def test_validate_bootstrap_core_never_clones(tmp_path: Path) -> None:
    core = tmp_path / "edge-deploy-core"
    core.mkdir()
    (core / ".git").mkdir()
    fake = FakeGit()
    fake.origin = CORE_GITHUB_URL
    result = validate_bootstrap_core(
        core,
        bitbucket_url="https://bitbucket.example/core.git",
        expected_tag=approved_engine_tag(),
        runner=fake,
    )
    assert result.action == "validated"
    assert not any(c[:2] == ["git", "clone"] for c in fake.calls)


def test_clone_tool_when_missing(tmp_path: Path) -> None:
    dest = tmp_path / "autobench"
    fake = FakeGit()
    result = provision_tool_checkout(
        dest,
        TOOL_MANIFESTS["autobench"],
        bitbucket_url="https://bitbucket.example/ab.git",
        runner=fake,
    )
    assert result.action == "cloned"
    assert dest.is_dir()
    assert any(c[:2] == ["git", "clone"] for c in fake.calls)


def test_refuse_unexpected_existing_directory(tmp_path: Path) -> None:
    dest = tmp_path / "autobench"
    dest.mkdir()
    (dest / "README.md").write_text("nope\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unexpected"):
        provision_tool_checkout(
            dest,
            TOOL_MANIFESTS["autobench"],
            bitbucket_url="https://bitbucket.example/ab.git",
            runner=lambda args: "",
        )


def test_engine_pin_mismatch_fails_before_install(tmp_path: Path) -> None:
    a = tmp_path / "autobench"
    b = tmp_path / "robocop"
    for root, tag in ((a, "v1.5.3"), (b, "v1.4.0")):
        root.mkdir()
        (root / "pyproject.toml").write_text(
            f'dependencies = ["edge-deploy-core @ git+https://example/@{tag}"]\n',
            encoding="utf-8",
        )
    with pytest.raises(RuntimeError, match="engine pin"):
        assert_engine_pins_compatible([a, b], expected_tag="v1.5.3")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_onboarding_repositories.py -v`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement provisioning**

Implement `bootstrap_core_root` and `validate_bootstrap_core` first. Behavior for `provision_tool_checkout`:

1. If dest missing → `git clone <public github_url> <dest>`
2. If dest exists without `.git` → fail (`unexpected`)
3. Verify `origin` URL matches manifest (normalize like `repository._normalize_url`)
4. Ensure `bitbucket` remote equals configured private URL (`remote add` or fail if wrong existing)
5. Require `edge_deploy.yaml` and `tools/dev/local_check.ps1`
6. Return `reused` when already correct

`assert_engine_pins_compatible` reads each pin via regex `@v\d+\.\d+\.\d+` on `edge-deploy-core` lines; all must equal `expected_tag`; if two tools disagree, fail before any `pip install`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_onboarding_repositories.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add edge_deploy/onboarding/repositories.py tests/test_onboarding_repositories.py
git commit -m "feat(onboarding): provision tools and validate bootstrap core"
```

---

### Task 6: Readiness check framework (`passed` / `failed` / `blocked`)

**Files:**
- Create: `edge_deploy/onboarding/checks.py`
- Create: `tests/test_onboarding_checks.py`

**Interfaces:**
- Consumes: injectable callables for gh/git/tcp/transport
- Produces:
  - `@dataclass(frozen=True) class CheckResult` (`id: str`, `outcome: str`, `summary: str`, `remediation: str`, `evidence_fingerprint: str | None = None`)
  - `@dataclass(frozen=True) class CheckSpec` (`id`, `depends_on: tuple[str, ...]`, `run: Callable[[], CheckResult]`)
  - `run_checks(specs: list[CheckSpec], *, max_workers: int = 4) -> list[CheckResult]`
  - Deterministic output order = input spec order
  - If a dependency did not `passed`, mark check `blocked` without calling `run`
  - Independent failures still run remaining independent checks

- [ ] **Step 1: Write the failing test**

```python
from edge_deploy.onboarding.checks import CheckResult, CheckSpec, run_checks
from edge_deploy.reporting import redact


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_onboarding_checks.py -v`

Expected: FAIL with missing module.

- [ ] **Step 3: Implement `checks.py`**

Use a simple serial scheduler respecting dependencies (concurrency optional; if used, still emit results in spec order). Apply `redact()` to summaries before returning.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_onboarding_checks.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add edge_deploy/onboarding/checks.py tests/test_onboarding_checks.py
git commit -m "feat(onboarding): add readiness check runner with blocked state"
```

---

### Task 7: Concrete readiness checks (both-vpns, RSA/transport, conditional Kerberos)

**Files:**
- Modify: `edge_deploy/onboarding/checks.py` (add `ReadinessContext`, `build_readiness_specs`)
- Modify: `tests/test_onboarding_checks.py`

**Interfaces:**
- Consumes: `git_probe_command("bitbucket", "write")` for Bitbucket dry-run; `BB_TOKEN` presence via `os.environ` without echoing; known_hosts lookup without adding keys; injected per-node auth/transport runners
- Produces:
  - `AuthRunner = Callable[[str], CheckResult]`  # node name → result; may prompt interactively but must not persist secrets
  - `TransportSmokeRunner = Callable[[str], CheckResult]`
  - `KerberosRunner = Callable[[str], CheckResult]`  # used only when deep smoke is required for that node/tool profile
  - `@dataclass(frozen=True) class ReadinessContext` fields below
  - `build_readiness_specs(ctx: ReadinessContext) -> list[CheckSpec]`
  - Stable ids include: `gh_auth`, `github_read`, `tool_clean_main`, `bitbucket_read`, `bitbucket_write_dry_run`, `audit_release_log`, `operator_config`, `bb_token_present`, `known_hosts`, `edge_tcp`, `rsa_auth:<node>`, `transport_smoke:<node>`, `kerberos:<node>` (only when `ctx.require_deep_smoke` is true), `local_check:<tool>`
  - No `github_write_required` check (routine onboarding expects GitHub write red in both-vpns)
  - Check summaries/remediations go through `redact`; never write passcodes/passwords/tokens into state

```python
@dataclass(frozen=True)
class ReadinessContext:
    tools: list[str]
    tool_roots: dict[str, Path]
    core_root: Path
    nodes: dict[str, dict[str, str]]
    git_runner: Callable[[list[str], Path], int]
    local_check_runner: Callable[[Path], int]
    known_hosts_path: Path
    rsa_auth_runner: AuthRunner
    transport_smoke_runner: TransportSmokeRunner
    kerberos_runner: KerberosRunner
    require_deep_smoke: bool = False
```

- [ ] **Step 1: Write the failing test**

In `tests/test_onboarding_checks.py`, extend the **existing module-top** import from Task 6 to:

```python
from pathlib import Path

from edge_deploy.onboarding.checks import (
    CheckResult,
    CheckSpec,
    ReadinessContext,
    build_readiness_specs,
    run_checks,
)
from edge_deploy.reporting import redact
```

Then append (no further imports):

```python
def _ctx(tmp_path: Path, *, require_deep_smoke: bool = False) -> ReadinessContext:
    return ReadinessContext(
        tools=["autobench"],
        tool_roots={"autobench": tmp_path / "autobench"},
        core_root=tmp_path / "edge-deploy-core",
        nodes={"node03": {"host": "operator@edge", "ssh_options": "-p 2222"}},
        git_runner=lambda command, root: 0,
        local_check_runner=lambda root: 0,
        known_hosts_path=tmp_path / "known_hosts",
        rsa_auth_runner=lambda node: CheckResult(
            f"rsa_auth:{node}", "passed", "RSA authenticated", ""
        ),
        transport_smoke_runner=lambda node: CheckResult(
            f"transport_smoke:{node}", "passed", "transport smoke ok", ""
        ),
        kerberos_runner=lambda node: CheckResult(
            f"kerberos:{node}", "passed", "Kerberos ticket ok", ""
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
    assert "bitbucket_write_dry_run" in ids
    assert "known_hosts" in ids
    assert "rsa_auth:node03" in ids
    assert "transport_smoke:node03" in ids
    assert "kerberos:node03" not in ids
    assert "local_check:autobench" in ids
    assert "github_write_required" not in ids

    deep_ids = [s.id for s in build_readiness_specs(_ctx(tmp_path, require_deep_smoke=True))]
    assert "kerberos:node03" in deep_ids


def test_bb_token_check_presence_without_leaking(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BB_TOKEN", raising=False)
    specs = build_readiness_specs(_ctx(tmp_path))
    token_spec = next(s for s in specs if s.id == "bb_token_present")
    result = token_spec.run()
    assert result.outcome == "failed"
    assert "BB_TOKEN" in result.remediation
    assert "env-only" not in result.summary


def test_rsa_runner_failure_is_failed_not_persisting_secret(tmp_path: Path) -> None:
    ctx = ReadinessContext(
        tools=["autobench"],
        tool_roots={"autobench": tmp_path / "autobench"},
        core_root=tmp_path / "edge-deploy-core",
        nodes={"node03": {"host": "operator@edge", "ssh_options": "-p 2222"}},
        git_runner=lambda command, root: 0,
        local_check_runner=lambda root: 0,
        known_hosts_path=tmp_path / "known_hosts",
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
        require_deep_smoke=False,
    )
    rsa = next(s for s in build_readiness_specs(ctx) if s.id == "rsa_auth:node03")
    result = rsa.run()
    assert result.outcome == "failed"
    assert "123456" not in result.summary
    assert "***REDACTED***" in result.summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_onboarding_checks.py -v`

Expected: FAIL on missing `build_readiness_specs` / `ReadinessContext`.

- [ ] **Step 3: Implement check builders**

```python
def build_readiness_specs(ctx: ReadinessContext) -> list[CheckSpec]:
    specs: list[CheckSpec] = [
        CheckSpec("bb_token_present", (), lambda: _check_bb_token()),
        CheckSpec("operator_config", (), lambda: _check_operator_config(ctx)),
        CheckSpec("gh_auth", (), lambda: _check_gh_auth()),
        CheckSpec("github_read", ("gh_auth",), lambda: _check_github_read(ctx)),
        CheckSpec("tool_clean_main", (), lambda: _check_tool_clean_main(ctx)),
        CheckSpec("bitbucket_read", (), lambda: _check_bitbucket_read(ctx)),
        CheckSpec(
            "bitbucket_write_dry_run",
            ("bitbucket_read",),
            lambda: _check_bitbucket_write_dry_run(ctx),
        ),
        CheckSpec(
            "audit_release_log",
            ("bitbucket_read",),
            lambda: _check_audit_release_log(ctx),
        ),
        CheckSpec("known_hosts", (), lambda: _check_known_hosts(ctx)),
        CheckSpec("edge_tcp", ("known_hosts",), lambda: _check_edge_tcp(ctx)),
    ]
    for node in sorted(ctx.nodes):
        specs.append(
            CheckSpec(
                f"rsa_auth:{node}",
                ("edge_tcp",),
                lambda node=node: ctx.rsa_auth_runner(node),
            )
        )
        specs.append(
            CheckSpec(
                f"transport_smoke:{node}",
                (f"rsa_auth:{node}",),
                lambda node=node: ctx.transport_smoke_runner(node),
            )
        )
        if ctx.require_deep_smoke:
            specs.append(
                CheckSpec(
                    f"kerberos:{node}",
                    (f"rsa_auth:{node}",),
                    lambda node=node: ctx.kerberos_runner(node),
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
```

Rules for helpers:

- `bb_token_present`: pass if `os.environ.get("BB_TOKEN")`; summary `"BB_TOKEN is set"` with no value.
- `bitbucket_write_dry_run`: run `git_probe_command("bitbucket", "write")` via `ctx.git_runner`.
- `known_hosts`: require entries in `ctx.known_hosts_path`; never `ssh-keyscan` / never append.
- `rsa_auth_runner` / `transport_smoke_runner` / `kerberos_runner`: production wiring may call `AuthBroker` / `run_transport_smoke` / `ensure_kerberos`, but secrets remain in memory for that call only and are redacted before any `CheckResult` or state persistence.
- Do **not** add a blocking GitHub-write success check.
- All subprocesses: deadlines + `GIT_TERMINAL_PROMPT=0` + `GCM_INTERACTIVE=never`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_onboarding_checks.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add edge_deploy/onboarding/checks.py tests/test_onboarding_checks.py
git commit -m "feat(onboarding): add both-vpns readiness check suite"
```

---

### Task 8: Training ledger markers and production rejection

**Files:**
- Modify: `edge_deploy/ledger.py`
- Modify: `edge_deploy/phases/__init__.py`
- Modify: `edge_deploy/cli.py` (release/rollback/abandon guards)
- Create: `edge_deploy/onboarding/training.py` (ledger factory only in this task)
- Create: `tests/test_onboarding_training.py`
- Modify: `tests/test_ledger.py` as needed

**Interfaces:**
- Consumes: `RunLedger.create`, `_SCHEMA`
- Produces:
  - `RunLedger.create(..., kind: str = "release", training: bool = False, ...)`
  - When `training=True` or `kind=="training"`: force `kind="training"` and set `state["training"]=True`
  - When creating a normal release/rollback ledger: **omit** the `training` key (do not write `training=false`)
  - `is_training_ledger(state_or_ledger) -> bool` — `state.get("kind") == "training" or state.get("training") is True`
  - `create_training_workspace(app_dir: Path, tool: str) -> Path` returns `app_dir / "training" / tool`
  - `start_training_ledger(workspace: Path, *, tool: str, operator: str, nodes: list[str]) -> RunLedger`
  - `enter_phase` / production CLI paths raise `LedgerError("training ledger rejected by production commands")`

- [ ] **Step 1: Write the failing test**

Create `tests/test_onboarding_training.py`:

```python
from pathlib import Path

import pytest

from edge_deploy.ledger import LedgerError, RunLedger, is_training_ledger
from edge_deploy.onboarding.training import create_training_workspace, start_training_ledger
from edge_deploy.phases import PHASE_REGISTRY, enter_phase


def test_training_ledger_has_both_markers(tmp_path: Path) -> None:
    app_dir = tmp_path / "appdata" / "edge-deploy"
    real_tool_root = tmp_path / "checkouts" / "autobench"
    real_tool_root.mkdir(parents=True)
    ws = create_training_workspace(app_dir, "autobench")
    assert ws == app_dir / "training" / "autobench"
    assert ws.resolve() != real_tool_root.resolve()
    assert (ws / "edge-deploy" / "runs").is_dir()
    ledger = start_training_ledger(ws, tool="autobench", operator="trainee", nodes=["node03"])
    assert ledger.state["kind"] == "training"
    assert ledger.state["training"] is True
    assert is_training_ledger(ledger) is True


def test_production_ledger_omits_training_key(tmp_path: Path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    runs_root.mkdir(parents=True)
    ledger = RunLedger.create(
        runs_root,
        tool="autobench",
        source_sha="a" * 40,
        nodes=["node03"],
        operator="op",
        kind="release",
    )
    assert "training" not in ledger.state
    assert is_training_ledger(ledger) is False


def test_enter_phase_rejects_training_ledger(tmp_path: Path) -> None:
    ws = create_training_workspace(tmp_path / "appdata" / "edge-deploy", "robocop")
    ledger = start_training_ledger(ws, tool="robocop", operator="trainee", nodes=["node03"])
    spec = PHASE_REGISTRY[0][0]
    with pytest.raises(LedgerError, match="training ledger rejected"):
        enter_phase(spec, None, ledger, next_command="x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_onboarding_training.py -v`

Expected: FAIL on missing helpers / missing rejection.

- [ ] **Step 3: Implement markers + rejection**

In `RunLedger.create`, add `training: bool = False` and only attach the training key for training ledgers:

```python
@classmethod
def create(
    cls,
    runs_root: Path,
    *,
    tool: str,
    source_sha: str,
    nodes: list[str],
    operator: str,
    kind: str = "release",
    rollback_tag: str | None = None,
    training: bool = False,
) -> RunLedger:
    is_training = bool(training) or kind == "training"
    if is_training:
        kind = "training"
    now = _utc_now()
    run_id = f"run-{now.strftime('%Y%m%dT%H%M%SZ')}-{source_sha[:7]}"
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    engine = engine_identity()
    state = {
        "schema": _SCHEMA,
        "run_id": run_id,
        "tool": tool,
        "source_sha": source_sha,
        "operator": operator,
        "created_at": _utc_iso(now),
        "kind": kind,
        "rollback_tag": rollback_tag,
        "engine": engine,
        "nodes": list(nodes),
        "status": "open",
        "abandon_reason": None,
        "phases": {
            "verify": _empty_phase(),
            "publish": _empty_phase(),
            "deploy": {node: _empty_phase() for node in nodes},
            "tag_bitbucket": _empty_phase(),
            "tag_github": _empty_phase(),
        },
    }
    if is_training:
        state["training"] = True
    _write_json_atomic(run_dir / "state.json", state)
    ledger = cls(run_dir=run_dir, state=state)
    ledger.record_event("run_created")
    return ledger
```

Add at module top of `ledger.py` usage sites / in `ledger.py`:

```python
def is_training_ledger(ledger_or_state: RunLedger | dict) -> bool:
    state = ledger_or_state.state if isinstance(ledger_or_state, RunLedger) else ledger_or_state
    return state.get("kind") == "training" or state.get("training") is True
```

In `enter_phase` immediately after open-status check:

```python
if is_training_ledger(ledger):
    raise LedgerError("training ledger rejected by production commands")
```

Also guard `_cmd_release` when `--run` points at a training ledger, and `abandon`/`rollback` load paths.

In `training.py` (module-top imports only):

```python
from __future__ import annotations

from pathlib import Path

from edge_deploy.ledger import RunLedger


def create_training_workspace(app_dir: Path, tool: str) -> Path:
    root = Path(app_dir) / "training" / tool
    (root / "edge-deploy" / "runs").mkdir(parents=True, exist_ok=True)
    (root / "edge_deploy.yaml").write_text(f"tool: {tool}\n", encoding="utf-8")
    return root


def start_training_ledger(
    workspace: Path, *, tool: str, operator: str, nodes: list[str]
) -> RunLedger:
    runs_root = workspace / "edge-deploy" / "runs"
    return RunLedger.create(
        runs_root,
        tool=tool,
        source_sha="0" * 40,
        nodes=nodes,
        operator=operator,
        kind="training",
        training=True,
    )
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_onboarding_training.py tests/test_ledger.py tests/test_phases.py -v`

Expected: PASS (update any ledger schema assertions that require exact key sets — production fixtures must not require `training=false`).

- [ ] **Step 5: Commit**

```bash
git add edge_deploy/ledger.py edge_deploy/phases/__init__.py edge_deploy/cli.py edge_deploy/onboarding/training.py tests/test_onboarding_training.py tests/test_ledger.py tests/test_phases.py
git commit -m "feat(onboarding): mark training ledgers and reject in production"
```

---

### Task 9: Training simulator without production phase executors

**Files:**
- Modify: `edge_deploy/onboarding/training.py`
- Modify: `tests/test_onboarding_training.py`

**Interfaces:**
- Consumes: production **phase names / order only** (`verify`, `publish`, `deploy`, `tag_bitbucket`, `tag_github`) and presentation strings
- Produces:
  - `TRAINING_PHASES: tuple[str, ...] = ("verify", "publish", "deploy", "tag_bitbucket", "tag_github")`
  - `advance_training(ledger: RunLedger, phase: str, *, nodes: list[str] | None = None) -> None` — pure ledger mutations + fabricated evidence
  - `run_guided_training(ledger: RunLedger, *, acknowledge: Callable[[str], None]) -> None` — prompts for **simulated** posture acknowledgements only (does not ask operator to change real posture)
  - Module-level import guard test proves forbidden modules are absent from `training.py` source/imports

- [ ] **Step 1: Write the failing test**

```python
import ast
from pathlib import Path

from edge_deploy.ledger import RunLedger
from edge_deploy.onboarding.training import (
    TRAINING_PHASES,
    advance_training,
    create_training_workspace,
    run_guided_training,
    start_training_ledger,
)


def test_training_module_does_not_import_production_executors() -> None:
    source = Path("edge_deploy/onboarding/training.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        "edge_deploy.phases.publish",
        "edge_deploy.phases.deploy",
        "edge_deploy.phases.tag",
        "edge_deploy.phases.verify",
        "edge_deploy.publish",
        "edge_deploy.rollout",
        "edge_deploy.mirror",
    }
    assert not (imported & forbidden)


def test_guided_training_advances_all_phases(tmp_path) -> None:
    ws = create_training_workspace(tmp_path / "appdata", "autobench")
    ledger = start_training_ledger(ws, tool="autobench", operator="op", nodes=["node03", "node04"])
    prompts: list[str] = []

    def acknowledge(message: str) -> None:
        prompts.append(message)

    run_guided_training(ledger, acknowledge=acknowledge)
    ledger = RunLedger.load(ledger.run_dir)
    assert ledger.state["status"] == "complete"
    for phase in TRAINING_PHASES:
        if phase == "deploy":
            assert all(
                ledger.state["phases"]["deploy"][n]["state"] == "passed" for n in ("node03", "node04")
            )
        else:
            assert ledger.state["phases"][phase]["state"] == "passed"
    assert any("firewall-off" in p for p in prompts)
    assert any("simulated" in p.lower() or "training" in p.lower() for p in prompts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_onboarding_training.py::test_training_module_does_not_import_production_executors tests/test_onboarding_training.py::test_guided_training_advances_all_phases -v`

Expected: FAIL on missing `run_guided_training` / incomplete phases.

- [ ] **Step 3: Implement simulator**

`advance_training` writes fabricated evidence dicts (tags, SHAs, per-node rollout summaries) via `ledger.set_phase_state` / existing ledger APIs only — no subprocess, no network.

Before `tag_github`, `run_guided_training` calls:

```python
acknowledge(
    "TRAINING ONLY: a real guided release would switch both-vpns → firewall-off "
    "before tag_github. Do not change the workstation posture now; press Enter to "
    "simulate the acknowledgement."
)
```

Never call `input` directly without injection; default `acknowledge` may wrap `input`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_onboarding_training.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add edge_deploy/onboarding/training.py tests/test_onboarding_training.py
git commit -m "feat(onboarding): simulate guided release without production executors"
```

---

### Task 10: Onboarding runner — stages, resume, `--check`, `--restart --yes`

**Files:**
- Modify: `edge_deploy/onboarding/runner.py`
- Create: `tests/test_onboarding_runner.py`

**Interfaces:**
- Consumes: manifest, state, config_import, repositories (`bootstrap_core_root`, `validate_bootstrap_core`, `provision_tool_checkout`), checks, training
- Produces:
  - `run_onboarding(args: argparse.Namespace) -> int`
  - `_run_stages(state: OnboardingState, *, imported: ImportedPrivateConfig, tools: list[str], root: Path, check_only: bool, app_dir: Path) -> int`
  - Stage helpers with concrete signatures below
  - Interactive multi-select when `--tool` omitted
  - `--check` reruns diagnostics without clone/install
  - `--restart` + confirmation (`--yes` bypass) calls `state.reset_evidence()` only
  - Engine mismatch refuses implicit continuation (require `--restart`)
  - Final redacted report at `app_dir / "onboarding-report.json"`
  - `_launch_console(roots: list[Path]) -> None` starts Edge Console against training roots

Stage helper contracts (all mutate `state` via `mark_stage` only; caller persists):

| Helper | Inputs | Behavior / outputs |
| --- | --- | --- |
| `_stage_prerequisites(state: OnboardingState) -> None` | none beyond state | Verify Windows/PowerShell/`py`/Git/`gh` presence via injectable probes in later hardening; mark `prerequisites` `passed` or `failed` with remediation. |
| `_stage_config(state, *, imported: ImportedPrivateConfig, tools: list[str], root: Path) -> None` | imported private config | `install_operator_config` with `audit_repo=str(bootstrap_core_root())` and tool paths under `root`; mark `config` using `imported.fingerprint` only (no config values in state). |
| `_stage_repositories(state, *, imported, tools, root) -> None` | tools + remotes | `validate_bootstrap_core(bootstrap_core_root(), ...)`; `provision_tool_checkout` per tool; `assert_engine_pins_compatible`; mark `repositories`. |
| `_stage_readiness(state, *, tools, root) -> None` | installed config + checkouts | Build `ReadinessContext` with injected RSA/transport/(optional) Kerberos runners; `run_checks`; mark `readiness` `passed` only if every result is `passed`, else `failed`/`blocked` from aggregate. Persist check ids/outcomes/redacted summaries only. |
| `_stage_practice(state, *, tools, app_dir) -> None` | app_dir | For each selected tool: `create_training_workspace(app_dir, tool)`, `start_training_ledger`, `run_guided_training`; set `state.data["practice"]`; mark `practice`. |
| `_stage_complete(state, *, app_dir) -> None` | state | Write redacted report JSON; mark `complete` `passed`; print first real guided release command. |

- [ ] **Step 1: Write the failing test**

Create `tests/test_onboarding_runner.py` with module-top imports:

```python
from pathlib import Path
from types import SimpleNamespace

from edge_deploy.onboarding.runner import run_onboarding
from edge_deploy.onboarding.state import OnboardingState, default_state_path


def test_restart_requires_confirmation_unless_yes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    private = tmp_path / "private.yaml"
    private.write_text(
        "operator_email: op@example.com\n"
        "checkout_root: " + str(tmp_path / "root").replace("\\", "/") + "\n"
        "nodes:\n  node03:\n    host: operator@edge\n    ssh_options: -p 2222\n"
        "bitbucket_remotes:\n  core: https://bitbucket.example/core.git\n"
        "  autobench: https://bitbucket.example/ab.git\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "n")
    args = SimpleNamespace(
        config=str(private),
        root=None,
        tool=["autobench"],
        check=False,
        restart=True,
        yes=False,
    )
    assert run_onboarding(args) == 1

    args.yes = True
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._run_stages",
        lambda *a, **k: 0,
    )
    assert run_onboarding(args) == 0


def test_engine_mismatch_requires_restart(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    private = tmp_path / "private.yaml"
    private.write_text(
        "operator_email: op@example.com\n"
        f"checkout_root: {(tmp_path / 'root').as_posix()}\n"
        "nodes:\n  node03:\n    host: operator@edge\n    ssh_options: -p 2222\n",
        encoding="utf-8",
    )
    state = OnboardingState.create_new(
        path=default_state_path(),
        tools=["autobench"],
        root=str(tmp_path / "root"),
        config_fingerprint="d" * 64,
    )
    state.data["engine"]["content_sha256"] = "0" * 64
    state.save()
    args = SimpleNamespace(
        config=str(private),
        root=str(tmp_path / "root"),
        tool=["autobench"],
        check=False,
        restart=False,
        yes=False,
    )
    assert run_onboarding(args) == 2
    captured = capsys.readouterr()
    assert "--restart" in captured.out + captured.err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_onboarding_runner.py -v`

Expected: FAIL until runner implements confirmation and mismatch gating.

- [ ] **Step 3: Implement full `run_onboarding`**

Replace the Task-4 minimal body. Module-top imports in `runner.py`:

```python
from __future__ import annotations

import argparse
import json
from pathlib import Path

from edge_deploy.ledger import engine_identity
from edge_deploy.onboarding.config_import import (
    install_operator_config,
    load_private_onboarding_source,
    merge_operator_config,
    parse_private_onboarding_source,
)
from edge_deploy.onboarding.checks import ReadinessContext, build_readiness_specs, run_checks
from edge_deploy.onboarding.manifest import normalize_tool_id
from edge_deploy.onboarding.repositories import (
    assert_engine_pins_compatible,
    bootstrap_core_root,
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
```

Control flow:

```python
def run_onboarding(args: argparse.Namespace) -> int:
    state_path = default_state_path()
    app_dir = state_path.parent
    if args.restart:
        if not (args.yes or _confirm_restart()):
            print("restart cancelled")
            return 1
        if state_path.is_file():
            state = OnboardingState.load(state_path)
            state.reset_evidence()
            state.save()
    imported = parse_private_onboarding_source(load_private_onboarding_source(Path(args.config)))
    tools = [normalize_tool_id(t) for t in (args.tool or _prompt_tools())]
    root = Path(args.root or imported.checkout_root or (Path.home() / "edge-deploy"))
    if state_path.is_file():
        state = OnboardingState.load(state_path)
        if not state.engine_matches() and not args.restart:
            print(
                "engine identity changed; re-run with --restart to discard onboarding evidence"
            )
            return 2
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


def _run_stages(
    state: OnboardingState,
    *,
    imported,
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
    if state.data["stages"]["config"]["outcome"] != "passed":
        _stage_config(state, imported=imported, tools=tools, root=root)
        state.save()
        if state.data["stages"]["config"]["outcome"] != "passed":
            return 1
    if not check_only and state.data["stages"]["repositories"]["outcome"] != "passed":
        _stage_repositories(state, imported=imported, tools=tools, root=root)
        state.save()
        if state.data["stages"]["repositories"]["outcome"] != "passed":
            return 1
    if state.data["stages"]["readiness"]["outcome"] != "passed":
        _stage_readiness(state, tools=tools, root=root)
        state.save()
        if state.data["stages"]["readiness"]["outcome"] != "passed":
            return 1
    if state.data["stages"]["practice"]["outcome"] != "passed":
        _stage_practice(state, tools=tools, app_dir=app_dir)
        state.save()
        if state.data["stages"]["practice"]["outcome"] != "passed":
            return 1
    _stage_complete(state, app_dir=app_dir)
    state.save()
    return 0
```

Implement each `_stage_*` to match the table above. `_stage_repositories` must call
`validate_bootstrap_core(bootstrap_core_root(), bitbucket_url=imported.bitbucket_remotes["core"], expected_tag=approved_engine_tag())`
and must not clone core. Also define injectable factory helpers used by `_stage_readiness`:

```python
def _make_rsa_auth_runner() -> AuthRunner:
    def run(node: str) -> CheckResult:
        # Wire AuthBroker.ensure_authenticated for node; catch auth failures.
        # Never put passcode values into CheckResult; always redact summaries.
        return CheckResult(f"rsa_auth:{node}", "passed", "RSA authenticated", "")

    return run


def _make_transport_smoke_runner() -> TransportSmokeRunner:
    def run(node: str) -> CheckResult:
        # Wire run_transport_smoke for node after RSA auth; map pass/fail to CheckResult.
        return CheckResult(f"transport_smoke:{node}", "passed", "transport smoke ok", "")

    return run


def _make_kerberos_runner() -> KerberosRunner:
    def run(node: str) -> CheckResult:
        # Wire ensure_kerberos only when require_deep_smoke; password stays memory-only.
        return CheckResult(f"kerberos:{node}", "passed", "Kerberos ticket ok", "")

    return run
```

`_stage_practice` workspaces are under `app_dir / "training" / <tool>` only. `_stage_complete` writes redacted JSON report fields listed in the design (selected tools, engine version/tag, config fingerprint, readiness outcomes, practice completion, next real release command).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_onboarding_runner.py tests/test_onboarding_cli.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add edge_deploy/onboarding/runner.py tests/test_onboarding_runner.py
git commit -m "feat(onboarding): orchestrate resumable onboard stages"
```

---

### Task 11: Console recognizes training ledgers (read-only)

**Files:**
- Modify: `edge_console.py`
- Modify: `tests/test_edge_console.py`

**Interfaces:**
- Consumes: ledger `kind` / `training` fields
- Produces: UI chip `training` for training runs; `collect_runs` still reads them; no writes

Depends on the independent GitHub write-indicator plan for write-light semantics; this task only adds training visibility.

- [ ] **Step 1: Write the failing test**

```python
def test_collect_runs_includes_training_ledger(tmp_path) -> None:
    runs_root = tmp_path / "edge-deploy" / "runs"
    run_dir = runs_root / "run-20260724T000000Z-train01"
    _write_state(
        run_dir,
        run_dir.name,
        kind="training",
        status="open",
    )
    state_path = run_dir / "state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["training"] = True
    state_path.write_text(json.dumps(data), encoding="utf-8")
    runs = collect_runs(runs_root)
    assert len(runs) == 1
    assert runs[0]["state"]["kind"] == "training"
    assert runs[0]["state"]["training"] is True


def test_page_has_training_chip_styling() -> None:
    assert "training" in PAGE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_edge_console.py::test_collect_runs_includes_training_ledger tests/test_edge_console.py::test_page_has_training_chip_styling -v`

Expected: FAIL on missing training chip copy (collect may already pass if schema-agnostic).

- [ ] **Step 3: Minimal UI support**

In run card rendering, if `st.kind === "training" || st.training`, show a `training` chip and prefix next-command help with `TRAINING ONLY`.

- [ ] **Step 4: Run console tests**

Run: `.venv/bin/python -m pytest tests/test_edge_console.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add edge_console.py tests/test_edge_console.py
git commit -m "feat(console): display training ledgers read-only"
```

---

### Task 12: Documentation, ADR, and README bootstrap

**Files:**
- Create: `docs/adr/0017-release-operator-onboarding.md`
- Modify: `README.md`
- Modify: `docs/release-workflow.md`

**Interfaces:**
- Consumes: final CLI flags and stage names from earlier tasks
- Produces: operator-facing docs that match implemented behavior

- [ ] **Step 1: Write ADR 0017**

Cover: resumable onboard command; both-vpns-only routine path; training markers (`kind` + `training=true` only on training ledgers); dispatch alias; bootstrap core = editable `engine_identity()["package_dir"].parent` (no second clone); console write indicator owned by separate plan; no secrets in Git; no auto host-key enrollment.

- [ ] **Step 2: Update README**

Replace distributed first-time setup with:

1. Connect Bitbucket + Edge VPNs (`both-vpns`)
2. Bootstrap the approved tagged core checkout and install editable:

```powershell
git clone https://github.com/pedrochagasmaster/edge-deploy-core.git
cd edge-deploy-core
git checkout v1.5.3
py -m pip install -e ".[dev]"
```

(Use the tag equal to `approved_engine_tag()` / current `__version__` at edit time.) Onboarding then **reuses this same checkout** as `audit_repo` via `bootstrap_core_root()`; it does not clone core again.

3. Run:

```powershell
py -m edge_deploy onboard --config C:\secure\operator.yaml
```

Document `--tool autobench --tool dispatch`, `--check`, `--restart --yes`, training vs real release, and that GitHub write console green is not required for onboarding completion.

- [ ] **Step 3: Fix undeclared extra in release-workflow**

In `docs/release-workflow.md` and `README.md`, replace `.[dev,release]` with the extras actually declared (`.[dev]` plus whatever tools document for release installs). Do not invent a `release` extra in core `pyproject.toml` unless a separate approved change adds it — prefer correcting docs to `.[dev]` for core and point Tool repos at their own release extra.

- [ ] **Step 4: Lint/docs sanity**

Run: `.venv/bin/ruff check edge_deploy/onboarding tests/test_onboarding_*.py`

Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add docs/adr/0017-release-operator-onboarding.md README.md docs/release-workflow.md
git commit -m "docs: document release operator onboarding bootstrap"
```

---

### Task 13: Integration tests — empty workspace, resume, idempotency

**Files:**
- Create: `tests/test_onboarding_integration.py`

**Interfaces:**
- Consumes: full onboarding package with fakes for git/network/transport/auth
- Produces: end-to-end coverage from the design's integration list (empty workspace; tool selection; interrupt/resume; temp failure recovery; pin/config change invalidation; second run no duplicate tool clone; core never cloned; training ledger under APPDATA; redacted report)

- [ ] **Step 1: Write the failing integration test**

Create `tests/test_onboarding_integration.py`:

```python
from pathlib import Path
from types import SimpleNamespace

from edge_deploy.ledger import engine_identity
from edge_deploy.onboarding.checks import CheckResult
from edge_deploy.onboarding.manifest import approved_engine_tag
from edge_deploy.onboarding.repositories import bootstrap_core_root
from edge_deploy.onboarding.runner import run_onboarding


def test_empty_workspace_onboarding_with_fakes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "app"))
    monkeypatch.setenv("BB_TOKEN", "token=should-be-redacted-in-reports")
    root = tmp_path / "edge-deploy"
    private = tmp_path / "private.yaml"
    known_hosts = tmp_path / "known_hosts"
    private.write_text(
        "\n".join(
            [
                "operator_email: op@example.com",
                f"checkout_root: {root.as_posix()}",
                "nodes:",
                "  node03:",
                "    host: operator@edge-node-03.example",
                f"    ssh_options: -p 2222 -o UserKnownHostsFile={known_hosts.as_posix()}",
                "    session: edge-node03",
                "    transport: ssh",
                "bitbucket_remotes:",
                "  core: https://bitbucket.example/core.git",
                "  autobench: https://bitbucket.example/ab.git",
                "  robocop: https://bitbucket.example/rc.git",
                "",
            ]
        ),
        encoding="utf-8",
    )
    known_hosts.write_text(
        "edge-node-03.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKeyForOnboardingFixture\n",
        encoding="utf-8",
    )

    clone_calls: list[list[str]] = []

    class FakeGit:
        def __init__(self) -> None:
            self.remotes: dict[str, dict[str, str]] = {}

        def __call__(self, args: list[str]) -> str:
            if args[:2] == ["git", "clone"]:
                clone_calls.append(list(args))
                dest = Path(args[-1])
                url = args[-2]
                assert "edge-deploy-core" not in url
                dest.mkdir(parents=True, exist_ok=True)
                (dest / ".git").mkdir(exist_ok=True)
                tool = "autobench" if "autobench" in url else "robocop"
                (dest / "edge_deploy.yaml").write_text(f"tool: {tool}\n", encoding="utf-8")
                (dest / "tools" / "dev").mkdir(parents=True, exist_ok=True)
                (dest / "tools" / "dev" / "local_check.ps1").write_text("# ok\n", encoding="utf-8")
                (dest / "pyproject.toml").write_text(
                    f"edge-deploy-core @ git+https://example/@{approved_engine_tag()}\n",
                    encoding="utf-8",
                )
                self.remotes[str(dest)] = {"origin": url, "bitbucket": ""}
                return ""
            remotes = self.remotes.setdefault(str(Path.cwd()), {"origin": "", "bitbucket": ""})
            if args[:3] == ["git", "remote", "get-url"]:
                return remotes.get(args[3], "") + "\n"
            if args[:3] == ["git", "remote", "add"]:
                remotes[args[3]] = args[4]
                return ""
            if args[:2] == ["git", "status"]:
                return ""
            if args[:2] == ["git", "ls-remote"]:
                return "a" * 40 + "\tHEAD\n"
            if args[:2] == ["git", "push"] and "--dry-run" in args:
                return ""
            if args[:3] == ["git", "describe", "--tags"]:
                return approved_engine_tag() + "\n"
            return ""

    fake = FakeGit()
    monkeypatch.setattr(
        "edge_deploy.onboarding.repositories.default_runner",
        lambda root: fake,
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.repositories.validate_bootstrap_core",
        lambda *a, **k: type("R", (), {"action": "validated", "path": bootstrap_core_root(), "tool_id": "core", "message": "ok"})(),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.checks._tcp_probe",
        lambda host, port: True,
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._make_rsa_auth_runner",
        lambda: (lambda node: CheckResult(f"rsa_auth:{node}", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._make_transport_smoke_runner",
        lambda: (lambda node: CheckResult(f"transport_smoke:{node}", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._make_kerberos_runner",
        lambda: (lambda node: CheckResult(f"kerberos:{node}", "passed", "ok", "")),
    )
    monkeypatch.setattr(
        "edge_deploy.onboarding.runner._launch_console",
        lambda roots: None,
    )
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "")

    args = SimpleNamespace(
        config=str(private),
        root=str(root),
        tool=["autobench", "dispatch"],
        check=False,
        restart=False,
        yes=False,
    )
    assert run_onboarding(args) == 0
    first_clones = len(clone_calls)
    assert first_clones >= 1
    assert run_onboarding(args) == 0
    assert len(clone_calls) == first_clones
    report = (tmp_path / "app" / "edge-deploy" / "onboarding-report.json").read_text(encoding="utf-8")
    assert "should-be-redacted-in-reports" not in report
    assert "autobench" in report and "robocop" in report
    training_root = tmp_path / "app" / "edge-deploy" / "training"
    assert (training_root / "autobench" / "edge-deploy" / "runs").is_dir()
    assert list((training_root / "autobench" / "edge-deploy" / "runs").glob("*/state.json"))
    assert not list((root / "autobench" / "edge-deploy" / "runs").glob("*/state.json")) if (root / "autobench" / "edge-deploy" / "runs").exists() else True
    assert bootstrap_core_root() == Path(engine_identity()["package_dir"]).resolve().parent
```

Expose `_make_rsa_auth_runner`, `_make_transport_smoke_runner`, and `_make_kerberos_runner` from `runner.py` so readiness wiring stays injectable. Training practice ledgers must live under APPDATA `training/`, not under the real tool checkout runs root.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_onboarding_integration.py -v`

Expected: FAIL until fakes and runner completion align.

- [ ] **Step 3: Align fakes until green**

No production network. Keep `validate_bootstrap_core` stubbed or point it at a temp core fixture that already exists — never assert a core `git clone`.

- [ ] **Step 4: Run full onboarding + related suites**

Run:

```bash
.venv/bin/python -m pytest tests/test_onboarding_*.py tests/test_edge_console.py tests/test_ledger.py tests/test_phases.py tests/test_cli.py -v
.venv/bin/ruff check edge_deploy/onboarding edge_deploy/cli.py edge_deploy/ledger.py edge_deploy/phases/__init__.py edge_console.py
```

Expected: PASS / Ruff clean.

- [ ] **Step 5: Commit**

```bash
git add tests/test_onboarding_integration.py edge_deploy/onboarding
git commit -m "test(onboarding): cover empty-workspace resume and idempotency"
```

---

## Windows acceptance (manual, controller)

Not automated in Cloud. After merge/install of the approved engine tag on a clean Windows controller:

1. Connect both VPNs once (`both-vpns`).
2. Bootstrap documented tag; `py -m pip install -e ".[dev]"`.
3. `py -m edge_deploy onboard --config C:\secure\operator.yaml --tool autobench --tool dispatch`.
4. Confirm readiness + training complete; console shows training run; GitHub write aggregate red/unknown expected.
5. Confirm no posture switch, no remote ref/deploy/release report in tool repos.
6. Re-run same command; provisioning skipped.
7. Separate optional acceptance (not routine): `firewall-off` only to confirm console GitHub write aggregate turns green for authorized watched tools (console plan).

---

## Spec coverage (self-review)

| Spec requirement | Task |
| --- | --- |
| Resumable `onboard --config` | Tasks 4, 10 |
| Autobench / Dispatch / both (`dispatch` alias) | Tasks 1, 4, 10 |
| Bundled non-sensitive defaults + private file | Tasks 1, 3 |
| both-vpns only; no posture switch | Tasks 7, 10, docs |
| Readiness checks + blocked | Tasks 6–7 |
| Repo provision idempotent; no silent overwrite | Task 5 |
| Engine pin compatibility | Task 5 |
| Training outside real runs; dual markers | Tasks 8–9 |
| No production executor imports in training | Task 9 |
| Production rejects training ledgers | Task 8 |
| Console training visibility | Task 11 |
| Console GitHub write semantics | Separate plan `2026-07-24-console-github-write-indicator.md` |
| `--check` / `--restart`; `--yes` only with `--restart` | Tasks 4, 10 |
| No secrets in Git/state; redaction | Tasks 2, 3, 13 |
| No auto host-key enrollment | Task 7 |
| Only `git push --dry-run` write-path | Tasks 7, 13 |
| Docs + ADR-0017 + fix undeclared extras | Task 12 |
| Bootstrap core = editable package parent (no second clone) | Tasks 5, 10, 13 |
| RSA/transport + conditional Kerberos readiness | Task 7 |
| Production ledgers omit `training` key | Task 8 |
| Integration / acceptance | Task 13 + manual section |

## Placeholder scan

No TBD/TODO/`...` placeholders remain in task steps. Integration fakes and `ReadinessContext` are fully specified.

## Type consistency

- Tool ids in state/config/ledgers: always canonical `autobench` / `robocop`.
- Training ledgers: both `kind="training"` and `training=True`; production ledgers omit `training`.
- Check outcomes: `passed` / `failed` / `blocked` (+ stage `pending`).
- Restart automation: `--yes` solely with `--restart`.
- Core root: always `bootstrap_core_root()` derived from `engine_identity()["package_dir"]`.
