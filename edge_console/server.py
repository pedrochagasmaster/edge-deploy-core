"""The local HTTP surface: read endpoints, plus the allowlisted action endpoints.

The server binds to loopback only. Read endpoints are open (they are what the
page renders); every mutating endpoint additionally requires the per-process
token embedded in the page, so no other page in the browser can drive a
release.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from edge_console.actions import ACTION_SPECS, ActionError, ActionRegistry, catalog
from edge_console.demo import build_demo_checkouts, demo_argv_builder
from edge_console.ledger import collect_runs_multi
from edge_console.page import PAGE
from edge_console.probes import PostureProber, ToolsProber

DEFAULT_PORT = 7643
MAX_BODY_BYTES = 64 * 1024
MAX_LONG_POLL_SECONDS = 25.0
# Actions that create or advance a Run must run in a real tool checkout.
# Training workspaces deliberately are not git checkouts (ADR-0017).
_NEEDS_CHECKOUT = frozenset(
    {"release", "verify", "publish", "deploy", "tag_bitbucket", "tag_github", "abandon"}
)


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "edge-console"
    protocol_version = "HTTP/1.1"
    roots: list[Path]
    prober: PostureProber
    tools_prober: ToolsProber
    registry: ActionRegistry | None
    demo: bool
    read_only: bool
    token: str

    def log_message(self, *args: object) -> None:  # keep the terminal quiet
        del args

    # -- plumbing ----------------------------------------------------------

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # A reload or a closed tab drops a long poll mid-flight; that is
            # routine, not something to spill a traceback over.
            self.close_connection = True

    def _send_json(self, payload: dict, status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

    def _error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ActionError("bad Content-Length") from exc
        if length > MAX_BODY_BYTES:
            # The body is left unread, so this connection can no longer be
            # reused: the next request would be parsed out of these bytes.
            self.close_connection = True
            raise ActionError("request body too large", status=413)
        if length <= 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ActionError("request body is not JSON") from exc
        if not isinstance(payload, dict):
            raise ActionError("request body must be a JSON object")
        return payload

    def _authorize(self) -> None:
        """Loopback host plus the page token: no other origin can act."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ActionError("unexpected Host header", status=403)
        if self.headers.get("X-Edge-Console-Token") != self.token:
            raise ActionError("missing or stale console token; reload the page", status=403)

    # -- reads -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        parsed = urlsplit(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/":
            body = PAGE.replace("__CONSOLE_TOKEN__", self.token)
            self._send(200, "text/html; charset=utf-8", body.encode("utf-8"))
        elif path == "/api/runs":
            self._send_json(
                {
                    "roots": [str(root) for root in self.roots],
                    "demo": self.demo,
                    "read_only": self.read_only,
                    "runs": collect_runs_multi(self.roots),
                }
            )
        elif path == "/api/tools":
            self._send_json({"demo": self.demo, **self.tools_prober.snapshot()})
        elif path == "/api/posture":
            self._send_json(self.prober.snapshot())
        elif path == "/api/actions":
            payload = {"read_only": self.read_only, "catalog": catalog(), "actions": []}
            if self.registry is not None:
                payload.update(self.registry.snapshot())
            self._send_json(payload)
        elif path.startswith("/api/actions/") and path.endswith("/output"):
            self._output(path.split("/")[3], query)
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def _output(self, action_id: str, query: dict) -> None:
        if self.registry is None:
            self._error(404, "no such action")
            return
        try:
            cursor = int((query.get("cursor") or ["0"])[0])
            wait = min(MAX_LONG_POLL_SECONDS, float((query.get("wait") or ["0"])[0]))
        except ValueError:
            self._error(400, "cursor and wait must be numbers")
            return
        seen = (query.get("seen") or [""])[0] or None
        try:
            runner = self.registry.get(action_id)
        except ActionError as exc:
            self._error(exc.status, str(exc))
            return
        runner.tick()
        self._send_json(
            runner.output(max(0, cursor), wait=max(0.0, wait), seen_prompt=seen)
        )

    # -- actions -----------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802 (stdlib API)
        path = urlsplit(self.path).path
        try:
            # Read the body before deciding anything: a rejected request that
            # leaves its body in the socket desynchronises the next keep-alive
            # request on the same connection.
            body = self._read_body()
            self._authorize()
            if self.read_only or self.registry is None:
                raise ActionError("this console was started read-only", status=403)
            if path == "/api/actions":
                self._start_action(body)
            elif path.startswith("/api/actions/") and path.endswith("/answer"):
                self._answer(path.split("/")[3], body)
            elif path.startswith("/api/actions/") and path.endswith("/cancel"):
                runner = self.registry.get(path.split("/")[3])
                runner.cancel()
                self._send_json(runner.snapshot())
            else:
                self._error(404, "not found")
        except ActionError as exc:
            self._error(exc.status, str(exc))

    def _start_action(self, body: dict) -> None:
        action = str(body.get("action") or "")
        if action in _NEEDS_CHECKOUT:
            root = self.registry.resolve_root(body.get("root"))
            if not (root / ".git").exists():
                raise ActionError(
                    f"{root} is not a git checkout; release commands only run in a tool checkout",
                    status=403,
                )
        runner = self.registry.start(body)
        self._send_json(runner.snapshot(), status=202)

    def _answer(self, action_id: str, body: dict) -> None:
        runner = self.registry.get(action_id)
        prompt_id = str(body.get("prompt_id") or "")
        value = body.get("value")
        if not isinstance(value, str):
            raise ActionError("value must be a string")
        runner.answer(prompt_id, value)
        self._send_json(runner.snapshot())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local release console for edge-deploy runs"
    )
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        help="Tool checkout containing edge-deploy/runs; repeat to watch several (default: cwd)",
    )
    parser.add_argument(
        "--github-write-root",
        action="append",
        default=None,
        help=(
            "Checkout used for GitHub write probes; repeat for several. "
            "Defaults to the same paths as --root when omitted"
        ),
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Serve fabricated checkouts driven by the offline simulator; no network probes",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Watch only: serve the dashboard with every action button disabled",
    )
    parser.add_argument(
        "--engine-python",
        default=None,
        help="Interpreter used for 'python -m edge_deploy' (default: the one running the console)",
    )
    parser.add_argument("--no-browser", action="store_true")
    return parser


