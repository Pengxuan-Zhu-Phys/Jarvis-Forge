"""Persistent dashboard preferences in the selected Forge home."""

from pathlib import Path

from .. import playlist_library
from ..builds import file_lock
from ..storage import ForgeError, read_json, write_json


def path(home):
    return home.root / "dashboard" / "config.json"


def load(home):
    location = path(home)
    if not location.exists():
        return {"schema_version": 1, "library_path": None, "port": 8787}
    value = read_json(location)
    playlist_library._fields(value, "Dashboard configuration", ("schema_version", "library_path", "port"))
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ForgeError(f"Unsupported dashboard configuration schema: {location}")
    directory = value["library_path"]
    if directory is not None and (not isinstance(directory, str) or not directory
                                  or "\0" in directory or not Path(directory).is_absolute()):
        raise ForgeError(f"Dashboard library_path must be an absolute directory: {location}")
    _port(value["port"])
    return value


def _port(value):
    if type(value) is not int or not 0 <= value <= 65535:
        raise ForgeError("Dashboard port must be an integer from 0 to 65535.")
    return value


def configure(home, *, library=None, port=None):
    home.require_initialized()
    with file_lock(home.root / "dashboard" / "config.lock", blocking=True):
        value = load(home)
        if library is not None:
            selected = playlist_library.load(library)
            value["library_path"] = str(Path(selected["path"]).parent)
        if port is not None:
            value["port"] = _port(port)
        write_json(path(home), value)
    return value


def resolve(home, *, library=None, port=None):
    value = load(home)
    directory = library if library is not None else value["library_path"]
    try:
        selected = playlist_library.selected(home, directory)
    except ForgeError as exc:
        if directory is None:
            raise ForgeError("No dashboard library configured.",
                             hint="jforge dashboard config --library /absolute/path/to/library") from exc
        raise
    return Path(selected["path"]).parent, _port(value["port"] if port is None else port)
