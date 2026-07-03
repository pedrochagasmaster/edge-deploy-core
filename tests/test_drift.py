"""Runtime drift verification, parametrized across both committed Tool Profiles.

``runtime_critical_paths`` and the remote runtime map are both driven by
``ToolProfile.runtime_paths`` globs, so the same engine discovers autobench's
``core/**/*.py`` and robocop's ``dispatch/**/*.tcss`` without code changes.
"""

from __future__ import annotations

import subprocess

import pytest

from edge_deploy import drift

# (tool, files to create under a temp root, expected runtime-critical set)
RUNTIME_TREE_CASES = [
    pytest.param(
        "autobench",
        [
            "benchmark.py",
            "tui_app.py",
            "core/engine.py",
            "core/sub/deep.py",
            "utils/io.py",
            "scripts/run.py",
            "README.md",
            "VERSION",
        ],
        {"benchmark.py", "tui_app.py", "core/engine.py", "core/sub/deep.py", "utils/io.py", "scripts/run.py"},
        id="autobench",
    ),
    pytest.param(
        "robocop",
        [
            "dispatch/app.py",
            "dispatch/app.tcss",
            "dispatch/widgets/table.py",
            "scr/Query.py",
            "scr/sub/Other.py",
            "dispatch/notes.md",
            "README.md",
        ],
        {"dispatch/app.py", "dispatch/app.tcss", "dispatch/widgets/table.py", "scr/Query.py", "scr/sub/Other.py"},
        id="robocop",
    ),
]

# (tool, a representative local runtime map)
LOCAL_MAP_CASES = [
    pytest.param("autobench", {"benchmark.py": "aaa", "core/engine.py": "bbb"}, id="autobench"),
    pytest.param("robocop", {"dispatch/app.py": "aaa", "scr/Query.py": "bbb"}, id="robocop"),
]


def _make_tree(root, files: list[str]) -> None:
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content", encoding="utf-8")


@pytest.mark.parametrize("tool, files, expected", RUNTIME_TREE_CASES)
def test_runtime_critical_paths_discovers_profile_globs(load_profile, tmp_path, tool, files, expected) -> None:
    profile = load_profile(tool)
    _make_tree(tmp_path, files)

    assert set(drift.runtime_critical_paths(profile, tmp_path)) == expected


def _git(root, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_local_runtime_map_reads_snapshot_tree_not_working_tree(load_profile, tmp_path) -> None:
    """Regression: rollback drift check globbed the working tree, then 128'd on
    ``git show`` for files added after the snapshot (and silently skipped files
    deleted since)."""
    profile = load_profile("autobench")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")

    _make_tree(tmp_path, ["benchmark.py", "core/engine.py"])
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "snapshot")
    snapshot = _git(tmp_path, "rev-parse", "HEAD")

    # HEAD moves on: one runtime file added, one deleted.
    (tmp_path / "core" / "added_later.py").write_text("new", encoding="utf-8")
    (tmp_path / "core" / "engine.py").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "later work")

    mapping = drift.local_runtime_map(profile, tmp_path, snapshot)

    assert "core/added_later.py" not in mapping  # not in the snapshot -> not expected on node
    assert "core/engine.py" in mapping  # deleted since, but the snapshot still ships it
    assert "benchmark.py" in mapping


@pytest.mark.parametrize(
    "pattern, path, matches",
    [
        ("core/**/*.py", "core/engine.py", True),
        ("core/**/*.py", "core/sub/deep.py", True),
        ("core/**/*.py", "coreX/engine.py", False),
        ("core/**/*.py", "core/engine.txt", False),
        ("benchmark.py", "benchmark.py", True),
        ("benchmark.py", "sub/benchmark.py", False),
        ("dispatch/**/*.tcss", "dispatch/widgets/table.tcss", True),
        ("*.py", "top.py", True),
        ("*.py", "sub/nested.py", False),
    ],
)
def test_glob_regex_mirrors_pathlib_glob_semantics(pattern: str, path: str, matches: bool) -> None:
    assert bool(drift._glob_regex(pattern).match(path)) is matches


def test_summarize_drift_counts_states() -> None:
    local = {"a.py": "1", "b.py": "2", "c.py": "3"}
    remote = {"a.py": "1", "b.py": "DIFF", "d.py": "9"}

    summary = drift.summarize_drift(local, remote)

    assert summary["MATCH"] == 1  # a.py
    assert summary["DRIFT"] == 1  # b.py
    assert summary["MISSING"] == 1  # c.py absent on node
    assert summary["EXTRA_RUNTIME"] == 1  # d.py only on node


@pytest.mark.parametrize("tool, remote_map", LOCAL_MAP_CASES)
def test_remote_runtime_map_parses_base64_payload(load_profile, fake_tmux, tool, remote_map) -> None:
    profile = load_profile(tool)
    driver = fake_tmux(remote_runtime=remote_map)

    result = drift.remote_runtime_map(driver, profile.repo_path, profile)

    assert result == remote_map
    assert driver.ran("base64 -d")  # exercised the inline-python-over-the-pane path


@pytest.mark.parametrize("tool, local_map", LOCAL_MAP_CASES)
def test_check_drift_passes_when_maps_match(
    load_profile, sample_node, fake_tmux, monkeypatch, tool, local_map
) -> None:
    profile = load_profile(tool)
    monkeypatch.setattr(drift, "local_runtime_map", lambda profile, root, commit: dict(local_map))
    driver = fake_tmux(remote_runtime=dict(local_map))

    report = drift.check_drift(driver, profile, sample_node, commit="c" * 40, local_root="/local/root")

    assert report.status == "passed"
    payload = report.to_payload()
    assert payload["operation"] == "drift"
    assert payload["node"] == "node03"
    assert payload["repo_path"] == profile.repo_path
    assert payload["deployment_commit"] == "c" * 40
    assert payload["install_decision"] == "not_applicable"
    assert payload["checks"][0]["name"] == "runtime_drift"
    assert payload["checks"][0]["passed"] is True


@pytest.mark.parametrize("tool, local_map", LOCAL_MAP_CASES)
def test_check_drift_fails_when_remote_has_drifted(
    load_profile, sample_node, fake_tmux, monkeypatch, tool, local_map
) -> None:
    profile = load_profile(tool)
    drifted_key = next(iter(local_map))
    remote_map = dict(local_map)
    remote_map[drifted_key] = "DRIFTED"

    monkeypatch.setattr(drift, "local_runtime_map", lambda profile, root, commit: dict(local_map))
    driver = fake_tmux(remote_runtime=remote_map)

    report = drift.check_drift(driver, profile, sample_node, commit="c" * 40, local_root="/local/root")

    assert report.status == "failed"
    check = report.checks[0]
    assert check.passed is False
    assert check.evidence["DRIFT"] >= 1
