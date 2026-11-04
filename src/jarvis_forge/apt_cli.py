"""APT-shaped commands backed by Forge recipes, never the system apt binary."""

from __future__ import annotations

import fnmatch
from pathlib import Path
import shlex
from typing import Optional

from packaging.version import Version
import typer

from .catalog import Catalog, requirement, source_config
from .packages import (
    edit_sources,
    environment_path,
    install,
    installed,
    plan_install,
    plan_remove,
    remove,
    update,
)
from .storage import ForgeError
from .presentation import line


app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Manage user-local software with Spack; existing source environments remain available.\n\n"
    "Command help: jforge apt COMMAND -h",
)


def _emit(ctx, command, **values):
    from .cli import emit

    return emit(ctx, "apt " + command, **values)


def _spack(ctx):
    from .spack_backend import default_backend

    return default_backend(ctx.obj.home)


def _progress():
    from .cli import _console

    return lambda message: line(_console(stderr=True), message)


def _spack_install(
    ctx, packages, jobs, timeout, dry_run, mode, cache="auto", command=None
):
    from . import spack_backend as backend

    _emit(
        ctx,
        command or mode,
        **backend.transact(
            ctx.obj.home,
            mode,
            packages,
            jobs=jobs,
            timeout=timeout,
            dry_run=dry_run,
            cache=cache,
            progress=_progress(),
        ),
    )


@app.callback()
def apt(ctx: typer.Context):
    from .apt_profiles import active_home

    ctx.obj.user_home = ctx.obj.home
    # Source selection must remain repairable when a saved scope is missing.
    if ctx.invoked_subcommand != "switch":
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
        False, "--installed", help="Show recorded installations only."
    ),
    upgradable: bool = typer.Option(
        False, "--upgradable", help="Show active packages with newer recipes."
    ),
):
    """List available or installed user-local packages."""
    if _spack(ctx):
        from . import spack_backend as backend

        if upgradable:
            raise ForgeError(
                "Use apt check to concretize a Spack plan, or apt upgrade to refresh the locked environment."
            )
        rows = (
            backend.current(ctx.obj.home)["packages"]
            if installed_only
            else backend.catalog(ctx.obj.home, pattern=pattern)
        )
        rows = [row for row in rows if fnmatch.fnmatchcase(row["name"], pattern)]
        if not ctx.obj.as_json and len(rows) > 1000:
            import sys

            sys.stdout.write("".join(row["name"] + "\n" for row in rows))
        else:
            _emit(ctx, "list", backend="spack", packages=rows)
        return
    records = installed(ctx.obj.home)
    catalog = Catalog(ctx.obj.home)
    if installed_only:
        packages = records
    else:
        packages = []
        for recipe in catalog.list():
            matches = [
                r
                for r in records
                if r["name"] == recipe["name"] and r["version"] == recipe["version"]
            ]
            active = next((r for r in matches if r["active"]), None)
            packages.append(
                {
                    "name": recipe["name"],
                    "version": recipe["version"],
                    "status": "installed" if matches else "available",
                    "active": bool(active),
                    "prefix": active["prefix"] if active else "",
                    "description": recipe["description"],
                }
            )
    if upgradable:
        from .dependencies import newer

        packages = [
            r
            for r in records
            if r["active"]
            and any(p["name"] == r["name"] and newer(p, r) for p in catalog.list())
        ]
    elif not installed_only:
        from .official import cached_packages

        known = {p["name"] for p in packages}
        packages.extend(
            p
            for p in cached_packages(ctx.obj.home)
            if p["name"] not in known and fnmatch.fnmatchcase(p["name"], pattern)
        )
    packages = [p for p in packages if fnmatch.fnmatchcase(p["name"], pattern)]
    # Rich tables calculate widths over every cell, which is costly for a whole distribution.
    if not ctx.obj.as_json and len(packages) > 1000:
        import sys

        for item in packages:
            sys.stdout.write(f"{item['name']} {item['version']} [{item['status']}]\n")
    else:
        _emit(ctx, "list", packages=packages)


@app.command()
def search(
    ctx: typer.Context,
    query: str = typer.Argument(
        ..., help="Match package names and descriptions.", metavar="<query>"
    ),
):
    """Search recipes and the imported official index without downloading."""
    if _spack(ctx):
        from .spack_backend import catalog

        _emit(
            ctx, "search", backend="spack", packages=catalog(ctx.obj.home, query=query)
        )
        return
    from .official import cached_packages

    packages = [
        {"name": p["name"], "version": p["version"], "description": p["description"]}
        for p in Catalog(ctx.obj.home).list(query)
    ]
    known = {p["name"] for p in packages}
    packages.extend(
        p for p in cached_packages(ctx.obj.home, query) if p["name"] not in known
    )
    _emit(ctx, "search", packages=packages)


