"""APT-shaped commands with a single Spack package backend."""

from __future__ import annotations

import fnmatch
from pathlib import Path
import shlex
import sys
from typing import Optional

import typer

from . import spack_backend as backend
from .apt_profiles import (
    active_home,
    archived_packages,
    ensure_active,
    profiles,
    switch,
)
from .package_operations import environment_path
from .storage import ForgeError

app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Manage user-local software with Spack.\n\nCommand help: jforge apt COMMAND -h",
)


def _emit(ctx, command, **values):
    from .cli import emit

    ctx.obj.apt_progress.close(success=values.get("status") == "passed")
    return emit(ctx, "apt " + command, **values)


def _progress(ctx):
    return ctx.obj.apt_progress


def _ready(ctx):
    ctx.obj.home = ensure_active(ctx.obj.user_home, progress=_progress(ctx))
    return ctx.obj.home


def _confirm(ctx, yes, message):
    if yes:
        return
    if ctx.obj.as_json or not sys.stdin.isatty():
        raise ForgeError("Removal requires --yes in non-interactive/JSON mode.")
    if not typer.confirm(message, default=False):
        raise ForgeError("Removal cancelled.")


def _transact(
    ctx,
    action,
    packages,
    jobs=1,
    timeout=3600,
    cache="auto",
    dry_run=False,
    command=None,
):
    preview = backend.transact(
        ctx.obj.home,
        action,
        packages,
        jobs=jobs,
        timeout=timeout,
        cache=cache,
        dry_run=True,
    )
    result = (
        preview
        if dry_run
        else backend.transact(
            _ready(ctx),
            action,
            packages,
            jobs=jobs,
            timeout=timeout,
            cache=cache,
            progress=_progress(ctx),
        )
    )
    _emit(ctx, command or action, **result)


@app.callback()
def apt(ctx: typer.Context):
    if ctx.invoked_subcommand == "shell-init":
        return

    from .apt_progress import AptProgress
    from .cli import _console

    ctx.obj.apt_progress = AptProgress(_console(stderr=True), verbose=ctx.obj.verbose,
                                     as_json=ctx.obj.as_json,
                                     check=ctx.invoked_subcommand == "check")
    ctx.call_on_close(ctx.obj.apt_progress.close)
    ctx.obj.user_home = ctx.obj.home
    if ctx.invoked_subcommand not in {"switch", "import"}:
        ctx.obj.home = active_home(ctx.obj.home)
    if ctx.invoked_subcommand is None:
        if ctx.obj.as_json:
            _emit(ctx, "help", commands=sorted(ctx.command.commands))
        else:
            typer.echo(ctx.get_help())


@app.command("list")
def list_packages(
    ctx: typer.Context,
    pattern: str = typer.Argument(
        "*", help="Package name pattern.", metavar="<pattern>"
    ),
    installed_only: bool = typer.Option(
        False, "--installed", help="Show active environment packages."
    ),
    upgradable: bool = typer.Option(
        False, "--upgradable", help="Explain dependency-based upgrade planning."
    ),
    archived: bool = typer.Option(
        False, "--archived", help="Read old source installation records."
    ),
):
    """List cached Spack packages or archived source installation records."""
    if upgradable:
        raise ForgeError(
            "Upgrades require solving the Spack environment.",
            hint="jforge apt check; jforge apt upgrade",
        )
    rows = (
        archived_packages(ctx.obj.user_home)
        if archived
        else (
            backend.current(ctx.obj.home)["packages"]
            if installed_only
            else backend.catalog(ctx.obj.home, pattern=pattern)
        )
    )
    rows = [r for r in rows if fnmatch.fnmatchcase(r["name"], pattern)]
    if not ctx.obj.as_json and len(rows) > 1000:
        sys.stdout.write("".join(r["name"] + "\n" for r in rows))
    else:
        _emit(ctx, "list", backend="spack", packages=rows)