def resolve_console_roots(args: argparse.Namespace) -> tuple[list[Path], list[Path]]:
    """Return (ledger_roots, github_write_roots). Write roots default to ledger roots."""
    ledger_roots = [Path(raw).resolve() for raw in (args.root or ["."])]
    if args.github_write_root:
        write_roots = [Path(raw).resolve() for raw in args.github_write_root]
    else:
        write_roots = list(ledger_roots)
    return ledger_roots, write_roots


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.demo:
        roots = build_demo_checkouts()
        write_roots = roots
    else:
        roots, write_roots = resolve_console_roots(args)

    tools_prober = ToolsProber(roots, demo=args.demo)
    registry = None
    if not args.read_only:
        registry = ActionRegistry(
            roots=roots,
            engine_python=args.engine_python,
            argv_builder=demo_argv_builder if args.demo else None,
            # A finished command usually moved the ledger or the checkout;
            # drop the cached divergence so the next poll re-reads reality.
            on_finish=lambda _runner: tools_prober.invalidate(),
        )

    ConsoleHandler.roots = roots
    ConsoleHandler.prober = PostureProber(demo=args.demo, roots=write_roots)
    ConsoleHandler.tools_prober = tools_prober
    ConsoleHandler.registry = registry
    ConsoleHandler.demo = args.demo
    ConsoleHandler.read_only = args.read_only
    ConsoleHandler.token = secrets.token_urlsafe(24)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), ConsoleHandler)
    url = f"http://127.0.0.1:{args.port}/"
    for root in roots:
        print(f"edge-console: watching {root / 'edge-deploy' / 'runs'}")
    if write_roots != roots:
        for root in write_roots:
            print(f"edge-console: github write probe root {root}")
    if args.demo:
        print("edge-console: DEMO — fabricated checkouts driven by the offline simulator")
    if args.read_only:
        print("edge-console: read-only — no command buttons")
    else:
        print(f"edge-console: can run {len(ACTION_SPECS)} allowlisted commands in the watched checkouts")
    print(f"edge-console: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if registry is not None:
            registry.shutdown()
        server.server_close()
    return 0
