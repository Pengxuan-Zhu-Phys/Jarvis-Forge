"""APT-shaped commands backed by Forge recipes, never the system apt binary."""

from __future__ import annotations

import fnmatch
from pathlib import Path
import shlex
from typing import Optional

from packaging.version import Version
import typer

from .catalog import Catalog, requirement, source_config
from .packages import (edit_sources, environment_path, install, installed, plan_install,
                       plan_remove, remove, update)
from .storage import ForgeError


app = typer.Typer(invoke_without_command=True, no_args_is_help=False,
                  help="Manage source-built packages in your Forge home without sudo.\n\n"
                       "Command help: jforge apt COMMAND -h")


def _emit(ctx, command, **values):
    from .cli import emit
    return emit(ctx, "apt " + command, **values)


@app.callback()
def apt(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        if ctx.obj.as_json:
            _emit(ctx, "help", commands=sorted(ctx.command.commands))
        else:
            typer.echo(ctx.get_help())


@app.command("list")
def list_packages(ctx: typer.Context,
                  pattern: str = typer.Argument("*", help="Package name pattern.", metavar="<pattern>"),
                  installed_only: bool = typer.Option(False, "--installed", help="Show recorded installations only."),
                  upgradable: bool = typer.Option(False, "--upgradable", help="Show active packages with newer recipes.")):
    """List available or installed user-local packages."""
    records = installed(ctx.obj.home)
    catalog = Catalog(ctx.obj.home)
    if installed_only:
        packages = records
    else:
        packages = []
        for recipe in catalog.list():
            matches = [r for r in records if r["name"] == recipe["name"] and r["version"] == recipe["version"]]
            active = next((r for r in matches if r["active"]), None)
            packages.append({"name": recipe["name"], "version": recipe["version"],
                             "status": "installed" if matches else "available", "active": bool(active),
                             "prefix": active["prefix"] if active else "", "description": recipe["description"]})
    if upgradable:
        packages = [r for r in records if r["active"] and any(
            p["name"] == r["name"] and (Version(p["version"]) > Version(r["version"]) or
             (Version(p["version"]) == Version(r["version"]) and p["source"] != r.get("source")))
            for p in catalog.list())]
    elif not installed_only:
        from .official import cached_packages
        known = {p["name"] for p in packages}
        packages.extend(p for p in cached_packages(ctx.obj.home) if p["name"] not in known
                        and fnmatch.fnmatchcase(p["name"], pattern))
    _emit(ctx, "list", packages=[p for p in packages if fnmatch.fnmatchcase(p["name"], pattern)])


@app.command()
def search(ctx: typer.Context,
           query: str = typer.Argument(..., help="Match package names and descriptions.", metavar="<query>")):
    """Search recipes and the imported official index without downloading."""
    from .official import cached_packages
    packages = [{"name": p["name"], "version": p["version"], "description": p["description"]}
                for p in Catalog(ctx.obj.home).list(query)]
    known = {p["name"] for p in packages}
    packages.extend(p for p in cached_packages(ctx.obj.home, query) if p["name"] not in known)
    _emit(ctx, "search", packages=packages)


@app.command()
def show(ctx: typer.Context,
         package: str = typer.Argument(..., help="Package or package=version.", metavar="<package>")):
    """Show recipe details, dependencies and installed locations."""
    req = requirement(package)
    choices = [p for p in Catalog(ctx.obj.home).list() if p["name"] == req.name and Version(p["version"]) in req.specifier]
    if not choices:
        from .official import cached_packages, _upstream_version
        choices = cached_packages(ctx.obj.home, req.name, exact=True, details=True)
        if req.specifier:
            choices = [p for p in choices if Version(_upstream_version(p["version"])) in req.specifier]
        if not choices:
            raise ForgeError(f"No recipe or imported official metadata matches {package}.", hint="jforge apt update")
        _emit(ctx, "show", package=choices[0], installed_instances=[r for r in installed(ctx.obj.home) if r["name"] == req.name])
        return
    recipe = max(choices, key=lambda p: Version(p["version"]))
    _emit(ctx, "show", package=recipe, installed_instances=[r for r in installed(ctx.obj.home) if r["name"] == req.name])


def _install(ctx, packages, jobs, timeout, dry_run, mode, official=True):
    from .cli import _console
    if dry_run:
        from .official import missing_packages, default_source
        missing = missing_packages(ctx.obj.home, packages or []) if official and mode in {"install", "reinstall"} else []
        if missing:
            _emit(ctx, mode, dry_run=True, plan={"requires_official_lookup": True,
                  "official_source": default_source(ctx.obj.home), "requested_packages": packages,
                  "packages": [{"name": name, "action": "official-lookup"} for name in missing],
                  "note": "Source archives, adapter and build requirements are resolved during install; no files downloaded."})
            return
        result = plan_install(ctx.obj.home, packages or [], jobs=jobs, mode=mode)
        _emit(ctx, mode, dry_run=True, plan=result)
    else:
        result = install(ctx.obj.home, packages or [], jobs=jobs, timeout=timeout, mode=mode,
                         progress=lambda message: _console(stderr=True).print(message, markup=False), official=official)
        _emit(ctx, mode, **result)


@app.command("install")
def install_packages(ctx: typer.Context,
                     packages: list[str] = typer.Argument(..., help="Package names or name=version specs.", metavar="<packages>"),
                     jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
                     timeout: float = typer.Option(3600, "--timeout", help="Timeout per build/check command, in seconds."),
                     dry_run: bool = typer.Option(False, "--dry-run", "-s", help="Show the plan without writing or downloading."),
                     no_official: bool = typer.Option(False, "--no-official", help="Use configured recipes only; skip official fallback.")):
    """Download, verify, build and install into private prefixes."""
    _install(ctx, packages, jobs, timeout, dry_run, "install", official=not no_official)


@app.command()
def reinstall(ctx: typer.Context,
              packages: list[str] = typer.Argument(..., help="Packages to rebuild into new instances.", metavar="<packages>"),
              jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
              timeout: float = typer.Option(3600, "--timeout", help="Timeout per build/check command, in seconds."),
              dry_run: bool = typer.Option(False, "--dry-run", "-s", help="Show the reinstall plan only.")):
    """Rebuild requested packages while keeping working instances."""
    _install(ctx, packages, jobs, timeout, dry_run, "reinstall")


@app.command()
def upgrade(ctx: typer.Context,
            packages: Optional[list[str]] = typer.Argument(None, help="Upgrade these packages, or all manual roots.", metavar="<packages>"),
            jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
            timeout: float = typer.Option(3600, "--timeout", help="Timeout per build/check command, in seconds."),
            dry_run: bool = typer.Option(False, "--dry-run", "-s", help="Show the upgrade plan only.")):
    """Select newer compatible recipes and retain previous instances."""
    _install(ctx, packages, jobs, timeout, dry_run, "upgrade")


@app.command("full-upgrade")
def full_upgrade(ctx: typer.Context,
                 jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
                 timeout: float = typer.Option(3600, "--timeout", help="Timeout per build/check command, in seconds."),
                 dry_run: bool = typer.Option(False, "--dry-run", "-s", help="Show the full upgrade plan only.")):
    """Upgrade all roots and dependencies; old instances are retained."""
    _install(ctx, [], jobs, timeout, dry_run, "full-upgrade")


@app.command()
def satisfy(ctx: typer.Context,
            constraints: list[str] = typer.Argument(..., help="Version constraints, such as 'eigen>=3.4'.", metavar="<constraints>"),
            jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
            timeout: float = typer.Option(3600, "--timeout", help="Timeout per build/check command, in seconds."),
            dry_run: bool = typer.Option(False, "--dry-run", "-s", help="Resolve constraints without installing.")):
    """Install recipes satisfying explicit version constraints."""
    if dry_run:
        _emit(ctx, "satisfy", dry_run=True, plan=plan_install(ctx.obj.home, constraints, jobs=jobs))
    else:
        from .cli import _console
        _emit(ctx, "satisfy", **install(ctx.obj.home, constraints, jobs=jobs, timeout=timeout,
                                      progress=lambda m: _console(stderr=True).print(m, markup=False)))


def _remove(ctx, packages, automatic, yes, dry_run):
    command = "autoremove" if automatic else "remove"
    plan = plan_remove(ctx.obj.home, packages, automatic=automatic)
    if dry_run:
        _emit(ctx, command, dry_run=True, **plan)
        return
    if plan["packages"] and not yes:
        import sys
        if ctx.obj.as_json or not sys.stdin.isatty():
            raise ForgeError("Removal requires --yes in non-interactive/JSON mode.",
                             hint=f"jforge apt {command} {' '.join(packages)} --yes")
        names = ", ".join(f"{p['name']}={p['version']} ({p['install_id']})" for p in plan["packages"])
        if not typer.confirm(f"Remove {names}?", default=False):
            raise ForgeError("Removal cancelled.")
    _emit(ctx, command, **remove(ctx.obj.home, packages, automatic=automatic, expected_keys=plan["keys"]))


@app.command("remove")
def remove_packages(ctx: typer.Context,
                    packages: list[str] = typer.Argument(..., help="Package names or name=version specs.", metavar="<packages>"),
                    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm removal of the selected installations."),
                    dry_run: bool = typer.Option(False, "--dry-run", "-s", help="Show what would be removed.")):
    """Remove Forge-owned installations after dependency checks."""
    _remove(ctx, packages, False, yes, dry_run)


@app.command()
def autoremove(ctx: typer.Context,
               yes: bool = typer.Option(False, "--yes", "-y", help="Confirm removal of unused automatic dependencies."),
               dry_run: bool = typer.Option(False, "--dry-run", "-s", help="Show unused automatic dependencies.")):
    """Remove automatic packages unreachable from retained manual installs."""
    _remove(ctx, [], True, yes, dry_run)


@app.command("update")
def update_sources(ctx: typer.Context,
                   recipes_only: bool = typer.Option(False, "--recipes-only", help="Refresh recipes without official repository metadata.")):
    """Refresh recipes and the selected official source index without upgrading."""
    _emit(ctx, "update", **update(ctx.obj.home, official=not recipes_only))


@app.command("import")
def import_official(ctx: typer.Context,
                    package: Optional[str] = typer.Argument(None, help="Optional package; omit to import the whole source index.", metavar="<package>"),
                    suite: str = typer.Option(..., "--suite", help="Distribution codename, such as bookworm or jammy.", metavar="<str>"),
                    origin: str = typer.Option("debian", "--origin", help="Official distribution: debian or ubuntu.", metavar="<debian|ubuntu>"),
                    component: str = typer.Option("main", "--component", help="Repository component.", metavar="<str>"),
                    name: Optional[str] = typer.Option(None, "--name", help="Forge package name.", metavar="<str>"),
                    template: Optional[str] = typer.Option(None, "--template", help="Reuse a reviewed Forge recipe's build adapter.", metavar="<str>"),
                    source_version: Optional[str] = typer.Option(None, "--source-version", help="Pin an exact Debian source version.", metavar="<str>"),
                    keyring: Optional[Path] = typer.Option(None, "--keyring", help="Require gpgv verification using this trusted archive keyring.", metavar="<path>")):
    """Import an official repository index, or track one source package."""
    from .official import import_package, import_repository
    if package is None:
        if name is not None or template is not None or source_version is not None:
            raise ForgeError("A package is required for --name, --template or --source-version.")
        _emit(ctx, "import", **import_repository(ctx.obj.home, origin=origin, suite=suite,
                                               component=component, keyring=keyring))
        return
    result = import_package(ctx.obj.home, package, origin=origin, suite=suite, component=component,
                            name=name, template_name=template, source_version=source_version, keyring=keyring)
    _emit(ctx, "import", **result)


@app.command("edit-sources")
def configure_sources(ctx: typer.Context,
                      add: Optional[str] = typer.Option(None, "--add", help="Add a local directory/file or HTTPS YAML index.", metavar="<source>"),
                      remove_source: Optional[str] = typer.Option(None, "--remove", help="Remove a configured source.", metavar="<source>"),
                      yes: bool = typer.Option(False, "--yes", "-y", help="Confirm removal of a configured source.")):
    """Show or edit Forge recipe sources without opening an editor."""
    if add or remove_source:
        if remove_source and not yes:
            import sys
            if ctx.obj.as_json or not sys.stdin.isatty():
                raise ForgeError("Removing a source requires --yes in non-interactive/JSON mode.")
            if not typer.confirm(f"Remove recipe source {remove_source}?", default=False):
                raise ForgeError("Source removal cancelled.")
        _emit(ctx, "edit-sources", **edit_sources(ctx.obj.home, add=add, remove_source=remove_source))
    else:
        from .official import default_source
        _emit(ctx, "edit-sources", path=str(ctx.obj.home.root / "apt/sources.json"),
              official_path=str(ctx.obj.home.root / "apt/official.json"), official_source=default_source(ctx.obj.home),
              **source_config(ctx.obj.home))


@app.command("env")
def env(ctx: typer.Context,
        path: bool = typer.Option(False, "--path", help="Print only the current activation script path.")):
    """Locate the versioned activation script for active packages."""
    if path and ctx.obj.as_json:
        raise ForgeError("--path and --json are mutually exclusive.")
    activation = environment_path(ctx.obj.home)
    if path:
        print(activation)
    else:
        _emit(ctx, "env", activation=activation, source_command="source " + shlex.quote(activation))
