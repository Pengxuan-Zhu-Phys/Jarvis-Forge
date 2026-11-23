"""Small, versioned adapter executed by `spack python`, never by the host CLI.

Spack owns DAGs, prefixes and environment semantics. JSON is the adapter boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
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


def snapshot(runtime_only=False, policy=None, overwrite=(), omit_cached_build_deps=False, *, emit=True, cached_index=False):
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
    if omit_cached_build_deps or cached_index:
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
        if not spec.external and spack.store.STORE.layout.projections.get("all") == "{name}-{version}" and (installed or Path(spec.prefix).exists()):
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
                "deprecated": bool(spec.package.versions.get(spec.version, {}).get("deprecated", False)),
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
    if runtime_only or policy is not None:document["inventory"]=store_inventory()
    if omit_cached_build_deps:
        document["cached_build_dependencies"] = "omit"
    if emit:
        print(json.dumps(document))
    return document


@contextmanager
def historical_build_tools(hashes):
    """Keep old build edges without offering their nodes to new solver inputs.

    Spack 1.2.2 registers concrete inputs twice, outside reuse.from selectors.
    Patch those two entry points only: a freshly validated external selector
    must still be able to offer the same tool hash for a new build-only use.
    """
    blocked = set(hashes)
    if not blocked:
        yield
        return
    import spack
    import spack.solver.asp as asp
    from jarvis_forge.spack_backend import ENGINE_VERSION

    extract = asp.Solver.__dict__.get('_extract_concrete_specs')
    define = asp.SpackSolverSetup.__dict__.get('define_concrete_input_specs')
    if (spack.__version__ != ENGINE_VERSION or not isinstance(extract, staticmethod)
            or not callable(extract.__func__) or not callable(define)):
        raise RuntimeError('Spack historical build-tool filtering requires the verified '
                           f'{ENGINE_VERSION} solver interface; running {spack.__version__}.')

    def eligible(spec):
        return not spec.concrete or spec.dag_hash() not in blocked

    def filtered_extract(specs):
        return [spec for spec in extract.__func__(specs) if eligible(spec)]

    def filtered_inputs(setup, specs, possible):
        for root in specs:
            if not eligible(root):
                raise RuntimeError(f'Historical build tool cannot be a preserved root: {root.name}')
            for spec in root.traverse():
                if spec.concrete and eligible(spec):
                    setup.register_concrete_spec(spec, possible)

    asp.Solver._extract_concrete_specs = staticmethod(filtered_extract)
    asp.SpackSolverSetup.define_concrete_input_specs = filtered_inputs
    try:
        yield
    finally:
        asp.Solver._extract_concrete_specs = extract
        asp.SpackSolverSetup.define_concrete_input_specs = define


def resolve_plan(options):
    """One engine import/solver session supplies DAG, cache status and plan."""
    import spack.main
    import spack.environment as ev
    import spack.spec
    import spack.traverse

    flags = options['flags']
    if flags[0] != 'concretize' or set(flags[1:]) - {'--fresh', '--force'}:
        raise ValueError('Unsupported concretization flags')
    output=''
    if not options.get('from_lock'):
        command = spack.main.SpackCommand('concretize')
        try:
            with historical_build_tools(options.get('historical_tool_hashes', [])):
                output = command(*flags[1:])
        except spack.main.SpackCommandError as exc:
            sys.stderr.write(command.output)
            raise SystemExit(exc.code) from exc
        except Exception:
            # SpackCommand captures descriptors even on failure. Preserve the
            # actual solver diagnostic instead of showing only its wrapper error.
            sys.stderr.write(command.output)
            raise
    dag = snapshot(emit=False)
    policy = options['policy']
    plan = snapshot(policy=policy, overwrite=options['overwrite'],
                    omit_cached_build_deps=options.get('omit_cached_build_deps', False),
                    cached_index=options.get('source_if_uncached', False), emit=False)
    if options.get('source_if_uncached') and not any(
            row.get('cached') and (not row['installed'] or row['name'] in options['overwrite'])
            for row in plan['packages']):
        policy = 'never'
        plan = snapshot(policy=policy, overwrite=options['overwrite'], emit=False)
    environment = ev.active_environment()
    tree = spack.spec.tree(environment.concrete_roots(), hashes=True, hashlen=7,
                           format=spack.spec.DISPLAY_FORMAT, key=spack.traverse.by_dag_hash)
    print(json.dumps({'dag':dag, 'plan':plan, 'policy':policy, 'tree':tree, 'concretize_output':output, 'inventory':store_inventory(options.get('database_checkpoint'))}))


def store_inventory(checkpoint=None):
    import spack.store
    db=spack.store.STORE.db
    rows=[]
    with db.read_transaction():
        if checkpoint and not Path(checkpoint).exists():
            with open(checkpoint,'x',encoding='utf-8') as stream:db._write_to_file(stream)
        for spec in db.query_local():
            rows.append({'name':spec.name,'version':str(spec.version),'hash':spec.dag_hash(),
                         'prefix':str(spec.prefix),'external':bool(spec.external),'node':spec.node_dict_with_hashes(),
                         'installed':True,'spec':str(spec)})
    return rows


def database_change(options):
    import spack.store
    db=spack.store.STORE.db
    with db.write_transaction():
        if options['action']=='restore':
            db._read_from_file(Path(options['checkpoint']))
        elif options['action']=='detach':
            for value in options['hashes']:
                for spec in db.get_by_hash_local(value,default=[]) or []:db.remove(spec)
        else:raise ValueError('Unknown database action')
    print(json.dumps({'status':'passed'}))


def prepare_backend():
    import spack.bootstrap

    with spack.bootstrap.ensure_bootstrap_configuration():
        spack.bootstrap.ensure_clingo_importable_or_raise()
        spack.bootstrap.ensure_gpg_in_path_or_raise()
    print(json.dumps({"solver": "ready", "signature_verification": "ready"}))


def trust_cache_keys(name):
    """Native key discovery can exit successfully even when no key was fetched."""
    import spack.binary_distribution as bd
    import spack.mirrors.mirror

    mirrors = spack.mirrors.mirror.MirrorCollection(binary=True)
    fingerprints = bd.get_keys(install=True, trust=True, yes_to_all=True, mirrors={name: mirrors[name]})
    if not fingerprints:
        raise SystemExit(f"Could not import any trusted signing keys from {name}.")
    print(json.dumps({"fingerprints": fingerprints, "mirror": name}))


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
    if sys.argv[1] == "build-tool-candidates":
        from jarvis_forge.build_tool_reuse import candidates
        print(json.dumps(candidates(json.loads(sys.argv[2]))))
    elif sys.argv[1] == "resolve-plan":
        resolve_plan(json.loads(sys.argv[2]))
    elif sys.argv[1] == "database":
        database_change(json.loads(sys.argv[2]))
    elif sys.argv[1] == "inventory":
        print(json.dumps({"packages":store_inventory(sys.argv[2] if len(sys.argv)>2 else None)}))
    elif sys.argv[1] == "prepare-backend":
        prepare_backend()
    elif sys.argv[1] == "trust-cache-keys":
        trust_cache_keys(sys.argv[2])
    elif sys.argv[1] in {"snapshot", "installed"}:
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
