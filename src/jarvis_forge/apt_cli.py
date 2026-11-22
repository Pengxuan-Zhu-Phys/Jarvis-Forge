"""Native package commands share verbs and profile selection with app builds."""

from __future__ import annotations
import fnmatch
from pathlib import Path
import shlex
from typing import Optional
import typer

from . import homebrew_backend, spack_backend as backend
from .apt_profiles import active_home, backend_name, ensure_active, validate_active
from .storage import ForgeError

app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Manage native packages in a project profile.",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _confirm(ctx, yes, message):
    if not ctx.obj.as_json:
        from .cli import _console
        from .presentation import line

        line(_console(stderr=True), message.rstrip("?"))


def _changes_confirmation(ctx, yes, changes):
    if progress := getattr(ctx.obj, "apt_progress", None):
        progress.close()
    if not ctx.obj.as_json:
        from .cli import _console
        from .presentation import line

        for action in ("install", "upgrade", "rebuild", "remove"):
            if rows := changes.get(action):
                line(
                    _console(stderr=True),
                    action.capitalize()
                    + ": "
                    + ", ".join(
                        row["name"] + "@" + str(row.get("version", "")) for row in rows
                    ),
                )


def _progress(ctx):
    return ctx.obj.apt_progress


def _emit(ctx, command, **values):
    from .cli import emit

    _progress(ctx).close(success=values.get("status") == "passed")
    return emit(ctx, "apt " + command, **values)


def _ready(ctx, yes=False):
    ctx.obj.home = ensure_active(
        ctx.obj.user_home,
        progress=_progress(ctx),
        confirm=lambda plan: _changes_confirmation(ctx, yes, plan),
    )
    return ctx.obj.home


def _driver(home):
    return homebrew_backend if backend_name(home) == "homebrew" else backend


def _transaction_activation(ctx, result):
    if result.get("state_committed") and not result.get("dry_run"):
        from .unified_profiles import refresh_activation

        if entry := refresh_activation(ctx.obj.home):
            return {
                **result,
                "environment_activation": entry,
                "source_command": "source " + shlex.quote(entry),
            }
    return result


def _transact(
    ctx,
    action,
    packages=(),
    *,
    timeout=3600,
    cache="auto",
    dry_run=False,
    yes=False,
    from_history=None,
):
    from .unified_profiles import record

    dry_run = dry_run or getattr(ctx.obj, "dry_run", False)
    if dry_run:
        if not record(ctx.obj.home):
            raise ForgeError(
                "Select a profile before computing an installation plan.",
                hint="jforge profile create ubuntu24",
            )
    else:
        _ready(ctx, yes=yes)
    driver = _driver(ctx.obj.home)
    if from_history is not None and driver is homebrew_backend:
        raise ForgeError("--from lock replay requires a Spack profile.")
    options = {
        "timeout": timeout,
        "cache": cache,
        "dry_run": dry_run,
        "progress": _progress(ctx),
        "confirm": lambda plan: _changes_confirmation(ctx, yes, plan),
    }
    if driver is backend:
        options["from_history"] = from_history
    result = driver.transact(ctx.obj.home, action, packages, **options)
    _emit(
        ctx,
        "install" if action == "reinstall" else action,
        **_transaction_activation(ctx, result),
    )


@app.callback()
def apt(ctx: typer.Context):
    from .cli import _console, help_requested

    if help_requested.get():
        return
    from .apt_progress import AptProgress

    ctx.obj.user_home = ctx.obj.home
    ctx.obj.apt_progress = AptProgress(
        _console(stderr=True), verbose=ctx.obj.verbose, as_json=ctx.obj.as_json
    )
    ctx.call_on_close(ctx.obj.apt_progress.close)
    ctx.obj.home = active_home(ctx.obj.home)
    if ctx.invoked_subcommand is not None:
        validate_active(ctx.obj.home)
    if ctx.invoked_subcommand is None:
        if ctx.obj.as_json:
            _emit(ctx, "help", commands=sorted(ctx.command.commands))
        else:
            typer.echo(ctx.get_help())


@app.command("list")
def list_packages(
    ctx: typer.Context,
    pattern: str = typer.Argument("*"),
    available: bool = typer.Option(False, "--available"),
):
    """List installed packages, or the native package catalog."""
    driver = _driver(ctx.obj.home)
    if available:
        rows = driver.catalog(ctx.obj.home, pattern=pattern)
    elif driver is homebrew_backend:
        rows = (
            driver.current(ctx.obj.home)["packages"]
            if driver.configured(ctx.obj.home)
            else []
        )
    else:
        rows = ctx.obj.home.packages()
    rows = [row for row in rows if fnmatch.fnmatchcase(row["name"], pattern)]
    _emit(ctx, "list", view="available" if available else "installed", packages=rows)


@app.command("search")
def search(ctx: typer.Context, term: str = typer.Argument(...)):
    """Search native package names and descriptions."""
    driver = _driver(ctx.obj.home)
    _emit(ctx, "search", query=term, packages=driver.search(ctx.obj.home, term))


