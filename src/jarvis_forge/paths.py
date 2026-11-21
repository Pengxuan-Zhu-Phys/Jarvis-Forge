"""Semantic directories and locked, ordered records; identity stays in JSON."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re

from .storage import ForgeError, now, read_json, write_json


def component(value):
    value = str(value)
    if (
        not value
        or value in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.-]*", value)
    ):
        raise ForgeError(f"Invalid managed directory name: {value!r}")
    return value


def semantic_name(name, version="dev"):
    return component(name) + "-" + component(version or "dev")


def reject_legacy(parent):
    path = Path(parent) / "metadata.json"
    if path.is_file() and read_json(path).get("kind") == "path-index":
        raise ForgeError(
            f"Unsupported allocated-name layout: {parent}",
            hint="Use a fresh Forge home: jforge init --home /path/to/new-forge",
        )


@contextmanager
def directory_lock(parent, name=".sequence.lock"):
    parent = Path(parent)
    if parent.is_symlink():
        raise ForgeError(f"Managed directory must not be a symbolic link: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(
        os.open(parent / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a+"
    ) as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def sequence(parent, action):
    """Reserve an ordered name; abandoned numbers are never recycled."""
    parent, action = Path(parent), component(action)
    reject_legacy(parent)
    with directory_lock(parent):
        path = parent / ".sequence.json"
        if path.is_symlink():
            raise ForgeError(f"Sequence metadata must not be a symbolic link: {path}")
        state = read_json(path) if path.is_file() else {"next": 1}
        if type(state.get("next")) is not int or state["next"] < 1:
            raise ForgeError(f"Invalid sequence metadata: {path}")
        existing = [
            int(match[1])
            for item in parent.iterdir()
            if (match := re.match(r"^(\d{4,})-", item.name))
        ]
        number = max(state["next"], max(existing, default=0) + 1)
        write_json(path, {"schema_version": 1, "kind": "sequence", "next": number + 1})
        return f"{number:04d}-{action}"


def metadata(path, kind, name, *, identity=None, version=None, **values):
    from .unified_profiles import host

    document = {
        "schema_version": 1,
        "kind": kind,
        "name": str(name),
        "identity": identity or {},
        "host": host(),
        "created_at": now(),
        "status": "ready",
        **values,
    }
    if version is not None:
        document["version"] = str(version)
    write_json(Path(path) / "metadata.json", document)
    return document


def named_path(parent, name, *, create=False):
    parent = Path(parent)
    reject_legacy(parent)
    result = parent / component(name)
    if result.is_symlink():
        raise ForgeError(f"Managed object must not be a symbolic link: {result}")
    return result


def object_path(parent, label, identity, *, create=False, version=None, code=None):
    """A software object has exactly one name-version path, never an allocated suffix."""
    parent = Path(parent)
    reject_legacy(parent)
    version = str(version or identity.get("version") or "dev")
    result = named_path(parent, semantic_name(label, version))
    marker = result / "metadata.json"
    if result.exists():
        if not marker.is_file() or read_json(marker).get("identity") != identity:
            raise ForgeError(
                f"Different inputs occupy {result}; rebuild explicitly in the original path."
            )
        return result
    if not create:
        return None
    with directory_lock(parent, ".objects.lock"):
        if marker.is_file():
            if read_json(marker).get("identity") != identity:
                raise ForgeError(
                    f"Different inputs occupy {result}; rebuild explicitly in the original path."
                )
        elif result.exists():
            raise ForgeError(f"Unregistered directory occupies {result}.")
        else:
            metadata(result, "software", label, version=version, identity=identity)
    return result


def record_path(parent, action, identity, *, create=False):
    """Find a JSON-backed record, or create a new ordered record under a lock."""
    parent = Path(parent)
    reject_legacy(parent)

    def find():
        for file in sorted(parent.glob("*/metadata.json"), reverse=True):
            row = read_json(file)
            if (
                row.get("kind") == "record"
                and row.get("name") == action
                and row.get("identity") == identity
            ):
                if file.parent.is_symlink():
                    raise ForgeError(
                        f"Managed record must not be a symbolic link: {file.parent}"
                    )
                return file.parent

    if found := find():
        return found
    if not create:
        return None
    with directory_lock(parent, ".records.lock"):
        if found := find():
            return found
        result = parent / sequence(parent, action)
        metadata(result, "record", action, identity=identity)
        return result


def logical_name(path):
    path = Path(path)
    marker = path / "metadata.json"
    return read_json(marker).get("name", path.name) if marker.is_file() else path.name


@contextmanager
def temporary_directory(parent, action):
    """Disposable work stays beneath its owner and has an ordered name."""
    import shutil

    path = Path(parent) / sequence(parent, action)
    metadata(path, "temporary", action)
    try:
        yield path
    finally:
        shutil.rmtree(path)
