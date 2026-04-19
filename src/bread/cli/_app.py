"""Shared Typer app instances for the bread CLI.

Defining `app` (and `db_app`) in their own module lets each command module
import them without a circular dependency on `bread.cli.__init__`.
"""

from __future__ import annotations

import typer

app = typer.Typer(name="bread", add_completion=False)
db_app = typer.Typer(name="db", help="Database commands")
app.add_typer(db_app)
