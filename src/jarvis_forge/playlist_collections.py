"""Derived and user-authored playlists, shared by CLI and dashboard."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from . import playlist_library as library, testing
from .builds import file_lock
from .storage import ForgeError, digest, identifier, now, read_json


def _ids(values):
    return list(dict.fromkeys(values))


def derive(selected, inventory):
    projects = {item["id"]: item for item in selected["projects"]}
    profiles = defaultdict(list)
    for case in inventory["cases"]:
        for profile in case["profiles"]:
            key = f"{case['project']}/{case['source']}@{profile}"
            profiles[key].append(case["id"])
            if profile in {"daily", "acceptance", "release"}:
                profiles[f"{case['shelf']}@{profile}"].append(case["id"])
                profiles[f"library@{profile}"].append(case["id"])
    result = {}
    for key, members in sorted(profiles.items()):
        scope, profile = key.rsplit("@", 1)
        project = projects.get(scope.split("/", 1)[0]) if "/" in scope else None
        result[key] = {"id": key, "stored": False, "title": profile.capitalize(), "profile": profile,
                       "project": project["id"] if project else None,
                       "shelf": project["shelf"] if project else scope if scope != "library" else None,
                       "members": members}
    return result


def _directory(selected):
    directory = Path(selected["path"]).parent / "playlists"
    if directory.is_symlink():
        raise ForgeError(f"Playlist storage cannot be a symlink: {directory}")
    return directory


def _path(selected, name):
    path = _directory(selected) / f"{identifier(name)}.json"
    if path.is_symlink():
        raise ForgeError(f"Playlist file cannot be a symlink: {path}")
    return path


def _validate(spec, name, selected):
    library._fields(spec, f"Playlist {name}",
                    ("schema", "id", "title", "entries", "frozen", "changes"),
                    ("goal", "shelf", "environment"))
    if type(spec["schema"]) is not int or spec["schema"] != 1 or spec["id"] != name:
        raise ForgeError(f"Playlist filename and schema do not match: {name}")
    library._text(spec["title"], "Playlist title")
    for key in ("goal", "environment", "shelf"):
        if key in spec:
            library._text(spec[key], f"Playlist {key}")
    if "environment" in spec:
        identifier(spec["environment"])
    if "shelf" in spec and spec["shelf"] not in {shelf["id"] for shelf in selected["shelves"]}:
        raise ForgeError(f"Unknown playlist shelf {spec['shelf']!r}.")
    if spec["frozen"] is not None and not isinstance(spec["frozen"], dict):
        raise ForgeError(f"Invalid freeze record in {name}.")
    if not isinstance(spec["entries"], list) or not isinstance(spec["changes"], list):
        raise ForgeError(f"Playlist {name} entries and changes must be lists.")
    seen = set()
    for entry in spec["entries"]:
        key = _entry_key(entry)
        if key in seen:
            raise ForgeError(f"Duplicate playlist entry: {key[1]}")
        seen.add(key)
    for change in spec["changes"]:
        library._fields(change, "Change", ("at", "op", "entry", "reason"))
        library._text(change["at"], "Change time")
        library._text(change["reason"], "Change reason")
        if change["op"] not in {"add", "remove"}:
            raise ForgeError(f"Invalid playlist change operation: {change['op']!r}")
        _entry_key(change["entry"])


def _entry_key(entry):
    library._fields(entry, "Playlist entry", (), ("case", "profile", "note"))
    fields = set(entry) & {"case", "profile"}
    if len(fields) != 1:
        raise ForgeError("Each playlist entry must have one case or profile.")
    field = fields.pop()
    if "note" in entry:
        library._text(entry["note"], "Playlist entry note")
    return field, library._text(entry[field], "Playlist entry ID")


def _closure(ids, cases):
    result, seen = [], set()

    def visit(case_id):
        if case_id not in cases:
            raise ForgeError(f"Unknown or unavailable playlist case: {case_id}")
        if case_id in seen:
            return
        seen.add(case_id)
        for parent in cases[case_id].get("needs", []):
            visit(parent)
        result.append(case_id)

    for case_id in ids:
        visit(case_id)
    return result


def resolve(spec, selected, inventory, derived):
    _validate(spec, spec["id"], selected)
    cases = {case["id"]: case for case in inventory["cases"]}
    members, direct = [], []
    for entry in spec["entries"]:
        field, key = _entry_key(entry)
        if field == "case":
            members.append(key)
            direct.append(key)
        else:
            if key not in derived:
                raise ForgeError(f"Unknown or unavailable playlist profile: {key}")
            members.extend(derived[key]["members"])
    members = _closure(members, cases)
    subjects = _ids(subject for key in members for subject in cases[key]["subjects"])
    combined = len(subjects) > 1
    if combined and not (spec.get("goal") and spec.get("environment")):
        raise ForgeError("Combined playlists require a goal and a named environment.")
    _check_groups(direct, inventory)
    return {**spec, "stored": True, "kind": "combined" if combined else "project",
            "subjects": subjects, "members": members}


def _check_groups(ids, inventory):
    """Reuse the test selector's file and marker rules for explicit members."""
    by_source = defaultdict(list)
    available = {row["id"]: row for row in inventory["cases"]}
    projects = {row["id"]: row for row in inventory["projects"]}
    for case_id in ids:
        case = available[case_id]
        if case["kind"] in {"check", "pytest"}:
            by_source[(case["project"], case["source"])].append(case_id.rsplit("/", 1)[1])
    for (pid, sid), names in by_source.items():
        project = projects[pid]
        source = next(item for item in project["sources"] if item["id"] == sid)
        card_path = (Path(project["root"]) / source["path"]).resolve()
        if not card_path.is_relative_to(project["root"]):
            raise ForgeError(f"Playlist card leaves the checkout: {card_path}")
        card = testing.load_card(card_path)
        testing.check_intersections(card, testing.select(card, groups=names)["ids"])


