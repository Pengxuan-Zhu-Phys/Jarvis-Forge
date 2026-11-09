"""Editable Release workspaces; project files stay outside the shared package store."""

import hashlib
import os
from pathlib import Path
import shlex

from .shell_repair import WORKSPACE_HEADER, activation_lock
from .storage import ForgeError, digest, read_json, write_json, write_text

MARKER = ".jforge/workspace.json"


def workspace_candidates(home):
    """Read the workspace index and the requests created by older clients."""
    from .package_operations import _inside

    for directory, pattern in (("builds/workspaces", "*.json"), ("runs", "build-*/request.json")):
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
    from .build_catalog import copy_bundle, entries, snapshot_id
    directory = directory.expanduser().resolve()
    if directory.exists() and not directory.is_dir():
        raise ForgeError(f"Workspace path is not a directory: {directory}")
    for reserved in (".jforge", "instances", "runs", "activate.sh"):
        path = directory / reserved
        if path.exists() or path.is_symlink():
            raise ForgeError(f"Workspace destination already contains {path}", hint="Choose a new directory.")
    output = copy_bundle(source, directory / "build.yaml", home=home)
    release = next((row for row in entries(home) if source.parent.name == snapshot_id(row)), None)
    write_json(directory / MARKER, {"schema_version": 1, "build": "build.yaml",
               "origin": str(source), "sha256": digest(source), "release": release, "home": str(home.root)})
    from .package_operations import _inside

    key = hashlib.sha256(os.fsencode(directory)).hexdigest()
    registration = _inside(home, home.root / "builds/workspaces" / (key + ".json"))
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


def publish(directory, result):
    from .cmake import runtime_activation

    selected = activation_output(result.get("outputs", {}))
    if not selected:
        return
    # Only successful, verified outputs reach this point. Failed attempts retain the previous entry.
    target = directory / "activate.sh"
    with activation_lock(target):
        activation = runtime_activation(Path(selected["activation"]))
        write_text(target, WORKSPACE_HEADER + "source " + shlex.quote(str(activation)) + " || return $?\n")
        write_json(directory / ".jforge/current.json", {"run_id": result["run_id"],
                   "artifact_id": selected["artifact_id"], "prefix": selected["prefix"], "activation": str(target)})