@app.command()
def search(
    ctx: typer.Context,
    query: str = typer.Argument(
        ..., help="Match cached Spack package names.", metavar="<query>"
    ),
):
    """Search cached package names without downloading."""
    _emit(
        ctx,
        "search",
        backend="spack",
        packages=backend.catalog(ctx.obj.home, query=query),
    )


@app.command()
def show(
    ctx: typer.Context,
    package: str = typer.Argument(
        ..., help="Package name or quoted Spack spec.", metavar="<package>"
    ),
):
    """Show Spack recipe metadata and recorded installation locations."""
    _emit(
        ctx,
        "show",
        package=backend.details(ctx.obj.home, package),
        installed_instances=[
            r
            for r in backend.current(ctx.obj.home)["packages"]
            if r["name"] == backend.name_of(package)
        ],
    )


@app.command("install")
def install_packages(
    ctx: typer.Context,
    packages: list[str] = typer.Argument(
        ..., help="Package names or quoted Spack specs.", metavar="<packages>"
    ),
    jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
    timeout: float = typer.Option(
        3600, "--timeout", min=0.1, help="Timeout per Spack command, in seconds."
    ),
    cache: str = typer.Option(
        "auto", "--cache", help="Binary cache policy: auto, only or never."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Preview requests without downloads or solving."
    ),
):
    """Install requested packages and their Spack dependencies."""
    _transact(ctx, "install", packages, jobs, timeout, cache, dry_run)


@app.command()
def reinstall(
    ctx: typer.Context,
    packages: list[str] = typer.Argument(
        ..., help="Packages to overwrite through Spack.", metavar="<packages>"
    ),
    jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
    timeout: float = typer.Option(
        3600, "--timeout", min=0.1, help="Timeout per Spack command, in seconds."
    ),
    cache: str = typer.Option(
        "auto", "--cache", help="Binary cache policy: auto, only or never."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Preview requests without installing."
    ),
):
    """Reinstall selected specs using Spack overwrite semantics."""
    _transact(ctx, "reinstall", packages, jobs, timeout, cache, dry_run)


@app.command()
def upgrade(
    ctx: typer.Context,
    packages: Optional[list[str]] = typer.Argument(
        None,
        help="Omit to re-solve all roots; change a root with install SPEC.",
        metavar="<packages>",
    ),
    jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
    timeout: float = typer.Option(
        3600, "--timeout", min=0.1, help="Timeout per Spack command, in seconds."
    ),
    cache: str = typer.Option(
        "auto", "--cache", help="Binary cache policy: auto, only or never."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Preview requests without solving."
    ),
):
    """Re-solve and upgrade the complete Spack environment."""
    _transact(ctx, "upgrade", packages or [], jobs, timeout, cache, dry_run)


@app.command("full-upgrade")
def full_upgrade(
    ctx: typer.Context,
    jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
    timeout: float = typer.Option(
        3600, "--timeout", min=0.1, help="Timeout per Spack command, in seconds."
    ),
    cache: str = typer.Option(
        "auto", "--cache", help="Binary cache policy: auto, only or never."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Preview requests without solving."
    ),
):
    """Re-solve and upgrade all explicit roots and dependencies."""
    _transact(ctx, "full-upgrade", [], jobs, timeout, cache, dry_run)


@app.command()
def satisfy(
    ctx: typer.Context,
    constraints: list[str] = typer.Argument(
        ..., help="Version constraints or quoted Spack specs.", metavar="<constraints>"
    ),
    jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
    timeout: float = typer.Option(
        3600, "--timeout", min=0.1, help="Timeout per Spack command, in seconds."
    ),
    cache: str = typer.Option(
        "auto", "--cache", help="Binary cache policy: auto, only or never."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Preview requests without solving."
    ),
):
    """Install specs satisfying explicit Spack constraints."""
    _transact(
        ctx, "install", constraints, jobs, timeout, cache, dry_run, command="satisfy"
    )


