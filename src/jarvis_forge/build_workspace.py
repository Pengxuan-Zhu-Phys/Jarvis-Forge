"""Editable Release workspaces; project files stay outside the shared package store."""

import os
from pathlib import Path
import shlex

from .shell_repair import WORKSPACE_HEADER
from .paths import record_path
from .storage import ForgeError, digest, read_json, write_json, write_text

MARKER = "metadata.json"


def workspace_candidates(home):
    """Read the workspace index and the requests created by older clients."""
    from .package_operations import _inside

    for directory, pattern in (
        ("builds/workspaces", "*/workspace.json"),
        ("runs", "*/request.json"),
    ):
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
    if metadata.get("kind") != "workspace":
        return None
    if metadata.get("schema_version") != 1 or metadata.get("build") != "build.yaml":
        raise ForgeError(f"Invalid build workspace: {directory}")
    return directory if recipe.is_dir() or recipe.name == "build.yaml" else None


def create(home, source, directory):
    from .build_catalog import copy_bundle, entries, cached_recipe
    from .builds import load

    directory = directory.expanduser().resolve()
    if directory.exists() and not directory.is_dir():
        raise ForgeError(f"Workspace path is not a directory: {directory}")
    for reserved in (
        "metadata.json",
        "build.yaml",
        ".jforge",
        "instances",
        "runs",
        "activate.sh",
        "src",
        "build",
        "install",
    ):
        path = directory / reserved
        if path.exists() or path.is_symlink():
            raise ForgeError(
                f"Workspace destination already contains {path}",
                hint="Choose a new directory.",
            )
    output = copy_bundle(source, directory / "build.yaml", home=home)
    release = next(
        (row for row in entries(home) if source == cached_recipe(home, row)), None
    )
    write_json(
        directory / MARKER,
        {
            "schema_version": 1,
            "kind": "workspace",
            "build": "build.yaml",
            "origin": str(source),
            "sha256": digest(source),
            "release": release,
            "home": str(home.root),
            "resource_hashes": {
                row["path"]: row["sha256"]
                for row in load(source, home=home)["card_sources"]
                if {"path", "content", "sha256"}.issubset(row)
            },
        },
    )
    from .package_operations import _inside

    record = record_path(
        _inside(home, home.root / "builds/workspaces"),
        "workspace",
        {"workspace": str(directory)},
        create=True,
    )
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
    return outputs.get("installation") or (
        next(iter(outputs.values())) if len(outputs) == 1 else None
    )


def profile_activations(directory):
    profiles = directory / ".jforge/profiles"
    if profiles.is_symlink():
        raise ForgeError(
            f"Managed profile directory must not be a symbolic link: {profiles}"
        )
    for entry in sorted(profiles.glob("*/activate.sh")):
        if entry.is_symlink() or entry.parent.is_symlink():
            raise ForgeError(
                f"Managed profile activation must not use symbolic links: {entry}"
            )
        yield entry


def publish(directory, result):
    from .cmake import runtime_activation

    selected = activation_output(result.get("outputs", {}))
    if not selected:
        return
    data = read_json(directory / "metadata.json")
    profile = result.get("profile_instance")
    prefix = Path(selected["prefix"])
    sources = []
    if profile:
        from .unified_profiles import base_home, record
        from .storage import ForgeHome
        from .env_entries import _activation_text
        from .package_operations import environment_path

        base = base_home(ForgeHome(Path(data["home"])))
        scope = ForgeHome(base.root / "profiles" / profile)
        managed = record(scope)
        scripts = [
            environment_path(scope),
            managed["tools"]["activation"],
        ]
        scripts.extend(
            app["activation"]
            for app in managed.get("apps", [])
            if app.get("enabled")
            and not app.get("external")
            and app.get("artifact_id") != selected["artifact_id"]
        )
        scripts.append(runtime_activation(Path(selected["activation"])))
        text = WORKSPACE_HEADER + _activation_text(
            profile, scripts, managed=managed, activation=directory / "activate.sh"
        )
    else:
        sources.append(runtime_activation(Path(selected["activation"])))
        text = WORKSPACE_HEADER + "".join(
            "source " + shlex.quote(str(path)) + " || return $?\n" for path in sources
        )
    write_text(directory / "activate.sh", text)
    data.update(
        profile=profile,
        run_id=result["run_id"],
        artifact_id=selected["artifact_id"],
        prefix=str(prefix),
        activation=str(directory / "activate.sh"),
        status="ready",
    )
    write_json(directory / "metadata.json", data)
