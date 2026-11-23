"""Borrow executable build tools without exposing their old runtime DAG as libraries."""
from pathlib import Path
import os
import re
import shlex
import shutil
import subprocess

from .storage import ForgeError, digest, read_json

PYTHON = {"python", "python-venv"}


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
            and a.get("parameters") == b.get("parameters") and a.get("arch") == b.get("arch"))


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


def candidates(options):
    """Run inside the pinned engine: the source Spack DB is authoritative."""
    import spack.store
    import spack.error
    import spack.spec
    import spack.traverse
    packages = read_json(Path(options["dag"]))["packages"]
    allowed, runtime_nodes = graph_roles(packages)
    needed = set(options.get("needed", []))
    tools = Path(options["tools"]).resolve()
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
                if request["name"] in fixed:
                    continue
                borrowed = bool(request.get("external") and Path(request["prefix"]).resolve().is_relative_to(tools / "pkg"))
                # An installed consumer may retain a historical build edge.
                # That tool is irrelevant until installation actually needs it.
                if borrowed and request["hash"] not in needed and runtime_nodes is not None and request["hash"] not in runtime_nodes:
                    continue
                if request["hash"] not in allowed:
                    if borrowed:
                        unsafe.append(request)
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
                                         "spec": spec.format("{name}{@versions}{compiler_flags}{variants}"),
                                         "parameters": source["node"]["parameters"],
                                         "checks": checks, "resolved_paths": {p: str(Path(p).resolve()) for p in checks},
                                         "runtime_prefixes": runtime})
                        found = True
                        break
                    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired, spack.error.SpackError) as exc:
                        rejected.append({"name": spec.name, "reason": str(exc)})
                if borrowed and not found:
                    unsafe.append(request)
    return {"selected": selected, "rejected": rejected, "unsafe_existing": unsafe}


def invalid_uses(packages, selection):
    allowed = build_only(packages)
    unsafe = []
    for request in packages:
        for source in selection:
            if (request["name"] == source["name"] and request.get("external")
                    and Path(request["prefix"]).resolve() == Path(source["prefix"]).resolve()):
                if request["hash"] not in allowed or request.get("node", {}).get("parameters") != source["parameters"]:
                    unsafe.append(request)
    return unsafe


class ReuseRejected(ForgeError):
    pass


