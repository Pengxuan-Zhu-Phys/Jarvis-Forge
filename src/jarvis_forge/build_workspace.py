"""Editable Release workspaces; project files stay outside the shared package store."""

import os
from pathlib import Path
import shlex

from .shell_repair import WORKSPACE_HEADER, activation_lock
from .paths import object_path
from .storage import ForgeError, digest, identifier, read_json, write_json, write_text

MARKER = ".jforge/workspace.json"


def workspace_candidates(home):
    """Read the workspace index and the requests created by older clients."""
    from .package_operations import _inside

    for directory, pattern in (("builds/workspaces", "*/workspace.json"), ("runs", "*/request.json")):
        for record in sorted((home.root / directory).glob(pattern)):
            try:
                value = read_json(_inside(home, record))
                if directory == "runs" and value.get("kind") != "build":
                    continue
                workspace = value.get("workspace")
                if workspace is None and directory == "runs":
                    continue
                if not isinstance(workspace, str) or not Path(workspace).is_absolute():
                    raise ForgeError(f"Invalid workspace path in {record}")
                yield Path(os.path.normpath(workspace)), None
            except ForgeError as exc:
                yield record, exc


def locate(recipe):
    recipe = Path(recipe).resolve()
    directory = recipe if recipe.is_dir() else recipe.parent
    marker = directory / MARKER
    if not marker.is_file():
        return None
    metadata = read_json(marker)
    if metadata.get("schema_version") != 1 or metadata.get("build") != "build.yaml":
        raise ForgeError(f"Invalid build workspace: {directory}")
    return directory if recipe.is_dir() or recipe.name == "build.yaml" else None


def create(home, source, directory):
    from .build_catalog import copy_bundle, entries, cached_recipe
    directory = directory.expanduser().resolve()
    if directory.exists() and not directory.is_dir():
        raise ForgeError(f"Workspace path is not a directory: {directory}")
    for reserved in (".jforge", "instances", "runs", "activate.sh"):
        path = directory / reserved
        if path.exists() or path.is_symlink():
            raise ForgeError(f"Workspace destination already contains {path}", hint="Choose a new directory.")
    output = copy_bundle(source, directory / "build.yaml", home=home)
    release = next((row for row in entries(home) if source == cached_recipe(home, row)), None)
    write_json(directory / MARKER, {"schema_version": 1, "build": "build.yaml",
               "origin": str(source), "sha256": digest(source), "release": release, "home": str(home.root)})
    from .package_operations import _inside

    record = object_path(_inside(home, home.root / "builds/workspaces"), "workspace", {"workspace": str(directory)}, create=True)
    registration = record / "workspace.json"
    write_json(registration, {"workspace": str(directory)})
    return output


def link_run(directory, run):
    target = directory / "runs" / run.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() and target.resolve() == run.resolve():
        return
    if target.exists() or target.is_symlink():
        raise ForgeError(f"Workspace run path already exists: {target}")
    target.symlink_to(run.resolve(), target_is_directory=True)


def activation_output(outputs):
    return outputs.get("installation") or (next(iter(outputs.values())) if len(outputs) == 1 else None)


def profile_activations(directory):
    profiles = directory / ".jforge/profiles"
    if profiles.is_symlink():
        raise ForgeError(f"Managed profile directory must not be a symbolic link: {profiles}")
    for entry in sorted(profiles.glob("*/activate.sh")):
        if entry.is_symlink() or entry.parent.is_symlink():
            raise ForgeError(f"Managed profile activation must not use symbolic links: {entry}")
        yield entry


def publish(directory, result):
    from .cmake import runtime_activation

    selected = activation_output(result.get("outputs", {}))
    if not selected:
        return
    # Only successful, verified outputs reach this point. Failed attempts retain the previous entry.
    target = directory / "activate.sh"
    with activation_lock(target):
        activation = runtime_activation(Path(selected["activation"]))
        instance = result.get("profile_instance")
        profiles = directory / ".jforge/profiles"
        if profiles.is_symlink():
            raise ForgeError(f"Managed profile directory must not be a symbolic link: {profiles}")
        if instance or profiles.is_dir():
            legacy = profiles / "legacy/activate.sh"
            if instance and target.is_file() and not profiles.is_dir():
                existing = target.read_text()
                if existing.startswith(WORKSPACE_HEADER):
                    write_text(legacy, existing)
            name = identifier(instance) if instance else "legacy"
            entry = profiles / name / "activate.sh"
            if entry.is_symlink() or entry.parent.is_symlink():
                raise ForgeError(f"Managed profile activation must not use symbolic links: {entry}")
            entry.parent.mkdir(parents=True, exist_ok=True)
            with activation_lock(entry):
                write_text(entry, WORKSPACE_HEADER + "source " + shlex.quote(str(activation)) + " || return $?\n")
                write_json(entry.with_name("current.json"), {"run_id": result["run_id"],
                           "artifact_id": selected["artifact_id"], "prefix": selected["prefix"], "activation": str(entry)})
            root = shlex.quote(str(profiles))
            write_text(target, WORKSPACE_HEADER + r'''case "${JFORGE_PROFILE_INSTANCE-}" in
  *[!A-Za-z0-9_.-]*|[!A-Za-z0-9]*)
    printf '%s\n' 'Invalid active profile ID.' >&2
    return 1 ;;
esac
''' + f'''if [ ! -f {root}/"${{JFORGE_PROFILE_INSTANCE:-legacy}}"/activate.sh ]; then
  printf '%s\\n' 'No successful build for the active profile in this workspace.' >&2
  return 1
fi
source {root}/"${{JFORGE_PROFILE_INSTANCE:-legacy}}"/activate.sh || return $?
''')
        else:
            write_text(target, WORKSPACE_HEADER + "source " + shlex.quote(str(activation)) + " || return $?\n")
        write_json(directory / ".jforge/current.json", {"run_id": result["run_id"],
                   "artifact_id": selected["artifact_id"], "prefix": selected["prefix"], "activation": str(target),
                   **({"profile_instance": instance} if instance else {})})
