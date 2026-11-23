"""Borrow executable build tools without exposing their old runtime DAG as libraries."""
from pathlib import Path
import copy
import os
import re
import shlex
import shutil
import subprocess

from .storage import ForgeError, digest, read_json

PYTHON = {"python", "python-venv"}


def _inside_store(prefix, store):
    path, directory = Path(prefix), Path(store)
    # A retargeted symlink does not turn an old Forge tool into an unrelated
    # external: it still needs source validation (and rejection if it escaped).
    return path.is_relative_to(directory) or path.resolve().is_relative_to(directory.resolve())


def lock_packages(file):
    """Read full concrete DAGs, including build edges absent from inventory."""
    document = read_json(Path(file))
    roots = {r['hash'] for r in document.get('roots', [])}
    return [dict(name=node['name'], version=str(node['version']), hash=key,
                 root=key in roots, external=bool(node.get('external')),
                 prefix=(node.get('external') or {}).get('path', ''), node=node)
            for key, node in document.get('concrete_specs', {}).items()]


def historical_references(home, packages, evidence=None):
    """The lock is authoritative; evidence can enrich, but never hide, a ref."""
    from .unified_profiles import record
    from .toolchains import suite
    fixed = suite((record(home) or {}).get('toolchain'))
    store = home.root / 'tools/pkg'
    sources = {row['hash']: row for row in (evidence or {}).get('retained', [])}
    for row in (evidence or {}).get('selected', []):
        for key in row.get('external_hashes', []):
            sources[key] = row
    result = []
    for row in packages:
        if not row.get('external') or not _inside_store(row['prefix'], store):
            continue
        # Fixed profile tools have their own binding and validation, including
        # explicit roots such as CBS's CMake request. They are not optional loans.
        if row['name'] in fixed:
            continue
        ref = {k: row[k] for k in ('hash', 'name', 'version', 'prefix')}
        ref['consumers'] = sorted({p['hash'] for p in packages
                                  if any(e['hash'] == row['hash'] for e in p.get('node', {}).get('dependencies', []))})
        if sources.get(row['hash'], {}).get('source_hash'):
            ref['source_hash'] = sources[row['hash']]['source_hash']
        result.append(ref)
    return sorted(result, key=lambda row: row['hash'])


def history(home, environment, *, preserve=False):
    """Gather exclusions from a committed lock and its durable evidence."""
    if not environment:
        return [], set()
    directory = Path(environment)
    evidence_file = directory / 'build-tool-reuse.json'
    evidence = read_json(evidence_file) if evidence_file.is_file() else {}
    packages = lock_packages(directory / 'spack.lock') if (directory / 'spack.lock').is_file() else []
    refs = historical_references(home, packages, evidence)
    if preserve:
        by_hash = {r['hash']: r for r in packages}
        old = {r['hash'] for r in refs}
        for root in (r for r in packages if r['root']):
            pending, seen = [root['hash']], set()
            while pending:
                key = pending.pop()
                if key in seen:
                    continue
                seen.add(key)
                if key in old:
                    raise ReuseRejected(f"Installed root {root['name']} uses historical tool {by_hash[key]['name']} "
                                        "as a root or link/runtime dependency.", code=1,
                                        hint=f"Explicitly rebuild {root['name']} with native application dependencies.")
                if key not in by_hash:
                    raise ReuseRejected(f"Installed root {root['name']} has an incomplete locked dependency graph.", code=1)
                pending.extend(e['hash'] for e in by_hash[key].get('node', {}).get('dependencies', [])
                               if set(e.get('parameters', {}).get('deptypes', [])) & {'link', 'run'})
    hashes = {r['hash'] for r in refs}
    hashes.update(r['hash'] for r in evidence.get('retained', []))
    hashes.update(h for r in evidence.get('selected', []) for h in r.get('external_hashes', []))
    return refs, hashes


def role_reason(row, packages):
    if row.get('root'):
        return 'root'
    for parent in packages:
        for edge in parent.get('node', {}).get('dependencies', []):
            if edge['hash'] == row['hash'] and set(edge.get('parameters', {}).get('virtuals', [])) & {'c', 'cxx', 'fortran'}:
                return 'compiler'
    _, runtime = graph_roles(packages)
    if runtime is None or row['hash'] in runtime:
        return 'runtime'
    return 'link-edge'


