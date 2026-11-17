"""Inspect and prepare a local playlist library."""

from pathlib import Path
from typing import Optional

import typer

from . import playlist_library as library
from .cli import _console, emit
from .presentation import error, line


_HELP = {"help_option_names": ["-h", "--help"]}
app = typer.Typer(help="Manage a library of test cases across projects.", no_args_is_help=True,
                  context_settings=_HELP)
project_app = typer.Typer(help="List projects and register local checkouts.", no_args_is_help=True,
                          context_settings=_HELP)
app.add_typer(project_app, name="project")


@app.callback()
def options(ctx: typer.Context, directory: Optional[Path] = typer.Option(
        None, "--library", help="Use this playlist library directory.", metavar="<dir>")):
    ctx.meta["playlist_library"] = directory


def _selected(ctx):
    return library.selected(ctx.obj.home, ctx.meta["playlist_library"])


@app.command("init")
def initialize(ctx: typer.Context, directory: Path = typer.Argument(..., metavar="<dir>"),
               name: Optional[str] = typer.Option(None, "--id", help="Library ID. Defaults to the directory name."),
               title: Optional[str] = typer.Option(None, "--title", help="Library title. Defaults to its ID.")):
    """Write a new library.yaml in the given directory."""
    result = library.initialize(directory, name, title)
    emit(ctx, "playlist init", library=result["id"], title=result["title"], path=result["path"])


@app.command()
def use(ctx: typer.Context, directory: Path = typer.Argument(..., metavar="<dir>")):
    """Remember a library for this Forge home."""
    result = library.use(ctx.obj.home, directory)
    emit(ctx, "playlist use", library=result["id"], title=result["title"], path=result["path"])


@project_app.command("list")
def project_list(ctx: typer.Context):
    """List projects and their resolved checkout paths."""
    selected = _selected(ctx)
    emit(ctx, "playlist project list", library=selected["id"], projects=library.projects(ctx.obj.home, selected))


@project_app.command("path")
def project_path(ctx: typer.Context, name: str = typer.Argument(..., metavar="<id>"),
                 directory: Path = typer.Argument(..., metavar="<dir>")):
    """Register this project's checkout path on the local machine."""
    selected = _selected(ctx)
    emit(ctx, "playlist project path", library=selected["id"],
         **library.register_root(ctx.obj.home, selected, name, directory))


def _report(ctx, command, result, *, strict=False):
    errors = result["errors"] + (result["warnings"] if strict else [])
    if ctx.obj.as_json:
        if errors:
            emit(ctx, command, ok=False, error="\n".join(errors), **result)
        else:
            emit(ctx, command, **result)
    else:
        diagnostics = _console(stderr=True)
        for warning in result["warnings"]:
            if not strict:
                line(diagnostics, f"Warning: {warning}")
        if errors:
            error(diagnostics, ctx.find_root().info_name, "\n".join(errors))
        else:
            emit(ctx, command, **result)
    if errors:
        raise typer.Exit(2)


@app.command()
def sync(ctx: typer.Context):
    """Refresh the local inventory from available test cards."""
    _report(ctx, "playlist sync", library.sync(ctx.obj.home, _selected(ctx)))


@app.command()
def verify(ctx: typer.Context, strict: bool = typer.Option(False, "--strict", help="Treat warnings as errors.")):
    """Validate the library and its available sources."""
    _report(ctx, "playlist verify", library.inventory(ctx.obj.home, _selected(ctx)), strict=strict)


@app.command()
def cases(ctx: typer.Context,
          shelf: Optional[str] = typer.Option(None, "--shelf", help="Select one shelf.", metavar="<id>"),
          project: Optional[str] = typer.Option(None, "--project", help="Select one project.", metavar="<id>")):
    """List test groups and checks without running them."""
    _report(ctx, "playlist cases", library.inventory(ctx.obj.home, _selected(ctx), shelf=shelf, project=project))
