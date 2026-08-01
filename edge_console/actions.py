"""Console-driven engine actions: allowlist, subprocess runner, prompt relay.

The console does not reimplement any release logic and never writes to a run
ledger. Every button maps to one entry in :data:`ACTION_SPECS`, and each entry
builds a fixed ``argv`` list — the same command the Release Operator would type
— from validated parameters. Commands run with ``shell=False`` in one of the
watched checkouts, so nothing the browser sends can become shell syntax.

What the console adds is transparency: it streams the engine's stdout/stderr
verbatim, and when the engine stops to ask the operator something (the RSA
passcode, a Kerberos password, a guided posture acknowledgement, a y/N gate) it
surfaces that question as a prompt and relays the answer to the process stdin.
Secrets are written straight through to the child process, never persisted, and
masked in the transcript (ADR-0002).

Switching the workstation firewall posture stays manual and outside this
allowlist: the console can only show the boundary and forward the operator's
"I have switched" acknowledgement.
"""

from __future__ import annotations

import codecs
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from edge_console.ledger import find_run_state, is_training_state

# Trailing text that means the engine is blocked on the operator. Matched
# against the unterminated tail of the stream, so a prompt is recognised the
# moment it is flushed — before any newline arrives.
_RSA_RE = re.compile(r"\[(?P<label>[^\]\n]{1,80})\]\s*Enter RSA PASSCODE:\s*$")
_KERBEROS_RE = re.compile(r"\[(?P<label>[^\]\n]{1,80})\]\s*Kerberos password:\s*$")
_POSTURE_RE = re.compile(
    r"Switch firewall posture to \[(?P<posture>[^\]\n]{1,80})\], then press Enter to continue\.\.\.\s*$"
)
_CONFIRM_RE = re.compile(r"(?P<question>[^\n]{0,160}?\[[yY]/[nN]\])\s*$")
_TRAILING_QUESTION_RE = re.compile(r"(?P<question>[^\n]{1,200}[:?])\s*$")

# How long the stream must sit on an unterminated line that looks like a
# question before the console offers a generic answer box. Known prompts are
# recognised immediately; this is the safety net so an unrecognised prompt can
# never silently hang the run.
IDLE_PROMPT_SECONDS = 2.5
# How long a stopped command gets to unwind on its own before it is terminated,
# and again before it is killed.
GRACEFUL_STOP_SECONDS = 5.0
OUTPUT_LIMIT_CHARS = 400_000
RETAINED_FINISHED_ACTIONS = 30

# Matched with fullmatch: Python's ``$`` also matches before a trailing
# newline, which would let a control character through the charset these
# patterns exist to guarantee.
# The posture capability an action needs, in the page's vocabulary. Advisory
# only: the console warns, the engine's own git-protocol probe decides.
CAPABILITIES = frozenset({"any", "bb", "edge", "both", "gh"})