class Planner:
    """Operation-local optional externals; native fallback never changes the source store."""
    def __init__(self, home, envdir, op, run, resolve, *, progress=None, replay=False, enabled=True, allow_new=True, committed_lock=None):
        self.home, self.envdir, self.op = home, Path(envdir), Path(op)
        self.run, self.resolve, self.progress = run, resolve, progress
        self.replay, self.enabled, self.allow_new = replay, enabled, allow_new
        self.committed_lock = committed_lock
        self.original = {}
        self.selection = []
        self.attempt = 0
        self.disabled = False
        self.omit_cached_build_deps = False

    def _save(self, packages):
        from .storage import write_json
        selected = []
        for source in self.selection:
            refs = [r["hash"] for r in packages if r.get("external") and r["name"] == source["name"]
                    and Path(r["prefix"]).resolve() == Path(source["prefix"]).resolve()]
            if refs:
                selected.append({**source, "external_hashes": refs})
        write_json(self.envdir / "build-tool-reuse.json", {"schema_version": 1, "selected": selected})

    def validate(self, packages):
        check_files(self.selection)
        if invalid_uses(packages, self.selection):
            raise ReuseRejected("A reused build tool became a link/runtime dependency.", code=1)

    def _fresh(self, policy, flags, label):
        lock = self.envdir / "spack.lock"
        if lock.exists():
            lock.rename(self.op / f"spack-before-{label}.lock")
        options = {"omit_cached_build_deps": True} if self.omit_cached_build_deps else {}
        if self.committed_lock is not None:
            # Recompute the new roots while Spack keeps the installed concrete roots.
            shutil.copy2(self.committed_lock, lock)
            retry_flags = list(flags)
        else:
            retry_flags = list(dict.fromkeys([*flags, "--force"]))
        return self.resolve(policy, retry_flags, suffix="-" + label, **options)

    def _fallback(self, policy, flags, hashes):
        import yaml
        from .storage import write_json
        config = self.envdir / ".jforge-config/packages.yaml"
        document = yaml.safe_load(config.read_text())
        for name, original in self.original.items():
            if original is None:
                document["packages"].pop(name, None)
            else:
                document["packages"][name] = original
        config.write_text(yaml.safe_dump(document, sort_keys=False))
        manifest = self.envdir / "spack.yaml"
        document = yaml.safe_load(manifest.read_text())
        for source in document["spack"]["concretizer"]["reuse"]["from"]:
            source.setdefault("exclude", []).extend("/" + h for h in sorted(set(hashes)))
        manifest.write_text(yaml.safe_dump(document, sort_keys=False))
        self.disabled = True
        self.selection = []
        write_json(self.envdir / "build-tool-reuse.json", {"schema_version": 1, "selected": []})
        if self.progress:
            self.progress("Build-tool reuse is unavailable; resolve with native builds")
        return self.apply(self._fresh(policy, flags, f"build-tools-fallback-{self.attempt}"), policy, flags,
                          omit_cached_build_deps=self.omit_cached_build_deps)

    def apply(self, plan, policy, flags, *, omit_cached_build_deps=False):
        import copy
        import yaml
        from .storage import write_json
        from .unified_profiles import record
        self.omit_cached_build_deps = omit_cached_build_deps
        dag = read_json(self.op / "dag.json")["packages"]
        if not self.enabled or not record(self.home):
            return plan
        tools = self.home.root / "tools"
        config_file = self.envdir / ".jforge-config/packages.yaml"
        config = yaml.safe_load(config_file.read_text())
        fixed = [name for name, value in config.get("packages", {}).items()
                 if isinstance(value, dict) and value.get("buildable") is False and name not in self.original]
        borrowed = [r for r in dag if r.get("external") and r["name"] not in fixed
                    and Path(r["prefix"]).resolve().is_relative_to(tools.resolve() / "pkg")]
        _, runtime_nodes = graph_roles(dag)
        needed = {r["hash"] for r in plan["packages"]}
        borrowed = [r for r in borrowed if r["hash"] in needed or runtime_nodes is None or r["hash"] in runtime_nodes]
        if self.disabled and not borrowed:
            return plan
        if not (tools / "pkg/.spack-db").is_dir():
            if not borrowed:
                return plan
            if self.replay or self.disabled:
                raise ReuseRejected("The historical build-tool store is unavailable.", code=1)
            self.attempt += 1
            return self._fallback(policy, flags, [r["hash"] for r in borrowed])
        self.attempt += 1
        label = f"build-tool-candidates-{self.attempt}"
        response = self.run({"dag": str(self.op / "dag.json"), "tools": str(tools), "fixed": fixed,
                             "needed": [r["hash"] for r in plan["packages"]]}, label)
        write_json(self.op / (label + ".json"), response)
        if response["unsafe_existing"]:
            if self.replay:
                raise ReuseRejected("Historical build tools are unavailable or required for linking/runtime.", code=1)
            if self.disabled:
                raise ReuseRejected("Native resolution retained an unsafe build-tool reference.", code=1)
            return self._fallback(policy, flags, [r["hash"] for r in response["unsafe_existing"]])
        selection = response["selected"]
        self.selection = selection
        existing = {r["hash"] for r in borrowed}
        proposed = [r for r in selection if r["target_hash"] not in existing]
        if not proposed or self.replay or self.disabled or not self.allow_new or policy == "only":
            try:
                self.validate(dag)
            except ReuseRejected:
                if self.replay or self.disabled:
                    raise
                return self._fallback(policy, flags, [r["hash"] for r in borrowed])
            self._save(dag)
            return plan
        rules = config.setdefault("packages", {})
        for source in selection:
            name = source["name"]
            if name not in self.original:
                self.original[name] = copy.deepcopy(rules.get(name))
            rule = rules.setdefault(name, {})
            external = {"spec": source["spec"], "prefix": source["prefix"]}
            rule["externals"] = [external]
            rule["buildable"] = False
        config_file.write_text(yaml.safe_dump(config, sort_keys=False))
        if self.progress:
            self.progress(f"Reuse {len(selection)} installed build tools from this profile")
        retry_label = f"build-tools-{self.attempt}"
        try:
            replacement = self._fresh(policy, flags, retry_label)
            dag = read_json(self.op / "dag.json")["packages"]
            self.validate(dag)
        except ForgeError as exc:
            from .spack_backend import _completed_install_failure, _reject_cache_security_failure
            _reject_cache_security_failure(self.op, "concretize-" + retry_label, exc)
            if not isinstance(exc, ReuseRejected) and not _completed_install_failure(self.op, "concretize-" + retry_label):
                raise
            return self._fallback(policy, flags, [r["hash"] for r in read_json(self.op / "dag.json")["packages"]
                                                if r.get("external") and r["name"] in self.original])
        self._save(dag)
        return replacement