@app.command()
def show(
    ctx: typer.Context,
    package: str = typer.Argument(
        ..., help="Package or package=version.", metavar="<package>"
    ),
):
    """Show recipe details, dependencies and installed locations."""
    if _spack(ctx):
        from .spack_backend import details, current, name_of

        _emit(
            ctx,
            "show",
            package=details(ctx.obj.home, package),
            installed_instances=[
                r
                for r in current(ctx.obj.home)["packages"]
                if r["name"] == name_of(package)
            ],
        )
        return
    req = requirement(package)
    from .dependencies import native, matches

    choices = [
        p
        for p in Catalog(ctx.obj.home).list()
        if matches(p, native(package, root=True))
    ]
    if not choices:
        from .official import cached_packages, _upstream_version

        choices = cached_packages(ctx.obj.home, req.name, exact=True, details=True)
        if req.specifier:
            choices = [
                p
                for p in choices
                if matches(
                    {
                        "name": req.name,
                        "version": _upstream_version(p["version"]),
                        "provenance": {
                            "kind": "official-apt-source",
                            "debian_version": p["version"],
                            "binary_versions": {req.name: p["version"]},
                        },
                    },
                    native(package, root=True),
                )
            ]
        if not choices:
            raise ForgeError(
                f"No recipe or imported official metadata matches {package}.",
                hint="jforge apt update",
            )
        _emit(
            ctx,
            "show",
            package=choices[0],
            installed_instances=[
                r for r in installed(ctx.obj.home) if r["name"] == req.name
            ],
        )
        return
    recipe = max(choices, key=lambda p: Version(p["version"]))
    _emit(
        ctx,
        "show",
        package=recipe,
        installed_instances=[
            r for r in installed(ctx.obj.home) if r["name"] == req.name
        ],
    )


def _install(ctx, packages, jobs, timeout, dry_run, mode, official=True, cache="auto"):
    if _spack(ctx):
        if not official:
            raise ForgeError(
                "--no-official belongs to legacy source recipes; Spack uses its configured repositories."
            )
        _spack_install(ctx, packages or [], jobs, timeout, dry_run, mode, cache)
        return
    if cache != "auto":
        raise ForgeError("--cache belongs to the Spack backend.")
    from .cli import _console

    if dry_run:
        from .official import missing_packages, default_source

        missing = (
            missing_packages(ctx.obj.home, packages or [])
            if official and mode in {"install", "reinstall"}
            else []
        )
        if missing:
            if not default_source(ctx.obj.home):
                raise ForgeError(
                    "No official source has been imported.",
                    hint="jforge apt import debian",
                )
            _emit(
                ctx,
                mode,
                dry_run=True,
                plan={
                    "requires_official_lookup": True,
                    "official_source": default_source(ctx.obj.home),
                    "requested_packages": packages,
                    "packages": [
                        {"name": name, "action": "official-lookup"} for name in missing
                    ],
                    "note": "Source archives, adapter and build requirements are resolved during install; no files downloaded.",
                },
            )
            return
        result = plan_install(ctx.obj.home, packages or [], jobs=jobs, mode=mode)
        _emit(ctx, mode, dry_run=True, plan=result)
    else:
        result = install(
            ctx.obj.home,
            packages or [],
            jobs=jobs,
            timeout=timeout,
            mode=mode,
            progress=lambda message: line(_console(stderr=True), message),
            official=official,
        )
        _emit(ctx, mode, **result)


@app.command("install")
def install_packages(
    ctx: typer.Context,
    packages: list[str] = typer.Argument(
        ..., help="Package names or name=version specs.", metavar="<packages>"
    ),
    jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
    timeout: float = typer.Option(
        3600, "--timeout", help="Timeout per build/check command, in seconds."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Show the plan without writing or downloading."
    ),
    no_official: bool = typer.Option(
        False, "--no-official", help="Use configured legacy recipes only."
    ),
    cache: str = typer.Option(
        "auto", "--cache", help="Spack binary-cache policy: auto, only or never."
    ),
):
    """Download, verify, build and install into private prefixes."""
    _install(
        ctx,
        packages,
        jobs,
        timeout,
        dry_run,
        "install",
        official=not no_official,
        cache=cache,
    )


