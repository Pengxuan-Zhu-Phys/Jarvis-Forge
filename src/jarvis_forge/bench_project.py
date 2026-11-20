"""Bench project discovery, initialization and verified package installation."""

from __future__ import annotations

import hashlib
from importlib.resources import files
import json
from pathlib import Path
import re
import tempfile

import yaml

from .builds import file_lock
from .paths import named_path
from .storage import ForgeError, digest, identifier, new_id, read_json, write_json, write_text
from .suite import UniqueLoader


MANIFEST = "jarvis.project.yaml"
FORMAT = "jarvis-forge-bench-project"
STATE = ".jforge/bench.json"
PENDING = ".jforge/bench-fetch.json"
PACK = ".jarvis-pack/manifest.json"
INPUTS = ("bin", "data", "deps")
ROOTS = {"outputs": "outputs", "logs": "logs", "checkpoints": "checkpoints"}


def relative(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ForgeError(f"Expected a project-relative path: {value!r}")
    return Path(value)


def inside(root, value):
    path = root / relative(value)
    for part in (*reversed(path.parents), path):
        if part.is_relative_to(root) and part.is_symlink():
            raise ForgeError(f"Bench project paths must not traverse symlinks: {part}")
    return path


def _descriptor(root):
    path = inside(root, MANIFEST)
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read project manifest {path}: {exc}") from exc
    return raw.get("project") if isinstance(raw, dict) else None


def _read(root, descriptor, *, allow_pending=False):
    if descriptor.get("version") != 1 or descriptor.get("runtime") != "jforge-bench":
        raise ForgeError(f"Unsupported bench project format in {root / MANIFEST}.")
    name = descriptor.get("name")
    if not isinstance(name, str):
        raise ForgeError(f"Missing project name in {root / MANIFEST}.")
    identifier(name)
    entries = descriptor.get("entrypoints")
    defaults = descriptor.get("defaults", {})
    roots = descriptor.get("artifact_roots", ROOTS)
    if not isinstance(entries, list) or not isinstance(defaults, dict) or not isinstance(roots, dict):
        raise ForgeError(f"Invalid project entrypoints, defaults or artifact roots in {root / MANIFEST}.")
    paths = [inside(root, item) for item in entries]
    if len(set(paths)) != len(paths) or any(path.suffix not in {".yaml", ".yml"} or
                                           not path.is_relative_to(root / "bin") for path in paths):
        raise ForgeError("Project entrypoints must be distinct YAML files under bin/.")
    main = defaults.get("main_config")
    if main is not None and inside(root, main) not in paths:
        raise ForgeError("The main bench card must be listed in project.entrypoints.")
    if set(roots) != set(ROOTS):
        raise ForgeError("Artifact roots must define outputs, logs and checkpoints.")
    forbidden = [root / item for item in (*INPUTS, MANIFEST, ".jforge", ".jarvis-pack")]
    for item in roots.values():
        path = inside(root, item)
        if any(path.is_relative_to(other) or other.is_relative_to(path) for other in forbidden):
            raise ForgeError(f"Overlapping bench project directory: {path}")
        forbidden.append(path)
    if not allow_pending and inside(root, PENDING).exists():
        raise ForgeError(f"Bench project fetch is incomplete: {root}", hint="Repeat jforge bench fetch <project>.")
    state = inside(root, STATE)
    record = read_json(state) if state.is_file() else {}
    if record:
        if record.get("schema") != "jforge.bench.project/1" or not isinstance(record.get("id"), str):
            raise ForgeError(f"Unsupported bench workspace record: {state}")
        identifier(record["id"])
    return {"root": root, "name": name, "entries": paths, "main": root / main if main else None,
            "roots": roots, "record": record}


def discover(directory=None, *, allow_pending=False):
    root = Path(directory or Path.cwd()).resolve()
    for parent in (root, *root.parents):
        if not (parent / MANIFEST).exists() and not (parent / MANIFEST).is_symlink():
            continue
        descriptor = _descriptor(parent)
        if isinstance(descriptor, dict) and descriptor.get("format") == FORMAT:
            return _read(parent, descriptor, allow_pending=allow_pending)
        return None
    return None


def initialize(path):
    root = Path(path).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with file_lock(inside(root, ".jforge/bench.lock"), blocking=True):
        manifest = inside(root, MANIFEST)
        state = inside(root, STATE)
        placeholder = None
        if not manifest.exists():
            # A directory may have a name that cannot itself be a project ID.
            name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", root.name).lstrip("._-")[:128] or "bench"
            write_text(manifest, yaml.safe_dump({"project": {
                "format": FORMAT, "version": 1, "name": name, "runtime": "jforge-bench",
                "defaults": {"main_config": "bin/bench.yaml"}, "entrypoints": ["bin/bench.yaml"],
                "artifact_roots": ROOTS}}, sort_keys=False))
            placeholder = digest(manifest)
        project = discover(root, allow_pending=True)
        if not project or project["root"] != root:
            raise ForgeError(f"Existing manifest is not a bench project: {manifest}")
        for entry in INPUTS:
            inside(root, entry).mkdir(exist_ok=True)
        if not project["record"]:
            write_json(state, {"schema": "jforge.bench.project/1", "id": new_id("bench-project"),
                               "placeholder": placeholder})
    return {"project": project["name"], "path": str(root), "text": f"Bench project ready at {root}\n"}


def resolve_card(value=None):
    path = Path(value).expanduser() if value is not None else None
    if path is not None and (path.is_absolute() or "/" in str(value)):
        return path
    project = discover()
    if not project:
        if path is not None:
            return path
        raise ForgeError("No bench project in this directory.", hint="jforge bench init ./")
    if path is None:
        if project["main"] is None:
            raise ForgeError("This project has no main bench card.", hint=f"Edit {project['root'] / MANIFEST}.")
        return project["main"]
    matches = [entry for entry in project["entries"] if entry.name == path.name]
    if len(matches) > 1:
        choices = ", ".join(str(entry.relative_to(project["root"])) for entry in matches)
        raise ForgeError(f"Bench card name is ambiguous: {path}", hint=f"Use an explicit path from {choices}.")
    if matches:
        return matches[0]
    return path


def identity(project):
    return project["record"].get("id") or str(project["root"])


def data_root(project, external=None, *, create=False):
    if external:
        if not project["record"]:
            raise ForgeError("Initialize the project before using an external data root.", hint="jforge bench init ./")
        return named_path(Path(external).expanduser().resolve(), identity(project), create=create)
    return inside(project["root"], f"{project['roots']['outputs']}/steps")


def active_run(home, project):
    for result in (home.root / "runs").glob("*/result.json"):
        item = read_json(result)
        if item.get("project_id") == identity(project) and item.get("status") == "running":
            return item["run_id"]
    return None


def ensure_idle(home, project):
    if run_id := active_run(home, project):
        raise ForgeError(f"Bench project has an active run: {run_id}", hint=f"jforge bench status {run_id}")


def _pack_path(name):
    path = relative(name)
    if name not in {MANIFEST, PACK} and (len(path.parts) < 2 or path.parts[0] not in INPUTS):
        raise ForgeError(f"Unsupported bench package path: {name}")
    return path


def _stage(name, target):
    catalog = files("jarvis_forge").joinpath("bench_projects")
    entry = json.loads(catalog.joinpath("catalog.json").read_text(encoding="utf-8")).get(name)
    if not entry:
        raise ForgeError(f"Unknown bench project: {name}")
    version = identifier(entry["version"])
    source = catalog.joinpath(name, version)
    if isinstance(source, Path):
        source = source.resolve()
    manifest_path = inside(source, PACK) if isinstance(source, Path) else source.joinpath(*Path(PACK).parts)
    manifest = manifest_path.read_bytes()
    sha256 = hashlib.sha256(manifest).hexdigest()
    if sha256 != entry["sha256"]:
        raise ForgeError(f"Bench package manifest checksum mismatch: {name} {version}")
    records = json.loads(manifest)
    if records["name"] != name or records["version"] != version or MANIFEST not in records["files"]:
        raise ForgeError(f"Invalid bench package manifest: {name} {version}")
    for filename, expected in records["files"].items():
        path = _pack_path(filename)
        if filename == PACK:
            raise ForgeError("Bench package manifest cannot list itself.")
        asset = inside(source, filename) if isinstance(source, Path) else source.joinpath(*path.parts)
        content = asset.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ForgeError(f"Bench package checksum mismatch: {filename}")
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    packed = target / PACK
    packed.parent.mkdir()
    packed.write_bytes(manifest)
    return {"name": name, "version": version, "sha256": sha256, "files": {**records["files"], PACK: sha256}}


def fetch(home, name):
    from . import bench_cards, bench_exec

    identifier(name)
    home.require_initialized()
    project = discover(allow_pending=True)
    if not project or not project["record"]:
        raise ForgeError("Initialize the bench project before fetching.", hint="jforge bench init ./")
    root = project["root"]
    with file_lock(inside(root, ".jforge/bench.lock"), blocking=True):
        project = discover(root, allow_pending=True)
        ensure_idle(home, project)
        record = project["record"]
        previous = record.get("package") or {}
        if previous.get("name", name) != name:
            raise ForgeError(f"This workspace is already bound to {previous['name']}.")
        with tempfile.TemporaryDirectory(prefix=".bench-fetch-", dir=root / ".jforge") as stage:
            target = Path(stage)
            try:
                release = _stage(name, target)
            except (OSError, ValueError, KeyError) as exc:
                raise ForgeError(f"Cannot read bench package {name}: {exc}") from exc
            pending = inside(root, PENDING)
            if pending.exists() and read_json(pending) != release:
                raise ForgeError("A different release is pending in this workspace.")
            package = discover(target)
            if not package or package["name"] != name or not package["main"]:
                raise ForgeError(f"Invalid project descriptor in bench package: {name}")
            old = dict(previous.get("files") or {})
            if record.get("placeholder"):
                old[MANIFEST] = record["placeholder"]
            new = release["files"]
            conflicts = []
            for filename in sorted(old.keys() | new.keys()):
                _pack_path(filename)
                dest = inside(root, filename)
                if dest.exists() and (not dest.is_file() or digest(dest) not in {old.get(filename), new.get(filename)}):
                    conflicts.append(filename)
                elif any(parent.exists() and not parent.is_dir() for parent in dest.parents if parent != root):
                    conflicts.append(filename)
            if conflicts:
                raise ForgeError("Bench package conflicts with local files:\n  " + "\n  ".join(conflicts))
            # Resolve each card and input from the verified package before changing the workspace.
            for entry in package["entries"]:
                card = bench_cards.load(entry)
                bench_cards.check_references(card, bench_cards._discover(card))
                bench_exec.prefetch(home, entry)
            write_json(pending, release)
            for filename, expected in sorted(new.items()):
                dest = inside(root, filename)
                if not dest.is_file() or digest(dest) != expected:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    (target / filename).replace(dest)
            for filename in sorted(old.keys() - new.keys()):
                inside(root, filename).unlink(missing_ok=True)
            write_json(inside(root, STATE), {**record, "placeholder": None, "package": release})
            pending.unlink()
    version = release["version"]
    return {"project": name, "version": version, "path": str(root),
            "text": f"Fetched {name} {version}\n{root}\n"}