def graph_roles(packages):
    """A root's link/run closure and every incoming link/compiler edge are barriers."""
    rows = {p["hash"]: p for p in packages}
    roots = [p["hash"] for p in packages if p.get("root")]
    if not roots:
        return set(), None
    runtime, reachable, incoming, forbidden = set(), set(), set(), set()
    def walk(todo, seen, types):
        while todo:
            key = todo.pop()
            if key in seen:
                continue
            if key not in rows:
                raise ValueError("Incomplete dependency graph")
            seen.add(key)
            for edge in rows[key].get("node", {}).get("dependencies", []):
                params = edge.get("parameters", {})
                kinds = set(params.get("deptypes", []))
                if not kinds or kinds - {"build", "link", "run", "test"}:
                    raise ValueError("Unknown dependency edge")
                if kinds & types:
                    todo.append(edge["hash"])
    try:
        walk(list(roots), runtime, {"link", "run"})
        walk(list(roots), reachable, {"build", "link", "run"})
        for key in reachable:
            for edge in rows[key].get("node", {}).get("dependencies", []):
                kinds = set(edge["parameters"]["deptypes"])
                if kinds & {"build", "run"}:
                    incoming.add(edge["hash"])
                if "link" in kinds or set(edge["parameters"].get("virtuals", [])) & {"c", "cxx", "fortran"}:
                    forbidden.add(edge["hash"])
    except (ValueError, KeyError, TypeError):
        return set(), None
    return {h for h in (reachable & incoming) - runtime - forbidden if rows[h]["name"] not in PYTHON}, runtime


def build_only(packages):
    return graph_roles(packages)[0]


def matches(requested, source):
    a, b = requested.get("node", {}), source.get("node", {})
    return (requested["name"] == source["name"] and requested["version"] == source["version"]
            and bool(a.get("package_hash")) and a.get("package_hash") == b.get("package_hash")
            and a.get("parameters") == b.get("parameters") and a.get("patches") == b.get("patches")
            and a.get("arch") == b.get("arch"))


def check_files(selection):
    for row in selection:
        try:
            for file, expected in row["checks"].items():
                path = Path(file)
                resolved = row.get("resolved_paths", {}).get(file)
                if (not path.is_file() or digest(path) != expected
                        or resolved is not None and str(path.resolve()) != resolved):
                    raise ReuseRejected(f"Reusable build tool changed: {path}", code=1)
            if any(not Path(p).is_dir() for p in row["runtime_prefixes"]):
                raise ReuseRejected(f"Reusable build tool runtime disappeared: {row['name']}", code=1)
        except OSError as exc:
            raise ReuseRejected(f"Cannot validate reusable build tool {row['name']}: {exc}", code=1) from exc


def _executables(prefix, runtime_prefixes):
    files = sorted({p for folder in ("bin", "sbin") for p in (prefix / folder).glob("*")
                    if p.is_file() and os.access(p, os.X_OK)})
    if not files:
        raise ValueError("Not an executable tool")
    env = {k: v for k, v in os.environ.items() if k not in {"LD_LIBRARY_PATH", "LD_PRELOAD"}}
    checks = {}
    search = os.pathsep.join([*(str(Path(p) / "bin") for p in runtime_prefixes), env.get("PATH", os.defpath)])
    for file in files:
        if not file.resolve().is_relative_to(prefix):
            raise ValueError("Executable escapes its tool prefix")
        checks[str(file)] = digest(file)
        with file.open("rb") as stream:
            header = stream.read(4096)
        if header.startswith(b"\x7fELF"):
            # Check both a clean loader and the invoking profile environment.
            # Resolve relocations too, so symbol/version failures cannot hide
            # behind a successful library filename lookup.
            for loader_env in (env, {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}):
                probe = subprocess.run(["ldd", "-r", str(file)], env=loader_env,
                                       capture_output=True, text=True, timeout=15)
                text = probe.stdout + probe.stderr
                if ("not found" in text or "undefined symbol" in text or (probe.returncode and not any(
                        value in text for value in ("not a dynamic executable", "statically linked")))):
                    raise ValueError("Executable runtime cannot be resolved")
                for match in re.finditer(r"(?:=>\s+|^\s*)(/[^\n]*?)\s+\(0x", text, re.MULTILINE):
                    library = Path(match[1].strip())
                    checks[str(library)] = digest(library)
        elif header.startswith(b"#!"):
            words = shlex.split(header.splitlines()[0][2:].decode())
            if not words or not Path(words[0]).is_absolute() or not os.access(words[0], os.X_OK):
                raise ValueError("Script interpreter is unavailable")
            if Path(words[0]).name.startswith("python") or (len(words) > 1 and words[1].startswith("python")):
                raise ValueError("Python remains selected by pyenv")
            checks[str(Path(words[0]))] = digest(Path(words[0]))
            if Path(words[0]).name == "env":
                if len(words) != 2 or not shutil.which(words[1], path=search):
                    raise ValueError("Script interpreter cannot be resolved")
                interpreter = Path(shutil.which(words[1], path=search))
                checks[str(interpreter)] = digest(interpreter)
        else:
            raise ValueError("Unknown executable format")
    return checks