@app.command()
def reinstall(
    ctx: typer.Context,
    packages: list[str] = typer.Argument(
        ..., help="Packages to rebuild into new instances.", metavar="<packages>"
    ),
    jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
    timeout: float = typer.Option(
        3600, "--timeout", help="Timeout per build/check command, in seconds."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Show the reinstall plan only."
    ),
):
    """Rebuild requested packages while keeping working instances."""
    _install(ctx, packages, jobs, timeout, dry_run, "reinstall")


@app.command()
def upgrade(
    ctx: typer.Context,
    packages: Optional[list[str]] = typer.Argument(
        None, help="Upgrade these packages, or all manual roots.", metavar="<packages>"
    ),
    jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
    timeout: float = typer.Option(
        3600, "--timeout", help="Timeout per build/check command, in seconds."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Show the upgrade plan only."
    ),
):
    """Select newer compatible recipes and retain previous instances."""
    _install(ctx, packages, jobs, timeout, dry_run, "upgrade")


@app.command("full-upgrade")
def full_upgrade(
    ctx: typer.Context,
    jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
    timeout: float = typer.Option(
        3600, "--timeout", help="Timeout per build/check command, in seconds."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Show the full upgrade plan only."
    ),
):
    """Upgrade all roots and dependencies; old instances are retained."""
    _install(ctx, [], jobs, timeout, dry_run, "full-upgrade")


@app.command()
def satisfy(
    ctx: typer.Context,
    constraints: list[str] = typer.Argument(
        ..., help="Version constraints, such as 'eigen>=3.4'.", metavar="<constraints>"
    ),
    jobs: int = typer.Option(1, "--jobs", "-j", min=1, help="Parallel build jobs."),
    timeout: float = typer.Option(
        3600, "--timeout", help="Timeout per build/check command, in seconds."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Resolve constraints without installing."
    ),
):
    """Install recipes satisfying explicit version constraints."""
    if _spack(ctx):
        _spack_install(
            ctx, constraints, jobs, timeout, dry_run, "install", command="satisfy"
        )
        return
    if dry_run:
        _emit(
            ctx,
            "satisfy",
            dry_run=True,
            plan=plan_install(ctx.obj.home, constraints, jobs=jobs),
        )
    else:
        from .cli import _console

        _emit(
            ctx,
            "satisfy",
            **install(
                ctx.obj.home,
                constraints,
                jobs=jobs,
                timeout=timeout,
                progress=lambda m: line(_console(stderr=True), m),
            ),
        )


def _remove(ctx, packages, automatic, yes, dry_run):
    command = "autoremove" if automatic else "remove"
    if _spack(ctx):
        if not yes and not dry_run:
            import sys

            if ctx.obj.as_json or not sys.stdin.isatty():
                raise ForgeError("Removal requires --yes in non-interactive/JSON mode.")
            if not typer.confirm(
                "Remove from the Spack environment? Saved revisions are retained.",
                default=False,
            ):
                raise ForgeError("Removal cancelled.")
        from . import spack_backend as backend

        if automatic:
            _emit(
                ctx,
                command,
                **backend.autoremove(
                    ctx.obj.home, dry_run=dry_run, progress=_progress()
                ),
            )
        else:
            _emit(
                ctx,
                command,
                **backend.transact(
                    ctx.obj.home,
                    "remove",
                    packages,
                    dry_run=dry_run,
                    progress=_progress(),
                ),
            )
        return
    plan = plan_remove(ctx.obj.home, packages, automatic=automatic)
    if dry_run:
        _emit(ctx, command, dry_run=True, **plan)
        return
    if plan["packages"] and not yes:
        import sys

        if ctx.obj.as_json or not sys.stdin.isatty():
            raise ForgeError(
                "Removal requires --yes in non-interactive/JSON mode.",
                hint=f"jforge apt {command} {' '.join(packages)} --yes",
            )
        names = ", ".join(
            f"{p['name']}={p['version']} ({p['install_id']})" for p in plan["packages"]
        )
        if not typer.confirm(f"Remove {names}?", default=False):
            raise ForgeError("Removal cancelled.")
    _emit(
        ctx,
        command,
        **remove(
            ctx.obj.home, packages, automatic=automatic, expected_keys=plan["keys"]
        ),
    )


@app.command("remove")
def remove_packages(
    ctx: typer.Context,
    packages: list[str] = typer.Argument(
        ..., help="Package names or name=version specs.", metavar="<packages>"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm removal of the selected installations."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Show what would be removed."
    ),
):
    """Remove Forge-owned installations after dependency checks."""
    _remove(ctx, packages, False, yes, dry_run)


