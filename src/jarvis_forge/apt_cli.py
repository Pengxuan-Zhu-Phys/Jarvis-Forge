"""Package commands for the selected Spack or Homebrew profile."""

from __future__ import annotations

import fnmatch
from pathlib import Path
import shlex
import sys
from typing import Optional

import typer

from . import homebrew_backend, spack_backend as backend
from .apt_profiles import (
    active_home,
    archived_packages,
    backend_name,
    create_homebrew,
    ensure_active,
    initialize_profile,
    profiles,
    switch,
    use,
)
from .package_operations import environment_path
from .storage import ForgeError

app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Manage native packages with Spack or Homebrew.\n\nCommand help: jforge apt COMMAND -h",
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


def _spack_only(ctx):
    if backend_name(ctx.obj.home) == "homebrew":
        raise ForgeError(f"apt {ctx.info_name} is only available for Spack profiles.",
                         hint="jforge apt profile")


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
    if backend_name(ctx.obj.home) == "homebrew":
        if command == "satisfy":
            _spack_only(ctx)
        if action == "install" and not dry_run and not homebrew_backend.configured(ctx.obj.home):
            _ready(ctx)
        _emit(ctx, command or action, **homebrew_backend.transact(
            ctx.obj.home, action, packages, jobs=jobs, timeout=timeout, cache=cache,
            dry_run=dry_run, progress=_progress(ctx)))
        return
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
    if ctx.invoked_subcommand not in {"switch", "import", "profile"}:
        ctx.obj.home = active_home(ctx.obj.home)
    if ctx.invoked_subcommand is None:
        if ctx.obj.as_json:
            _emit(ctx, "help", commands=sorted(name for name, command in ctx.command.commands.items()
                                               if not command.hidden))
        else:
            typer.echo(ctx.get_help())


@app.command("list")
def list_packages(
    ctx: typer.Context,
    pattern: str = typer.Argument(
        "*", help="Package name pattern.", metavar="<pattern>"
    ),
    installed_only: bool = typer.Option(
        False, "--installed", help="Show installed packages, the default.", rich_help_panel="Legacy options"
    ),
    available: bool = typer.Option(
        False, "--available", help="Show the selected backend's package catalog."
    ),
    upgradable: bool = typer.Option(
        False, "--upgradable", help="Explain dependency-based upgrade planning."
    ),
    archived: bool = typer.Option(
        False, "--archived", help="Read old source installation records."
    ),
):
    """List packages installed in the selected profile."""
    if upgradable:
        _spack_only(ctx)
        raise ForgeError(
            "Upgrades require solving the Spack environment.",
            hint="jforge apt check; jforge apt upgrade",
        )
    if available and (installed_only or archived):
        raise ForgeError("--available cannot be combined with --installed or --archived.")
    driver = homebrew_backend if backend_name(ctx.obj.home) == "homebrew" else backend
    if archived:
        rows = archived_packages(ctx.obj.user_home)
        view = "archived"
    elif available:
        rows = driver.catalog(ctx.obj.home, pattern=pattern)
        view = "available"
    else:
        if driver is homebrew_backend:
            rows = homebrew_backend.inventory(ctx.obj.home) if homebrew_backend.configured(ctx.obj.home) else []
        else:
            rows = backend.current(ctx.obj.home)["packages"]
        view = "installed"
    rows = [r for r in rows if fnmatch.fnmatchcase(r["name"], pattern)]
    if available and not ctx.obj.as_json and len(rows) > 1000:
        sys.stdout.write("".join(r["name"] + "\n" for r in rows))
    else:
        _emit(ctx, "list", backend="homebrew" if driver is homebrew_backend else "spack", view=view, packages=rows)


@app.command()
def search(
    ctx: typer.Context,
    query: str = typer.Argument(
        ..., help="Match package names in the selected backend.", metavar="<query>"
    ),
):
    """Search the selected backend's package catalog."""
    driver = homebrew_backend if backend_name(ctx.obj.home) == "homebrew" else backend
    _emit(
        ctx,
        "search",
        backend="homebrew" if driver is homebrew_backend else "spack",
        packages=driver.catalog(ctx.obj.home, query=query),
    )