@app.command("show")
def show(
    ctx: typer.Context,
    package: str = typer.Argument(...),
    path: bool = typer.Option(False, "--path"),
):
    """Show package details; --path prints the selected installation prefix."""
    if path and ctx.obj.as_json:
        raise ForgeError("--path and --json are mutually exclusive.")
    driver = _driver(ctx.obj.home)
    name = driver.name_of(package)
    state = (
        driver.current(ctx.obj.home)
        if driver.configured(ctx.obj.home)
        else {"packages": []}
    )
    rows = [row for row in state["packages"] if row["name"] == name]
    if path:
        if len(rows) != 1 or not Path(rows[0]["prefix"]).is_dir():
            raise ForgeError(
                f"No active installed prefix for {name}.", hint="jforge apt list"
            )
        print(rows[0]["prefix"])
        return
    details = (
        driver.read_details(ctx.obj.home, package)
        if driver is backend
        else driver.details(ctx.obj.home, package)
    )
    _emit(ctx, "show", package=details, installed_instances=rows)


@app.command("install")
def install_packages(
    ctx: typer.Context,
    packages: Optional[list[str]] = typer.Argument(None),
    rebuild: list[str] = typer.Option([], "--rebuild"),
    from_history: Optional[str] = typer.Option(None, "--from"),
    timeout: float = typer.Option(3600, "--timeout", min=0.1),
    cache: str = typer.Option("auto", "--cache"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Resolve and install packages, rebuild named packages, or replay a history lock."""
    if from_history and (packages or rebuild):
        raise ForgeError("--from cannot be combined with packages or --rebuild.")
    if not from_history and not packages and not rebuild:
        raise ForgeError("Supply packages, --rebuild NAME, or --from NUMBER.")
    if rebuild and packages:
        raise ForgeError("Use --rebuild NAME separately from new package requests.")
    if rebuild:
        if _driver(ctx.obj.home) is homebrew_backend:
            _transact(
                ctx,
                "reinstall",
                rebuild,
                timeout=timeout,
                cache=cache,
                dry_run=dry_run,
                yes=yes,
            )
            return
        rows = {row["name"]: row for row in backend.current(ctx.obj.home)["packages"]}
        if missing := set(rebuild) - rows.keys():
            raise ForgeError(
                "Packages are not installed: " + ", ".join(sorted(missing))
            )
        packages = [rows[name]["spec"] for name in rebuild]
    _transact(
        ctx,
        "reinstall" if rebuild else "install",
        packages or [],
        timeout=timeout,
        cache=cache,
        dry_run=dry_run,
        yes=yes,
        from_history=from_history,
    )


@app.command("upgrade")
def upgrade(
    ctx: typer.Context,
    packages: Optional[list[str]] = typer.Argument(None),
    timeout: float = typer.Option(3600, "--timeout", min=0.1),
    cache: str = typer.Option("auto", "--cache"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Upgrade selected packages, or re-solve the whole profile."""
    _transact(
        ctx,
        "upgrade",
        packages or [],
        timeout=timeout,
        cache=cache,
        dry_run=dry_run,
        yes=yes,
    )


@app.command("remove")
def remove_packages(
    ctx: typer.Context,
    packages: list[str] = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Delete packages and their reverse dependencies; keep automatic dependencies."""
    _transact(ctx, "remove", packages, dry_run=dry_run, yes=yes)


@app.command("autoremove")
def autoremove(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Reclaim unused automatic packages and retained old versions."""
    dry_run = dry_run or getattr(ctx.obj, "dry_run", False)
    home = ctx.obj.home if dry_run else _ready(ctx)
    result = _driver(home).autoremove(
        home,
        dry_run=dry_run,
        progress=_progress(ctx),
        confirm=lambda plan: _changes_confirmation(ctx, yes, plan),
    )
    _emit(ctx, "autoremove", **_transaction_activation(ctx, result))


@app.command("update")
def update_sources(
    ctx: typer.Context, dry_run: bool = typer.Option(False, "--dry-run")
):
    """Refresh the native package recipe catalog without upgrading installed packages."""
    if dry_run or getattr(ctx.obj, "dry_run", False):
        _emit(
            ctx,
            "update",
            dry_run=True,
            backend=backend_name(ctx.obj.home),
            status="planned",
        )
        return
    home = _ready(ctx)
    driver = _driver(home)
    result = (
        driver.update(home)
        if driver is homebrew_backend
        else driver.update(home, progress=_progress(ctx))
    )
    _emit(ctx, "update", **result)


@app.command("sources")
def sources(
    ctx: typer.Context,
    add: Optional[str] = typer.Option(None, "--add"),
    url: Optional[str] = typer.Option(None, "--url"),
    remove: Optional[str] = typer.Option(None, "--remove"),
    trust_key: Optional[Path] = typer.Option(None, "--trust-key"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """List or change signed Spack mirrors and trusted keys."""
    if backend_name(ctx.obj.home) != "spack":
        raise ForgeError("Binary mirrors and signing keys belong to Spack profiles.")
    backend.validate_sources(add=add, url=url, remove=remove, trust_key=trust_key)
    if dry_run or getattr(ctx.obj, "dry_run", False):
        _emit(
            ctx,
            "sources",
            dry_run=True,
            status="planned",
            add=add,
            url=url,
            remove=remove,
            trust_key=str(trust_key) if trust_key else None,
        )
        return
    if remove:
        _confirm(ctx, yes, "Remove this binary mirror?")
    home = _ready(ctx) if any((add, remove, trust_key)) else ctx.obj.home
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


def shell_init(ctx: typer.Context):
    """Print the Bash/Zsh shell hook; installing it is an explicit user action."""
    if ctx.obj.as_json:
        raise ForgeError("shell-init cannot be combined with --json.")
    explicit = ctx.find_root().params.get("home")
    options = " " + shlex.join(["--home", explicit]) if explicit else ""
    from .shell_handoff import HOOK

    print(HOOK.replace("__JFORGE_HOME_ARGS__", options))
