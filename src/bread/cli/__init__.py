"""Bread CLI package.

The Typer `app` is defined in `_app.py`; importing the command modules below
registers their commands as a side effect. New command groups should be added
here so they hang off `bread <command>`.
"""

from __future__ import annotations

from bread.cli import admin, backtest, data, journal, orders, run  # noqa: F401
from bread.cli._app import app

__all__ = ["app"]
