"""Explicit user environment selection and versioned shell management."""

import os
from pathlib import Path
from typing import Optional

import typer

from .cli import emit
from .environment_registry import add, list_environments, repair, show
from .storage import ForgeError


app = typer.Typer(add_completion=False, pretty_exceptions_enable=False,
                  context_settings={"help_option_names": ["-h", "--help"]},
                  help="Choose the environment a shell loads.\n\n"
                       "jforge env switch <name> selects apt or a named environment.\n"
                       "jforge apt profile <name> selects the package profile used by apt.\n"
                       "jforge env metadata [name] shows that environment's metadata file.\n"
                       "jforge env drop root removes that build from the apt environment.\n\n"
                       "Command help: jforge env COMMAND -h")


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
    """List named shell environments and configured package profiles."""
    from .apt_profiles import profiles

    records = list_environments(ctx.obj.home)
    rows = [{"name": r["name"], "revision": r["revision"], "status": r["status"],
             "path": r.get("entry") or r["activation"], "description": r["description"]} for r in records]
    emit(ctx, "env list", environments=rows, profiles=profiles(ctx.obj.home))


@app.command("metadata")
def show_metadata(ctx: typer.Context,
                  name: Optional[str] = typer.Argument(None, help="Environment name. Defaults to the selected environment.", metavar="[name]")):
    """Show one environment's metadata file."""
    from .env_entries import directory, load_metadata, read_selection

    if name is None:
        name = read_selection(ctx.obj.home)
        if name is None:
            raise ForgeError("No environment is selected.", hint="jforge env switch apt")
    record = load_metadata(ctx.obj.home, name)
    emit(ctx, "env metadata", path=str(directory(ctx.obj.home, name) / "env.json"), metadata=record)


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
        print(result.get("entry") or result["activation"] if revision is None else result["activation"])
    else:
        emit(ctx, "env show", environment=result)


@app.command("repair")
def repair_environment(ctx: typer.Context,
                       name: Optional[str] = typer.Argument(None, help="Repair this named environment or the active apt environment.", metavar="<name>"),
                       shell: Optional[Path] = typer.Option(None, "--shell", help="Repair only this activation file.", metavar="<path>"),
                       dry_run: bool = typer.Option(False, "--dry-run", help="Check and report settings without writing files."),
                       hook: bool = typer.Option(False, "--hook", help="Register the Forge hook in the Bash or Zsh startup files.")):
    """Discover and repair Forge-managed shell environments."""
    if name is not None and shell is not None:
        raise ForgeError("Choose either an environment name or --shell.")
    if hook and (name is not None or shell is not None):
        raise ForgeError("--hook applies to managed environment repair without a name or --shell.")
    if name == "apt":
        from .apt_profiles import active_home
        from . import homebrew_backend
        from .shell_repair import repair_apt

        selected = active_home(ctx.obj.home)
        report = (homebrew_backend.repair(selected, dry_run=dry_run) if homebrew_backend.configured(selected)
                  else repair_apt(ctx.obj.home, dry_run=dry_run))
    elif name is not None:
        report = repair(ctx.obj.home, name, dry_run=dry_run)
    elif shell is not None:
        from .shell_repair import repair_file

        report = repair_file(shell, dry_run=dry_run)
    else:
        from .managed_envs import repair_managed

        report = repair_managed(ctx.obj.home, dry_run=dry_run, install_hook=hook)
        if report["errors"]:
            emit(ctx, "env repair", ok=False, error={
                "type": "ShellRepairFailed", "message": "Some managed environments could not be repaired.",
                "hint": None,
            }, **report)
            if not ctx.obj.as_json:
                from .cli import _console
                from .presentation import line

                console = _console(stderr=True)
                for failure in report["errors"]:
                    line(console, f"{failure['kind']} {failure['name']} · {failure['message']}", style="red")
            raise typer.Exit(1)
    emit(ctx, "env repair", **report)


def _shell_effect(ctx, *, applies):
    """Say whether the loaded shell hook will apply this command in the current shell."""
    if applies and os.environ.get("_JFORGE_HOOK_LOADED") == "1" and not ctx.obj.as_json:
        return "pending"
    return "unchanged"


def _current_path(home):
    from .env_entries import read_selection

    name = read_selection(home)
    if name is None:
        return None
    try:
        result = show(home, name, strict=True)
    except ForgeError:
        if name == "apt":
            return None
        raise
    return result.get("entry") or result["activation"]


@app.command("current")
def current_environment(ctx: typer.Context,
                        path: bool = typer.Option(False, "--path", help="Print only the activation script path.")):
    """Show the environment selected for the shell."""
    from .env_entries import read_selection

    if path and ctx.obj.as_json:
        raise ForgeError("--path and --json are mutually exclusive.")
    name = read_selection(ctx.obj.home)
    if path:
        selected = _current_path(ctx.obj.home)
        if selected:
            print(selected)
        return
    if name is None:
        emit(ctx, "env current", selected=None, detached=True)
        return
    try:
        environment = show(ctx.obj.home, name, strict=True)
    except ForgeError:
        if name != "apt":
            raise
        emit(ctx, "env current", selected=None, detached=False)
        return
    emit(ctx, "env current", selected=name, detached=False,
         path=environment.get("entry") or environment["activation"], environment=environment)


@app.command("switch")
def switch_environment(ctx: typer.Context,
                       name: str = typer.Argument(..., help="Environment to load. Use apt for the package environment.", metavar="<name>")):
    """Save the environment new shells load. The Forge hook also loads it in this shell."""
    from .env_entries import write_selection

    record = show(ctx.obj.home, name, strict=True)
    write_selection(ctx.obj.home, name)
    emit(ctx, "env switch", selected=name, path=record.get("entry") or record["activation"],
         description=record.get("description"), shell=_shell_effect(ctx, applies=True))


@app.command("detach")
def detach_environment(ctx: typer.Context):
    """Unload the selected environment. The environment files stay in place."""
    from .env_entries import read_selection, write_selection

    previous = read_selection(ctx.obj.home)
    write_selection(ctx.obj.home, None)
    emit(ctx, "env detach", detached=True, previous=previous, shell=_shell_effect(ctx, applies=True))


@app.command("drop")
def drop_environment_build(ctx: typer.Context,
                           build_id: str = typer.Argument(..., help="Attached build to remove. The installation stays on disk.", metavar="<app>")):
    """Remove one attached build from the apt environment."""
    from .apt_profiles import active_home
    from .env_entries import drop_build, read_selection

    record = drop_build(ctx.obj.home, active_home(ctx.obj.home), build_id)
    emit(ctx, "env drop", build_id=build_id, path=record["activation"],
         description=record["description"], builds=record["builds"],
         shell=_shell_effect(ctx, applies=read_selection(ctx.obj.home) is not None))