@app.command()
def autoremove(
    ctx: typer.Context,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm removal of unused automatic dependencies."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-s", help="Show unused automatic dependencies."
    ),
):
    """Remove automatic packages unreachable from retained manual installs."""
    _remove(ctx, [], True, yes, dry_run)


@app.command("update")
def update_sources(
    ctx: typer.Context,
    recipes_only: bool = typer.Option(
        False,
        "--recipes-only",
        help="Refresh recipes without official repository metadata.",
    ),
):
    """Refresh recipes and the selected official source index without upgrading."""
    if _spack(ctx):
        if recipes_only:
            raise ForgeError("--recipes-only applies to the legacy source backend.")
        from .spack_backend import update as update_spack

        _emit(ctx, "update", **update_spack(ctx.obj.home, progress=_progress()))
        return
    _emit(ctx, "update", **update(ctx.obj.home, official=not recipes_only))


@app.command("switch")
def switch_source(
    ctx: typer.Context,
    source: Optional[str] = typer.Argument(
        None, help="Select spack, debian, ubuntu or legacy.", metavar="<source>"
    ),
    suite: Optional[str] = typer.Option(
        None, "--suite", help="Distribution codename, e.g. noble."
    ),
    component: Optional[str] = typer.Option(
        None, "--component", help="Comma-separated components, e.g. main,universe."
    ),
    keyring: Optional[Path] = typer.Option(
        None, "--keyring", help="Legacy archive keyring."
    ),
    name: str = typer.Option(
        "default", "--name", help="Name of the Spack package environment."
    ),
    python: Optional[Path] = typer.Option(
        None, "--python", help="User-selected existing Python for Spack dependencies."
    ),
):
    """Switch isolated package, metadata and shell environments automatically."""
    from .apt_profiles import switch, profiles

    base = ctx.obj.user_home or ctx.obj.home
    if source is None:
        if suite or component or keyring or python or name != "default":
            raise ForgeError("Choose a backend when supplying source options.")
        _emit(ctx, "switch", environments=profiles(base))
    else:
        if not ctx.obj.as_json:
            from .cli import _console

            line(
                _console(stderr=True),
                f"Selecting {source} package environment; preparing metadata and shell activation...",
            )
        _emit(
            ctx,
            "switch",
            **switch(
                base,
                source,
                suite=suite,
                component=component,
                keyring=keyring,
                name=name,
                python=str(python) if python else None,
                progress=_progress(),
            ),
        )


