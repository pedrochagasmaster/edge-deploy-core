from __future__ import annotations

from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "e2e-release.ps1"


def test_e2e_release_distinguishes_candidate_version_from_published_pin() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    candidate_version = project["project"]["version"]
    script = SCRIPT_PATH.read_text(encoding="utf-8-sig")

    assert f"$script:ExpectedEngineVersion = '{candidate_version}'" in script
    assert "$script:PublishedEngineTag = 'v1.4.0'" in script
