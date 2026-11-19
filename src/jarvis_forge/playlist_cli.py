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
               title: Optional[str] = typer.Option(None, "--title", help="Library title. Defaults to its ID."),
               demo: Optional[str] = typer.Option(None, "--demo", help="Seed a library for the SARAH-Jarvis demo.",
                                                   metavar="<sarah>")):
    """Write a new library.yaml in the given directory."""
    result = library.initialize(directory, name, title, demo=demo)
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
    from . import playlist_collections as collections
    selected = _selected(ctx)
    result = library.inventory(ctx.obj.home, selected)
    _, errors = collections.catalog(ctx.obj.home, selected, inventory=result)
    result["errors"].extend(errors)
    _report(ctx, "playlist verify", result, strict=strict)


@app.command()
def cases(ctx: typer.Context,
          shelf: Optional[str] = typer.Option(None, "--shelf", help="Select one shelf.", metavar="<id>"),
          project: Optional[str] = typer.Option(None, "--project", help="Select one project.", metavar="<id>")):
    """List test groups and checks without running them."""
    _report(ctx, "playlist cases", library.inventory(ctx.obj.home, _selected(ctx), shelf=shelf, project=project))


@app.command("list")
def list_playlists(ctx: typer.Context):
    """List derived profiles and saved playlists."""
    from . import playlist_collections as collections
    selected = _selected(ctx)
    rows, errors = collections.catalog(ctx.obj.home, selected)
    emit(ctx, "playlist list", library=selected["id"], playlists=rows, errors=errors)


@app.command()
def show(ctx: typer.Context, name: str = typer.Argument(..., metavar="<id>")):
    """Show a playlist and its resolved members."""
    from . import playlist_collections as collections
    selected = _selected(ctx)
    emit(ctx, "playlist show", library=selected["id"], playlist=collections.show(ctx.obj.home, selected, name))


def _entries(cases, profiles):
    return [{"case": item} for item in cases] + [{"profile": item} for item in profiles]


@app.command()
def create(ctx: typer.Context, name: str = typer.Argument(..., metavar="<id>"),
           title: str = typer.Option(..., "--title", metavar="<text>"),
           goal: Optional[str] = typer.Option(None, "--goal"),
           shelf: Optional[str] = typer.Option(None, "--shelf"),
           env: Optional[str] = typer.Option(None, "--env"),
           case: list[str] = typer.Option([], "--case"),
           profile: list[str] = typer.Option([], "--profile")):
    """Save a new playlist of cases and live profile references."""
    from . import playlist_collections as collections
    selected = _selected(ctx)
    item = collections.create(ctx.obj.home, selected, name, title=title, goal=goal, shelf=shelf,
                              environment=env, entries=_entries(case, profile))
    emit(ctx, "playlist create", library=selected["id"], playlist=item)


def _edit(ctx, operation, name, case, profile, reason):
    from . import playlist_collections as collections
    selected = _selected(ctx)
    item = collections.edit(ctx.obj.home, selected, name, operation, _entries(case, profile), reason=reason)
    emit(ctx, f"playlist {operation}", library=selected["id"], playlist=item)


@app.command()
def add(ctx: typer.Context, name: str = typer.Argument(..., metavar="<id>"),
        case: list[str] = typer.Option([], "--case"),
        profile: list[str] = typer.Option([], "--profile"),
        reason: Optional[str] = typer.Option(None, "--reason")):
    """Append cases or profiles to a saved playlist."""
    _edit(ctx, "add", name, case, profile, reason)


@app.command()
def remove(ctx: typer.Context, name: str = typer.Argument(..., metavar="<id>"),
           case: list[str] = typer.Option([], "--case"),
           profile: list[str] = typer.Option([], "--profile"),
           reason: Optional[str] = typer.Option(None, "--reason"),
           yes: bool = typer.Option(False, "--yes", help="Confirm removing the selected entries.")):
    """Remove entries from a saved playlist."""
    if not yes:
        from .storage import ForgeError
        raise ForgeError("Pass --yes to remove the selected playlist entries.")
    _edit(ctx, "remove", name, case, profile, reason)


def _job_result(ctx, command, selected, state, waited):
    ok = not waited or state["status"] == "passed"
    emit(ctx, command, ok=ok, library=selected["id"], job=state)
    if not ok:
        raise typer.Exit(130 if state["status"] == "interrupted" else 1)


@app.command()
def run(ctx: typer.Context, name: Optional[str] = typer.Argument(None, metavar="<id>"),
        case: list[str] = typer.Option([], "--case"),
        env: Optional[str] = typer.Option(None, "--env"),
        jobs: int = typer.Option(1, "--jobs", "-j", min=1, max=8),
        fresh: bool = typer.Option(False, "--fresh"),
        wait: bool = typer.Option(False, "--wait", help="Wait for the final result.")):
    """Queue a playlist or a selection of cases for verification."""
    from . import playlist_jobs
    selected = _selected(ctx)
    state = playlist_jobs.submit(ctx.obj.home, selected, name=name, cases=case, jobs=jobs,
                                 environment=env, fresh=fresh)
    if wait:
        state = playlist_jobs.wait_job(ctx.obj.home, selected, state["id"])
    _job_result(ctx, "playlist run", selected, state, wait)


@app.command()
def jobs(ctx: typer.Context, limit: int = typer.Option(25, "--limit", min=1, max=250)):
    """List queued, running, and recent playlist jobs."""
    from . import playlist_jobs
    selected = _selected(ctx)
    emit(ctx, "playlist jobs", library=selected["id"], jobs=playlist_jobs.list_jobs(ctx.obj.home, selected, limit))


@app.command()
def job(ctx: typer.Context, job_id: str = typer.Argument(..., metavar="<job>"),
        wait: bool = typer.Option(False, "--wait", help="Wait for the final result.")):
    """Show a playlist job and its case results."""
    from . import playlist_jobs
    selected = _selected(ctx)
    get = playlist_jobs.wait_job if wait else playlist_jobs.get
    _job_result(ctx, "playlist job", selected, get(ctx.obj.home, selected, job_id), wait)


@app.command()
def cancel(ctx: typer.Context, job_id: str = typer.Argument(..., metavar="<job>")):
    """Cancel a queued job or interrupt its active tasks."""
    from . import playlist_jobs
    selected = _selected(ctx)
    _job_result(ctx, "playlist cancel", selected, playlist_jobs.cancel(ctx.obj.home, selected, job_id), False)
