"""Start and inspect the local playlist dashboard."""

from pathlib import Path
from typing import Optional

import typer

from . import playlist_library
from .dashboard import server
from .storage import ForgeError


app = typer.Typer(help="Serve the playlist dashboard on localhost.", no_args_is_help=True,
                  context_settings={"help_option_names": ["-h", "--help"]})


def _show(ctx, command, record):
    if ctx.obj.as_json:
        from .cli import emit
        emit(ctx, command, **record)
    else:
        typer.echo(record.get("url") or "Dashboard stopped.")


@app.command()
def start(ctx: typer.Context,
          port: int = typer.Option(8787, "--port", min=0, max=65535, help="Loopback port, or 0 for a free port."),
          foreground: bool = typer.Option(False, "--foreground", help="Run the service in this terminal."),
          library: Optional[Path] = typer.Option(None, "--library", metavar="<dir>")):
    """Start the dashboard and print its local session link."""
    if foreground and ctx.obj.as_json:
        raise ForgeError("Use --json with a background dashboard.")
    selected = playlist_library.selected(ctx.obj.home, library)
    record = server.start(ctx.obj.home, Path(selected["path"]).parent, port, foreground=foreground)
    if record:
        _show(ctx, "dashboard start", record)


@app.command()
def status(ctx: typer.Context):
    """Print the active dashboard's local session link."""
    _show(ctx, "dashboard status", server.status(ctx.obj.home))


@app.command()
def stop(ctx: typer.Context):
    """Stop the dashboard server."""
    _show(ctx, "dashboard stop", server.stop(ctx.obj.home))
