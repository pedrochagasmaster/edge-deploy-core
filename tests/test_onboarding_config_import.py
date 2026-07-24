from pathlib import Path
from types import SimpleNamespace

import pytest

from edge_deploy.onboarding import config_import as config_import_mod
from edge_deploy.onboarding.config_import import (
    fingerprint_config_bytes,
    install_operator_config,
    load_private_onboarding_source,
    merge_operator_config,
    parse_private_onboarding_source,
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
    imported = load_private_onboarding_source(path)
    assert imported.operator_mapping["operator_email"] == "op@example.com"
    assert imported.checkout_root == "C:/edge-deploy"
    assert imported.fingerprint == fingerprint_config_bytes(path.read_bytes())


def test_load_fingerprints_original_file_bytes(tmp_path) -> None:
    path_a = tmp_path / "a.yaml"
    path_b = tmp_path / "b.yaml"
    # Semantically identical YAML; formatting-only difference must change fingerprint.
    path_a.write_text(
        "operator_email: op@example.com\ncheckout_root: C:/edge-deploy\n",
        encoding="utf-8",
    )
    path_b.write_text(
        "operator_email:   op@example.com\n\ncheckout_root: C:/edge-deploy\n",
        encoding="utf-8",
    )
    imported_a = load_private_onboarding_source(path_a)
    imported_b = load_private_onboarding_source(path_b)
    assert imported_a.fingerprint == fingerprint_config_bytes(path_a.read_bytes())
    assert imported_b.fingerprint == fingerprint_config_bytes(path_b.read_bytes())
    assert imported_a.fingerprint != imported_b.fingerprint
    assert imported_a.operator_mapping["operator_email"] == imported_b.operator_mapping["operator_email"]
    assert imported_a.checkout_root == imported_b.checkout_root


def test_parse_requires_source_bytes() -> None:
    with pytest.raises(TypeError):
        parse_private_onboarding_source({"operator_email": "op@example.com"})  # type: ignore[call-arg]


def test_parse_separates_bitbucket_remotes() -> None:
    raw = {
        "operator_email": "op@example.com",
        "checkout_root": "C:/edge-deploy",
        "bitbucket_remotes": {
            "core": "https://bitbucket.example/core.git",
            "autobench": "https://bitbucket.example/ab.git",
            "robocop": "https://bitbucket.example/rc.git",
            "extra": "https://bitbucket.example/ignored.git",
        },
        "nodes": {"node03": {"host": "operator@edge", "ssh_options": "-p 2222"}},
    }
    source_bytes = b"operator_email: op@example.com\n"
    imported = parse_private_onboarding_source(raw, source_bytes=source_bytes)
    assert imported.fingerprint == fingerprint_config_bytes(source_bytes)
    assert imported.checkout_root == "C:/edge-deploy"
    assert imported.bitbucket_remotes == {
        "core": "https://bitbucket.example/core.git",
        "autobench": "https://bitbucket.example/ab.git",
        "robocop": "https://bitbucket.example/rc.git",
    }
    assert "extra" not in imported.bitbucket_remotes
    assert "bitbucket_remotes" not in imported.operator_mapping
    assert "checkout_root" not in imported.operator_mapping


def test_parse_rejects_non_mapping_bitbucket_remotes() -> None:
    with pytest.raises(ValueError, match="bitbucket_remotes"):
        parse_private_onboarding_source(
            {
                "operator_email": "op@example.com",
                "bitbucket_remotes": "https://bitbucket.example/core.git",
            },
            source_bytes=b"x",
        )


def test_node_allowlist_strips_private_metadata(tmp_path) -> None:
    private = {
        "operator_email": "op@example.com",
        "nodes": {
            "node03": {
                "host": "operator@edge.example",
                "ssh_options": "-p 2222",
                "session": "edge",
                "transport": "ssh",
                "region": "us-east",
                "notes": "do-not-install",
            }
        },
    }
    merged = merge_operator_config(private, audit_repo=str(tmp_path / "core"), tools={})
    assert set(merged["nodes"]["node03"]) == {"host", "ssh_options", "session", "transport"}
    assert "region" not in merged["nodes"]["node03"]
    assert "notes" not in merged["nodes"]["node03"]

    dest = tmp_path / "config.yaml"
    install_operator_config(merged, dest, permission_setter=lambda p: None)
    text = dest.read_text(encoding="utf-8")
    assert "region" not in text
    assert "notes" not in text
    assert "do-not-install" not in text


def test_default_permission_setter_icacls_uses_20s_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    dest = tmp_path / "config.yaml"
    dest.write_text("operator_email: op@example.com\n", encoding="utf-8")
    recorded: dict[str, object] = {}

    def fake_run(args, **kwargs):
        recorded["args"] = list(args)
        recorded["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(config_import_mod.sys, "platform", "win32")
    monkeypatch.setattr(config_import_mod.subprocess, "run", fake_run)
    monkeypatch.setenv("USERNAME", "operator")
    config_import_mod._default_permission_setter(dest)
    assert recorded["args"][0] == "icacls"
    assert recorded["kwargs"]["timeout"] == 20.0
    assert recorded["kwargs"]["capture_output"] is True