@app.command()
def show(
    ctx: typer.Context,
    package: str = typer.Argument(
        ..., help="Package name or quoted Spack spec.", metavar="<package>"
    ),
):
    """Show package metadata and installed instances."""
    if backend_name(ctx.obj.home) == "homebrew":
        formula = homebrew_backend.details(ctx.obj.home, package)
        _emit(ctx, "show", backend="homebrew", package=formula,
              installed_instances=[row for row in homebrew_backend.inventory(ctx.obj.home)
                                   if row["name"] == formula["name"]])
        return
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
        3600, "--timeout", min=0.1, help="Installation command timeout, in seconds."
    ),
    cache: str = typer.Option(
        "auto", "--cache", help="Binary cache policy: auto, only or never."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Preview requests without installing packages."
    ),
):
    """Install or select packages and their dependencies."""
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
            "Remove roots from the selected profile? Saved revisions are retained.",
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
    _spack_only(ctx)
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
    """Refresh the active package catalog and this system's build recipes."""
    from . import build_catalog

    initialize_profile(ctx.obj.user_home)
    target = _ready(ctx)
    driver = backend_name(target)
    result = (homebrew_backend.update(target) if driver == "homebrew"
              else backend.update(target, progress=_progress(ctx)))
    _progress(ctx)("Refresh build recipes for this system")
    try:
        catalog = build_catalog.update(ctx.obj.user_home)
    except (ForgeError, OSError) as exc:
        detail = getattr(exc, "hint", None)
        raise ForgeError(f"{driver.capitalize()} metadata was refreshed. Build recipe refresh failed.\n{exc}",
                         code=1, hint=detail or "Retry the build catalog with jforge build update.") from exc
    _emit(ctx, "update", **{**result, "backend": driver, "build_catalog": catalog})


@app.command("switch", hidden=True)
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
    """Legacy command. See jforge apt profile."""
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


@app.command("profile")
def manage_profile(
    ctx: typer.Context,
    name: Optional[str] = typer.Argument(None, metavar="<name>", help="Package profile name or ID. Omit to list."),
    create: bool = typer.Option(False, "--create", help="Create and select the named profile."),
    archived: bool = typer.Option(False, "--archived", help="Include archived source profiles in the list."),
    profile_backend: Optional[str] = typer.Option(None, "--backend", metavar="<spack|homebrew>",
                                                   help="Backend for a new profile. Defaults to spack."),
):
    """List profiles, or select the named package profile."""
    home = ctx.obj.user_home
    if profile_backend is not None and not create:
        raise ForgeError("--backend requires --create.")
    if profile_backend not in {None, "spack", "homebrew"}:
        raise ForgeError("Choose spack or homebrew for --backend.")
    if name is None:
        if create:
            raise ForgeError("--create requires a profile name.")
        _emit(ctx, "profile", environments=profiles(home, archived=archived))
        return
    if archived:
        raise ForgeError("--archived applies when listing profiles without a name.")
    if not create:
        _emit(ctx, "profile", **use(home, name))
        return
    if profile_backend == "homebrew":
        _emit(ctx, "profile", **create_homebrew(home, name))
        return
    if name == "direct":
        raise ForgeError("The direct profile is reserved for an existing root Spack installation.",
                         hint="jforge apt profile direct")
    _emit(ctx, "profile", **switch(home, name=name.removeprefix("spack-"),
                                  create_only=True, progress=_progress(ctx)))


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
    _spack_only(ctx)
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
    _spack_only(ctx)
    _emit(ctx, "edit-sources", **backend.sources(ctx.obj.home))


@app.command("env")
def env(
    ctx: typer.Context,
    path: bool = typer.Option(
        False, "--path", help="Print only the activation script path."
    ),
):
    """Locate the current package profile activation script."""
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
    _spack_only(ctx)
    _emit(
        ctx,
        "compilers",
        **backend.compilers(_ready(ctx), find=find, progress=_progress(ctx)),
    )


