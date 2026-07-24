import json

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
