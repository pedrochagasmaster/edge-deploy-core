"""Shared Edge Node Python interpreter resolution.

Bare ``python3`` is not reliably on PATH on the Edge Nodes: interpreters live
under ``/sys_apps_01/python/python3XX/bin/`` and are exposed via versioned
names or aliases (see autobench's ``install.sh`` and ``_install_python_expr``
in :mod:`edge_deploy.rollout`). Every remote Python invocation must resolve a
concrete interpreter through this expression instead of assuming ``python3``.
"""

from __future__ import annotations

# Shell expression (POSIX sh compatible) resolving the best available Python.
# The trailing printf fallback keeps the expression non-empty so a missing
# interpreter fails loudly at exec time with the attempted path in the error.
REMOTE_PYTHON_EXPR = (
    '"$(command -v python3.11 || command -v python3.10 || command -v python3 || '
    'printf %s /sys_apps_01/python/python310/bin/python3.10)"'
)
