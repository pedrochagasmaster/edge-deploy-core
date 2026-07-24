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

    def invalidate_from(self, stage: str) -> None:
        """Reset ``stage`` and every later stage to pending; clear practice evidence."""
        if stage not in ONBOARDING_STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        start = ONBOARDING_STAGES.index(stage)
        for name in ONBOARDING_STAGES[start:]:
            self.data["stages"][name] = {
                "outcome": "pending",
                "inputs": {},
                "checks": [],
                "updated_at": None,
            }
        self.data["practice"] = {"completed": False, "run_id": None}

    def apply_selection(
        self,
        *,
        tools: list[str],
        root: str,
        config_fingerprint: str,
    ) -> bool:
        """Update selection fields; invalidate config-onward when any change.

        Returns True when config-dependent stages were invalidated.
        Never stores private config values — only the byte fingerprint.
        """
        changed = (
            list(self.data.get("tools") or []) != list(tools)
            or str(self.data.get("root") or "") != str(root)
            or str(self.data.get("config_fingerprint") or "") != str(config_fingerprint)
        )
        self.data["tools"] = list(tools)
        self.data["root"] = str(root)
        self.data["config_fingerprint"] = str(config_fingerprint)
        if changed:
            self.invalidate_from("config")
        return changed
