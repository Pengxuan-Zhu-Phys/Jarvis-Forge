"""Explicit user environment selection and versioned shell management."""

from pathlib import Path
from typing import Optional

import typer

from .cli import emit
from .environment_registry import add, list_environments, repair, show
from .storage import ForgeError


app = typer.Typer(add_completion=False, pretty_exceptions_enable=False,
                  context_settings={"help_option_names": ["-h", "--help"]},
                  help="Manage shared, named environment scripts.")


@app.command("add")
def add_environment(ctx: typer.Context,
                    name: str = typer.Argument(..., help="User-selected environment name.", metavar="<name>"),
                    shell: Path = typer.Option(..., "--shell", help="Copy this Bash environment script.", metavar="<path>"),
                    replace: bool = typer.Option(False, "--replace", help="Save a new revision of an existing environment."),
                    description: Optional[str] = typer.Option(None, "--description", help="Describe this environment.", metavar="<str>")):
    """Register a private script copy without activating it."""
    emit(ctx, "env add", environment=add(ctx.obj.home, name, shell, replace=replace, description=description))


@app.command("list")
def list_environment(ctx: typer.Context):
    """List registered scripts and the apt environment."""
    records = list_environments(ctx.obj.home)
    rows = [{"name": r["name"], "revision": r["revision"], "status": r["status"],
             "path": r["activation"], "description": r["description"]} for r in records]
    emit(ctx, "env list", environments=rows)


@app.command("show")
def show_environment(ctx: typer.Context,
                     name: str = typer.Argument(..., help="Registered environment name.", metavar="<name>"),
                     revision: Optional[str] = typer.Option(None, "--revision", help="Read a retained revision.", metavar="<revision>"),
                     path: bool = typer.Option(False, "--path", help="Print only the activation script path.")):
    """Show a registered environment without executing its script."""
    if path and ctx.obj.as_json:
        raise ForgeError("--path and --json are mutually exclusive.")
    result = show(ctx.obj.home, name, revision, strict=path)
    if path:
        print(result["activation"])
    else:
        emit(ctx, "env show", environment=result)


@app.command("repair")
def repair_environment(ctx: typer.Context,
                       name: Optional[str] = typer.Argument(None, help="Repair this named environment or the active apt environment.", metavar="<name>"),
                       shell: Optional[Path] = typer.Option(None, "--shell", help="Activation file to repair. Defaults to ./activate.sh.", metavar="<path>"),
                       dry_run: bool = typer.Option(False, "--dry-run", help="Check and report settings without writing files.")):
    """Remove invalid literal paths from an activation script."""
    if name is not None and shell is not None:
        raise ForgeError("Choose either an environment name or --shell.")
    if name == "apt":
        from .shell_repair import repair_apt

        report = repair_apt(ctx.obj.home, dry_run=dry_run)
    elif name is not None:
        report = repair(ctx.obj.home, name, dry_run=dry_run)
    else:
        from .shell_repair import repair_file

        report = repair_file(shell or Path("activate.sh"), dry_run=dry_run)
    emit(ctx, "env repair", **report)
