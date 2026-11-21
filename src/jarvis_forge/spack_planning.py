"""Per-operation reuse of a combined Spack resolution process, never a solver cache."""

import json
from pathlib import Path

from .storage import ForgeError, digest, read_json, write_json


def stamp(envdir):
    directory = Path(envdir)
    paths = [directory / "spack.yaml", directory / "spack.lock"]
    paths += sorted((directory / ".jforge-config").glob("*.yaml"))
    return {str(path.relative_to(directory)): digest(path) for path in paths if path.is_file()}


def save(op, envdir, request, output):
    try:
        value = json.loads(output)
        if not isinstance(value["dag"]["packages"], list) or not isinstance(value["plan"]["packages"], list):
            raise ValueError("Missing package arrays")
        if not isinstance(value["tree"], str):
            raise ValueError("Missing dependency tree")
        if value["policy"] not in {"auto", "only", "never"}:
            raise ValueError("Invalid cache policy")
    except (ValueError, TypeError, KeyError) as exc:
        raise ForgeError("Spack returned an invalid combined dependency plan.", code=1) from exc
    write_json(Path(op) / "planning.json", {"schema_version": 1, "environment": str(envdir),
               "stamp": stamp(envdir), "request": request, "result": value})


def current(op, envdir):
    path = Path(op) / "planning.json"
    if not path.is_file():
        return None
    value = read_json(path)
    if value.get("environment") != str(envdir) or value.get("stamp") != stamp(envdir):
        return None
    return value


def query(op, envdir, args, label):
    if (envdir is None or len(args) < 3 or args[0] != "python"
            or Path(args[1]).name != "spack_bridge.py"):
        return None
    value = current(op, envdir)
    if value is None:
        return None
    action = args[2]
    request, result = value["request"], value["result"]
    if action == "snapshot" and label.startswith("dag"):
        return json.dumps(result["dag"])
    if action == "tree":
        return result["tree"]
    if action == "installation-plan" and len(args) in {5, 6}:
        if (args[3] == result["policy"] and json.loads(args[4]) == request["overwrite"]
                and (len(args) == 6) == request.get("omit_cached_build_deps", False)):
            return json.dumps(result["plan"])
    return None
