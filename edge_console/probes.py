"""Posture and divergence probes.

Bitbucket and Edge stay TCP-only and are labelled as such. The GitHub
capability light is a per-watched-tool authenticated, empty
``git-receive-pack`` POST: it reaches the write-side HTTP path but sends no
update commands, pack, or ref mutation. Divergence facts use read-only git
(``rev-parse``/``rev-list`` locally, ``ls-remote`` for GitHub main, which is a
github-read action and so works in every posture — ADR-0013).
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from edge_console.demo import demo_ci, demo_git
from edge_console.ledger import collect_runs, runs_root_for
from edge_deploy.config import (
    DEFAULT_OPERATOR_CONFIG_PATH,
    load_operator_config,
    load_tool_profile,
)
from edge_deploy.preflight import endpoint_from_node

PROBE_TIMEOUT = 1.5
PROBE_CACHE_SECONDS = 10.0
GITHUB_WRITE_TIMEOUT = 20.0
GITHUB_WRITE_STATUSES = frozenset({"ok", "fail", "unknown"})
GIT_TIMEOUT = 10.0
GH_TIMEOUT = 15.0
TOOLS_CACHE_SECONDS = 30.0

STATIC_GROUPS: dict[str, list[tuple[str, int]]] = {
    "bitbucket": [("scm.mastercard.int", 443)],
}

_TOOL_NAME_RE = re.compile(r"^tool:\s*[\"']?([A-Za-z0-9_-]+)", re.MULTILINE)


# ---------------------------------------------------------------------------
# GitHub write probe
# ---------------------------------------------------------------------------

def _github_receive_pack_url(remote_url: str) -> str | None:
    """Authenticated Smart HTTP write endpoint for a GitHub remote."""
    remote_url = remote_url.strip()
    if remote_url.startswith("git@github.com:"):
        path = remote_url.removeprefix("git@github.com:")
    else:
        parsed = urllib.parse.urlsplit(remote_url)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    if path.count("/") != 1:
        return None
    return f"https://github.com/{path}.git/git-receive-pack"


def _default_github_write_runner(repo_root: Path, *, timeout: float) -> int:
    """Send an authenticated receive-pack request containing only a flush packet.

    ``git push --dry-run`` stops after GETting the receive-pack advertisement,
    so it cannot distinguish read-capable postures from a blocked write POST.
    A lone ``0000`` flush packet reaches the real write endpoint while
    requesting no ref update.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        if remote.returncode != 0:
            return remote.returncode
        endpoint = _github_receive_pack_url(remote.stdout.decode().strip())
        if endpoint is None:
            return -1

        credential = subprocess.run(
            ["git", "credential", "fill"],
            input=b"protocol=https\nhost=github.com\n\n",
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        if credential.returncode != 0:
            return credential.returncode
        fields = dict(
            line.split(b"=", 1)
            for line in credential.stdout.splitlines()
            if b"=" in line
        )
        username = fields.get(b"username", b"x-access-token")
        password = fields.get(b"password")
        if not password:
            return -1
        authorization = base64.b64encode(username + b":" + password).decode("ascii")
        request = urllib.request.Request(
            endpoint,
            data=b"0000",
            headers={
                "Authorization": f"Basic {authorization}",
                "Content-Type": "application/x-git-receive-pack-request",
                "Accept": "application/x-git-receive-pack-result",
                "User-Agent": "git/edge-deploy-posture-probe",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            return 0 if response.status == 200 else response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (subprocess.TimeoutExpired, TimeoutError):
        return -1
    except (OSError, UnicodeError, ValueError):
        return -1


def probe_github_write(
    root: Path,
    *,
    runner=None,
    timeout: float = GITHUB_WRITE_TIMEOUT,
) -> dict:
    """Non-mutating GitHub write probe for one watched checkout.

    Status is ``ok`` (exit 0), ``fail`` (definitive non-zero), or ``unknown``
    (missing checkout, timeout, or runner cannot execute). Never guesses the
    failure cause (posture vs credentials vs authz).
    """
    root = Path(root)
    tool = root.name
    if not (root.is_dir() and (root / ".git").exists()):
        return {
            "tool": tool,
            "root": str(root),
            "status": "unknown",
            "detail": "checkout missing or not a git repository",
        }
    if runner is None:
        code = _default_github_write_runner(root, timeout=timeout)
    else:
        code = runner(root)
    if code == 0:
        status = "ok"
        detail = "authenticated git-receive-pack POST passed"
    elif code < 0:
        status = "unknown"
        detail = f"write probe timed out or could not run (code {code})"
    else:
        status = "fail"
        detail = f"git-receive-pack POST failed (code {code})"
    return {
        "tool": tool,
        "root": str(root),
        "status": status,
        "detail": detail,
    }


def aggregate_github_write(tool_results: list[dict]) -> str:
    if not tool_results:
        return "unknown"
    statuses = [str(item.get("status") or "unknown") for item in tool_results]
    if any(status == "fail" for status in statuses):
        return "fail"
    if any(status != "ok" for status in statuses):
        return "unknown"
    return "ok"


# ---------------------------------------------------------------------------
# TCP posture probes (informational only — see ADR-0012)
# ---------------------------------------------------------------------------

def _edge_endpoints() -> list[tuple[str, int]]:
    """Edge node endpoints from the operator config; empty if config cannot be loaded."""
    try:
        operator = load_operator_config(DEFAULT_OPERATOR_CONFIG_PATH)
        endpoints = []
        for name in sorted(operator.nodes):
            resolved = endpoint_from_node(operator.nodes[name])
            endpoints.append((resolved.hostname, resolved.port))
        return endpoints
    except Exception:
        return []


def _probe_one(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
            return True
    except OSError:
        return False


DEMO_POSTURE_TOOLS = (
    {
        "tool": "autobench",
        "root": "(demo)/autobench",
        "status": "fail",
        "detail": "demo: GitHub write unavailable outside firewall-off",
    },
    {
        "tool": "robocop",
        "root": "(demo)/robocop",
        "status": "fail",
        "detail": "demo: GitHub write unavailable outside firewall-off",
    },
)


class PostureProber:
    def __init__(self, demo: bool, roots: list[Path] | None = None) -> None:
        self._demo = demo
        self._roots = list(roots or [])
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0

    def snapshot(self) -> dict:
        if self._demo:
            tools = [dict(row) for row in DEMO_POSTURE_TOOLS]
            return {
                "probed_at": time.strftime("%H:%M:%SZ", time.gmtime()),
                "groups": {
                    "github": {
                        "aggregate": aggregate_github_write(tools),
                        "tools": tools,
                    },
                    "bitbucket": [
                        {"endpoint": "scm.mastercard.int:443", "reachable": True},
                    ],
                    "edge": [
                        {"endpoint": "hdpedge03.mastercard.int:2222", "reachable": True},
                        {"endpoint": "hdpedge04.mastercard.int:2222", "reachable": True},
                    ],
                },
            }
        with self._lock:
            if self._cached and time.monotonic() - self._cached_at < PROBE_CACHE_SECONDS:
                return self._cached
        groups: dict[str, list[tuple[str, int]]] = dict(STATIC_GROUPS)
        edge = _edge_endpoints()
        if edge:
            groups["edge"] = edge
        flat = [(name, host, port) for name, eps in groups.items() for host, port in eps]
        with ThreadPoolExecutor(max_workers=max(1, len(flat) or 1)) as pool:
            reachable = list(pool.map(lambda item: _probe_one(item[1], item[2]), flat)) if flat else []
        result: dict = {
            "probed_at": time.strftime("%H:%M:%SZ", time.gmtime()),
            "groups": {},
        }
        for (name, host, port), ok in zip(flat, reachable):
            result["groups"].setdefault(name, []).append(
                {"endpoint": f"{host}:{port}", "reachable": ok}
            )
        with ThreadPoolExecutor(max_workers=max(1, len(self._roots) or 1)) as pool:
            write_tools = list(pool.map(probe_github_write, self._roots)) if self._roots else []
        # Strip command from API payload (argv is an implementation detail).
        api_tools = [
            {
                "tool": row["tool"],
                "root": row["root"],
                "status": row["status"],
                "detail": row["detail"],
            }
            for row in write_tools
        ]
        result["groups"]["github"] = {
            "aggregate": aggregate_github_write(api_tools),
            "tools": api_tools,
        }
        with self._lock:
            self._cached = result
            self._cached_at = time.monotonic()
        return result


# ---------------------------------------------------------------------------
# Release guidance: which tools have drifted from the deployed state.
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT) -> str | None:
    """Run one read-only git command; None on any failure (never raises)."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never block on a credential prompt
    env["GCM_INTERACTIVE"] = "never"
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _tool_name(root: Path, runs: list[dict]) -> str:
    """The checkout's tool: its committed profile, else the ledger, else the directory."""
    try:
        match = _TOOL_NAME_RE.search((root / "edge_deploy.yaml").read_text(encoding="utf-8"))
    except OSError:
        match = None
    if match:
        return match.group(1)
    dated = sorted(runs, key=lambda r: r["state"].get("created_at", ""), reverse=True)
    for run in dated:
        tool = run["state"].get("tool")
        if tool:
            return str(tool)
    return root.name


def _checkout_state(root: Path, git=_git) -> dict:
    """Which branch the checkout is on, and whether the tree is clean.

    The engine's own gate (``repository.inspect_repository``) refuses a release
    unless the checkout is on ``main`` with a clean tree, *before* it compares
    any SHA — so a card that only compares SHAs can show an all-clear for a
    checkout the engine will turn away.

    ``--branch`` is what makes this answerable: it puts a ``## …`` header on the
    output, so a clean tree is still distinguishable from a failed command.
    """
    status = git(root, "status", "--porcelain", "--branch", "--untracked-files=all")
    if not status:
        return {"branch": None, "on_main": None, "dirty": None}
    lines = status.splitlines()
    if not lines[0].startswith("## "):
        return {"branch": None, "on_main": None, "dirty": None}
    name = lines[0][3:].split("...")[0].strip()
    detached = name == "HEAD" or name.startswith("HEAD (")
    branch = "(detached HEAD)" if detached else name
    # Generated release reports are the one untracked path the engine forgives.
    dirty = any(
        line.strip() and not line.startswith("?? edge-deploy/reports/")
        for line in lines[1:]
    )
    return {"branch": branch, "on_main": (not detached) and name == "main", "dirty": dirty}


def _normalize_remote(value: str) -> str:
    """Same comparison ``repository._normalize_url`` makes."""
    return value.strip().removesuffix("/").removesuffix(".git").lower()


def _release_source_state(root: Path, git=_git) -> dict:
    """The rest of the engine's release gate that is not about commits.

    ``inspect_repository`` also requires both remotes to match the tool's
    committed profile, and verify then runs the committed gate script
    (ADR-0016). Both refuse before anything is published, and both are a file
    read away.
    """
    state: dict = {"profile": None, "remotes": None, "local_check": None, "deep_smoke": False}
    try:
        profile = load_tool_profile(root)
    except Exception as exc:
        # verify loads the same file and dies on it, so say so rather than
        # letting a mistyped --root look like a healthy checkout.
        state["profile"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        return state
    missing = [
        field for field, value in (("github_url", profile.github_url),
                                   ("bitbucket_url", profile.bitbucket_url))
        if not value
    ]
    state["profile"] = {
        "ok": not missing,
        "detail": f"edge_deploy.yaml does not define {' or '.join(missing)}" if missing else "",
    }
    wrong = []
    for remote, expected in (("origin", profile.github_url), ("bitbucket", profile.bitbucket_url)):
        if not expected:
            continue
        actual = git(root, "remote", "get-url", remote, timeout=5.0)
        if actual is None:
            wrong.append(f"{remote} is not configured")
        elif _normalize_remote(actual) != _normalize_remote(expected):
            wrong.append(f"{remote} points at {actual}")
    state["remotes"] = {"ok": not wrong, "detail": "; ".join(wrong)}
    state["local_check"] = (root / "tools" / "dev" / "local_check.ps1").is_file()
    # Deep smoke is the only thing that asks for a Kerberos password, so the
    # console offers it only where the tool actually declares one.
    state["deep_smoke"] = bool(getattr(profile.smoke, "deep", None))
    return state


def probe_github_ci(root: Path, commit: str | None, *, runner=None) -> dict:
    """Whether GitHub CI succeeded for exactly this commit.

    Verify refuses without it (``require_successful_github_ci``). This is the
    one gate the console reports rather than predicts hard: it needs ``gh`` and
    the network, and it can change between this answer and the click — so an
    unknown never blocks anything.
    """
    if not commit:
        return {"status": "unknown", "detail": "no commit to check"}
    command = [
        "gh", "run", "list", "--commit", commit, "--branch", "main",
        "--workflow", "CI", "--json", "conclusion", "--limit", "20",
    ]
    try:
        completed = (runner or _run_gh)(root, command)
    except Exception as exc:  # gh missing, unauthenticated, offline
        return {"status": "unknown", "detail": f"{type(exc).__name__}: {exc}"}
    if completed is None:
        return {"status": "unknown", "detail": "gh could not report CI status"}
    try:
        runs = json.loads(completed)
    except ValueError:
        return {"status": "unknown", "detail": "gh returned unexpected output"}
    conclusions = [str(item.get("conclusion") or "") for item in runs]
    if any(value == "success" for value in conclusions):
        return {"status": "success", "detail": ""}
    if not conclusions:
        return {"status": "missing", "detail": f"no CI run recorded for {commit[:7]}"}
    if any(value == "" for value in conclusions):
        return {"status": "pending", "detail": f"CI has not finished for {commit[:7]}"}
    return {"status": "failed", "detail": f"CI concluded {conclusions[0]} for {commit[:7]}"}


def _run_gh(root: Path, command: list[str]) -> str | None:
    env = dict(os.environ)
    env["GH_PROMPT_DISABLED"] = "1"
    completed = subprocess.run(
        command, cwd=str(root), capture_output=True, text=True, timeout=GH_TIMEOUT, env=env
    )
    return completed.stdout if completed.returncode == 0 else None


def _last_deployed(runs: list[dict]) -> dict | None:
    """The newest complete run: what the Edge Nodes are believed to hold.

    Rollbacks count — a completed rollback's source_sha *is* the deployed state.
    """
    complete = [r["state"] for r in runs if r["state"].get("status") == "complete"]
    if not complete:
        return None
    last = max(complete, key=lambda s: s.get("created_at", ""))
    return {
        "sha": last.get("source_sha", ""),
        "run_id": last.get("run_id", ""),
        "kind": last.get("kind", "release"),
        "created_at": last.get("created_at", ""),
    }


def probe_divergence(root: Path, runs: list[dict], *, git=_git) -> dict:
    """Compare the last-deployed source SHA to checkout HEAD and GitHub main.

    Two independent comparisons: deployed-vs-HEAD says whether a release would
    ship something new; HEAD-vs-origin/main says whether the checkout and
    GitHub disagree. Any git failure degrades that fact to None — the UI shows
    what it cannot know rather than guessing.

    ``ahead`` counts locally (rev-list), so it is exact only when the live
    ls-remote proves the checkout holds all of GitHub main (``ahead_exact``);
    when the checkout is stale the count is a lower bound — still exactly what
    a release would ship right now, since a release ships checkout HEAD.

    When stale, ``stale_direction`` says which side is ahead — but only when
    GitHub main's commit object exists locally (a prior fetch), because both
    range counts need it: "local_behind" (pull), "local_ahead" (unpushed work:
    push/PR first — verify needs green GitHub CI on HEAD), "forked" (both
    moved), or None when git cannot tell.

    ``branch`` / ``on_main`` / ``dirty`` carry the rest of the engine's release
    gate, which SHAs alone cannot see: a feature branch sitting exactly on
    origin/main compares as perfectly in sync and is still refused.
    """
    deployed = _last_deployed(runs)
    head = git(root, "rev-parse", "HEAD", timeout=5.0)
    ls = git(root, "ls-remote", "origin", "refs/heads/main")
    origin_main = ls.split()[0] if ls else None
    ahead = None
    if deployed and deployed["sha"] and head:
        count = git(root, "rev-list", "--count", f"{deployed['sha']}..HEAD", timeout=5.0)
        if count and count.isdigit():
            ahead = int(count)
    stale = bool(head and origin_main and head != origin_main)
    ahead_exact = bool(head and origin_main and head == origin_main)
    stale_direction = None
    behind_origin = None
    ahead_of_origin = None
    if stale:
        behind_raw = git(root, "rev-list", "--count", f"HEAD..{origin_main}", timeout=5.0)
        ahead_raw = git(root, "rev-list", "--count", f"{origin_main}..HEAD", timeout=5.0)
        if behind_raw and behind_raw.isdigit() and ahead_raw and ahead_raw.isdigit():
            behind_origin = int(behind_raw)
            ahead_of_origin = int(ahead_raw)
            if behind_origin and not ahead_of_origin:
                stale_direction = "local_behind"
            elif ahead_of_origin and not behind_origin:
                stale_direction = "local_ahead"
            elif behind_origin and ahead_of_origin:
                stale_direction = "forked"
    if head is None:
        verdict = "unknown"
    elif deployed is None:
        verdict = "never_released"
    elif head != deployed["sha"]:
        verdict = "diverged"
    elif stale:
        verdict = "checkout_stale"
    else:
        verdict = "up_to_date"
    return {
        "verdict": verdict,
        "deployed": deployed,
        "head": head,
        "origin_main": origin_main,
        **_checkout_state(root, git=git),
        **_release_source_state(root, git=git),
        "ahead": ahead,
        "ahead_exact": ahead_exact,
        "stale": stale,
        "stale_direction": stale_direction,
        "behind_origin": behind_origin,
        "ahead_of_origin": ahead_of_origin,
    }


def probe_tool(root: Path, *, git=_git, ci=probe_github_ci) -> dict:
    """Everything the tool card needs: identity, open run, nodes, divergence."""
    runs = collect_runs(runs_root_for(root))
    open_run = next(
        (r["state"]["run_id"] for r in runs if r["state"].get("status") == "open"), None
    )
    dated = sorted(runs, key=lambda r: r["state"].get("created_at", ""), reverse=True)
    nodes = list(dated[0]["state"].get("nodes") or []) if dated else []
    entry = {
        "root": str(root),
        "tool": _tool_name(root, runs),
        "open_run_id": open_run,
        "nodes": nodes,
    }
    entry.update(probe_divergence(root, runs, git=git))
    entry["ci"] = ci(root, entry.get("head"))
    return entry


class ToolsProber:
    """Cached per-checkout divergence probe (ls-remote hits the network)."""

    def __init__(self, roots: list[Path], demo: bool, git=None, ci=None) -> None:
        self._roots = roots
        self._git = git or (demo_git if demo else _git)
        # --demo has no GitHub to ask, and a canned "success" would be a lie
        # dressed as a fact; report it as unknown, which blocks nothing.
        self._ci = ci or (demo_ci if demo else probe_github_ci)
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at = 0.0

    def invalidate(self) -> None:
        """Drop the cache so the next snapshot re-reads git and the ledgers."""
        with self._lock:
            self._cached = None
            self._cached_at = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            if self._cached and time.monotonic() - self._cached_at < TOOLS_CACHE_SECONDS:
                return self._cached
        with ThreadPoolExecutor(max_workers=max(1, len(self._roots))) as pool:
            tools = list(
                pool.map(lambda root: probe_tool(root, git=self._git, ci=self._ci), self._roots)
            )
        result = {"probed_at": time.strftime("%H:%M:%SZ", time.gmtime()), "tools": tools}
        with self._lock:
            self._cached = result
            self._cached_at = time.monotonic()
        return result
