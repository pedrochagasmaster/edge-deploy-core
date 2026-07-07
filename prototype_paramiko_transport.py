# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "paramiko==5.0.0",
# ]
# ///
"""DISPOSABLE PROTOTYPE: drive persistent Paramiko transport probes by hand.

Question being tested: can one strictly verified, keyboard-interactive Paramiko
Transport replace repeated controller-side SSH processes while retaining the
command, transfer, PTY, and keepalive behavior required by edge-deploy-core?

Run from the repository root:
    uv run prototype_paramiko_transport.py --node node03

Delete this script and ``edge_deploy/_prototype_paramiko_transport.py`` after
the operator records the experiment's verdict outside GitHub.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from edge_deploy._prototype_paramiko_transport import (
    PersistentParamikoPrototype,
    PrototypeChecklist,
    load_node_settings,
)
from edge_deploy.config import DEFAULT_OPERATOR_CONFIG_PATH


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the disposable persistent-Paramiko transport checklist.",
    )
    parser.add_argument("--node", required=True, help="Operator-config node label (for example, node03).")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_OPERATOR_CONFIG_PATH,
        help="Private operator-config path (default: repository interface default).",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    prototype: PersistentParamikoPrototype | None = None
    exit_code = 0
    try:
        settings = load_node_settings(args.node, args.config)
        prototype = PersistentParamikoPrototype(settings)
        prototype.connect()
        print(f"Authenticated {args.node}; persistent transport #1 ready.")
        exit_code = PrototypeChecklist(args.node, prototype).run()
    except (KeyboardInterrupt, EOFError):
        print("\nPrototype interrupted.")
        exit_code = 130
    except Exception as exc:  # Prototype deliberately suppresses endpoint/config details.
        print(f"Prototype failed ({type(exc).__name__}); endpoint details were suppressed.")
        exit_code = 1
    finally:
        if prototype is not None:
            cleanup_confirmed = prototype.close()
            print("Remote scratch cleanup confirmed." if cleanup_confirmed else "Remote scratch cleanup FAILED.")
            print("Persistent transport explicitly closed.")
            if not cleanup_confirmed and exit_code == 0:
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
