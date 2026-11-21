"""Human-facing app lifecycle; package installation remains native to apt."""

from contextlib import nullcontext
from pathlib import Path
from typing import Optional
import typer
import yaml
import math

from . import build_apps as apps, build_catalog as catalog, builds
from .build_names import app_name
from .storage import ForgeError

app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Install, inspect and remove source-built apps in a profile.",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def parameters(values):
    result = {}
    for item in values:
        if "=" not in item:
            raise ForgeError("Use --set NAME=VALUE.")
        name, text = item.split("=", 1)
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ForgeError(f"Invalid parameter {name}: {exc}") from exc
        if (
            not name
            or not isinstance(value, (str, int, float, bool))
            or isinstance(value, float)
            and not math.isfinite(value)
        ):
            raise ForgeError("Build parameters must be scalar values.")
        result[name] = value
    return result


def _scope(ctx):
    from .apt_profiles import active_home

    return active_home(ctx.obj.home)


def _rows(ctx):
    from .unified_profiles import record

    scope = _scope(ctx)
    return apps.installed(ctx.obj.home, scope) if record(scope) else []


def _emit(ctx, command, result):
    from .cli import emit

    failed = result.get("status") in {"failed", "interrupted"}
    result = {**result}
    if failed:
        run = result.get("runs", [{}])[-1]
        result["error"] = {
            "type": "BuildFailed",
            "message": result.get("error") or "App operation failed.",
            "hint": run.get("hint"),
        }
    emit(ctx, "build " + command, ok=not failed, **result)
    if failed:
        raise typer.Exit(130 if result["status"] == "interrupted" else 1)