@app.command("remove")
def remove_packages(
    ctx: typer.Context,
    packages: list[str] = typer.Argument(
        ..., help="Explicit roots to remove from the environment.", metavar="<packages>"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm removal."),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Preview root removal."
    ),
):
    """Drop explicit roots while retaining stored packages and history."""
    if not dry_run:
        _confirm(
            ctx,
            yes,
            "Remove roots from the Spack environment? Saved revisions are retained.",
        )
    _transact(ctx, "remove", packages, dry_run=dry_run)


@app.command()
def autoremove(
    ctx: typer.Context,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm Spack garbage collection."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Describe conservative garbage collection."
    ),
):
    """Collect unused Spack packages, preserving saved environment revisions."""
    if not dry_run:
        _confirm(
            ctx, yes, "Collect unused Spack packages? Saved environments are retained."
        )
    _emit(
        ctx,
        "autoremove",
        **backend.autoremove(
            ctx.obj.home if dry_run else _ready(ctx),
            dry_run=dry_run,
            progress=_progress(ctx),
        ),
    )


@app.command("update")
def update_sources(ctx: typer.Context):
    """Refresh the engine-compatible Spack recipes and package-name cache."""
    _emit(ctx, "update", **backend.update(_ready(ctx), progress=_progress(ctx)))


@app.command("switch")
def switch_source(
    ctx: typer.Context,
    source: Optional[str] = typer.Argument(
        None,
        help="Use spack to select a package profile; omit to list profiles.",
        metavar="<spack>",
    ),
    name: str = typer.Option(
        "default", "--name", help="Spack profile name.", metavar="<name>"
    ),
    direct: bool = typer.Option(
        False,
        "--direct",
        help="Select an existing Spack environment stored at the Forge root.",
    ),
    archived: bool = typer.Option(
        False, "--archived", help="Include read-only old source directories."
    ),
):
    """Select a Spack package profile or list saved profiles."""
    if source is None:
        if direct or name != "default":
            raise ForgeError("Select spack before setting --name or --direct.")
        _emit(
            ctx, "switch", environments=profiles(ctx.obj.user_home, archived=archived)
        )
    else:
        if archived:
            raise ForgeError(
                "--archived lists saved directories and cannot activate them."
            )
        _emit(
            ctx,
            "switch",
            **switch(
                ctx.obj.user_home,
                source,
                name=name,
                direct=direct,
                progress=_progress(ctx),
            ),
        )


@app.command("import")
def import_spack(
    ctx: typer.Context,
    source: str = typer.Argument(
        "spack", help="Import the Spack backend.", metavar="<spack>"
    ),
):
    """Prepare and select the default Spack profile."""
    _emit(ctx, "import", **switch(ctx.obj.user_home, source, progress=_progress(ctx)))


