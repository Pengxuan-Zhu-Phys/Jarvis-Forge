"""Small, versioned adapter executed by `spack python`, never by the host CLI.

Spack owns DAGs, prefixes and environment semantics. JSON is the adapter boundary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys


def include_build_dependencies(policy, *, external=False, installed=False, cached=False, overwrite=False):
    """Return whether installing this spec also requires its build dependencies.

    Binary-only installs never do. A source install does, except for a spec that
    is already installed and is not being overwritten. An automatic install
    builds from source only when the spec is not installed and has no binary:
    a cached spec is installed from that binary and does not need the tools
    that created the archive.
    """
    if policy not in {"only", "never", "auto"}:
        raise ValueError(policy)
    if external or policy == "only":
        return False
    if overwrite:
        return True
    if installed:
        return False
    if policy == "never":
        return True
    return not cached


def installation_specs(environment, roots, policy, overwrite, *, omit_cached_build_deps=False):
    """Ask the pinned Spack installer which dependencies this request needs.

    With ``omit_cached_build_deps``, a spec that has a binary does not contribute
    its build dependencies. Spack's automatic policy still queues those tools for
    every spec that is not installed yet, and a package-name store cannot hold
    both copies. Forge installs the binaries first, then builds only the specs
    that have no binary; this walk is that installation.
    """
    if omit_cached_build_deps:
        import spack.binary_distribution as bd
        import spack.deptypes as dt

        bd.load_buildcache_index()
        index = bd.BINARY_INDEX
        overwrite_names = set(overwrite)
        selected = {}

        def visit(spec):
            digest = spec.dag_hash()
            if digest in selected:
                return
            selected[digest] = spec
            depflag = dt.LINK | dt.RUN
            if include_build_dependencies(
                policy,
                external=bool(spec.external),
                installed=bool(spec.installed),
                cached=digest in index,
                overwrite=spec.name in overwrite_names,
            ):
                depflag |= dt.BUILD
            for dep in spec.dependencies(deptype=depflag):
                visit(dep)

        for root in roots:
            visit(root)
        return selected.values()

    from spack.installer import BuildRequest

    overwrite_hashes = [
        spec.dag_hash() for spec in environment.all_specs() if spec.name in overwrite
    ]
    policies = {"only": "cache_only", "never": "cache_never", "auto": "auto"}
    install_args = {
        "root_policy": policies[policy],
        "dependencies_policy": policies[policy],
        "overwrite": overwrite_hashes,
    }
    selected = {}
    for root in roots:
        selected[root.dag_hash()] = root
        request = BuildRequest(root.package, dict(install_args))
        for spec in request.traverse_dependencies():
            selected[spec.dag_hash()] = spec
    return selected.values()


def snapshot(runtime_only=False, policy=None, overwrite=(), omit_cached_build_deps=False):
    import spack.environment as ev
    import spack.traverse
    import spack.store
    import spack.user_environment as uenv

    environment = ev.active_environment()
    if environment is None:
        raise RuntimeError("An active Spack environment is required")
    roots = list(environment.concrete_roots())
    root_hashes = {s.dag_hash() for s in roots}
    packages = []
    binary_index = None
    if omit_cached_build_deps:
        import spack.binary_distribution as bd

        bd.load_buildcache_index()
        binary_index = bd.BINARY_INDEX
    # A binary installation may omit tools used only to build the archive.
    if policy is not None:
        specs = installation_specs(
            environment, roots, policy, overwrite, omit_cached_build_deps=omit_cached_build_deps
        )
    else:
        specs = (spack.traverse.traverse_nodes(roots, deptype=("link", "run"))
                 if runtime_only else environment.all_specs())
    for spec in specs:
        if omit_cached_build_deps:
            installed = bool(spec.external or spec.installed)
        else:
            installed = bool(spec.external or spack.store.STORE.db.query_one(spec))
        prefix_matches = False
        if not spec.external and spack.store.STORE.layout.projections == {"all": "{name}"} and (installed or Path(spec.prefix).exists()):
            # Read and verify .spack/spec.json through Spack itself. Forge does
            # not introduce its own prefix metadata format or overwrite policy.
            from spack.directory_layout import DirectoryLayoutError
            try:
                spack.store.STORE.layout.ensure_installed(spec)
                prefix_matches = True
            except DirectoryLayoutError:
                if installed:
                    raise
        packages.append(
            {
                "name": spec.name,
                "version": str(spec.version),
                "hash": spec.dag_hash(),
                "prefix": str(spec.prefix),
                "external": bool(spec.external),
                "installed": installed,
                "prefix_matches": prefix_matches,
                "root": spec.dag_hash() in root_hashes,
                "spec": str(spec),
                "node": spec.node_dict_with_hashes(),
            }
        )
        if binary_index is not None:
            packages[-1]["cached"] = (not spec.external) and spec.dag_hash() in binary_index
    modifications = []
    if all(row["installed"] for row in packages):
        for op in uenv.environment_modifications_for_specs(*roots):
            row = {"type": type(op).__name__, "name": op.name}
            for field in ["value", "separator", "force", "raw"]:
                if hasattr(op, field):
                    row[field] = getattr(op, field)
            modifications.append(row)
    document = {"packages": packages, "modifications": modifications}
    if omit_cached_build_deps:
        document["cached_build_dependencies"] = "omit"
    print(json.dumps(document))


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


def dependency_tree():
    import spack.environment as ev
    import spack.spec
    import spack.traverse

    environment = ev.active_environment()
    if environment is None:
        raise RuntimeError("An active Spack environment is required")
    print(spack.spec.tree(environment.concrete_roots(), hashes=True, hashlen=7,
                          format=spack.spec.DISPLAY_FORMAT, key=spack.traverse.by_dag_hash))


if __name__ == "__main__":
    if sys.argv[1] in {"snapshot", "installed"}:
        snapshot(runtime_only=sys.argv[1] == "installed")
    elif sys.argv[1] == "installation-plan":
        if len(sys.argv) > 5 or (len(sys.argv) == 5 and sys.argv[4] != "omit-cached-build-deps"):
            raise SystemExit("Unknown installation-plan option")
        snapshot(
            policy=sys.argv[2],
            overwrite=json.loads(sys.argv[3]),
            omit_cached_build_deps=len(sys.argv) == 5,
        )
    elif sys.argv[1] == "tree":
        dependency_tree()
    elif sys.argv[1] == "render":
        render(sys.argv[2])
    else:
        raise SystemExit("Unknown Spack bridge operation")
