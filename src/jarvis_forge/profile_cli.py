"""Platforms provide tools; profiles select packages, Python and app records."""

from pathlib import Path
from typing import Optional
import typer
import yaml
from . import unified_profiles as profiles
from .storage import ForgeError, read_json, write_text

app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Create and select project profiles that share a platform tool suite.",
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def list_profiles(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        from .cli import emit

        emit(ctx, "profile", **profiles.catalog(ctx.obj.home))


@app.command("check")
def check(ctx: typer.Context, preset: Optional[str] = typer.Argument(None)):
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
    platform: Optional[str] = typer.Option(None, "--platform"),
    preset: Optional[str] = typer.Option(None, "--preset"),
    source: Optional[Path] = typer.Option(None, "--from"),
    stage: Optional[Path] = typer.Option(None, "--stage"),
    target: Optional[str] = typer.Option(None, "--target"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Create a profile and its platform, or reproduce an exported profile."""
    from .cli import emit, _console
    from .apt_cli import _changes_confirmation
    from .apt_progress import AptProgress

    if dry_run or ctx.obj.dry_run:
        if source:
            from .profile_exports import load

            document = load(source)
            preset = document["platform"]["preset"]
            platform = platform or document["platform"]["name"]
        choice = preset or platform
        checked = profiles.check(ctx.obj.home, choice)
        emit(
            ctx,
            "profile create",
            id=name,
            platform=platform or "host default",
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
        "platform": platform,
        "preset": preset,
        "stage": stage,
        "target": target,
        "progress": progress,
        "confirm": lambda plan: _changes_confirmation(ctx, yes, plan),
    }
    try:
        if source:
            from .profile_exports import reproduce

            result = reproduce(ctx.obj.home, name, source, **options)
        else:
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
    emit(ctx, "profile use", **profiles.set_default(ctx.obj.home, name))


@app.command("show")
def show(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None),
    path: bool = typer.Option(False, "--path"),
):
    """Show a profile or platform; --path prints its activation entry."""
    from .cli import emit

    if name is None:
        from .apt_profiles import active_home

        scope = active_home(ctx.obj.home)
        name = scope.root.name
    else:
        scope = profiles._scoped_home(ctx.obj.home, "profiles", name)
    data = profiles.record(scope)
    if not data:
        scope = profiles._scoped_home(ctx.obj.home, "platforms", name)
        data = profiles.platform_record(ctx.obj.home, name)
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


@app.command("export")
def export(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    output: Optional[Path] = typer.Option(None, "--output", "-o"),
):
    """Export pinned package inputs and portable app recipes as profile YAML."""
    from .cli import emit
    from .profile_exports import export as describe

    value = describe(ctx.obj.home, name)
    text = yaml.safe_dump(value, sort_keys=False)
    if output:
        if not ctx.obj.dry_run:
            write_text(output.expanduser().resolve(), text)
        emit(
            ctx,
            "profile export",
            path=str(output.expanduser().resolve()),
            profile=value,
            dry_run=ctx.obj.dry_run,
        )
    elif ctx.obj.as_json:
        emit(ctx, "profile export", profile=value)
    else:
        typer.echo(text, nl=False)


@app.command("remove")
def remove(
    ctx: typer.Context,
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Remove a profile, or a platform no profile is using."""
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
