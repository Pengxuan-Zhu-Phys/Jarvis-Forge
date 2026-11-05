"""Versioned project builds, CMake instances and verified cross-build outputs."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import shutil
import sys

import yaml
from jsonschema import Draft202012Validator
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.version import Version

from . import cmake
from .downloads import unpack
from .environment_registry import show as registered_environment
from .package_operations import run_command, verified_archive
from .storage import ForgeError, ForgeHome, digest, identifier, new_id, now, read_json, write_json
from .suite import UniqueLoader, expand


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def load(path, *, home=None, library_files=None):
    path = Path(path).expanduser().resolve()
    try:
        text = path.read_text()
        config = yaml.load(text, Loader=UniqueLoader)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read build {path}: {exc}") from exc
    raw_config = config
    card_sources = []
    if isinstance(config, dict) and ("Workflow" in config or ("name" in config and "schema_version" not in config)):
        from .build_cards import normalize
        config, card_sources = normalize(config, path, home, library_files)
    schema = json.loads(files("jarvis_forge").joinpath("schemas/build.schema.json").read_text())
    errors = list(Draft202012Validator(schema).iter_errors(config))
    if errors:
        error = errors[0]
        raise ForgeError(f"Invalid build at {'.'.join(map(str, error.absolute_path))}: {error.message}")
    for name in [*config.get("sources", {}), *config.get("requires", {}), *config.get("outputs", {})]:
        identifier(name)
    environment = config.get("environment", {})
    if environment.get("name") and environment.get("shell"):
        raise ForgeError("Build environment selects name or shell, not both.")
    for key in environment.get("variables", {}):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ForgeError(f"Invalid environment variable: {key}")
    steps = config["steps"]
    if any(s["uses"] == "check" for s in steps):
        from . import standard_checks
        for step in steps:
            if step["uses"] == "check":
                standard_checks.validate(step["with"]["spec"])
        card_sources.append({"card": "jforge:standard_checks", "sha256": digest(Path(standard_checks.__file__))})
    ids = [s["id"] for s in steps]
    if len(set(ids)) != len(ids):
        raise ForgeError("Build step IDs must be unique.")
    pending, ordered, done = list(steps), [], set()
    while pending:
        ready = [s for s in pending if set(s.get("needs", [])) <= done]
        if not ready:
            raise ForgeError("Build has a dependency cycle or unknown prerequisite.")
        for step in ready:
            options = step["with"]
            dependencies = options.get("dependencies", [])
            for dep in dependencies:
                if dep.startswith("requires."):
                    if dep[9:] not in config.get("requires", {}):
                        raise ForgeError(f"Unknown build requirement: {dep}")
                elif dep not in step.get("needs", []):
                    raise ForgeError(f"Step {step['id']} must declare needs: [{dep}] before consuming it.")
            for values in options.get("paths", {}).values():
                if any("\x00" in v for v in values):
                    raise ForgeError("Environment paths cannot contain NUL.")
            ordered.append(step)
            pending.remove(step)
            done.add(step["id"])
    for output in config.get("outputs", {}).values():
        if output["step"] not in ids or any(s not in ids for s in output["publish_after"]):
            raise ForgeError("Build output references an unknown step.")
        producer = next(s for s in steps if s["id"] == output["step"])
        if producer["uses"] != "cmake.project" or "export" not in producer["with"]:
            raise ForgeError("An installation output requires a cmake.project export.")
        if "install" not in producer["with"].get("phases", ["configure", "build", "install"]):
            raise ForgeError("An installation output requires the install phase.")
    for requirement in config.get("requires", {}).values():
        try:
            SpecifierSet(requirement.get("version", ""))
        except InvalidSpecifier as exc:
            raise ForgeError(f"Invalid artifact version constraint: {exc}") from exc
    raw_sha = digest(path)
    recipe_sha = fingerprint({"raw": raw_sha, "config": config, "cards": card_sources}) if card_sources else raw_sha
    from .build_workspace import locate
    workspace = locate(path)
    return {"path": str(path), "text": text, "sha256": recipe_sha, "raw_sha256": raw_sha,
            "raw_config": raw_config, "card_sources": card_sources, "config": config, "ordered": ordered,
            "workspace": str(workspace) if workspace else None}


def graph(home, reference, *, parameters=None):
    from .build_catalog import resolve
    nodes, visiting = {}, set()

    def visit(path):
        item = load(path, home=home)
        key = item["sha256"]
        if key in visiting:
            raise ForgeError("Cross-build dependency cycle detected.")
        if key in nodes:
            return key
        visiting.add(key)
        links = {}
        for name, req in item["config"].get("requires", {}).items():
            links[name] = visit(resolve(home, req.get("build", req.get("workflow")), Path(path).parent))
        visiting.remove(key)
        selected = {**item["config"].get("parameters", {})}
        for name, value in (parameters or {}).items():
            if name in selected:
                selected[name] = value
        nodes[key] = {**item, "requirements": links, "parameters": selected}
        return key

    root = visit(resolve(home, reference))
    unknown = set(parameters or {}) - set(nodes[root]["parameters"])
    if unknown:
        raise ForgeError("Unknown build parameters: " + ", ".join(sorted(unknown)))
    return {"root": root, "nodes": nodes}


def plan(home, reference, *, parameters=None, environment_name=None):
    value = graph(home, reference, parameters=parameters)
    commands = []
    for key, node in value["nodes"].items():
        for step in node["ordered"]:
            if step["uses"] == "cmake.project":
                options = step["with"]
                commands.append({"build": node["config"]["id"], "step": step["id"],
                    "argv": cmake.commands(options, options["source"], "<instance>/build/" + step["id"],
                                           "<install-prefix>")})
    return {"build_id": value["nodes"][value["root"]]["config"]["id"],
            "workspace": value["nodes"][value["root"]].get("workspace"),
            "cmake_commands": commands,
            "environment_name": environment_name,
            "root": value["root"], "nodes": [{"recipe": k, "build": v["config"]["id"],
            "parameters": v["parameters"], "requires": v["requirements"], "steps": v["ordered"],
            "sources": v["config"].get("sources", {})} for k, v in value["nodes"].items()],
            "note": "Read-only graph; packages and compiler compatibility are resolved during execution."}


def interpolate(value, bindings):
    if isinstance(value, str):
        return expand(value, bindings)
    if isinstance(value, list):
        return [interpolate(v, bindings) for v in value]
    if isinstance(value, dict):
        return {k: interpolate(v, bindings) for k, v in value.items()}
    return value


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ForgeError(f"Build instance is busy: {path}", code=1) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def process_environment(values):
    previous = os.environ.copy()
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def safe_environment(values, extra=()):
    # Store explicit build context, not SSH agent sockets, tokens or passwords.
    keys = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "CC", "CXX", "FC",
            "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH", "LD_LIBRARY_PATH",
            "CMAKE_PREFIX_PATH", "CMAKE_INCLUDE_PATH", "CMAKE_LIBRARY_PATH", "PKG_CONFIG_PATH", "PYTHONPATH", "PYENV_ROOT", "PYENV_VERSION",
            "VIRTUAL_ENV", "LOADEDMODULES", "MODULEPATH", *extra}
    return {k: v for k, v in values.items() if k in keys}


def source_identity(source, parent):
    if "path" not in source:
        return source
    path = (parent / Path(source["path"]).expanduser()).resolve()
    if not path.is_dir():
        raise ForgeError(f"Local source directory missing: {path}")
    rows = []
    for directory, dirs, names in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in {".git", "build", "__pycache__", ".venv"})
        for name in sorted(names):
            file = Path(directory) / name
            rows.append((str(file.relative_to(path)), os.readlink(file) if file.is_symlink() else digest(file)))
    return {"path": str(path), "sha256": fingerprint(rows)}


def artifact(home, artifact_id):
    path = home.root / "builds/artifacts" / identifier(artifact_id) / "manifest.json"
    value = read_json(path)
    if value.get("status") != "ready":
        raise ForgeError(f"Artifact is not ready: {artifact_id}")
    cmake.verify_manifest(Path(value["prefix"]), value["files"])
    for package in value.get("runtime_packages", []):
        if not Path(package["prefix"]).is_dir():
            raise ForgeError(f"Artifact runtime dependency is missing: {package['name']}", code=1)
    for file, expected in value.get("evidence", {}).items():
        if not Path(file).is_file() or digest(Path(file)) != expected:
            raise ForgeError(f"Artifact dependency evidence is missing or modified: {file}", code=1)
    for dep in value.get("dependencies", []):
        artifact(home, dep)
    return value


def compatible(value, request, context):
    return (value["output"] == request["output"] and value["build_id"] == request["build_id"]
            and value.get("contract") == request.get("contract", "cmake-prefix/v1")
            and Version(value["version"]) in SpecifierSet(request.get("version", ""))
            and set(request.get("features", [])) <= set(value.get("features", []))
            and value["context"] == context)


def find_artifact(home, request, context):
    paths = list((home.root / "builds/artifacts").glob("*/manifest.json"))
    if request.get("artifact"):
        result = artifact(home, request["artifact"])
        if not compatible(result, request, context):
            raise ForgeError("Explicit artifact does not satisfy build requirements.")
        return result
    matches = []
    for path in paths:
        value = read_json(path)
        if compatible(value, request, context):
            try:
                matches.append(artifact(home, value["artifact_id"]))
            except ForgeError:
                continue
    if len(matches) > 1:
        raise ForgeError("Multiple compatible artifacts; bind requires.NAME.artifact explicitly: " +
                         ", ".join(v["artifact_id"] for v in matches))
    return matches[0] if matches else None


def execute(home, reference=None, *, parameters=None, environment_name=None, jobs=1, resume=None, progress=None):
    home.require_initialized()
    if jobs < 1:
        raise ForgeError("Jobs must be positive.")
    run_id = identifier(resume) if resume else new_id("build")
    directory = home.root / "runs" / run_id
    if resume:
        request = read_json(directory / "request.json")
        if request.get("kind") != "build":
            raise ForgeError("This run is not a project build.")
        selected_graph = read_json(directory / "graph.json")
        environment_name = request.get("environment_name")
    else:
        selected_graph = graph(home, reference, parameters=parameters)
        request = {"kind": "build", "run_id": run_id, "suite_id": selected_graph["nodes"][selected_graph["root"]]["config"]["id"],
                   "started_at": now(), "environment_name": environment_name,
                   "workspace": selected_graph["nodes"][selected_graph["root"]].get("workspace")}
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / "run.lock"):
        if not resume:
            write_json(directory / "request.json", request)
            write_json(directory / "graph.json", selected_graph)
        attempts = directory / "attempts" / new_id("attempt")
        attempts.mkdir(parents=True)
        result = {**request, "path": str(directory), "attempt": str(attempts), "status": "running", "nodes": {}}
        write_json(directory / "result.json", result)
        state_path = directory / "graph.lock.json"
        state = read_json(state_path) if resume and state_path.exists() else {"schema_version": 1, "nodes": {}}
        from .build_logging import ProcessLog
        process_log = ProcessLog(attempts / "build.log", "Jarvis-Forge.Build", progress)

        def event(message, component="Build"):
            with (attempts / "events.jsonl").open("a") as out:
                out.write(json.dumps({"at": now(), "message": message}) + "\n")
            process_log(message, module="Jarvis-Forge." + component)

        def run_node(key, policy="reuse-or-build", requirement=None):
            if key in result["nodes"]:
                return result["nodes"][key]
            node = selected_graph["nodes"][key]
            config = node["config"]
            if config.get("platforms") and sys.platform not in config["platforms"]:
                raise ForgeError(f"Build {config['id']} does not support {sys.platform}.")
            old = state["nodes"].get(key, {})
            node_dir = attempts / key[:16]
            node_dir.mkdir()
            (node_dir / "build.yaml").write_text(node["text"])
            if old.get("environment"):
                environment = old["environment"]
                environment_origin = old.get("environment_origin", {})
                observed = cmake.tools(environment)
                if observed != old["tools"]:
                    raise ForgeError("Selected compiler/Python changed since the saved run.", code=1)
            else:
                config_env = config.get("environment", {})
                name = environment_name or config_env.get("name")
                if name:
                    profile = registered_environment(home, name, revision=config_env.get("revision"), strict=True)
                    original = Path(profile["activation"])
                    environment_origin = {"name": name, "revision": profile["revision"], "files": profile["files"]}
                else:
                    original = ((Path(node["path"]).parent / config_env["shell"]).resolve()
                                if config_env.get("shell") else None)
                    environment_origin = {"shell": str(original), "sha256": digest(original)} if original else {"inherited": True}
                variables = config_env.get("variables", {})
                captured = cmake.capture(original)
                environment = safe_environment({**captured, **variables}, variables)
                observed = cmake.tools(environment)
            context = {"tools": observed, "platform": sys.platform, "machine": os.uname().machine,
                       "cxx_standard": str(node["parameters"].get("cxx_standard", "unspecified"))}
            if requirement:
                # A consumer's selected C++ standard is an explicit ABI requirement.
                if context["cxx_standard"] != requirement["context"]["cxx_standard"] or context["tools"] != requirement["context"]["tools"]:
                    raise ForgeError(f"Incompatible compiler/Python/C++ standard for dependency {config['id']}.")
                req = {**requirement, "build_id": config["id"]}
                if old.get("outputs"):
                    outputs = {n: artifact(home, v["artifact_id"]) for n, v in old["outputs"].items()}
                    selected = outputs.get(req["output"])
                    if not selected or not compatible(selected, req, context):
                        raise ForgeError("Locked artifact is incompatible with its consumer.")
                    result["nodes"][key] = {"outputs": outputs, "status": "reused"}
                    return result["nodes"][key]
                if policy != "rebuild":
                    existing = find_artifact(home, req, context)
                    if existing:
                        row = {"outputs": {req["output"]: existing}, "status": "reused"}
                        state["nodes"][key] = row
                        write_json(state_path, state)
                        result["nodes"][key] = row
                        event(f"Reuse {config['id']}: {existing['artifact_id']}")
                        return row
                if policy == "reuse-only":
                    raise ForgeError(f"No compatible installed artifact for {config['id']}.")
            dependencies = {}
            for name, child in node["requirements"].items():
                req = config["requires"][name]
                provided = run_node(child, req.get("policy", "reuse-or-build"), {**req, "context": context})
                if req["output"] not in provided["outputs"]:
                    raise ForgeError(f"Dependency does not export {req['output']}.")
                dependencies[name] = provided["outputs"][req["output"]]
                if not compatible(dependencies[name], {**req, "build_id": selected_graph["nodes"][child]["config"]["id"]}, context):
                    raise ForgeError(f"Dependency {name} is incompatible with this consumer.")
            parent = Path(node["path"]).parent
            sources = {n: source_identity(v, parent) for n, v in config.get("sources", {}).items()}
            identity = {"recipe": key, "parameters": node["parameters"], "sources": sources, "context": context,
                        "environment": environment, "environment_origin": environment_origin,
                        "requires": {n: a["artifact_id"] for n, a in dependencies.items()}}
            workspace = Path(node["workspace"]) if node.get("workspace") else None
            if workspace:
                identity["workspace"] = str(workspace)
            if policy == "rebuild":
                identity["rebuild"] = old.get("rebuild", run_id)
            instance = fingerprint(identity)
            if old.get("instance") and old["instance"] != instance:
                raise ForgeError("Build inputs changed; start a new run instead of resuming.")
            work = workspace / "instances" / instance if workspace else home.root / "builds/work" / instance
            work.mkdir(parents=True, exist_ok=True)
            if workspace:
                from .build_workspace import link_run
                link_run(workspace, directory)
                event(f"Workspace: {workspace}; instance: {work}")
            with file_lock(work / "instance.lock"):
                existing_outputs = {}
                for name in config.get("outputs", {}):
                    aid = identifier(config["id"] + "-" + instance[:20] + "-" + name)
                    if (home.root / "builds/artifacts" / aid / "manifest.json").exists():
                        existing_outputs[name] = artifact(home, aid)
                if existing_outputs and len(existing_outputs) == len(config.get("outputs", {})):
                    row = {"status": "reused", "outputs": existing_outputs, "instance": instance}
                    state["nodes"][key] = {**old, **row}
                    write_json(state_path, state)
                    result["nodes"][key] = row
                    return row
                row = {"instance": instance, "environment": environment, "tools": observed, "steps": old.get("steps", {}),
                       "outputs": old.get("outputs", {}), "rebuild": identity.get("rebuild"), "environment_origin": environment_origin}
                state["nodes"][key] = row
                write_json(state_path, state)
                bindings = {"build_dir": str(parent), "workflow_dir": str(parent), "work": str(work), "python": observed.get("python", {}).get("path", "python3")}
                for resource in node.get("card_sources", []):
                    if "script_id" not in resource:
                        continue
                    content = resource["content"].encode("utf-8")
                    if hashlib.sha256(content).hexdigest() != resource["sha256"]:
                        raise ForgeError("Modified companion script snapshot.")
                    frozen = work / "check-scripts" / resource["sha256"] / Path(resource["path"]).name
                    frozen.parent.mkdir(parents=True, exist_ok=True)
                    frozen.write_bytes(content)
                    archived = node_dir / "scripts" / resource["path"]
                    archived.parent.mkdir(parents=True, exist_ok=True)
                    archived.write_bytes(content)
                    bindings["scripts." + resource["script_id"]] = str(frozen)
                bindings.update({"parameters." + k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in node["parameters"].items()})
                pending_archives = {}
                for name, item in sources.items():
                    if "path" in item:
                        source_dir = Path(item["path"])
                    else:
                        target = work / "sources" / name
                        pending_archives[name] = (item, target)
                        source_dir = target / item.get("subdir", "")
                        if not source_dir.resolve().is_relative_to(target.resolve()):
                            raise ForgeError("Source subdirectory escapes the extracted archive.")
                    if "path" in item and not source_dir.is_dir():
                        raise ForgeError(f"Source directory not found: {source_dir}")
                    bindings["sources." + name] = str(source_dir)
                for name, value in dependencies.items():
                    bindings[f"requires.{name}.prefix"] = value["prefix"]
                records = {}
                for index, step in enumerate(node["ordered"], 1):
                    step_id = step["id"]
                    event(f"[{index}/{len(node['ordered'])}] {config['id']} / {step_id}")
                    # Download sources only when the first consuming step is ready;
                    # ROOT preflight and apt dependencies run before its large archive.
                    for name, (source_spec, target) in list(pending_archives.items()):
                        if "${sources." + name + "}" not in json.dumps(step["with"]):
                            continue
                        marker = target / ".jforge-extracted"
                        if not marker.is_file():
                            event(f"Download source {name}; logs: {node_dir / (name + '-download.stderr.log')}")
                            archive = verified_archive(home, source_spec, node_dir / (name + "-download"))
                            if workspace:
                                local_archive = work / "downloads" / (name + "-" + source_spec["sha256"])
                                local_archive.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copyfile(archive, local_archive)
                                archive = local_archive
                            if target.exists():
                                shutil.rmtree(target)
                            target.parent.mkdir(exist_ok=True)
                            event(f"Extract source {name}")
                            unpack(archive, target)
                            marker.write_text(source_spec["sha256"])
                        if not Path(bindings["sources." + name]).is_dir():
                            raise ForgeError(f"Source subdirectory missing: {bindings['sources.' + name]}")
                        del pending_archives[name]
                    options = interpolate(step["with"], bindings)
                    step_dir = node_dir / step_id
                    step_dir.mkdir()
                    try:
                        record = _step(home, step, options, work, step_dir, environment, observed,
                                       context, records, dependencies, jobs, event, row["steps"].get(step_id), workspace=workspace)
                        records[step_id] = record
                        row["steps"][step_id] = record
                        if record.get("prefix"):
                            bindings[f"steps.{step_id}.prefix"] = record["prefix"]
                        ambiguous = set()
                        for package in record.get("packages", []):
                            name = package["name"]
                            binding = f"packages.{name}.prefix"
                            if binding in bindings and bindings[binding] != package["prefix"]:
                                ambiguous.add(binding)
                            bindings[binding] = package["prefix"]
                        for binding in ambiguous:
                            bindings.pop(binding, None)
                        write_json(step_dir / "result.json", record)
                        write_json(state_path, state)
                    except BaseException:
                        row["steps"].pop(step_id, None)
                        write_json(state_path, state)
                        raise
                outputs = {}
                for name, spec in config.get("outputs", {}).items():
                    producer = records[spec["step"]]
                    if not producer.get("export"):
                        raise ForgeError("Output has no installation export.")
                    metadata = producer["export"]
                    artifact_id = identifier(config["id"] + "-" + instance[:20] + "-" + name)
                    evidence = {**producer.get("evidence", {})}
                    for dep in records.values():
                        evidence.update(dep.get("evidence", {}))
                    installed = {"artifact_id": artifact_id, "build_id": config["id"], "output": name,
                                 "recipe": key, "run_id": run_id, "instance": instance,
                                 "status": "ready", "contract": "cmake-prefix/v1", "context": context,
                                 "prefix": producer["prefix"], "version": metadata["version"],
                                 "features": metadata.get("features", []), "package": metadata["package"],
                                 "runtime_packages": producer.get("runtime_packages", []),
                                 "cmake_dir": metadata.get("cmake_dir"), "activation": producer["activation"],
                                 "dependencies": [v["artifact_id"] for v in dependencies.values()],
                                 "evidence": evidence, "files": cmake.manifest(Path(producer["prefix"]))}
                    write_json(home.root / "builds/artifacts" / artifact_id / "manifest.json", installed)
                    outputs[name] = installed
                row["outputs"] = outputs
                write_json(state_path, state)
                finished = {"status": "passed", "outputs": outputs, "instance": instance}
                result["nodes"][key] = finished
                return finished

        try:
            final = run_node(selected_graph["root"])
            result.update(status="passed", outputs=final["outputs"])
            if request.get("workspace"):
                from .build_workspace import publish
                publish(Path(request["workspace"]), result)
        except KeyboardInterrupt:
            result.update(status="interrupted", error="Interrupted.")
        except (ForgeError, OSError, ValueError) as exc:
            result.update(status="failed", error=str(exc))
        finally:
            result["finished_at"] = now()
            process_log("Build " + result["status"] + (": " + result["error"] if result.get("error") else ""),
                        level="ERROR" if result["status"] == "failed" else "WARNING" if result["status"] == "interrupted" else "INFO")
            write_json(attempts / "result.json", result)
            write_json(directory / "result.json", result)
        return result


def _step(home, step, options, work, logs, environment, observed, context, records, dependencies, jobs, progress, previous, *, workspace=None):
    used = []
    for name in options.get("dependencies", []):
        used.append(dependencies[name[9:]] if name.startswith("requires.") else records[name])
    selected_packages = {}
    for dependency in used:
        local_packages = {}
        for package in dependency.get("runtime_packages", dependency.get("packages", [])):
            name = package["name"]
            identity = package.get("hash", package.get("install_id", package["prefix"]))
            local_packages.setdefault(name, set()).add(identity)
        for name, identities in local_packages.items():
            if name in selected_packages and selected_packages[name] != identities:
                raise ForgeError(f"Conflicting dependency instances for {name}; select a compatible dependency environment.")
            selected_packages[name] = identities
    scripts = [v["activation"] for v in used if v.get("activation")]
    paths = {k: list(v) for k, v in options.get("paths", {}).items()}
    prefix_paths = [v["prefix"] for v in used if v.get("prefix")]
    prefix_paths.extend(package["prefix"] for dependency in used for package in dependency.get("packages", []))
    if prefix_paths:
        paths.setdefault("CMAKE_PREFIX_PATH", []).extend(prefix_paths)
    if step["uses"] == "cmake.project":
        write_json(logs / "dependency-paths.json", {key: list(dict.fromkeys(values)) for key, values in paths.items()})
        progress("Dependency paths: " + str(logs / "dependency-paths.json"), component="CMake")
    script = cmake.write_environment(logs / "environment.sh", base=environment, scripts=scripts, paths=paths)
    number = 0

    def invoke(argv, cwd):
        nonlocal number
        number += 1
        progress("Command: " + __import__("shlex").join(argv),
                 component="CMake" if step["uses"] == "cmake.project" else "Checker" if step["uses"] == "check" else "Calculator")
        try:
            run_command(argv, cwd, script, logs, str(number), step.get("timeout_seconds", 86400), progress=progress, process_env=environment)
        finally:
            for stream in ("stdout", "stderr"):
                source_log = logs / f"command-{number}.{stream}.log"
                if source_log.exists():
                    with (logs / (stream + ".log")).open("ab") as combined:
                        combined.write(source_log.read_bytes())
        return (logs / f"command-{number}.stdout.log").read_text(errors="replace")

    if step["uses"] == "apt.install":
        if previous:
            for file, sha in previous["evidence"].items():
                if not Path(file).is_file() or digest(Path(file)) != sha:
                    raise ForgeError(f"Locked apt dependency changed: {file}")
            return previous
        from . import spack_backend as backend
        target = ForgeHome(home.root / "apt/profiles" / ("spack-build-" + work.name[:20] + "-" + step["id"]))
        target.initialize()
        write_json(target.root / "config.json", {**read_json(target.root / "config.json"), "apt_base_home": str(home.root)})
        with process_environment(environment):
            # These builds use upstream Spack recipes; an unpublished optional
            # extension Release must not block their builtin package dependencies.
            backend.bootstrap(target, python=observed.get("python", {}).get("path"), progress=progress, extensions=False)
            installed = backend.transact(target, "install", options["specs"], jobs=jobs,
                                         timeout=step.get("timeout_seconds", 86400), progress=progress)
        lock = Path(installed["lockfile"])
        activation = installed["activation"]
        adapter = Path(activation).parent / "spack-environment.sh"
        if adapter.is_file():
            activation = str(adapter)
        evidence = {str(lock): digest(lock)}
        evidence.update({str(p): digest(p) for p in Path(activation).parent.iterdir() if p.is_file()})
        return {"status": "passed", "packages": installed["packages"], "activation": activation,
                "evidence": evidence,
                "profile": str(target.root)}
    if step["uses"] == "check":
        from .standard_checks import execute
        report = execute(options["spec"], observed["python"]["path"], lambda argv: invoke(argv, work), logs)
        return {"status": "passed", "report": report, "artifacts": {str(logs / "checks.json"): digest(logs / "checks.json")}}
    if step["uses"] == "command":
        cwd = Path(options.get("cwd", str(work)))
        if not cwd.is_dir():
            raise ForgeError(f"Command working directory missing: {cwd}")
        invoke(options["argv"], cwd)
        artifacts = {}
        for name in options.get("artifacts", []):
            file = cwd / name
            if not file.resolve().is_relative_to(cwd.resolve()) or not file.is_file():
                raise ForgeError(f"Required command artifact missing or outside cwd: {file}", code=1)
            artifacts[str(file)] = digest(file)
        return {"status": "passed", "artifacts": artifacts, "activation": str(script)}
    build = work / "build" / step["id"]
    prefix = work / "install" / step["id"] if workspace else home.root / "builds/installs" / step["id"] / work.name
    source = Path(options["source"])
    if not source.is_dir():
        raise ForgeError(f"CMake source not found: {source}")
    build.mkdir(parents=True, exist_ok=True)
    configured = json.loads(json.dumps(options))
    cache = configured.setdefault("cache", {})
    for key, value in observed.items():
        variable = {"CC": "CMAKE_C_COMPILER", "CXX": "CMAKE_CXX_COMPILER", "FC": "CMAKE_Fortran_COMPILER", "python": "Python3_EXECUTABLE"}.get(key)
        if variable is None:
            continue
        cache.setdefault(variable, {"type": "FILEPATH", "value": value["path"]})
    for dependency in used:
        if dependency.get("package") and dependency.get("cmake_dir"):
            variable = dependency["package"] + "_DIR"
            cache[variable] = {"type": "PATH", "value": str(Path(dependency["prefix"]) / dependency["cmake_dir"])}
    selected_standard = str(cache.get("CMAKE_CXX_STANDARD", {}).get("value", "unspecified"))
    if context["cxx_standard"] != "unspecified" and context["cxx_standard"] != selected_standard:
        raise ForgeError("CMake C++ standard must match the declared cxx_standard parameter.")
    invocation = cmake.commands(configured, source, build, prefix, jobs=jobs)
    requested = fingerprint({"configure": invocation["configure"],
                             "environment": digest(script)})
    marker = build / ".jforge-configure.json"
    for phase in options.get("phases", ["configure", "build", "install"]):
        if phase == "configure":
            if marker.exists() and read_json(marker)["fingerprint"] == requested:
                progress("Reuse CMake configuration")
                continue
            if marker.exists():
                raise ForgeError("CMake configuration changed; use a new build instance.")
            query = build / ".cmake/api/v1/query"
            query.mkdir(parents=True, exist_ok=True)
            for kind in ("codemodel-v2", "cache-v2", "toolchains-v1"):
                (query / kind).touch()
        invoke(invocation[phase], source)
        if phase == "configure":
            actual = cmake.cache_values(build)
            for dep in used:
                if dep.get("package") and dep.get("cmake_dir"):
                    expected = Path(dep["prefix"]) / dep["cmake_dir"]
                    if Path(actual.get(dep["package"] + "_DIR", "")).resolve() != expected.resolve():
                        raise ForgeError("CMake selected a different dependency installation.", code=1)
            if "CMAKE_CXX_STANDARD" in cache and str(cache["CMAKE_CXX_STANDARD"]["value"]) != actual.get("CMAKE_CXX_STANDARD"):
                raise ForgeError("CMake selected a different C++ standard.", code=1)
            for selected, variable in (("CC", "CMAKE_C_COMPILER"), ("CXX", "CMAKE_CXX_COMPILER"), ("FC", "CMAKE_Fortran_COMPILER")):
                if selected in observed and actual.get(variable) and Path(actual[variable]).resolve() != Path(observed[selected]["path"]).resolve():
                    raise ForgeError(f"CMake selected a different {selected}; select it in the environment instead.", code=1)
            write_json(logs / "cmake-cache.json", actual)
            shutil.copyfile(build / "CMakeCache.txt", logs / "CMakeCache.txt")
            write_json(marker, {"fingerprint": requested})
    reply = build / ".cmake/api/v1/reply"
    if reply.is_dir():
        shutil.copytree(reply, logs / "cmake-file-api", dirs_exist_ok=True)
    for name in options.get("required_files", []):
        if not (prefix / name).is_file():
            raise ForgeError(f"Installed file missing: {prefix / name}", code=1)
    metadata = options.get("export")
    if not metadata:
        return {"status": "passed", "prefix": str(prefix), "activation": str(script)}
    if context["cxx_standard"] == "unspecified":
        actual_standard = cmake.cache_values(build).get("CMAKE_CXX_STANDARD")
        if actual_standard:
            raise ForgeError("Exported CMake projects must declare a matching cxx_standard parameter.")
    exported_paths = {k: [str(prefix / v) if not Path(v).is_absolute() else v for v in values]
                      for k, values in metadata.get("paths", {}).items()}
    activation_scripts = [*scripts]
    if metadata.get("activation"):
        activation_scripts.append(str(prefix / metadata["activation"]))
    for name in [metadata.get("cmake_dir"), metadata.get("activation")]:
        if name and not (prefix / name).exists():
            raise ForgeError(f"Export path missing: {prefix / name}", code=1)
    exported_paths.setdefault("CMAKE_PREFIX_PATH", []).insert(0, str(prefix))
    # Consumer environments must not inherit the producer's compiler/Python/PATH.
    # Dependency activation scripts and explicit paths carry the runtime closure.
    activation = work / (step["id"] + "-activate.sh")
    cmake.write_environment(activation, scripts=activation_scripts, paths=exported_paths)
    evidence = {str(activation): digest(activation)}
    for dep in used:
        evidence.update(dep.get("evidence", {}))
    runtime_names = set(metadata.get("runtime_packages", []))
    runtime_packages = [p for dep in used for p in dep.get("packages", []) if p["name"] in runtime_names]
    runtime_packages += [p for dep in used for p in dep.get("runtime_packages", [])]
    for package in runtime_packages:
        for directory in (Path(package["prefix"]) / "lib", Path(package["prefix"]) / "lib64"):
            if directory.is_dir():
                for library in directory.glob("lib*"):
                    if library.is_file() and (".so" in library.name or ".dylib" in library.name):
                        evidence[str(library)] = digest(library)
    return {"status": "passed", "prefix": str(prefix), "activation": str(activation), "runtime_packages": runtime_packages,
            "package": metadata["package"], "cmake_dir": metadata.get("cmake_dir"),
            "version": metadata["version"], "features": metadata.get("features", []),
            "export": metadata, "evidence": evidence}


def step_path(home, run_id, step_name):
    """Resolve a step in the latest attempt; qualify duplicates as build.step."""
    directory = home.run_path(run_id)
    result = read_json(directory / "result.json")
    attempt = Path(result["attempt"])
    graph_value = read_json(directory / "graph.json")
    matches = []
    for key, node in graph_value["nodes"].items():
        for step in node["ordered"]:
            if step_name in {step["id"], node["config"]["id"] + "." + step["id"]}:
                candidate = attempt / key[:16] / step["id"]
                if candidate.is_dir():
                    matches.append(candidate)
    if len(matches) != 1:
        raise ForgeError("Step unavailable or ambiguous; use --step WORKFLOW.STEP.")
    return matches[0]


def summary(result):
    return {k: v for k, v in result.items() if k not in {"nodes", "outputs"}} | {
        "outputs": [{"name": n, **{k: a.get(k) for k in ("artifact_id", "version", "prefix", "activation")}}
                    for n, a in result.get("outputs", {}).items()],
        "nodes": [{"recipe": k, "status": v["status"], "instance": v.get("instance")}
                  for k, v in result.get("nodes", {}).items()]}