@app.callback()
def group(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        if ctx.obj.as_json:
            from .cli import emit

            emit(ctx, "build help", commands=sorted(ctx.command.commands))
        else:
            typer.echo(ctx.get_help())


@app.command("list")
def list_builds(
    ctx: typer.Context, available: bool = typer.Option(False, "--available")
):
    """List this profile's installed apps and ON/OFF state, or available recipes."""
    from .cli import emit

    rows = (
        [{**row, "app": app_name(row["id"])} for row in catalog.entries(ctx.obj.home)]
        if available
        else _rows(ctx)
    )
    emit(
        ctx,
        "build list",
        view="available" if available else "installed",
        builds=rows,
        libraries=catalog.libraries(ctx.obj.home) if available else [],
    )


@app.command("search")
def search(ctx: typer.Context, term: str = typer.Argument(...)):
    """Search the available app recipes."""
    from .cli import emit

    rows = [
        {**row, "app": app_name(row["id"])}
        for row in catalog.entries(ctx.obj.home)
        if term.casefold()
        in (app_name(row["id"]) + " " + row.get("description", "")).casefold()
    ]
    emit(ctx, "build search", view="available", builds=rows, libraries=[])


@app.command("show")
def show(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    path: bool = typer.Option(False, "--path"),
    expanded: bool = typer.Option(False, "--expanded"),
):
    """Show an installed app or recipe; --path prints its installation prefix."""
    from .cli import emit, _console
    from .presentation import show_build

    selected, version = apps.split(name)
    # Recipe inspection does not require an installed app or a usable profile.
    rows = [] if expanded or apps._local(name) else _rows(ctx)
    matches = [
        row
        for row in rows
        if row["name"] == selected
        and (version is None and row.get("enabled") or version == row["version"])
    ]
    if path:
        if ctx.obj.as_json or expanded:
            raise ForgeError("--path cannot be combined with --json or --expanded.")
        if len(matches) != 1 or matches[0]["status"] != "ready":
            raise ForgeError(
                f"No unique installed prefix for {name}.", hint="jforge build list"
            )
        print(matches[0]["prefix"])
        return
    if matches and not expanded:
        emit(ctx, "build show", app=matches[0])
        return
    source = catalog.resolve(ctx.obj.home, name)
    if expanded:
        value = builds.load(source, home=ctx.obj.home)
        if ctx.obj.as_json:
            emit(
                ctx,
                "build show",
                path=str(source),
                build=value["config"],
                host_requirements=builds.host_requirements(value["config"]),
            )
        else:
            typer.echo(yaml.safe_dump(value["config"], sort_keys=False))
        return
    try:
        raw = yaml.load(source.read_text(), Loader=builds.UniqueLoader)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read build {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ForgeError("Build recipe must be a mapping.")
    requirements = builds.host_requirements(raw)
    if ctx.obj.as_json:
        emit(
            ctx,
            "build show",
            path=str(source),
            build=raw,
            host_requirements=requirements,
        )
    else:
        show_build(
            _console(),
            raw,
            path=source,
            sha256=builds.digest(source),
            requirements=requirements,
        )


@app.command("fetch")
def fetch(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    directory: Optional[Path] = typer.Argument(None),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Fetch a recipe into the profile, or an explicit external workspace DIR."""
    from .cli import emit

    if dry_run or ctx.obj.dry_run:
        scope = apps.selected(ctx.obj.home)
        source = apps._source(ctx.obj.home, scope, name, fresh=True)
        loaded = builds.load(source, home=ctx.obj.home)
        target = directory or scope.root / "src" / apps.semantic_name(
            app_name(loaded["config"]["id"]), builds._declared_version(loaded["config"])
        )
        emit(ctx, "build fetch", workspace=str(target), dry_run=True)
        return
    from .apt_cli import _changes_confirmation

    scope = apps.selected(
        ctx.obj.home,
        write=True,
        confirm=lambda plan: _changes_confirmation(ctx, False, plan),
    )
    with apps.locked(scope):
        target = apps.fetch(ctx.obj.home, name, directory, scope=scope)
    emit(ctx, "build fetch", path=str(target / "build.yaml"), workspace=str(target))


@app.command("install")
def install(
    ctx: typer.Context,
    names: Optional[list[str]] = typer.Argument(None),
    rebuild: bool = typer.Option(False, "--rebuild"),
    setting: list[str] = typer.Option([], "--set"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    no_activate: bool = typer.Option(False, "--no-activate"),
):
    """Resolve dependencies, build, verify and enable apps; resume unchanged failed work."""
    from .cli import _console
    from .presentation import line
    from .apt_cli import _changes_confirmation

    if not names:
        from .build_workspace import locate

        if locate(Path.cwd()):
            names = [str(Path.cwd())]
        else:
            raise ForgeError("Supply an app name or workspace directory.")
    dry_run = dry_run or ctx.obj.dry_run
    results = []
    for name in names:
        result = apps.install(
            ctx.obj.home,
            name,
            parameters=parameters(setting),
            rebuild=rebuild,
            dry_run=dry_run,
            confirm=lambda plan: _changes_confirmation(ctx, yes, plan),
            progress=lambda text: line(_console(stderr=True), text),
            verbose=ctx.obj.verbose,
            as_json=ctx.obj.as_json,
        )
        results.append(result)
        if result["status"] in {"failed", "interrupted"}:
            _emit(ctx, "install", result)
    result = {
        "status": "planned" if dry_run else "passed",
        "dry_run": dry_run,
        "apps": [row for item in results for row in item.get("apps", [])],
        "runs": [row for item in results for row in item.get("runs", [])],
        "actions": [item.get("action", "planned") for item in results],
        "activation": results[-1].get("activation"),
        "no_activate": no_activate,
        "plans": [item for item in results if item.get("dry_run")],
    }
    _emit(ctx, "install", result)


@app.command("remove")
def remove(
    ctx: typer.Context,
    names: list[str] = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Remove apps; external workspace files remain user-owned."""
    from .apt_cli import _changes_confirmation

    scope = apps.selected(ctx.obj.home)
    with nullcontext() if dry_run or ctx.obj.dry_run else apps.locked(scope):
        result = apps.remove(
            ctx.obj.home,
            names,
            scope=scope,
            dry_run=dry_run or ctx.obj.dry_run,
            confirm=lambda plan: _changes_confirmation(ctx, yes, plan),
        )
    _emit(ctx, "remove", result)


@app.command("autoremove")
def autoremove(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Reclaim disabled internal app versions and their source/build trees."""
    from .apt_cli import _changes_confirmation

    scope = apps.selected(ctx.obj.home)
    with nullcontext() if dry_run or ctx.obj.dry_run else apps.locked(scope):
        result = apps.autoremove(
            ctx.obj.home,
            scope=scope,
            dry_run=dry_run or ctx.obj.dry_run,
            confirm=lambda plan: _changes_confirmation(ctx, yes, plan),
        )
    _emit(ctx, "autoremove", result)


@app.command("update")
def update(
    ctx: typer.Context,
    source: str = typer.Option(catalog.INDEX_URL, "--source"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Refresh app recipes without changing installed apps."""
    from .cli import emit

    if dry_run or ctx.obj.dry_run:
        emit(ctx, "build update", dry_run=True, status="planned", source=source)
        return
    result = catalog.update(ctx.obj.home, source)
    emit(
        ctx,
        "build update",
        **{
            **result,
            "builds": [{**row, "app": app_name(row["id"])} for row in result["builds"]],
        },
    )


@app.command("upgrade")
def upgrade(
    ctx: typer.Context,
    names: Optional[list[str]] = typer.Argument(None),
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Upgrade changed recipes or dependencies, preserving edited workspaces."""
    from .cli import _console
    from .presentation import line
    from .apt_cli import _changes_confirmation

    result = apps.upgrade(
        ctx.obj.home,
        names or [],
        dry_run=dry_run or ctx.obj.dry_run,
        confirm=lambda plan: _changes_confirmation(ctx, yes, plan),
        progress=lambda text: line(_console(stderr=True), text),
    )
    _emit(ctx, "upgrade", result)
