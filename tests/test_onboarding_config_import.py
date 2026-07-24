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