def external_spec(spec):
    fmt = "{name}{@versions}{compiler_flags}{variants}"
    if "patches" not in spec.variants:
        return spec.format(fmt)

    # A textual patch variant lacks Spack's internal application order. Check
    # what its external parser actually preserves before asking for a full solve.
    from spack.externals import external_spec as parse_external
    constraint = spec.copy(deps=False)
    del constraint.variants["patches"]
    text = constraint.format(fmt)
    parsed = parse_external({"spec": text, "prefix": str(spec.prefix)}).node_dict_with_hashes()
    source = spec.node_dict_with_hashes()
    if (parsed.get("parameters", {}).get("patches") != source["parameters"]["patches"]
            or parsed.get("patches") != source.get("patches")):
        raise ValueError("Spack external cannot reproduce the installed patch hashes and order")
    return text


def candidates(options):
    """Run inside the pinned engine: the source Spack DB is authoritative."""
    import spack.store
    import spack.error
    import spack.spec
    import spack.traverse
    packages = read_json(Path(options["dag"]))["packages"]
    allowed, runtime_nodes = graph_roles(packages)
    needed = set(options.get("needed", []))
    tools_path = Path(options['tools'])
    tools = tools_path.resolve()
    fixed = set(options.get("fixed", []))
    selected, rejected, unsafe = [], [], []
    with spack.store.use_store(str(tools / "pkg"), extra_data={
            "padded_length": False, "projections": {"all": "{name}-{version}"}}):
        with spack.store.STORE.db.read_transaction():
            specs = list(spack.store.STORE.db.query_local())
            by_name = {}
            for spec in specs:
                if not spec.external:
                    by_name.setdefault(spec.name, []).append(spec)
            for request in packages:
                if request["name"] in fixed or request['name'] in PYTHON:
                    continue
                borrowed = bool(request.get("external") and _inside_store(request['prefix'], tools_path / 'pkg'))
                # An installed consumer may retain a historical build edge.
                # That tool is irrelevant until installation actually needs it.
                if borrowed and request["hash"] not in needed and runtime_nodes is not None and request["hash"] not in runtime_nodes:
                    continue
                if request["hash"] not in allowed:
                    if borrowed:
                        unsafe.append({**request, 'reason': role_reason(request, packages)})
                    continue
                if not borrowed and (request.get("installed") or request["hash"] not in needed):
                    continue
                found = False
                for spec in by_name.get(request["name"], []):
                    source = {"name": spec.name, "version": str(spec.version), "node": spec.node_dict_with_hashes()}
                    if not matches(request, source):
                        continue
                    prefix = Path(spec.prefix).resolve()
                    if borrowed and prefix != Path(request["prefix"]).resolve():
                        continue
                    try:
                        constraint = external_spec(spec)
                        checks, runtime = {}, []
                        for dep in spack.traverse.traverse_nodes([spec], deptype=("link", "run")):
                            if dep.name in PYTHON:
                                raise ValueError("Python remains selected by pyenv")
                            dep_prefix = Path(dep.prefix).resolve()
                            if not dep_prefix.is_dir():
                                raise ValueError("Missing runtime prefix")
                            runtime.append(str(dep_prefix))
                            if not dep.external:
                                if not dep_prefix.is_relative_to(tools / "pkg"):
                                    raise ValueError("Runtime escapes the source store")
                                file = dep_prefix / ".spack/spec.json"
                                if file.is_symlink():
                                    raise ValueError("Spec metadata must not be a symlink")
                                actual = spack.spec.Spec.from_specfile(str(file))
                                if actual.dag_hash() != dep.dag_hash():
                                    raise ValueError("Runtime identity changed")
                                checks[str(file)] = digest(file)
                        checks.update(_executables(prefix, runtime))
                        selected.append({"name": spec.name, "version": str(spec.version), "prefix": str(prefix),
                                         "source_hash": spec.dag_hash(), "target_hash": request["hash"],
                                         "spec": constraint, "patches": source["node"].get("patches"),
                                         "parameters": source["node"]["parameters"],
                                         "checks": checks, "resolved_paths": {p: str(Path(p).resolve()) for p in checks},
                                         "runtime_prefixes": runtime})
                        found = True
                        break
                    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired, spack.error.SpackError) as exc:
                        rejected.append({"name": spec.name, "reason": str(exc)})
                if borrowed and not found:
                    unsafe.append({**request, 'reason': 'missing-source'})
    return {"selected": selected, "rejected": rejected, "unsafe_existing": unsafe}


