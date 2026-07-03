"""Runtime drift verification: compare a Tool's runtime-critical files at the intended
Snapshot (local git blobs) against the files actually present on an Edge Node.

Generalized from robocop's ``drift.py``: the runtime-critical path set is driven by
``ToolProfile.runtime_paths`` globs instead of a hardcoded ``dispatch/`` + ``scr/`` filter.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from pathlib import Path

from edge_deploy.config import ToolProfile
from edge_deploy.remote_python import REMOTE_PYTHON_EXPR
from edge_deploy.reporting import OperationReport, ReportCheck, report_node_name
from edge_deploy.tmux_driver import TmuxDriver


def runtime_critical_paths(profile: ToolProfile, root: str | Path) -> list[str]:
    """Enumerate the local files matching the profile's ``runtime_paths`` globs.

    Patterns are evaluated with :meth:`pathlib.Path.glob` (so ``**`` recurses); literal
    entries like ``benchmark.py`` resolve to themselves when present.
    """
    root_path = Path(root)
    matched: set[str] = set()
    for pattern in profile.runtime_paths:
        for path in root_path.glob(pattern):
            if path.is_file():
                matched.add(path.relative_to(root_path).as_posix())
    return sorted(matched)


def summarize_drift(local: dict[str, str], remote: dict[str, str]) -> dict[str, int]:
    summary = {"MATCH": 0, "DRIFT": 0, "MISSING": 0, "EXTRA_RUNTIME": 0}
    for path, local_md5 in local.items():
        remote_md5 = remote.get(path)
        if remote_md5 is None:
            summary["MISSING"] += 1
        elif remote_md5 == local_md5:
            summary["MATCH"] += 1
        else:
            summary["DRIFT"] += 1
    for path in remote:
        if path not in local:
            summary["EXTRA_RUNTIME"] += 1
    return summary


def _git_output(args: list[str], root: str | Path, *, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=root, check=True, capture_output=True, text=text)


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a ``runtime_paths`` glob into a regex over posix repo-relative paths.

    Mirrors :meth:`pathlib.Path.glob` semantics for the shapes profiles use:
    ``**`` spans zero or more directories, ``*`` / ``?`` stay within a segment.
    """
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(parts) + r"\Z")


def snapshot_runtime_paths(profile: ToolProfile, root: str | Path, commit: str) -> list[str]:
    """Enumerate the runtime-critical paths as they exist in ``commit``'s tree.

    Deliberately *not* the working tree: on a rollback the Snapshot is older than
    HEAD, so files added since would 128 out of ``git show`` (and files deleted
    since would silently escape drift checking).
    """
    listing = _git_output(["git", "ls-tree", "-r", "--name-only", "-z", commit], root).stdout
    names = [name for name in listing.split("\0") if name]
    regexes = [_glob_regex(pattern) for pattern in profile.runtime_paths]
    return sorted({name for name in names if any(rx.match(name) for rx in regexes)})


def local_runtime_map(profile: ToolProfile, root: str | Path, commit: str) -> dict[str, str]:
    """Map each runtime-critical path in ``commit``'s tree to the md5 of its blob."""
    _git_output(["git", "rev-parse", "--verify", commit], root)
    mapping: dict[str, str] = {}
    for path in snapshot_runtime_paths(profile, root, commit):
        blob = subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        mapping[path] = hashlib.md5(blob).hexdigest()
    return mapping


def _extract_payload(screen: str, start: str, end: str) -> str:
    start_index = screen.rfind(start)
    end_index = screen.find(end, start_index + len(start)) if start_index != -1 else -1
    if start_index == -1 or end_index == -1 or end_index <= start_index:
        raise RuntimeError(f"Could not find payload markers {start!r} / {end!r}")
    return screen[start_index + len(start):end_index].strip()


def _remote_python(driver: TmuxDriver, script: str, *, timeout: float = 60.0) -> tuple[str, int]:
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    command = f"printf %s {encoded} | base64 -d | {REMOTE_PYTHON_EXPR} -"
    return driver.run_remote(command, timeout=timeout)


def remote_runtime_map(driver: TmuxDriver, repo_path: str, profile: ToolProfile) -> dict[str, str]:
    """Hash the runtime-critical files present on the node, discovered via ``runtime_paths``."""
    script = f"""
import hashlib
import json
from pathlib import Path

root = Path({repo_path!r})
patterns = {profile.runtime_paths!r}
payload = {{}}
for pattern in patterns:
    for path in root.glob(pattern):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            payload.setdefault(rel, hashlib.md5(path.read_bytes()).hexdigest())
print("DRIFT_PAYLOAD_START")
print(json.dumps(payload, sort_keys=True))
print("DRIFT_PAYLOAD_END")
"""
    screen, code = _remote_python(driver, script, timeout=120)
    if code != 0:
        raise RuntimeError(f"Remote runtime scan failed with exit code {code}")
    payload = "".join(_extract_payload(screen, "DRIFT_PAYLOAD_START", "DRIFT_PAYLOAD_END").splitlines())
    return json.loads(payload)


def check_drift(
    driver: TmuxDriver,
    profile: ToolProfile,
    node: "object",
    *,
    commit: str,
    local_root: str | Path,
) -> OperationReport:
    """Compare local (at ``commit``) vs remote runtime maps and build an OperationReport."""
    local = local_runtime_map(profile, local_root, commit)
    remote = remote_runtime_map(driver, profile.repo_path, profile)
    summary = summarize_drift(local, remote)
    check = ReportCheck(
        name="runtime_drift",
        passed=summary["DRIFT"] == 0 and summary["MISSING"] == 0,
        message=(
            f"MATCH={summary['MATCH']} DRIFT={summary['DRIFT']} "
            f"MISSING={summary['MISSING']} EXTRA_RUNTIME={summary['EXTRA_RUNTIME']}"
        ),
        evidence=summary,
    )
    return OperationReport(
        operation="drift",
        status="passed" if check.passed else "failed",
        node=report_node_name(node),
        host=getattr(node, "host", ""),
        repo_path=profile.repo_path,
        deployment_commit=commit,
        install_decision="not_applicable",
        checks=[check],
    )
