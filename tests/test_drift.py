"""Runtime drift verification, parametrized across both committed Tool Profiles.

``runtime_critical_paths`` and the remote runtime map are both driven by
``ToolProfile.runtime_paths`` globs, so the same engine discovers autobench's
``core/**/*.py`` and robocop's ``dispatch/**/*.tcss`` without code changes.
"""

from __future__ import annotations

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