@app.command("sources")
def sources(
    ctx: typer.Context,
    add: Optional[str] = typer.Option(
        None, "--add", help="Name of a binary mirror to add.", metavar="<name>"
    ),
    url: Optional[str] = typer.Option(
        None, "--url", help="HTTPS, file or OCI mirror URL.", metavar="<url>"
    ),
    remove: Optional[str] = typer.Option(
        None, "--remove", help="Binary mirror to remove.", metavar="<name>"
    ),
    trust_key: Optional[Path] = typer.Option(
        None, "--trust-key", help="User-trusted signing key file.", metavar="<path>"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm mirror removal."),
):
    """List or manage Spack binary mirrors and trusted signing keys."""
    backend.validate_sources(add=add, url=url, remove=remove, trust_key=trust_key)
    if remove:
        _confirm(ctx, yes, "Remove this Spack mirror?")
    home = _ready(ctx) if any([add, remove, trust_key]) else ctx.obj.home
    _emit(
        ctx,
        "sources",
        **backend.sources(
            home,
            add=add,
            url=url,
            remove=remove,
            trust_key=trust_key,
            progress=_progress(ctx),
        ),
    )


@app.command("edit-sources")
def edit_sources(ctx: typer.Context):
    """Show Spack mirrors; use apt sources options to edit them."""
    _emit(ctx, "edit-sources", **backend.sources(ctx.obj.home))


@app.command("env")
def env(
    ctx: typer.Context,
    path: bool = typer.Option(
        False, "--path", help="Print only the activation script path."
    ),
):
    """Locate the current Spack environment activation script."""
    if path and ctx.obj.as_json:
        raise ForgeError("--path and --json are mutually exclusive.")
    activation = environment_path(ctx.obj.home)
    if path:
        print(activation)
    else:
        _emit(
            ctx,
            "env",
            activation=activation,
            source_command="source " + shlex.quote(activation),
        )


@app.command("check")
def check_spack(
    ctx: typer.Context,
    packages: Optional[list[str]] = typer.Argument(
        None,
        help="Specs to concretize; defaults to active roots.",
        metavar="<packages>",
    ),
    timeout: float = typer.Option(
        3600, "--timeout", min=0.1, help="Timeout per solver command, in seconds."
    ),
):
    """Save a concrete dependency plan without building packages."""
    _transact(ctx, "check", packages or [], timeout=timeout)


@app.command("compilers")
def spack_compilers(
    ctx: typer.Context,
    find: bool = typer.Option(
        False, "--find", help="Discover compilers from the current PATH."
    ),
):
    """Report or discover Spack compilers."""
    _emit(
        ctx,
        "compilers",
        **backend.compilers(_ready(ctx), find=find, progress=_progress(ctx)),
    )


@app.command("shell-init")
def shell_init(ctx: typer.Context):
    """Print a Bash/Zsh hook for automatic activation after package changes."""
    if ctx.obj.as_json:
        raise ForgeError("shell-init cannot be used with --json.")
    explicit_home = ctx.find_root().params.get("home")
    home_args = " " + shlex.join(["--home", explicit_home]) if explicit_home is not None else ""
    print(r"""# Add eval "$(command jforge apt shell-init)" to your shell startup file.
jforge() {
    local _jf_arg _jf_next=0 _jf_home='' _jf_apt=0 _jf_change=0 _jf_path _jf_command='' _jf_action='' _jf_skip=0 _jf_target=0
    for _jf_arg in "$@"; do
        if [ "$_jf_next" = 1 ]; then _jf_home=$_jf_arg; _jf_next=0; continue; fi
        case "$_jf_arg" in
            --) break ;;
            --home) _jf_next=1 ;;
            --home=*) _jf_home=${_jf_arg#--home=} ;;
            --help|-h|--dry-run|-s|--version|-v|-V|--json) _jf_skip=1 ;;
            --debug|-d) ;;
            --*) ;;

            *)
                if [ -z "$_jf_command" ]; then _jf_command=$_jf_arg
                elif [ -z "$_jf_action" ]; then _jf_action=$_jf_arg
                else _jf_target=1; fi ;;

        esac
    done
    if [ "$_jf_command" = apt ]; then
        case "$_jf_action" in install|reinstall|upgrade|full-upgrade|satisfy|switch|import|remove|autoremove) _jf_change=1 ;; esac
    fi
    if [ "$_jf_skip" = 1 ] || { [ "$_jf_action" = switch ] && [ "$_jf_target" = 0 ]; }; then _jf_change=0; fi
    if command jforge "$@"; then
        if [ "$_jf_change" = 1 ]; then
            if [ -n "$_jf_home" ]; then
                _jf_path=$(command jforge --home "$_jf_home" apt env --path) || _jf_path=''
            else
                _jf_path=$(command jforge apt env --path) || _jf_path=''
            fi
            if [ -z "$_jf_path" ] || ! source "$_jf_path" >&2; then
                printf '%s\n' 'jforge: Package command succeeded. Could not refresh the shell environment.' >&2
            fi
        fi
    else
        return $?
    fi
}
_jforge_apt_initial=$(command jforge__JFORGE_HOME_ARGS__ apt env --path 2>/dev/null) || _jforge_apt_initial=''
if [ -n "$_jforge_apt_initial" ] && ! source "$_jforge_apt_initial" >&2; then
    printf '%s\n' 'jforge: Could not load the selected package environment.' >&2
fi
unset _jforge_apt_initial
""".replace("__JFORGE_HOME_ARGS__", home_args))
