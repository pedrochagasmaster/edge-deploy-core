"""``py -m edge_deploy`` entry point."""

from __future__ import annotations

import sys

from edge_deploy.cli import main

if __name__ == "__main__":
    sys.exit(main())
