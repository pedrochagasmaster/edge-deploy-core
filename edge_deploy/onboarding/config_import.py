"""Private onboarding YAML → validated OperatorConfig install (no secrets)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from edge_deploy.config import DEFAULT_OPERATOR_CONFIG_PATH, OperatorConfig, _load_yaml_mapping
from edge_deploy.reporting import redact

FORBIDDEN_CONFIG_KEYS = frozenset(
    {"bb_token", "token", "password", "passcode", "secret", "authorization"}
)
_BITBUCKET_REMOTE_KEYS = ("core", "autobench", "robocop")
_OPERATOR_MAPPING_KEYS = frozenset({"operator_email", "audit_repo", "nodes", "tools"})


def fingerprint_config_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def reject_credential_fields(data: dict) -> None:
    """Raise ValueError if any nested mapping key looks credential-shaped."""

    def _walk(obj: Any) -> None:
        if isinstance(obj, Mapping):
            for key, value in obj.items():
                if str(key).lower() in FORBIDDEN_CONFIG_KEYS:
                    raise ValueError(f"credential field forbidden in config: {key}")
                _walk(value)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                _walk(item)

    _walk(data)


def load_private_onboarding_source(path: Path) -> dict:
    """Parse a pre-provisioned private onboarding YAML into a mapping."""
    data = _load_yaml_mapping(path)
    reject_credential_fields(data)
    return data


def _normalize_nodes(raw_nodes: Any) -> dict[str, Any]:
    if not raw_nodes:
        return {}
    if not isinstance(raw_nodes, Mapping):
        raise ValueError("nodes must be a mapping")
    nodes: dict[str, Any] = {}
    for name, node in raw_nodes.items():
        if isinstance(node, Mapping):
            nodes[str(name)] = dict(node)
        else:
            nodes[str(name)] = {"host": node}
    return nodes


def merge_operator_config(
    private: dict,
    *,
    audit_repo: str,
    tools: dict[str, str],
) -> dict:
    """Keep OperatorConfig fields only; fill audit_repo/tools from checkout stage."""
    reject_credential_fields(private)
    merged = {
        "operator_email": str(private.get("operator_email", "") or ""),
        "audit_repo": str(audit_repo),
        "nodes": _normalize_nodes(private.get("nodes")),
        "tools": {str(name): str(path) for name, path in tools.items()},
    }
    # Validate against the public OperatorConfig contract; discard staging keys.
    OperatorConfig.from_mapping(merged)
    return merged


@dataclass(frozen=True)
class ImportedPrivateConfig:
    """Allowlisted private-source fields separated from OperatorConfig install data."""

    operator_mapping: dict
    checkout_root: str | None
    bitbucket_remotes: dict[str, str]
    fingerprint: str


def parse_private_onboarding_source(
    raw: dict,
    *,
    source_bytes: bytes | None = None,
    fingerprint: str | None = None,
) -> ImportedPrivateConfig:
    """Split staging metadata from OperatorConfig-compatible fields."""
    reject_credential_fields(raw)
    if fingerprint is None:
        if source_bytes is not None:
            fingerprint = fingerprint_config_bytes(source_bytes)
        else:
            fingerprint = fingerprint_config_bytes(
                json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
            )
    checkout = raw.get("checkout_root")
    checkout_root = str(checkout) if checkout not in (None, "") else None
    remotes_in = raw.get("bitbucket_remotes") or {}
    bitbucket_remotes: dict[str, str] = {}
    if isinstance(remotes_in, Mapping):
        for key in _BITBUCKET_REMOTE_KEYS:
            value = remotes_in.get(key)
            if value not in (None, ""):
                bitbucket_remotes[key] = str(value)
    operator_mapping = {
        "operator_email": str(raw.get("operator_email", "") or ""),
        "audit_repo": "",
        "nodes": _normalize_nodes(raw.get("nodes")),
        "tools": {},
    }
    OperatorConfig.from_mapping(operator_mapping)
    return ImportedPrivateConfig(
        operator_mapping=operator_mapping,
        checkout_root=checkout_root,
        bitbucket_remotes=bitbucket_remotes,
        fingerprint=fingerprint,
    )


def _default_permission_setter(path: Path) -> None:
    """Restrictive owner-only permissions; best-effort on Windows via icacls."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{os.environ.get('USERNAME', os.environ.get('USER', 'USER'))}:F",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        pass


def _write_yaml_atomic(path: Path, payload: dict) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    raw = text.encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(raw)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
    return path.read_bytes()


def install_operator_config(
    merged: dict,
    destination: Path | None = None,
    *,
    permission_setter: Callable[[Path], None] | None = None,
) -> str:
    """Atomically write OperatorConfig YAML; return SHA-256 of installed bytes."""
    reject_credential_fields(merged)
    # Drop anything that is not an OperatorConfig field before persistence.
    payload = {key: merged[key] for key in _OPERATOR_MAPPING_KEYS if key in merged}
    if "nodes" in payload:
        payload["nodes"] = _normalize_nodes(payload.get("nodes"))
    if "tools" in payload and isinstance(payload["tools"], Mapping):
        payload["tools"] = {str(k): str(v) for k, v in payload["tools"].items()}
    OperatorConfig.from_mapping(payload)
    dest = Path(destination) if destination is not None else DEFAULT_OPERATOR_CONFIG_PATH
    setter = _default_permission_setter if permission_setter is None else permission_setter
    try:
        written = _write_yaml_atomic(dest, payload)
        setter(dest)
    except OSError as exc:
        raise OSError(redact(f"failed to install operator config at {dest}: {exc}")) from exc
    return fingerprint_config_bytes(written)
