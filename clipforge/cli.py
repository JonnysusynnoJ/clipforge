"""ClipForge command line interface."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from clipforge.core import db, doctor
from clipforge.core.logging import configure_logging
from clipforge.core.settings import get_settings

app = typer.Typer(
    help="ClipForge: clipping pipeline for Whop Content Rewards.", no_args_is_help=True
)
db_app = typer.Typer(help="Database commands.", no_args_is_help=True)
app.add_typer(db_app, name="db")

console = Console()

_STYLE = {doctor.Status.OK: "green", doctor.Status.WARN: "yellow", doctor.Status.FAIL: "red"}


@app.callback()
def _main() -> None:
    settings = get_settings()
    configure_logging(settings.paths.logs)


@app.command("doctor")
def doctor_cmd() -> None:
    """Check ffmpeg, folders, database, ML backends, secrets and publishing config."""
    results = doctor.run_all(get_settings())
    table = Table(title="clipforge doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    for r in results:
        table.add_row(r.name, f"[{_STYLE[r.status]}]{r.status.value.upper()}[/]", r.detail)
    console.print(table)
    fails = sum(r.status is doctor.Status.FAIL for r in results)
    warns = sum(r.status is doctor.Status.WARN for r in results)
    console.print(f"{len(results)} checks: {fails} failed, {warns} warnings")
    raise typer.Exit(code=1 if fails else 0)


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply database migrations."""
    settings = get_settings()
    settings.ensure_dirs()
    db.upgrade(settings.database_url, revision)
    console.print(f"database at {revision}: {settings.paths.db_file}")


if __name__ == "__main__":
    app()