_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ActionError(Exception):
    """A request the console refuses to turn into a command."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ActionSpec:
    """One allowlisted command the console may run in a watched checkout."""

    id: str
    label: str
    kind: str  # "engine" (py -m edge_deploy ...) or "git"
    cap: str  # one of CAPABILITIES
    args: Callable[[dict], list[str]]
    params: tuple[str, ...] = ()
    needs_run: bool = False
    destructive: bool = False
    summary: str = ""


def _deploy_args(params: dict) -> list[str]:
    args = ["deploy", "--run", params["run_id"]]
    nodes = params.get("nodes")
    if nodes:
        args += ["--nodes", ",".join(nodes)]
    return args


def _release_args(params: dict) -> list[str]:
    args = ["release", "--guided"]
    if params.get("run_id"):
        args += ["--run", params["run_id"]]
    return args


ACTION_SPECS: dict[str, ActionSpec] = {
    spec.id: spec
    for spec in (
        ActionSpec(
            id="verify",
            label="Run verify",
            kind="engine",
            cap="any",
            args=lambda p: ["verify", "--run", p["run_id"]],
            params=("run_id",),
            needs_run=True,
            summary="Inspect the checkout, GitHub CI, and the committed tool gate.",
        ),
        ActionSpec(
            id="publish",
            label="Run publish",
            kind="engine",
            cap="bb",
            args=lambda p: ["publish-phase", "--run", p["run_id"]],
            params=("run_id",),
            needs_run=True,
            summary="Publish the reviewed source to Bitbucket.",
        ),
        ActionSpec(
            id="deploy",
            label="Run deploy",
            kind="engine",
            cap="both",
            args=_deploy_args,
            params=("run_id", "nodes"),
            needs_run=True,
            summary="Roll the pending Edge Nodes to the published snapshot.",
        ),
        ActionSpec(
            id="tag_bitbucket",
            label="Tag Bitbucket",
            kind="engine",
            cap="bb",
            args=lambda p: ["tag-bitbucket", "--run", p["run_id"]],
            params=("run_id",),
            needs_run=True,
            summary="Push the immutable release tag to Bitbucket.",
        ),
        ActionSpec(
            id="tag_github",
            label="Tag GitHub",
            kind="engine",
            cap="gh",
            args=lambda p: ["tag-github", "--run", p["run_id"]],
            params=("run_id",),
            needs_run=True,
            summary="Push the same release tag to GitHub and close the run.",
        ),
        ActionSpec(
            id="release",
            label="Start guided release",
            kind="engine",
            cap="any",
            args=_release_args,
            params=("run_id",),
            summary="Walk every phase, pausing at each posture boundary and RSA prompt.",
        ),
        ActionSpec(
            id="abandon",
            label="Abandon run",
            kind="engine",
            cap="any",
            args=lambda p: ["abandon", "--run", p["run_id"], "--reason", p["reason"]],
            params=("run_id", "reason"),
            needs_run=True,
            destructive=True,
            summary="Close an unresolved run with a recorded reason.",
        ),
        ActionSpec(
            id="status",
            label="Refresh status",
            kind="engine",
            cap="any",
            args=lambda p: ["status"],
            summary="Print the engine's own view of every run under this checkout.",
        ),
        # Both talk only to the node, and neither is posture-gated by the
        # engine — so they need the Edge VPN, not the Bitbucket one too.
        ActionSpec(
            id="preflight",
            label="Preflight node",
            kind="engine",
            cap="edge",
            args=lambda p: ["preflight", "--node", p["node"]],
            params=("node",),
            summary="Check DNS and TCP reachability for one Edge Node.",
        ),
        ActionSpec(
            id="transport_smoke",
            label="Smoke transport",
            kind="engine",
            cap="edge",
            args=lambda p: ["transport-smoke", "--node", p["node"]],
            params=("node",),
            summary="Authenticate once and exercise command, transfer, PTY, and keepalive.",
        ),
        ActionSpec(
            id="git_pull",
            label="Pull from GitHub",
            kind="git",
            cap="any",
            args=lambda p: ["pull", "--ff-only", "origin", "main"],
            summary="Fast-forward the checkout to GitHub main.",
        ),
        ActionSpec(
            id="git_rebase",
            label="Rebase onto GitHub",
            kind="git",
            cap="any",
            args=lambda p: ["pull", "--rebase", "origin", "main"],
            summary="Reconcile a forked checkout with GitHub main.",
        ),
        ActionSpec(
            id="git_push",
            label="Push to GitHub",
            kind="git",
            cap="gh",
            args=lambda p: ["push", "origin", "main"],
            summary="Publish local commits so verify can see them with green CI.",
        ),
    )
}


def display_command(spec: ActionSpec, args: list[str]) -> str:
    """The command as an operator would type it — shown next to every button."""
    if spec.kind == "git":
        return "git " + " ".join(args)
    return "py -m edge_deploy " + " ".join(_quote(arg) for arg in args)


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------

def _clean_run_id(raw: object) -> str:
    value = str(raw or "")
    if not _RUN_ID_RE.fullmatch(value):
        raise ActionError(f"invalid run id: {value!r}")
    return value


def _clean_nodes(raw: object) -> list[str]:
    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        candidates = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, list):
        candidates = [str(part).strip() for part in raw]
    else:
        raise ActionError("nodes must be a list or comma-separated string")
    nodes = [node for node in candidates if node]
    for node in nodes:
        if not _NODE_RE.fullmatch(node):
            raise ActionError(f"invalid node name: {node!r}")
    return nodes


def _clean_node(raw: object) -> str:
    value = str(raw or "")
    if not _NODE_RE.fullmatch(value):
        raise ActionError(f"invalid node name: {value!r}")
    return value


def _clean_reason(raw: object) -> str:
    value = _CONTROL_RE.sub(" ", str(raw or "")).strip()
    if not value:
        raise ActionError("abandon needs a reason")
    return value[:200]


# ---------------------------------------------------------------------------
# One running command
# ---------------------------------------------------------------------------

@dataclass
class _Prompt:
    id: str
    kind: str  # secret | ack | choice | text
    name: str  # rsa | kerberos | posture | confirm | unknown
    title: str
    detail: str
    raw: str
    asked_at: float = field(default_factory=time.time)

    def payload(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "title": self.title,
            "detail": self.detail,
            "raw": self.raw,
        }


class ActionRunner:
    """A single engine invocation, streamed to the browser and answerable."""

    def __init__(
        self,
        *,
        action_id: str,
        spec: ActionSpec,
        argv: list[str],
        cwd: Path,
        command: str,
        root: str,
        run_id: str | None,
        tool: str | None,
        on_finish: Callable[[ActionRunner], None] | None = None,
    ) -> None:
        self.id = action_id
        self.spec = spec
        self.argv = argv
        self.cwd = cwd
        self.command = command
        self.root = root
        self.run_id = run_id
        self.tool = tool
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.exit_code: int | None = None
        self.status = "starting"
        self.error: str | None = None
        self._on_finish = on_finish
        self._proc: subprocess.Popen | None = None
        self._cond = threading.Condition()
        self._text = ""
        self._dropped = 0
        self._pending_line = ""
        self._last_output = time.monotonic()
        self._prompt: _Prompt | None = None
        self._prompt_seq = 0
        self._cancel_requested = False
        self._secrets: list[str] = []
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        env = dict(os.environ)
        # Unbuffered so a prompt written without a newline reaches the browser
        # the instant the engine flushes it.
        env["PYTHONUNBUFFERED"] = "1"
        # There is no terminal to answer a credential helper on: fail fast
        # instead of blocking on a prompt nothing can see.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - argv list, shell=False, allowlisted
                self.argv,
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env=env,
            )
        except OSError as exc:
            self.status = "failed"
            self.error = f"could not start {self.argv[0]}: {exc}"
            self.finished_at = time.time()
            self._append(f"[console] {self.error}\n")
            if self._on_finish:
                try:
                    self._on_finish(self)
                except Exception:  # a cache hook must not break the response
                    pass
            return
        self.status = "running"
        self._append(f"[console] {self.command}\n[console] cwd {self.cwd}\n\n")
        threading.Thread(target=self._pump, name=f"action-{self.id}", daemon=True).start()
        if self._cancel_requested:
            # Stop was pressed while the process was still being spawned.
            self.cancel()

    def _pump(self) -> None:
        stream = self._proc.stdout if self._proc else None
        try:
            while stream is not None:
                chunk = stream.read(4096)
                if not chunk:
                    break
                self._append(self._decoder.decode(chunk))
            # Flush any partial character left at end of stream rather than
            # dropping it.
            self._append(self._decoder.decode(b"", final=True))
        except (OSError, ValueError):
            pass
        code = self._proc.wait() if self._proc else -1
        for pipe in (getattr(self._proc, "stdin", None), stream):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        with self._cond:
            self.exit_code = code
            self.status = "exited"
            self.finished_at = time.time()
            self._prompt = None
            # Masking only ever protects future output, and there is none now.
            self._secrets.clear()
            self._text += f"\n[console] exit code {code}\n"
            self._cond.notify_all()
        if self._on_finish:
            try:
                self._on_finish(self)
            except Exception:  # a cache-invalidation hook must not kill the reader
                pass

    def cancel(self) -> None:
        """Stop the command, giving the engine a chance to unwind first.

        Terminating outright leaves the run lock behind, and no console action
        can pass ``--force-lock`` — so a stopped run would need a terminal to
        rescue. Closing stdin first turns a pending prompt into the EOF the
        engine already handles: the guided loop prints its resume command and
        releases the lock on the way out.
        """
        proc = self._proc
        if proc is None:
            # Still inside Popen; start() terminates it as soon as it exists.
            self._cancel_requested = True
            return
        if proc.poll() is not None:
            return
        self._append("\n[console] stopping — letting the engine unwind\n")
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        threading.Thread(target=self._escalate, args=(proc,), daemon=True).start()

    def _escalate(self, proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=GRACEFUL_STOP_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        self._append("[console] cancelled by operator\n")
        try:
            proc.terminate()
            proc.wait(timeout=GRACEFUL_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self._append("[console] the command ignored terminate; killing it\n")
            try:
                proc.kill()
            except OSError:
                pass
        except OSError:
            pass

    # -- output ------------------------------------------------------------

    def _append(self, text: str) -> None:
        if not text:
            return
        for secret in self._secrets:
            text = text.replace(secret, "********")
        with self._cond:
            self._text += text
            if len(self._text) > OUTPUT_LIMIT_CHARS:
                cut = len(self._text) - OUTPUT_LIMIT_CHARS // 2
                self._text = self._text[cut:]
                self._dropped += cut
            self._pending_line = self._text.rsplit("\n", 1)[-1]
            self._last_output = time.monotonic()
            self._detect_prompt_locked()
            self._cond.notify_all()

    def _detect_prompt_locked(self) -> None:
        if self._prompt is not None or self.status != "running":
            return
        line = self._pending_line
        if not line.strip():
            return
        match = _RSA_RE.search(line)
        if match:
            self._set_prompt_locked(
                kind="secret",
                name="rsa",
                title=f"RSA passcode · {match.group('label')}",
                detail=(
                    "SecurID tokencodes are single-use and rotate about every 60 seconds. "
                    "The code is written straight to the engine process, never stored or logged."
                ),
                raw=line,
            )
            return
        match = _KERBEROS_RE.search(line)
        if match:
            self._set_prompt_locked(
                kind="secret",
                name="kerberos",
                title=f"Kerberos password · {match.group('label')}",
                detail="Needed only when a deep smoke check requires a Kerberos ticket.",
                raw=line,
            )
            return
        match = _POSTURE_RE.search(line)
        if match:
            posture = match.group("posture")
            self._set_prompt_locked(
                kind="ack",
                name="posture",
                title=f"Switch the workstation posture to {posture}",
                detail=(
                    "Posture changes are manual and stay outside the console. Switch the "
                    "firewall yourself, then acknowledge — the engine re-probes before continuing."
                ),
                raw=line,
            )
            return
        match = _CONFIRM_RE.search(line)
        if match:
            self._set_prompt_locked(
                kind="choice",
                name="confirm",
                title=match.group("question").strip(),
                detail="The engine is waiting on a yes/no answer.",
                raw=line,
            )
            return

    def _set_prompt_locked(self, *, kind: str, name: str, title: str, detail: str, raw: str) -> None:
        self._prompt_seq += 1
        self._prompt = _Prompt(
            id=f"{self.id}-p{self._prompt_seq}",
            kind=kind,
            name=name,
            title=title,
            detail=detail,
            raw=raw.strip(),
        )

    def tick(self) -> None:
        """Offer a generic answer box for an unrecognised question that has stalled."""
        with self._cond:
            if self._prompt is not None or self.status != "running":
                return
            if time.monotonic() - self._last_output < IDLE_PROMPT_SECONDS:
                return
            match = _TRAILING_QUESTION_RE.search(self._pending_line)
            if not match:
                return
            self._set_prompt_locked(
                kind="text",
                name="unknown",
                title=match.group("question").strip(),
                detail=(
                    "The engine printed this and stopped without a newline. The console does "
                    "not recognise it, so the answer is sent through as typed."
                ),
                raw=self._pending_line,
            )
            self._cond.notify_all()

    def answer(self, prompt_id: str, value: str) -> None:
        if _CONTROL_RE.search(value):
            # One answer is one line: a newline here would pre-answer whatever
            # the engine asks next.
            raise ActionError("an answer cannot contain control characters")
        with self._cond:
            prompt = self._prompt
            if prompt is None or prompt.id != prompt_id:
                raise ActionError("that prompt is no longer waiting", status=409)
            # A question the console did not recognise may well be asking for a
            # secret, so it is masked like one — the safety net fails closed.
            if prompt.kind in ("secret", "text"):
                # An empty answer is a real thing the operator can send (the
                # engine reads it as "no code"), so the transcript must not
                # claim a secret was typed when none was.
                echo = "********\n" if value else "\n"
                if value:
                    self._secrets.append(value)
            else:
                echo = f"{value}\n"
            self._prompt = None
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            self._restore_prompt(prompt)
            raise ActionError("the command is no longer running", status=409)
        # Echo first: the engine's reply to this answer must not appear above it.
        self._append(echo)
        payload = (value + "\n").encode("utf-8")
        try:
            written = 0
            while written < len(payload):
                # Unbuffered pipes can short-write; None means "wrote it all".
                sent = proc.stdin.write(payload[written:])
                written = len(payload) if sent is None else written + sent
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            self._append("[console] that answer could not be delivered\n")
            self._restore_prompt(prompt)
            raise ActionError(f"could not reach the command: {exc}", status=409) from exc

    def _restore_prompt(self, prompt: _Prompt) -> None:
        """Put an undelivered question back: the engine is still waiting on it."""
        with self._cond:
            if self._prompt is None and self.status == "running":
                self._prompt = prompt
            self._cond.notify_all()

    # -- views -------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._cond:
            return {
                "id": self.id,
                "action": self.spec.id,
                "label": self.spec.label,
                "command": self.command,
                "cap": self.spec.cap,
                "root": self.root,
                "run_id": self.run_id,
                "tool": self.tool,
                "status": self.status,
                "exit_code": self.exit_code,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "prompt": self._prompt.payload() if self._prompt else None,
                "cursor": self._dropped + len(self._text),
            }

    def output(self, cursor: int, wait: float = 0.0, seen_prompt: str | None = None) -> dict:
        """Everything after ``cursor``, waiting up to ``wait`` seconds for more.

        ``seen_prompt`` is the prompt the caller has already rendered. Without
        it a pending question would satisfy every poll instantly, and the
        client would spin for the whole time the operator takes to answer —
        which is precisely the longest wait in a release.
        """
        deadline = time.monotonic() + max(0.0, wait)
        end = cursor
        text = ""
        reset = False
        with self._cond:
            while True:
                end = self._dropped + len(self._text)
                if cursor < self._dropped:
                    start = 0
                    reset = True
                else:
                    start = min(len(self._text), cursor - self._dropped)
                    reset = False
                text = self._text[start:]
                pending = self._prompt.id if self._prompt else None
                unseen = pending is not None and pending != seen_prompt
                if text or reset or unseen or self.status != "running":
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 1.0))
        payload = self.snapshot()
        payload["text"] = text
        payload["cursor"] = end
        payload["reset"] = reset
        return payload


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ActionRegistry:
    """Validates requests, enforces one command per checkout, keeps transcripts."""

    def __init__(
        self,
        *,
        roots: list[Path],
        engine_python: str | None = None,
        argv_builder: Callable[[ActionSpec, list[str], Path], list[str]] | None = None,
        on_finish: Callable[[ActionRunner], None] | None = None,
    ) -> None:
        self._roots = {str(Path(root).resolve()): Path(root).resolve() for root in roots}
        self._engine_python = engine_python or sys.executable
        self._argv_builder = argv_builder or self._default_argv
        self._on_finish = on_finish
        self._lock = threading.Lock()
        self._runners: dict[str, ActionRunner] = {}
        self._order: list[str] = []

    def _default_argv(self, spec: ActionSpec, args: list[str], cwd: Path) -> list[str]:
        del cwd
        if spec.kind == "git":
            return ["git", *args]
        return [self._engine_python, "-m", "edge_deploy", *args]

    def resolve_root(self, raw: object) -> Path:
        candidate = str(raw or "")
        if not candidate:
            raise ActionError("a checkout root is required")
        resolved = self._roots.get(str(Path(candidate).resolve()))
        if resolved is None:
            raise ActionError("that checkout is not watched by this console", status=403)
        return resolved

    def build_params(self, spec: ActionSpec, root: Path, body: dict) -> dict:
        params: dict = {}
        if "run_id" in spec.params:
            raw = body.get("run_id")
            if raw or spec.needs_run:
                run_id = _clean_run_id(raw)
                state = find_run_state(root, run_id)
                if state is None:
                    raise ActionError(f"no run {run_id} under {root}", status=404)
                if is_training_state(state):
                    raise ActionError(
                        "training ledgers are practice-only; the console never runs "
                        "production commands against them",
                        status=403,
                    )
                params["run_id"] = run_id
        if "nodes" in spec.params:
            params["nodes"] = _clean_nodes(body.get("nodes"))
        if "node" in spec.params:
            params["node"] = _clean_node(body.get("node"))
        if "reason" in spec.params:
            params["reason"] = _clean_reason(body.get("reason"))
        return params

    def start(self, body: dict) -> ActionRunner:
        spec = ACTION_SPECS.get(str(body.get("action") or ""))
        if spec is None:
            raise ActionError(f"unknown action: {body.get('action')!r}", status=404)
        root = self.resolve_root(body.get("root"))
        params = self.build_params(spec, root, body)
        args = spec.args(params)
        argv = self._argv_builder(spec, args, root)
        command = display_command(spec, args)

        with self._lock:
            for runner in self._runners.values():
                # "starting" counts as busy: the runner is registered before
                # Popen returns, and a second request in that window would
                # otherwise slip past the one-command-per-checkout rule.
                if runner.status in ("starting", "running") and runner.root == str(root):
                    raise ActionError(
                        f"{runner.command} is still running in this checkout", status=409
                    )
            action_id = uuid.uuid4().hex[:12]
            runner = ActionRunner(
                action_id=action_id,
                spec=spec,
                argv=argv,
                cwd=root,
                command=command,
                root=str(root),
                run_id=params.get("run_id"),
                tool=str(body.get("tool") or "") or None,
                on_finish=self._on_finish,
            )
            self._runners[action_id] = runner
            self._order.append(action_id)
            self._evict_locked()
        runner.start()
        return runner

    def _evict_locked(self) -> None:
        while len(self._order) > RETAINED_FINISHED_ACTIONS:
            for index, action_id in enumerate(self._order):
                if self._runners[action_id].status not in ("starting", "running"):
                    self._order.pop(index)
                    self._runners.pop(action_id, None)
                    break
            else:
                return

    def get(self, action_id: str) -> ActionRunner:
        with self._lock:
            runner = self._runners.get(action_id)
        if runner is None:
            raise ActionError("no such action", status=404)
        return runner

    def snapshot(self) -> dict:
        with self._lock:
            runners = [self._runners[action_id] for action_id in self._order]
        for runner in runners:
            runner.tick()
        return {"actions": [runner.snapshot() for runner in runners]}

    def shutdown(self) -> None:
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            runner.cancel()


def catalog() -> list[dict]:
    """The allowlist, for the page footer and for tests that assert the surface."""
    return [
        {
            "id": spec.id,
            "label": spec.label,
            "kind": spec.kind,
            "cap": spec.cap,
            "destructive": spec.destructive,
            "summary": spec.summary,
        }
        for spec in ACTION_SPECS.values()
    ]
