"""Start and inspect the local playlist dashboard."""

from pathlib import Path
from typing import Optional

import typer

from .dashboard import config as preferences, server
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
          port: Optional[int] = typer.Option(None, "--port", min=0, max=65535, help="Override the saved port; 0 selects a free port."),
          foreground: bool = typer.Option(False, "--foreground", help="Run the service in this terminal."),
          library: Optional[Path] = typer.Option(None, "--library", help="Override the saved playlist library.", metavar="<dir>")):
    """Start the dashboard and print its local session link."""
    if foreground and ctx.obj.as_json:
        raise ForgeError("Use --json with a background dashboard.")
    directory, port = preferences.resolve(ctx.obj.home, library=library, port=port)
    record = server.start(ctx.obj.home, directory, port, foreground=foreground)
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


@app.command()
def config(ctx: typer.Context,
           library: Optional[Path] = typer.Option(None, "--library", help="Save this playlist library's absolute path.", metavar="<dir>"),
           port: Optional[int] = typer.Option(None, "--port", min=0, max=65535, help="Save the loopback port; 0 selects a free port.")):
    """Show or update dashboard/config.json in the Forge home."""
    home = ctx.obj.home
    value = (preferences.configure(home, library=library, port=port)
             if library is not None or port is not None else preferences.load(home))
    if ctx.obj.as_json:
        from .cli import emit
        emit(ctx, "dashboard config", config_path=str(preferences.path(home)), **value)
    else:
        typer.echo(f"Configuration: {preferences.path(home)}")
        typer.echo(f"Library: {value['library_path'] or 'Not configured (falls back to playlist selection)'}")
        typer.echo(f"Port: {value['port']}")


@app.command()
def restart(ctx: typer.Context):
    """Restart the dashboard using its saved configuration."""
    home = ctx.obj.home
    directory, port = preferences.resolve(home)
    # Validate the next library before stopping a working service.
    server.stop(home)
    _show(ctx, "dashboard restart", server.start(home, directory, port))
