"""The five commands for complete built-in compiler environments."""

from pathlib import Path
from typing import Optional
import typer
from . import unified_profiles as profiles
from .storage import ForgeError, read_json

app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Check, create, use, show and remove built-in compiler environments.",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _name(name):
    if name not in profiles.presets():
        raise ForgeError(f"Unknown built-in profile: {name}.", hint="Choose ubuntu22, ubuntu24, ubuntu26 or macos.")


@app.callback()
def list_profiles(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        from .cli import emit

        typer.echo(ctx.get_help())


@app.command("check")
def check(ctx: typer.Context, preset: str = typer.Argument(...)):
    """Check host, CPU and external Python prerequisites without installing anything."""
    from .cli import emit

    result = profiles.check(ctx.obj.home, preset)
    failed = bool(preset) and all(
        row["status"] not in {"buildable", "ready"} for row in result["profiles"]
    )
    emit(ctx, "profile check", ok=not failed, **result)
    if failed:
        raise typer.Exit(2)


@app.command("create")
def create(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    stage: Optional[Path] = typer.Option(None, "--stage"),
    target: Optional[str] = typer.Option(None, "--target"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Create or resume this complete built-in environment."""
    from .cli import emit, _console
    from .apt_cli import _changes_confirmation
    from .apt_progress import AptProgress

    if dry_run or ctx.obj.dry_run:
        checked = profiles.check(ctx.obj.home, name)
        emit(
            ctx,
            "profile create",
            id=name,
            status="planned",
            dry_run=True,
            prerequisites=checked,
            activation=str(ctx.obj.home.root / "profiles" / name / "activate.sh"),
        )
        return
    progress = AptProgress(
        _console(stderr=True), verbose=ctx.obj.verbose, as_json=ctx.obj.as_json
    )
    ctx.obj.apt_progress = progress
    options = {
        "stage": stage,
        "target": target,
        "progress": progress,
        "confirm": lambda plan: _changes_confirmation(ctx, yes, plan),
    }
    try:
        result = profiles.create(ctx.obj.home, name, **options)
    finally:
        progress.close()
    emit(ctx, "profile create", **result)


@app.command("use")
def use(ctx: typer.Context, name: str = typer.Argument(...)):
    """Select this host type's default profile and hand it to the shell hook."""
    from .cli import emit

    if ctx.obj.dry_run:
        scope = profiles._scoped_home(ctx.obj.home, "profiles", name)
        data = profiles.record(scope)
        if not data:
            raise ForgeError(f"No profile named {name}.")
        emit(
            ctx,
            "profile use",
            **{
                **data,
                "status": "planned",
                "dry_run": True,
                "activation": str(scope.root / "activate.sh"),
            },
        )
        return
    _name(name)
    emit(ctx, "profile use", **profiles.set_default(ctx.obj.home, name))


@app.command("show")
def show(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None),
    path: bool = typer.Option(False, "--path"),
):
    """List built-in environments, or show one profile and its activation entry."""
    from .cli import emit

    if name is None:
        if path:
            raise ForgeError("Give a profile name with --path.")
        emit(ctx, "profile show", view="list", **profiles.catalog(ctx.obj.home))
        return
    _name(name)
    scope = profiles._scoped_home(ctx.obj.home, "profiles", name)
    data = profiles.record(scope)
    if not data:
        raise ForgeError(f"Profile {name} is not installed.", hint=f"jforge profile create {name}")
    if path:
        if ctx.obj.as_json:
            raise ForgeError("--path and --json are mutually exclusive.")
        print(scope.root / "activate.sh")
        return
    state = (
        read_json(scope.root / "packages.json")
        if (scope.root / "packages.json").is_file()
        else {}
    )
    emit(
        ctx,
        "profile show",
        **{
            **data,
            "activation": str(scope.root / "activate.sh"),
            "packages": len(state.get(data["backend"], {}).get("packages", [])),
            "apps": len(data.get("apps", [])),
            "python": data.get("python", data.get("bootstrap_python", {})),
            "history": [
                read_json(file)
                for file in sorted((scope.root / "history").glob("*/result.json"))
            ],
        },
    )


@app.command("remove")
def remove(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Remove this complete environment."""
    _name(name)
    from .cli import emit
    from .apt_cli import _confirm

    emit(
        ctx,
        "profile remove",
        **profiles.remove(
            ctx.obj.home,
            name,
            dry_run=dry_run or ctx.obj.dry_run,
            confirm=lambda: _confirm(ctx, yes, f"Remove {name}?"),
        ),
    )