class ReuseRejected(ForgeError):
    pass


class InvalidReferences(ReuseRejected):
    def __init__(self, rows, reason):
        super().__init__(reason, code=1)
        self.rows = rows


class Planner:
    """Operation-local optional externals; native fallback never changes the source store."""
    def __init__(self, home, envdir, op, run, resolve, *, progress=None, replay=False, enabled=True, allow_new=True,
                 committed_lock=None, historical=(), excluded_hashes=()):
        self.home, self.envdir, self.op = home, Path(envdir), Path(op)
        self.run, self.resolve, self.progress = run, resolve, progress
        self.replay, self.enabled, self.allow_new = replay, enabled, allow_new
        self.committed_lock = committed_lock
        self.original = {}
        self.selection = []
        self.attempt = 0
        self.historical = list(historical)
        self.excluded_hashes = set(excluded_hashes)
        self.rejected_hashes = set()
        self.notices = set()
        self.omit_cached_build_deps = False

    def _needed_tools(self, dag, plan, fixed):
        store = self.home.root / "tools/pkg"
        needed = {r["hash"] for r in plan["packages"]}
        _, runtime = graph_roles(dag)
        return [r for r in dag if r.get("external") and r["name"] not in fixed and r['name'] not in PYTHON
                and (self.replay or r["hash"] in needed or runtime is None or r["hash"] in runtime)
                and _inside_store(r['prefix'], store)]

    def _restore_rules(self, rules):
        for name, original in self.original.items():
            if original is None:
                rules.pop(name, None)
            else:
                rules[name] = copy.deepcopy(original)

    def _references(self, packages):
        by_prefix = {}
        for row in packages:
            if row.get("external"):
                key = row["name"], Path(row["prefix"]).resolve()
                by_prefix.setdefault(key, []).append(row)
        for source in self.selection:
            refs = by_prefix.get((source["name"], Path(source["prefix"]).resolve()))
            if refs:
                yield source, refs

    def _save(self, packages):
        from .storage import write_json
        selected = [{**source, "external_hashes": [r["hash"] for r in refs]}
                    for source, refs in self._references(packages)]
        retained = historical_references(self.home, packages, {
            'selected': selected, 'retained': self.historical})
        write_json(self.envdir / "build-tool-reuse.json", {"schema_version": 1, "selected": selected,
                                                         "retained": retained})
        return selected

    def validate(self, packages):
        allowed = build_only(packages)
        for source, refs in self._references(packages):
            for row in refs:
                node = row.get("node", {})
                if (row["hash"] not in allowed or node.get("parameters") != source["parameters"]
                        or node.get("patches") != source.get("patches")):
                    raise InvalidReferences(refs, f"Reused tool {source['name']} changed role, parameters or patch order.")
            try:
                check_files([source])
            except ReuseRejected as exc:
                raise InvalidReferences(refs, str(exc)) from exc

    def _fresh(self, policy, flags, label):
        lock = self.envdir / "spack.lock"
        if lock.exists():
            lock.rename(self.op / f"spack-before-{label}.lock")
        options = {"omit_cached_build_deps": True} if self.omit_cached_build_deps else {}
        options['historical_tool_hashes'] = sorted(self.excluded_hashes)
        if self.committed_lock is not None:
            # Recompute the new roots while Spack keeps the installed concrete roots.
            shutil.copy2(self.committed_lock, lock)
            retry_flags = list(flags)
        else:
            retry_flags = list(dict.fromkeys([*flags, "--force"]))
        return self.resolve(policy, retry_flags, suffix="-" + label, **options)

    def _fallback(self, policy, flags, hashes, *, unsafe=()):
        import yaml
        hashes = set(hashes)
        added = hashes - self.excluded_hashes
        if not added:
            names = ', '.join(sorted({r['name'] for r in unsafe})) or 'build tools'
            raise ReuseRejected(f"Cannot resolve {names} for their new dependency use: "
                                "historical references are already excluded; another solve would repeat the same inputs.",
                                code=1, hint="Review the operation's build-tool candidate diagnostics before retrying.")
        self.excluded_hashes.update(added)
        self.rejected_hashes.update(hashes)
        config = self.envdir / ".jforge-config/packages.yaml"
        document = yaml.safe_load(config.read_text())
        self._restore_rules(document["packages"])
        config.write_text(yaml.safe_dump(document, sort_keys=False))
        manifest = self.envdir / "spack.yaml"
        document = yaml.safe_load(manifest.read_text())
        for source in document["spack"]["concretizer"]["reuse"]["from"]:
            values = self.rejected_hashes if source['type'] == 'external' else self.excluded_hashes
            source['exclude'] = sorted(set(source.get('exclude', [])) | {'/' + h for h in values})
        manifest.write_text(yaml.safe_dump(document, sort_keys=False))
        self.selection = []
        self._save(read_json(self.op / 'dag.json')['packages'])
        if self.progress:
            if unsafe:
                self.progress("Resolve new dependency uses natively · " + ', '.join(
                    sorted({r['name'] + ' (' + r.get('reason', 'changed use') + ')' for r in unsafe})))
            else:
                self.progress("Build-tool reuse is unavailable; resolve with native builds")
        return self.apply(self._fresh(policy, flags, f"build-tools-fallback-{self.attempt}"), policy, flags,
                          omit_cached_build_deps=self.omit_cached_build_deps, native_only=not unsafe)

    def _role_notices(self, dag):
        if not self.progress:
            return
        allowed = build_only(dag)
        by_hash = {r['hash']: r for r in dag}
        for old in self.historical:
            if old['hash'] not in by_hash:
                continue
            uses = [r for r in dag if r['name'] == old['name'] and r['hash'] != old['hash']
                    and r['hash'] not in allowed]
            if not uses or old['hash'] in self.notices:
                continue
            consumers = ', '.join(sorted({by_hash[h]['name'] for h in old['consumers'] if h in by_hash}))
            self.progress(f"{old['name']}: keep the historical build tool" + (f" for {consumers}" if consumers else '')
                          + "; resolve the new " + role_reason(uses[0], dag) + " use natively")
            self.notices.add(old['hash'])

    def apply(self, plan, policy, flags, *, omit_cached_build_deps=False, native_only=False):
        import yaml
        from .storage import write_json
        from .unified_profiles import record
        from .toolchains import suite
        self.omit_cached_build_deps = omit_cached_build_deps
        dag = read_json(self.op / "dag.json")["packages"]
        profile = record(self.home) if self.enabled else None
        if not profile:
            return plan
        tools = self.home.root / "tools"
        config_file = self.envdir / ".jforge-config/packages.yaml"
        config = yaml.safe_load(config_file.read_text())
        fixed = sorted(suite(profile.get('toolchain')))
        borrowed = self._needed_tools(dag, plan, fixed)
        self._role_notices(dag)
        rejected = [r for r in borrowed if r['hash'] in self.rejected_hashes]
        if rejected:
            return self._fallback(policy, flags, [r['hash'] for r in rejected], unsafe=rejected)
        if native_only and not borrowed:
            self._save(dag)
            return plan
        if not (tools / "pkg/.spack-db").is_dir():
            if not borrowed:
                self.selection = []
                self._save(dag)
                return plan
            if self.replay or native_only:
                raise ReuseRejected("The historical build-tool store is unavailable.", code=1)
            self.attempt += 1
            return self._fallback(policy, flags, [r["hash"] for r in borrowed])
        self.attempt += 1
        label = f"build-tool-candidates-{self.attempt}"
        response = self.run({"dag": str(self.op / "dag.json"), "tools": str(tools), "fixed": fixed,
                             "needed": sorted({r["hash"] for r in plan["packages"]}
                                              | ({r['hash'] for r in borrowed} if self.replay else set()))}, label)
        write_json(self.op / (label + ".json"), response)
        if response["unsafe_existing"]:
            if self.replay:
                raise ReuseRejected("Historical build tools are unavailable or required for linking/runtime.", code=1)
            if native_only:
                uses = ', '.join(sorted(r['name'] + ' (' + r.get('reason', 'changed use') + ')'
                                       for r in response['unsafe_existing']))
                raise ReuseRejected("Native resolution still requires historical build tools: " + uses, code=1)
            unsafe = response["unsafe_existing"]
            return self._fallback(policy, flags, [r["hash"] for r in unsafe], unsafe=unsafe)
        selection = [r for r in response["selected"] if r["target_hash"] not in self.rejected_hashes]
        self.selection = selection
        existing = {r["hash"] for r in borrowed}
        proposed = [r for r in selection if r["target_hash"] not in existing]
        if not proposed or self.replay or native_only or not self.allow_new or policy == "only":
            try:
                self.validate(dag)
            except InvalidReferences as exc:
                if self.replay or native_only:
                    raise
                return self._fallback(policy, flags, [r["hash"] for r in exc.rows], unsafe=exc.rows)
            self._save(dag)
            return plan
        rules = config.setdefault("packages", {})
        for source in selection:
            name = source["name"]
            if name not in self.original:
                self.original[name] = copy.deepcopy(rules.get(name))

        def trial(rows, label):
            self._restore_rules(rules)
            for source in rows:
                rule = rules.setdefault(source["name"], {})
                rule["externals"] = [{"spec": source["spec"], "prefix": source["prefix"]}]
                rule["buildable"] = False
            config_file.write_text(yaml.safe_dump(config, sort_keys=False))
            self.selection = rows
            try:
                replacement = self._fresh(policy, flags, label)
                solved = read_json(self.op / "dag.json")["packages"]
                verified = {(r["name"], Path(r["prefix"]).resolve()) for r in rows}
                for ref in self._needed_tools(solved, replacement, fixed):
                    if (ref["name"], Path(ref["prefix"]).resolve()) not in verified:
                        raise ReuseRejected(f"Unverified build-tool reference: {ref['name']}", code=1)
                self.validate(solved)
            except ForgeError as exc:
                from .spack_backend import _completed_install_failure, _reject_cache_security_failure
                _reject_cache_security_failure(self.op, "concretize-" + label, exc)
                if not isinstance(exc, ReuseRejected) and not _completed_install_failure(self.op, "concretize-" + label):
                    raise
                return None
            return replacement

        if self.progress:
            self.progress(f"Check {len(proposed)} candidate build tools from this profile")
        label = f"build-tools-{self.attempt}"
        replacement = trial(selection, label)
        if replacement is None and len(selection) > 1:
            # Keep each successful subset in the next trial so the final plan
            # proves the retained tools can be used together.
            retained = [r for r in selection if r["target_hash"] in existing]
            middle = len(proposed) // 2
            pending = [proposed[middle:], proposed[:middle]] if middle else []
            sequence = 0
            while pending:
                group = pending.pop()
                sequence += 1
                replacement = trial(retained + group, f"{label}-subset-{sequence}")
                if replacement is not None:
                    retained.extend(group)
                elif len(group) > 1:
                    middle = len(group) // 2
                    pending.extend((group[middle:], group[:middle]))
            if replacement is None and retained:
                replacement = trial(retained, label + "-retained")
        if replacement is None:
            rejected = {r['target_hash'] for r in selection}
            rejected.update(r['hash'] for r in read_json(self.op / 'dag.json')['packages']
                            if r.get('external') and r['name'] in self.original)
            return self._fallback(policy, flags, rejected)
        reused = self._save(read_json(self.op / "dag.json")["packages"])
        if self.progress:
            names = ", ".join(row["name"] for row in reused)
            self.progress(f"Reused {len(reused)} build tools" + (f" · {names}" if names else ""))
        return replacement
