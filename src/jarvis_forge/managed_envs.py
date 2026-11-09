"""Discover and repair the shell entry points owned by a Forge home."""

from pathlib import Path
from subprocess import TimeoutExpired

from . import build_workspace, environment_registry
from .package_operations import _inside
from .shell_hook import BOOTSTRAP_HEADER, repair_bootstrap, repair_startup, startup_files
from .shell_repair import WORKSPACE_HEADER, repair_apt_profile, repair_artifact, repair_file
from .storage import ForgeError, ForgeHome, read_json


def repair_managed(home, *, dry_run=False, install_hook=False):
    results, errors = [], []
    startup = startup_files()
    if install_hook and not startup:
        raise ForgeError("Shell hook setup requires Bash or Zsh selected through SHELL.")

    def failed(kind, name, exc):
        errors.append({"kind": kind, "name": str(name), "message": str(exc)})

    def run(kind, name, operation, *args, **options):
        try:
            report = operation(*args, dry_run=dry_run, **options)
        except (ForgeError, OSError, ValueError, TimeoutExpired) as exc:
            failed(kind, name, exc)
        else:
            if report is not None:
                results.append({**report, "kind": kind, "name": str(name)})

    candidates = set()
    for path, error in build_workspace.workspace_candidates(home):
        if error:
            failed("workspace record", path, error)
        else:
            candidates.add(path)
    # Existing installations may have had their old run history pruned.
    try:
        cwd = Path.cwd()
        ancestors = (cwd, *cwd.parents)
    except FileNotFoundError:
        ancestors = ()  # A shell can still be sitting in a deleted workspace.
    for path in ancestors:
        if (path / build_workspace.MARKER).is_file():
            candidates.add(path)
            break

    workspace_sources, entries = set(), []
    for directory in sorted(candidates):
        marker = directory / build_workspace.MARKER
        entry = directory / "activate.sh"
        try:
            if directory.resolve() != directory or marker.parent.is_symlink() or marker.is_symlink():
                raise ForgeError(f"Managed workspace paths must not be symbolic links: {directory}")
            if not marker.is_file():
                if not entry.exists() and not entry.is_symlink():
                    workspace_sources.add(entry)
                continue
            record = read_json(marker)
            if record.get("home") != str(home.root):
                continue
            if record.get("schema_version") != 1 or record.get("build") != "build.yaml":
                raise ForgeError(f"Invalid build workspace: {directory}")
            if entry.is_symlink():
                raise ForgeError(f"Managed activation must not be a symbolic link: {entry}")
            workspace_sources.add(entry)
            if entry.exists():
                with entry.open("rb") as stream:
                    if stream.readline() != WORKSPACE_HEADER.encode():
                        raise ForgeError(f"Unrecognized workspace activation script: {entry}")
                entries.append(entry)
        except (ForgeError, OSError, ValueError) as exc:
            failed("workspace", directory, exc)

    for entry in entries:
        run("workspace", entry.parent, repair_file, entry, workspace_sources=workspace_sources, save_unchanged=False)
    for manifest in sorted((home.root / "builds/artifacts").glob("*/manifest.json")):
        try:
            record = read_json(_inside(home, manifest))
            prefix = record.get("prefix")
            if record.get("status") != "ready" or not isinstance(prefix, str):
                continue
            prefix = Path(prefix)
            if not prefix.is_relative_to(home.root / "builds/installs") or not prefix.is_dir():
                continue  # Workspace outputs are handled through the current workspace entry.
            if record.get("artifact_id") != manifest.parent.name or not isinstance(record.get("activation"), str):
                raise ForgeError(f"Invalid activation in build artifact record: {manifest}")
            run("artifact", record["artifact_id"], repair_artifact, home, record, workspace_sources=workspace_sources)
        except (ForgeError, OSError, ValueError) as exc:
            failed("artifact", manifest.parent.name, exc)
    for pointer in sorted((home.root / "environments").glob("*/current.json")):
        name = pointer.parent.name
        if name.lower() != "apt":
            run("named", name, environment_registry.repair, home, name,
                workspace_sources=workspace_sources, save_unchanged=False)

    for profile in (home.root, *sorted((home.root / "apt/profiles").glob("spack-*"))):
        name = "direct" if profile == home.root else profile.name
        try:
            state = _inside(home, profile / "apt/state.json")
            if state.is_file():
                record = read_json(state)
                if record.get("spack") and record.get("activation"):
                    run("apt", name, repair_apt_profile, ForgeHome(profile), save_unchanged=False)
        except (ForgeError, OSError, ValueError) as exc:
            failed("apt", name, exc)

    for path, shell in startup:
        run("hook", path, repair_startup, home, path, install=install_hook, shell=shell)

    bootstrap = Path.home() / ".jarvis-pyenv/jforge-bootstrap/activate.sh"
    try:
        existed = bootstrap.is_file() and not bootstrap.is_symlink()
        run("bootstrap hook", bootstrap, repair_bootstrap, home, bootstrap)
        if existed:
            with bootstrap.open("rb") as stream:
                generated = stream.readline() == BOOTSTRAP_HEADER.encode()
            if generated:
                run("bootstrap", bootstrap, repair_file, bootstrap, workspace_sources=workspace_sources, save_unchanged=False)
    except OSError as exc:
        failed("bootstrap", bootstrap, exc)

    return {"scope": "managed", "dry_run": dry_run, "targets": results, "errors": errors,
            "changed": sum(result["changed"] for result in results),
            **{key: sum(result[key] for result in results) for key in ("removed", "unchecked", "deferred")}}