def catalog(home, selected, *, inventory=None):
    if inventory is None:
        inventory = library.inventory(home, selected)
    derived = derive(selected, inventory)
    result, errors = list(derived.values()), []
    for path in sorted(_directory(selected).glob("*.json")):
        try:
            path = _path(selected, path.stem)
            record = read_json(path)
            _validate(record, path.stem, selected)
            if path.read_bytes() != library._json(record).encode("utf-8"):
                errors.append(f"{path.stem}: saved playlist must use canonical JSON.")
        except (ForgeError, OSError, ValueError) as error:
            errors.append(str(error))
            continue
        try:
            item = resolve(record, selected, inventory, derived)
        except ForgeError as error:
            item = {**record, "stored": True, "members": [], "problem": str(error)}
            errors.append(f"{path.stem}: {error}")
        result.append({**item, "digest": digest(path)})
    return result, errors


def show(home, selected, name):
    items, _ = catalog(home, selected)
    for item in items:
        if item["id"] == name:
            return item
    raise ForgeError(f"Unknown playlist: {name}", hint="jforge playlist list")


def _lock(home, selected):
    return file_lock(library._state(home, selected) / "playlists.lock", blocking=True)


def create(home, selected, name, *, title, entries, goal=None, shelf=None, environment=None):
    path = _path(selected, name)
    spec = {"schema": 1, "id": name, "title": title, "entries": entries, "frozen": None, "changes": []}
    spec.update({key: value for key, value in (("goal", goal), ("shelf", shelf), ("environment", environment))
                 if value is not None})
    with _lock(home, selected):
        if path.exists():
            raise ForgeError(f"Playlist already exists: {name}")
        inventory = library.inventory(home, selected)
        item = resolve(spec, selected, inventory, derive(selected, inventory))
        library._write(path, spec)
        return {**item, "digest": digest(path)}


def edit(home, selected, name, op, entries, *, reason=None, expected=None):
    path = _path(selected, name)
    if op not in {"add", "remove"} or not entries:
        raise ForgeError("Choose at least one case or profile to add or remove.")
    with _lock(home, selected):
        if expected is not None and (not path.exists() or digest(path) != expected):
            raise ForgeError("Playlist changed since it was opened. Reload and try again.", code=409)
        spec = read_json(path)
        _validate(spec, name, selected)
        if spec["frozen"] is not None:
            library._text(reason, "Reason for changing a frozen playlist")
        previous = {_entry_key(entry): entry for entry in spec["entries"]}
        keys = [_entry_key(entry) for entry in entries]
        if len(set(keys)) != len(keys):
            raise ForgeError("The same entry was provided more than once.")
        if op == "add":
            duplicate = next((key[1] for key in keys if key in previous), None)
            if duplicate:
                raise ForgeError(f"Playlist already contains {duplicate}.")
            changed = entries
            spec["entries"] += entries
        else:
            missing = next((key[1] for key in keys if key not in previous), None)
            if missing:
                raise ForgeError(f"Playlist does not contain {missing}.")
            changed = [previous.pop(key) for key in keys]
            spec["entries"] = list(previous.values())
        if spec["frozen"] is not None:
            spec["changes"] += [{"at": now(), "op": op, "entry": entry, "reason": reason} for entry in changed]
        inventory = library.inventory(home, selected)
        item = resolve(spec, selected, inventory, derive(selected, inventory))
        library._write(path, spec)
        return {**item, "digest": digest(path)}
