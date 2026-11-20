"""Short physical names; full identities live in atomic JSON indexes."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from .storage import ForgeError, now, read_json, write_json

MAX_NAME = 24


def short_label(value):
    if re.fullmatch(r"[a-fA-F0-9]{7,}", str(value)):
        return "item"
    words = re.findall(r"[a-z0-9]+", str(value).lower())
    label = ""
    for word in words:
        candidate = "-".join(filter(None, (label, word)))
        if len(candidate) > 15:
            break
        label = candidate
    return label or (words[0][:15] if words else "item")


def _key(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _index(parent):
    file = parent / "metadata.json"
    if file.is_symlink():
        raise ForgeError(f"Managed path index must not be a symbolic link: {file}")
    if not file.exists():
        return {"schema_version": 1, "kind": "path-index", "next_id": 1, "objects": {}}
    value = read_json(file)
    if (value.get("schema_version") != 1 or value.get("kind") != "path-index"
            or type(value.get("next_id")) is not int or value["next_id"] < 1
            or not isinstance(value.get("objects"), dict)):
        raise ForgeError(f"Invalid short-path index: {file}")
    names = set()
    for key, entry in value["objects"].items():
        if (not isinstance(entry, dict) or "identity" not in entry
                or _key(entry["identity"]) != key or not isinstance(entry.get("name"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", entry["name"])
                or len(entry["name"]) > MAX_NAME or entry["name"] in names):
            raise ForgeError(f"Invalid short-path record: {file}")
        names.add(entry["name"])
    return value


def physical_name(parent, label, identity, *, create=False, code="A"):
    """Look up a name without writing, or reserve it under a shared filesystem lock."""
    parent = Path(parent)
    key = _key(identity)

    def lookup(value):
        entry = value["objects"].get(key)
        if entry is None:
            return None
        name = entry.get("name", "") if isinstance(entry, dict) else ""
        if (not isinstance(entry, dict) or entry.get("identity") != identity
                or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", name)
                or len(name) > MAX_NAME):
            raise ForgeError(f"Invalid short-path record: {parent / 'metadata.json'}")
        if (parent / name).is_symlink():
            raise ForgeError(f"Managed object must not be a symbolic link: {parent / name}")
        return name

    if not create:
        return lookup(_index(parent))
    if parent.is_symlink():
        raise ForgeError(f"Managed path index must not be a symbolic link: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / ".paths.lock"
    if lock_path.is_symlink():
        raise ForgeError(f"Managed path lock must not be a symbolic link: {lock_path}")
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = _index(parent)
        if found := lookup(value):
            return found
        number = value["next_id"]
        names = {entry.get("name") for entry in value["objects"].values() if isinstance(entry, dict)}
        if not re.fullmatch("[A-Z]", code):
            raise ForgeError("A physical instance code must be one uppercase letter.")
        stem = short_label(label)
        alphabet = code + "".join(letter for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if letter != code)
        def available(candidate):
            return candidate not in names and not (parent / candidate).exists() and not (parent / candidate).is_symlink()
        # Keep the meaningful name and prefer a two-character suffix. When all
        # 234 suffixes for that name have been used, grow only
        # the decimal part; never recycle history or introduce padded zeros.
        name = next((f"{stem}-{letter}{digit}" for letter in alphabet for digit in range(1, 10)
                     if available(f"{stem}-{letter}{digit}")), None)
        if name is None:
            digit = 10
            while not available(f"{stem}-{code}{digit}"):
                digit += 1
            name = f"{stem}-{code}{digit}"
            if len(name) > MAX_NAME:
                raise ForgeError(f"Short-path sequence is exhausted: {parent}")
        value["next_id"] = number + 1
        value["objects"][key] = {"name": name, "identity": identity, "created_at": now(), "label": str(label)}
        write_json(parent / "metadata.json", value)
        return name


def object_path(parent, label, identity, *, create=False, code="A"):
    name = physical_name(parent, label, identity, create=create, code=code)
    return Path(parent) / name if name else None


def named_path(parent, name, *, create=False):
    """Keep short declared names; index long logical names without truncation.

    An unregistered read returns a non-materialized preview location. Writers
    must request creation before using it. No managed object can reserve that
    preview name through this interface.
    """
    parent = Path(parent)
    if (len(name.encode("ascii", errors="replace")) <= MAX_NAME and name.isascii()
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
            and not re.fullmatch(r"[a-fA-F0-9]{7,}", name)):
        return parent / name
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ForgeError(f"Invalid managed directory name: {name!r}")
    return object_path(parent, name, {"name": name}, create=create) or parent / ".unbuilt"


def logical_name(path):
    """Recover a declared name for inventories and pruning, never parse a slot."""
    path = Path(path)
    for entry in _index(path.parent)["objects"].values():
        if entry.get("name") == path.name and isinstance(entry.get("identity"), dict):
            return entry["identity"].get("name", path.name)
    return path.name
