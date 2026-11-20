"""Portable playlist libraries and their local test inventories."""

from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
import re

import yaml

from . import playlist_commands, testing
from .builds import file_lock
from .paths import named_path
from .storage import ForgeError, digest, identifier, read_json, write_text
from .suite import UniqueLoader


def _fields(value, label, required, optional=()):
    if not isinstance(value, dict):
        raise ForgeError(f"{label} must be a mapping.")
    unknown = value.keys() - set(required) - set(optional)
    missing = set(required) - value.keys()
    if unknown:
        raise ForgeError(f"{label} has unknown fields: " + ", ".join(sorted(map(str, unknown))))
    if missing:
        raise ForgeError(f"{label} is missing fields: " + ", ".join(sorted(missing)))


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ForgeError(f"{label} must be a nonempty string.")
    return value


def _relative(value, label, *, allow_parent=False):
    value = _text(value, label)
    path = Path(value)
    if path.is_absolute() or PureWindowsPath(value).drive or "\\" in value or value.startswith("~"):
        raise ForgeError(f"{label} must be a relative path: {value!r}")
    if not allow_parent and ".." in PureWindowsPath(value).parts:
        raise ForgeError(f"{label} must be inside the project without '..': {value!r}")


def _strings(value, label, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ForgeError(f"{label} must be a {'nonempty ' if nonempty else ''}list.")
    for item in value:
        _text(item, label)
    return value


def _records(items, label):
    if not isinstance(items, list):
        raise ForgeError(f"{label} must be a list.")
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ForgeError(f"Each entry in {label} must be a mapping.")
        name = identifier(_text(item.get("id"), f"{label} ID"))
        if name in seen:
            raise ForgeError(f"Duplicate ID {name!r} in {label}.")
        seen.add(name)
        yield item


def _validate_source(source, project, projects):
    label = f"Source {project}/{source['id']}"
    common = ("id", "kind")
    kind = source.get("kind")
    if kind == "test":
        _fields(source, label, (*common, "path"), ("env", "subjects"))
    elif kind == "bench":
        _fields(source, label, (*common, "path"), ("env", "subjects", "profile", "executor"))
    elif kind == "command":
        _fields(source, label, (*common, "list", "run"), ("env", "subjects"))
        for key in ("list", "run"):
            _strings(source[key], f"{label}.{key}", nonempty=True)
        for value in ("${request}", "${result_dir}"):
            if source["run"].count(value) != 1:
                raise ForgeError(f"{label}.run must pass {value} as a separate argument.")
    else:
        raise ForgeError(f"{label} has invalid kind {kind!r}; use test, bench or command.")
    if "path" in source:
        _relative(source["path"], f"{label}.path")
    for key in ("env", "profile", "executor"):
        if key in source:
            identifier(_text(source[key], f"{label}.{key}"))
    subjects = _strings(source.get("subjects", []), f"{label}.subjects")
    if len(subjects) != len(set(subjects)) or project in subjects:
        raise ForgeError(f"{label}.subjects must name distinct other projects.")
    for subject in subjects:
        if subject not in projects:
            raise ForgeError(f"{label} names unknown subject {subject!r}.")


def load(directory):
    directory = Path(directory).expanduser().resolve()
    path = directory / "library.yaml"
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read playlist library {path}: {exc}") from exc
    _fields(data, "Playlist library", ("schema_version", "kind", "id", "title", "shelves", "projects"))
    if type(data["schema_version"]) is not int or data["schema_version"] != 1 or data["kind"] != "playlist-library":
        raise ForgeError(f"Expected kind: playlist-library and schema_version: 1 in {path}.")
    identifier(_text(data["id"], "Library ID"))
    _text(data["title"], "Library title")
    shelves = {row["id"]: row for row in _records(data["shelves"], "shelves")}
    for shelf in shelves.values():
        if shelf["id"] == "library":
            raise ForgeError("library is reserved for whole-library profiles.")
        _fields(shelf, f"Shelf {shelf['id']}", ("id", "title"))
        _text(shelf["title"], f"Shelf {shelf['id']} title")
    projects = {row["id"]: row for row in _records(data["projects"], "projects")}
    for name, project in projects.items():
        _fields(project, f"Project {name}", ("id", "title", "shelf", "sources"),
                ("root", "distributions", "environment"))
        _text(project["title"], f"Project {name} title")
        if _text(project["shelf"], f"Project {name} shelf") not in shelves:
            raise ForgeError(f"Project {name} names unknown shelf {project['shelf']!r}.")
        if "root" in project:
            _relative(project["root"], f"Project {name}.root", allow_parent=True)
        if "environment" in project:
            identifier(_text(project["environment"], f"Project {name}.environment"))
        for dist in _strings(project.get("distributions", []), f"Project {name}.distributions"):
            if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", dist) is None:
                raise ForgeError(f"Project {name} has invalid distribution name {dist!r}.")
        for source in _records(project["sources"], f"sources of project {name}"):
            _validate_source(source, name, projects)
    return {**data, "path": str(path)}


def initialize(directory, name=None, title=None, *, demo=None):
    if demo not in {None, "sarah"}:
        raise ForgeError("Available demo: sarah.")
    directory = Path(directory).expanduser().resolve()
    name = identifier(name if name is not None else directory.name)
    title = _text(title if title is not None else "Research Workshop" if demo else name, "Library title")
    data = {"schema_version": 1, "kind": "playlist-library", "id": name, "title": title,
            "shelves": [], "projects": []}
    if demo:
        module = ["${python}", "-m", "jarvis_forge.playlist_demo"]
        data["shelves"].append({"id": "examples", "title": "Examples"})
        data["projects"].append({"id": "sarah", "title": "SARAH-Jarvis", "shelf": "examples",
                                 "sources": [{"id": "demo", "kind": "command", "list": [*module, "list"],
                                              "run": [*module, "run", "${request}", "${result_dir}"]}]})
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "library.yaml"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(data, stream, sort_keys=False, allow_unicode=True)
    except FileExistsError as exc:
        raise ForgeError(f"Playlist library already exists: {path}") from exc
    return {**data, "path": str(path)}


def _json(data):
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _write(path, data):
    content = _json(data)
    if not path.is_file() or path.read_bytes() != content.encode("utf-8"):
        write_text(path, content)


def _directory(record, label):
    if not isinstance(record, str) or "\0" in record or not Path(record).is_absolute():
        raise ForgeError(f"{label} must be an absolute local directory path.")
    return Path(record)


def selected(home, explicit=None):
    directory = explicit if explicit is not None else os.environ.get("JFORGE_LIBRARY")
    if directory:
        return load(directory)
    selection = home.root / "playlists/selection.json"
    if selection.is_file() or selection.is_symlink():
        return load(_directory(read_json(selection).get("directory"), "Selected library"))
    raise ForgeError("No playlist library selected.", hint="jforge playlist use <dir>")


def use(home, directory):
    library = load(directory)
    _write(home.root / "playlists/selection.json", {"directory": str(Path(library["path"]).parent)})
    return library


def _state(home, library, *, create=False):
    return named_path(home.root / "playlists", library["id"], create=create)


def _roots(path):
    return read_json(path) if path.exists() or path.is_symlink() else {}


def register_root(home, library, name, directory):
    if not any(project["id"] == name for project in library["projects"]):
        raise ForgeError(f"Unknown playlist project {name!r}.", hint="jforge playlist project list")
    path = Path(directory).expanduser().resolve()
    if not path.is_dir():
        raise ForgeError(f"Project checkout directory does not exist: {path}")
    state = _state(home, library, create=True)
    with file_lock(state / "roots.lock", blocking=True):
        _write(state / "roots.json", {**_roots(state / "roots.json"), name: str(path)})
    return {"id": name, "root": str(path), "root_source": "registered", "available": True}


def projects(home, library):
    registered = _roots(_state(home, library) / "roots.json")
    directory = Path(library["path"]).parent
    result = []
    for project in library["projects"]:
        name = project["id"]
        if name in registered:
            path = _directory(registered[name], f"Registered root for {name}").resolve()
            source = "registered"
        elif "root" in project:
            path = (directory / project["root"]).resolve()
            source = "relative"
        else:
            path, source = None, None
        result.append({**project, "root": str(path) if path else None, "root_source": source,
                       "available": path is not None and path.is_dir()})
    return result


def _test_cases(project, source):
    root = Path(project["root"])
    path = (root / source["path"]).resolve()
    if not path.is_relative_to(root):
        raise ForgeError(f"Test card path leaves the checkout: {source['path']}")
    try:
        sha256 = digest(path)
        card = testing.load_card(path)
        if digest(path) != sha256:
            raise ForgeError(f"Test card changed while being read: {path}")
    except (OSError, ValueError, TypeError) as exc:
        raise ForgeError(f"Cannot read test card {path}: {exc}") from exc
    if card["schema_version"] != 2:
        raise ForgeError(f"Playlist test sources require schema 2: {path}")
    if not Path(card["project"]).is_relative_to(root):
        raise ForgeError(f"Test card project directory leaves the checkout: {card['project']}")
    warnings = testing.validate_card(card)
    profiles = {name: set(testing.select(card, profile=name)["ids"]) for name in card["profiles"]}
    prefix = f"{project['id']}/{source['id']}/"
    shared = {"project": project["id"], "source": source["id"], "shelf": project["shelf"],
              "subjects": [project["id"], *source.get("subjects", [])], "sha256": sha256}
    cases = []
    for name in card["order"]:
        group = name in card["groups"]
        step = card["groups"][name] if group else card["checks"][name]
        cases.append({**shared, "id": prefix + name, "kind": "pytest" if group else "check",
                      "profiles": [profile for profile, ids in profiles.items() if name in ids],
                      "needs": [prefix + dependency for dependency in step.get("needs", [])],
                      "exclusive": step.get("exclusive", []), "timeout_seconds": step.get("timeout_seconds", 3600)})
    return cases, warnings


def inventory(home, library, *, shelf=None, project=None):
    entries = projects(home, library)
    if shelf is not None and not any(row["id"] == shelf for row in library["shelves"]):
        raise ForgeError(f"Unknown playlist shelf {shelf!r}.")
    if project is not None and not any(row["id"] == project for row in entries):
        raise ForgeError(f"Unknown playlist project {project!r}.", hint="jforge playlist project list")
    entries = [row for row in entries if (shelf is None or row["shelf"] == shelf)
               and (project is None or row["id"] == project)]
    cases, warnings, errors = [], [], []
    for row in entries:
        name = row["id"]
        if not row["available"]:
            warnings.append(f"Project {name} is unavailable: {row['root'] or 'no checkout registered'}")
            continue
        for source in row["sources"]:
            label = f"{name}/{source['id']}"
            if source["kind"] == "bench":
                warnings.append(f"{label}: bench sources are not supported yet.")
                continue
            try:
                if source["kind"] == "command":
                    found, notices = playlist_commands.inventory(row, source), []
                else:
                    found, notices = _test_cases(row, source)
            except ForgeError as exc:
                errors.append(f"{label}: {exc}")
            else:
                cases.extend(found)
                warnings.extend(f"{label}: {notice}" for notice in notices)
    return {"schema_version": 1, "library": library["id"], "path": library["path"],
            "projects": entries, "cases": cases, "warnings": warnings, "errors": errors}


def sync(home, library):
    state = _state(home, library, create=True)
    with file_lock(state / "roots.lock", blocking=True):
        result = inventory(home, library)
        if not result["errors"]:
            _write(state / "inventory.json", result)
    return result
