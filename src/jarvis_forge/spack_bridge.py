"""Small, versioned adapter executed by `spack python`, never by the host CLI.

Spack owns DAGs, prefixes and environment semantics. JSON is the adapter boundary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys


def snapshot():
    import spack.environment as ev
    import spack.store
    import spack.user_environment as uenv

    environment = ev.active_environment()
    if environment is None:
        raise RuntimeError("An active Spack environment is required")
    roots = list(environment.concrete_roots())
    root_hashes = {s.dag_hash() for s in roots}
    packages = []
    for spec in environment.all_specs():
        installed = bool(spec.external or spack.store.STORE.db.query_one(spec))
        packages.append(
            {
                "name": spec.name,
                "version": str(spec.version),
                "hash": spec.dag_hash(),
                "prefix": str(spec.prefix),
                "external": bool(spec.external),
                "installed": installed,
                "root": spec.dag_hash() in root_hashes,
                "spec": str(spec),
                "node": spec.node_dict_with_hashes(),
            }
        )
    modifications = []
    if all(row["installed"] for row in packages):
        for op in uenv.environment_modifications_for_specs(*roots):
            row = {"type": type(op).__name__, "name": op.name}
            for field in ["value", "separator", "force", "raw"]:
                if hasattr(op, field):
                    row[field] = getattr(op, field)
            modifications.append(row)
    print(json.dumps({"packages": packages, "modifications": modifications}))


def render(path):
    from spack.util import environment as env

    allowed = {
        name: getattr(env, name)
        for name in [
            "SetEnv",
            "UnsetEnv",
            "AppendFlagsEnv",
            "RemoveFlagsEnv",
            "SetPath",
            "AppendPath",
            "PrependPath",
            "RemovePath",
            "RemoveFirstPath",
            "RemoveLastPath",
            "DeprioritizeSystemPaths",
            "PruneDuplicatePaths",
        ]
    }
    modifications = env.EnvironmentModifications()
    for row in json.loads(Path(path).read_text()):
        if row["type"] not in allowed or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", row["name"]
        ):
            raise RuntimeError("Unsupported Spack environment modification")
        cls = allowed[row["type"]]
        if row["type"] == "SetPath":
            op = cls(
                row["name"],
                row["value"].split(row.get("separator", os.pathsep)),
                separator=row.get("separator", os.pathsep),
            )
        elif "value" in row:
            kwargs = {
                key: row[key]
                for key in ["separator", "force", "raw"]
                if key in row and (row["type"] != "SetEnv" or key != "separator")
            }
            op = cls(row["name"], row["value"], **kwargs)
        else:
            op = cls(row["name"])
        modifications.env_modifications.append(op)
    print(modifications.shell_modifications("sh"))


if __name__ == "__main__":
    if sys.argv[1] == "snapshot":
        snapshot()
    elif sys.argv[1] == "render":
        render(sys.argv[2])
    else:
        raise SystemExit("Unknown Spack bridge operation")