@app.command("shell-init")
def shell_init(ctx: typer.Context):
    """Print a Bash/Zsh hook for automatic activation after installs and builds."""
    if ctx.obj.as_json:
        raise ForgeError("shell-init cannot be used with --json.")
    explicit_home = ctx.find_root().params.get("home")
    home_args = " " + shlex.join(["--home", explicit_home]) if explicit_home is not None else ""
    print(r"""# Add eval "$(command jforge apt shell-init)" to your shell startup file.
jforge() {
    local _jf_arg _jf_next=0 _jf_home='' _jf_change=0 _jf_path _jf_command='' _jf_action='' _jf_skip=0 _jf_target=0
    local _jf_no_activate=0 _jf_handoff _jf_status _jf_hook
    for _jf_arg in "$@"; do
        if [ "$_jf_next" = 1 ]; then _jf_home=$_jf_arg; _jf_next=0; continue; fi
        case "$_jf_arg" in
            --) break ;;
            --home) _jf_next=1 ;;
            --home=*) _jf_home=${_jf_arg#--home=} ;;
            --help|-h|--dry-run|-s|--version|-v|-V|--json) _jf_skip=1 ;;
            --no-activate) _jf_no_activate=1 ;;
            --shell|--shell=*) _jf_target=1 ;;
            --debug|-d) ;;
            --*) ;;

            *)
                if [ -z "$_jf_command" ]; then _jf_command=$_jf_arg
                elif [ -z "$_jf_action" ]; then _jf_action=$_jf_arg
                else _jf_target=1; fi ;;

        esac
    done
    if [ "$_jf_command" = apt ]; then
        case "$_jf_action" in install|reinstall|upgrade|full-upgrade|satisfy|switch|profile|import|remove|autoremove|update) _jf_change=1 ;; esac
    fi
    if [ "$_jf_skip" = 1 ]; then _jf_change=0; fi
    case "$_jf_action:$_jf_target" in switch:0|profile:0) _jf_change=0 ;; esac
    case "$-:$_jf_command:$_jf_action:$_jf_skip:$_jf_no_activate" in
        *i*:build:run:0:0|*i*:build:resume:0:0)
            if _jf_handoff=$(mktemp "${TMPDIR:-/tmp}/jforge-build-activate.XXXXXXXX"); then
                if _JFORGE_BUILD_ACTIVATION=1 command jforge "$@" 9>"$_jf_handoff"; then
                    _jf_path=''
                    IFS= read -r -d '' _jf_path < "$_jf_handoff" || _jf_path=''
                    command rm -- "$_jf_handoff" 2>/dev/null || :
                    if [ -n "$_jf_path" ]; then
                        if source "$_jf_path" >&2; then
                            printf '%s\n' 'jforge: Build environment activated.' >&2
                        else
                            printf 'jforge: Build passed. Could not activate %s\n' "$_jf_path" >&2
                        fi
                    fi
                    return 0
                else
                    _jf_status=$?
                    command rm -- "$_jf_handoff" 2>/dev/null || :
                    return "$_jf_status"
                fi
            fi
            ;;
    esac
    if command jforge "$@"; then
        case "$-:$_jf_command:$_jf_action:$_jf_skip:$_jf_target" in
            *i*:env:repair:0:0)
                if [ -n "$_jf_home" ]; then
                    _jf_hook=$(command jforge --home "$_jf_home" apt shell-init) || _jf_hook=''
                else
                    _jf_hook=$(command jforge apt shell-init) || _jf_hook=''
                fi
                if [ -n "$_jf_hook" ] && ! eval "$_jf_hook"; then
                    printf '%s\n' 'jforge: Could not refresh the shell hook.' >&2
                fi
                ;;
        esac
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
_JFORGE_HOOK_LOADED=1
""".replace("__JFORGE_HOME_ARGS__", home_args))