@app.command("sources")
def list_official_sources(
    ctx: typer.Context,
    add: Optional[str] = typer.Option(
        None, "--add", help="Add a signed Spack binary mirror."
    ),
    url: Optional[str] = typer.Option(
        None, "--url", help="Mirror HTTPS, OCI or file URL."
    ),
    remove: Optional[str] = typer.Option(
        None, "--remove", help="Remove a Spack mirror."
    ),
    trust_key: Optional[Path] = typer.Option(
        None, "--trust-key", help="Trust this explicitly selected mirror signing key."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm mirror removal."),
):
    """List supported official sources and the currently imported source."""
    if _spack(ctx):
        from .spack_backend import sources

        if remove and not yes:
            raise ForgeError("Mirror removal requires --yes.")
        _emit(
            ctx,
            "sources",
            **sources(
                ctx.obj.home,
                add=add,
                url=url,
                remove=remove,
                trust_key=trust_key,
                progress=_progress(),
            ),
        )
        return
    if any([add, url, remove, trust_key]):
        raise ForgeError(
            "Select the Spack backend to manage binary-cache mirrors.",
            hint="jforge apt switch spack",
        )
    from .official import REPOSITORIES, default_source

    current = default_source(ctx.obj.home)
    defaults = {"debian": ("bookworm", "main"), "ubuntu": ("noble", "main,universe")}
    sources = [
        {
            "name": name,
            "url": url,
            "suite": defaults[name][0],
            "component": defaults[name][1],
            "active": bool(current and current["origin"] == name),
            "selected_suite": current["suite"]
            if current and current["origin"] == name
            else None,
            "selected_component": ",".join(
                current.get("components", [current["component"]])
            )
            if current and current["origin"] == name
            else None,
            "description": f"{defaults[name][0]}/{defaults[name][1]} — {url}",
        }
        for name, url in REPOSITORIES.items()
    ]
    _emit(ctx, "sources", sources=sources, selected=current)


@app.command("import")
def import_official(
    ctx: typer.Context,
    package: Optional[str] = typer.Argument(
        None,
        help="Optional package; omit to import the whole source index.",
        metavar="<package>",
    ),
    suite: Optional[str] = typer.Option(
        None,
        "--suite",
        help="Distribution codename; defaults to the selected source.",
        metavar="<str>",
    ),
    origin: Optional[str] = typer.Option(
        None,
        "--origin",
        help="Official distribution: debian or ubuntu.",
        metavar="<debian|ubuntu>",
    ),
    component: Optional[str] = typer.Option(
        None,
        "--component",
        help="Repository component; defaults to main for a new source.",
        metavar="<str>",
    ),
    name: Optional[str] = typer.Option(
        None, "--name", help="Forge package name.", metavar="<str>"
    ),
    template: Optional[str] = typer.Option(
        None,
        "--template",
        help="Reuse a reviewed Forge recipe's build adapter.",
        metavar="<str>",
    ),
    source_version: Optional[str] = typer.Option(
        None,
        "--source-version",
        help="Pin an exact Debian source version.",
        metavar="<str>",
    ),
    keyring: Optional[Path] = typer.Option(
        None,
        "--keyring",
        help="Require gpgv verification using this trusted archive keyring.",
        metavar="<path>",
    ),
):
    """Import an official repository index, or track one source package."""
    if package == "spack":
        if any([suite, origin, component, name, template, source_version, keyring]):
            raise ForgeError(
                "Spack uses its own recipes; use apt switch spack --name NAME for named environments."
            )
        from .apt_profiles import switch

        _emit(
            ctx,
            "import",
            **switch(ctx.obj.user_home or ctx.obj.home, "spack", progress=_progress()),
        )
        return
    from .spack_backend import configured as spack_configured

    if spack_configured(ctx.obj.home):
        raise ForgeError(
            "Select a legacy source environment before importing Debian/Ubuntu metadata.",
            hint="jforge apt switch ubuntu",
        )
    from .official import (
        DEFAULT_SOURCE,
        REPOSITORIES,
        default_source,
        import_package,
        import_repository,
    )

    current = default_source(ctx.obj.home) or DEFAULT_SOURCE
    if (
        package in REPOSITORIES
        and origin is None
        and name is None
        and template is None
        and source_version is None
    ):
        origin, package = package, None
    selected_origin = origin or current["origin"]
    if selected_origin not in {"debian", "ubuntu"}:
        raise ForgeError("--origin must be debian or ubuntu.")
    selected_suite = suite or (
        current["suite"]
        if selected_origin == current["origin"]
        else {"debian": "bookworm", "ubuntu": "noble"}[selected_origin]
    )
    same_source = (
        selected_origin == current["origin"] and selected_suite == current["suite"]
    )
    selected_component = component or (
        ",".join(current.get("components", [current["component"]]))
        if same_source
        else ("main,universe" if selected_origin == "ubuntu" else "main")
    )
    same_source = same_source and selected_component == ",".join(
        current.get("components", [current["component"]])
    )
    selected_keyring = (
        keyring
        if keyring is not None
        else (current.get("keyring") if same_source else None)
    )
    if ctx.obj.user_home is not None and ctx.obj.home != ctx.obj.user_home:
        from .official import default_source

        configured = default_source(ctx.obj.home)
        if (selected_origin, selected_suite, selected_component) != (
            configured["origin"],
            configured["suite"],
            ",".join(configured.get("components", [configured["component"]])),
        ):
            raise ForgeError(
                "Use jforge apt switch to select a different source environment."
            )
    if package is None:
        if name is not None or template is not None or source_version is not None:
            raise ForgeError(
                "A package is required for --name, --template or --source-version."
            )
        _emit(
            ctx,
            "import",
            **import_repository(
                ctx.obj.home,
                origin=selected_origin,
                suite=selected_suite,
                component=selected_component,
                keyring=selected_keyring,
            ),
        )
        return
    result = import_package(
        ctx.obj.home,
        package,
        origin=selected_origin,
        suite=selected_suite,
        component=selected_component,
        name=name,
        template_name=template,
        source_version=source_version,
        keyring=selected_keyring,
    )
    _emit(ctx, "import", **result)


@app.command("edit-sources")
def configure_sources(
    ctx: typer.Context,
    add: Optional[str] = typer.Option(
        None,
        "--add",
        help="Add a local directory/file or HTTPS YAML index.",
        metavar="<source>",
    ),
    remove_source: Optional[str] = typer.Option(
        None, "--remove", help="Remove a configured source.", metavar="<source>"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm removal of a configured source."
    ),
):
    """Show or edit Forge recipe sources without opening an editor."""
    if _spack(ctx):
        if add or remove_source:
            raise ForgeError(
                "Use apt sources to manage Spack mirrors; legacy YAML recipe sources belong to a source environment."
            )
        from .spack_backend import sources

        _emit(ctx, "edit-sources", **sources(ctx.obj.home))
        return
    if add or remove_source:
        if remove_source and not yes:
            import sys

            if ctx.obj.as_json or not sys.stdin.isatty():
                raise ForgeError(
                    "Removing a source requires --yes in non-interactive/JSON mode."
                )
            if not typer.confirm(
                f"Remove recipe source {remove_source}?", default=False
            ):
                raise ForgeError("Source removal cancelled.")
        _emit(
            ctx,
            "edit-sources",
            **edit_sources(ctx.obj.home, add=add, remove_source=remove_source),
        )
    else:
        from .official import default_source

        _emit(
            ctx,
            "edit-sources",
            path=str(ctx.obj.home.root / "apt/sources.json"),
            official_path=str(ctx.obj.home.root / "apt/official.json"),
            official_source=default_source(ctx.obj.home),
            **source_config(ctx.obj.home),
        )


@app.command("env")
def env(
    ctx: typer.Context,
    path: bool = typer.Option(
        False, "--path", help="Print only the current activation script path."
    ),
):
    """Locate the versioned activation script for active packages."""
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


@app.command("shell-init")
def shell_init(ctx: typer.Context):
    """Print a Bash/Zsh hook for automatic activation after package changes."""
    if ctx.obj.as_json:
        raise ForgeError("shell-init cannot be used with --json.")
    print(r"""# Add eval "$(command jforge apt shell-init)" to your shell startup file.
jforge() {
    local _jf_arg _jf_next=0 _jf_home='' _jf_apt=0 _jf_change=0 _jf_path _jf_command='' _jf_action='' _jf_skip=0 _jf_target=0
    for _jf_arg in "$@"; do
        if [ "$_jf_next" = 1 ]; then _jf_home=$_jf_arg; _jf_next=0; continue; fi
        case "$_jf_arg" in
            --) break ;;
            --home) _jf_next=1 ;;
            --home=*) _jf_home=${_jf_arg#--home=} ;;
            --help|-h|--dry-run|-s|--version|-v|-V) _jf_skip=1 ;;
            --json|--debug|-d) ;;
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
                _jf_path=$(command jforge --home "$_jf_home" apt env --path 2>/dev/null) || _jf_path=''
            else
                _jf_path=$(command jforge apt env --path 2>/dev/null) || _jf_path=''
            fi
            [ -z "$_jf_path" ] || source "$_jf_path"
        fi
    else
        return $?
    fi
}
_jforge_apt_initial=$(command jforge apt env --path 2>/dev/null) || _jforge_apt_initial=''
if [ -n "$_jforge_apt_initial" ]; then source "$_jforge_apt_initial"; fi
unset _jforge_apt_initial
""")


@app.command("check")
def check_spack(
    ctx: typer.Context,
    packages: Optional[list[str]] = typer.Argument(
        None,
        help="Packages/specs to concretize; defaults to active roots.",
        metavar="<packages>",
    ),
    timeout: float = typer.Option(
        3600,
        "--timeout",
        min=0.1,
        help="Timeout for each solver or verification command.",
    ),
):
    """Resolve a Spack dependency plan and verify installed files without installing packages."""
    if not _spack(ctx):
        raise ForgeError(
            "Spack check requires a Spack environment.", hint="jforge apt switch spack"
        )
    from .spack_backend import transact

    _emit(
        ctx,
        "check",
        **transact(
            ctx.obj.home, "check", packages or [], timeout=timeout, progress=_progress()
        ),
    )


@app.command("compilers")
def spack_compilers(
    ctx: typer.Context,
    find: bool = typer.Option(
        False, "--find", help="Discover compilers from the user's current PATH."
    ),
):
    """Report or discover Spack compilers without choosing a different shell environment."""
    if not _spack(ctx):
        raise ForgeError(
            "Select a Spack environment first.", hint="jforge apt switch spack"
        )
    from .spack_backend import compilers

    _emit(ctx, "compilers", **compilers(ctx.obj.home, find=find, progress=_progress()))
