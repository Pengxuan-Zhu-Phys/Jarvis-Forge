"""Versioned project builds, CMake instances and verified cross-build outputs."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import shlex
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
from .build_names import app_name
from .paths import object_path, physical_name, named_path
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
        phases = producer["with"].get("phases", ["configure", "build", "install"])
        if producer["with"].get("in_place"):
            if "install" in phases:
                raise ForgeError("An in-place build does not run the install phase.")
        elif "install" not in phases:
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


def host_requirements(config):
    workflow = config.get("Workflow")
    platforms = config.get("platforms") or (workflow.get("platforms") if isinstance(workflow, dict) else None)
    result = {"platforms": platforms} if platforms else {}
    steps = config.get("steps")
    if config.get("dependencies") or (isinstance(steps, list) and any(
            isinstance(step, dict) and step.get("uses") == "apt.install" for step in steps)):
        result["apt_backend"] = "spack"
    return result


def plan(home, reference, *, parameters=None, environment_name=None, source=None):
    value = graph(home, reference, parameters=parameters)
    _apply_source_override(value, source)
    commands = []
    for key, node in value["nodes"].items():
        for step in node["ordered"]:
            if step["uses"] == "cmake.project":
                options = step["with"]
                build_dir = "<preset-binary-dir>" if options.get("binary_dir") == "preset" else "<instance>/build/" + step["id"]
                prefix = "<source>" if options.get("in_place") else "<install-prefix>"
                commands.append({"build": node["config"]["id"], "step": step["id"],
                    "phases": options.get("phases", ["configure", "build", "install"]),
                    "argv": cmake.commands(options, options["source"], build_dir, prefix)})
    return {"build_id": value["nodes"][value["root"]]["config"]["id"],
            "workspace": value["nodes"][value["root"]].get("workspace"),
            "cmake_commands": commands,
            "environment_name": environment_name,
            "root": value["root"], "nodes": [{"recipe": k, "build": v["config"]["id"],
            "parameters": v["parameters"], "requires": v["requirements"], "steps": v["ordered"],
            **({"outputs_off": names} if (names := v["config"].get("environment", {}).get("outputs_off")) else {}),
            "sources": v["config"].get("sources", {}), "host_requirements": host_requirements(v["config"])}
            for k, v in value["nodes"].items()],
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
def file_lock(path, *, blocking=False, message=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise ForgeError(message or f"Build instance is busy: {path}", code=1) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def source_cmake_lock(home, source):
    """Lock one checkout's shared CMake tree without changing its source identity."""
    source = Path(source).resolve()
    git = source / ".git"
    gitdir = None
    if git.is_dir():
        gitdir = git
    elif git.is_file():
        for line in git.read_text(errors="replace").splitlines():
            if line.startswith("gitdir:"):
                candidate = Path(line.split(":", 1)[1].strip())
                gitdir = candidate if candidate.is_absolute() else (source / candidate).resolve()
                break
    if gitdir is not None:
        return gitdir / "jforge-cmake.lock"
    return object_path(home.root / "builds/source-locks", source.name, {"source": str(source)}, create=True) / "cmake.lock"


def _apply_source_override(selected_graph, source):
    """Point the root card's single local checkout at another directory."""
    if source is None:
        return
    node = selected_graph["nodes"][selected_graph["root"]]
    sources = node["config"].get("sources", {})
    local = [name for name, item in sources.items() if isinstance(item, dict) and "path" in item]
    if len(local) != 1:
        raise ForgeError("--source overrides a card with exactly one local source directory.")
    path = Path(source).expanduser().resolve()
    if not path.is_dir():
        raise ForgeError(f"Local source directory missing: {path}")
    sources[local[0]] = {"path": str(path)}


def _instance_directory(base, config, instance, *, installs=None):
    work = object_path(base, config["id"], {"instance": instance, "build": config["id"]}, create=True)
    work.mkdir(parents=True, exist_ok=True)
    write_json(work / ".jforge-instance.json", {"schema_version": 1, "instance": instance, "build": config["id"]})
    return work


def artifact_key(home, build, instance, output, *, create=False):
    return physical_name(home.root / "builds/artifacts", build,
                         {"build": build, "instance": instance, "output": output}, create=create)


def _artifact_exists(home, config, instance, name):
    key = artifact_key(home, config["id"], instance, name)
    return key is not None and (home.root / "builds/artifacts" / key / "manifest.json").is_file()


@contextmanager
def process_environment(values):
    previous = os.environ.copy()
    os.environ.clear()
    os.environ.update(values)
    if "JFORGE_CONTROLLER_LAUNCHER" in previous:
        os.environ["JFORGE_CONTROLLER_LAUNCHER"] = previous["JFORGE_CONTROLLER_LAUNCHER"]
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


BUILD_ENV_KEYS = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "CC", "CXX", "FC",
                  "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH", "LD_LIBRARY_PATH",
                  "CMAKE_PREFIX_PATH", "CMAKE_INCLUDE_PATH", "CMAKE_LIBRARY_PATH", "PKG_CONFIG_PATH",
                  "PYTHONHOME", "PYTHONPATH", "PYENV_ROOT", "PYENV_VERSION", "VIRTUAL_ENV", "LOADEDMODULES",
                  "MODULEPATH", "JFORGE_ACTIVE_OUTPUTS"}


def safe_environment(values, extra=()):
    # Store explicit build context, not SSH agent sockets, tokens or passwords.
    keys = BUILD_ENV_KEYS.union(extra)
    return {k: v for k, v in values.items() if k in keys}


def source_identity(source, parent):
    if "path" not in source:
        return source
    path = (parent / Path(source["path"]).expanduser()).resolve()
    if not path.is_dir():
        raise ForgeError(f"Local source directory missing: {path}")
    # A git checkout's build writes ignored files into the tree. Hashing every
    # file would change the identity after each build and force a rebuild.
    from .testing_run import Source
    git_id = Source(path).identity()
    if git_id is not None:
        recorded = {key: git_id[key] for key in ("commit", "tree", "dirty", "dirty_digest")}
        return {"path": str(path), "git": recorded, "sha256": git_id["source_key"]}
    rows = []
    excluded = {".git", "build", "__pycache__", ".venv"}

    def visit(directory, ancestors):
        resolved = directory.resolve()
        if resolved in ancestors:
            raise ForgeError(f"Cyclic local source directory link: {directory}")
        subdirectories = []
        for file in sorted(directory.iterdir()):
            relative = str(file.relative_to(path))
            if file.is_dir():
                if file.name in excluded:
                    continue
                subdirectories.append(file)
                if file.is_symlink():
                    rows.append((relative, os.readlink(file), "directory"))
            elif file.is_symlink():
                rows.append((relative, os.readlink(file), digest(file) if file.is_file() else None))
            elif file.is_file():
                rows.append((relative, digest(file)))
            else:
                raise ForgeError(f"Unsupported local source file: {file}")
        for child in subdirectories:
            visit(child, ancestors | {resolved})

    visit(path, set())
    return {"path": str(path), "sha256": fingerprint(rows)}


def artifact(home, artifact_id):
    path = home.root / "builds/artifacts" / identifier(artifact_id) / "manifest.json"
    value = read_json(path)
    if value.get("status") != "ready":
        raise ForgeError(f"Artifact is not ready: {artifact_id}")
    cmake.verify_manifest(Path(value["prefix"]), value["files"])
    if value.get("in_place"):
        cache = Path(value.get("cmake_cache", ""))
        if not cache.is_file() or digest(cache) != value.get("cmake_cache_sha256"):
            raise ForgeError(f"In-place CMake cache changed: {cache}", code=1)
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
    return (value["output"] == request["output"] and app_name(value["build_id"]) == app_name(request["build_id"])
            and (not request.get("artifact") or value["artifact_id"] == request["artifact"])
            and value.get("contract") == request.get("contract", "cmake-prefix/v1")
            and Version(value["version"]) in SpecifierSet(request.get("version", ""))
            and set(request.get("features", [])) <= set(value.get("features", []))
            and value["context"] == context)


def find_artifact(home, request, context, *, apt=None):
    paths = list((home.root / "builds/artifacts").glob("*/manifest.json"))
    if request.get("artifact"):
        result = artifact(home, request["artifact"])
        if not compatible(result, request, context):
            raise ForgeError("Explicit artifact does not satisfy build requirements.")
        return result
    matches = []
    for path in paths:
        value = read_json(path)
        if compatible(value, request, context) and (apt is None or value.get("apt") == apt):
            try:
                matches.append(artifact(home, value["artifact_id"]))
            except ForgeError:
                continue
    if len(matches) > 1:
        raise ForgeError("Multiple compatible artifacts; bind requires.NAME.artifact explicitly: " +
                         ", ".join(v["artifact_id"] for v in matches))
    return matches[0] if matches else None


def _apt_signature(roots, packages):
    return fingerprint({"roots": sorted(roots),
                        "packages": sorted((p["name"], p["hash"], p["prefix"]) for p in packages)})


def _apt_snapshot(profile):
    from .spack_backend import current

    state = current(ForgeHome(Path(profile["path"])))
    return {"profile": profile, "signature": _apt_signature(state["roots"], state["packages"])}


def _find_apt_instance(home, config, identity, apt):
    """Find a complete build whose installed apt DAG matches the current one."""
    folder = home.root / "builds/artifacts"
    build_identity = fingerprint(identity)
    for path in sorted(folder.glob("*/manifest.json")):
        value = read_json(path)
        if value.get("build_identity") != build_identity or value.get("apt") != apt:
            continue
        instance = value["instance"]
        if all(_artifact_exists(home, config, instance, name)
               for name in config.get("outputs", {})):
            return value["apt_input"]
    apt_input = apt["signature"]
    while True:
        instance = fingerprint({**identity, "apt_input": apt_input})
        if not any(_artifact_exists(home, config, instance, name)
                   for name in config.get("outputs", {})):
            return apt_input
        apt_input = fingerprint(apt_input)


def _source_archive(home, source, log, name, *, show_progress):
    if not show_progress or os.environ.get("NO_COLOR"):
        return verified_archive(home, source, log)

    from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
    from .cli import _console

    console = _console(stderr=True)
    if not console.is_terminal or console.is_dumb_terminal:
        return verified_archive(home, source, log)
    with Progress(
        SpinnerColumn(), TextColumn("{task.description}", markup=False),
        BarColumn(bar_width=None), TaskProgressColumn(text_format_no_percentage=""),
        DownloadColumn(binary_units=True), console=console, transient=True,
        redirect_stdout=False, redirect_stderr=False,
    ) as display:
        task = None

        def update(completed, total):
            nonlocal task
            if task is None:
                task = display.add_task(f"Downloading {name}", completed=completed, total=total)
            else:
                display.update(task, completed=completed, total=total, refresh=True)

        return verified_archive(home, source, log, progress=update)


class _AptInstallProgress:
    """Drive one build package step with the apt install display."""

    def __init__(self, record, display, logs):
        self.record = record
        self.display = display
        self.verbose = display.verbose
        self.combined_logs = logs

    def __call__(self, message):
        self.record(message, component="APT", echo=False)
        self.display(message)

    def activity(self, description, *, download=False):
        self.record(description, component="APT", echo=False)
        return self.display.activity(description, download=download)

    def installation_plan(self, packages, overwrite=()):
        self.display.installation_plan(packages, overwrite)

    def output(self, stream, data):
        self.display.output(stream, data)

    def output_finished(self):
        self.display.output_finished()

    def command_failure(self, number, code, stderr):
        return self.display.command_failure(number, code, stderr)


def _step_title(step):
    name, _, action = step["id"].partition(".")
    if action.startswith("command-"):
        if name == "preflight":
            return "Preflight checks" if step["uses"] == "check" else "Preflight command"
        if name in {"checks", "acceptance"} or f"{name}.install" in step.get("needs", ()):
            return "Acceptance checks" if step["uses"] == "check" else "Acceptance command"
    if step["uses"] == "apt.install":
        return "Install dependencies" if name == "packages" else f"Install {name} dependencies"
    if step["uses"] == "cmake.project":
        return "Build " + step["id"].removesuffix(".install")
    if step["uses"] == "check":
        return f"Check {name}"
    return "Run " + step["id"]


def _require_spack_profile(selected, build_id=None):
    from .apt_profiles import backend_name

    if backend_name(selected) != "spack":
        build = f"Build {build_id}" if build_id else "This build"
        raise ForgeError(f"{build} requires a Spack apt profile. Current profile {selected.root.name} uses Homebrew.",
                         hint="Run jforge apt profile to list profiles, then select a Spack profile.")


def _check_host(home, selected_graph, saved_nodes=None, apt_profile=None):
    from .apt_profiles import active_home

    nodes = selected_graph["nodes"]
    saved_nodes = saved_nodes or {}
    for node in nodes.values():
        config = node["config"]
        platforms = config.get("platforms", [])
        if platforms and sys.platform not in platforms:
            supported = ", ".join(platforms)
            raise ForgeError(f"Build {config['id']} requires platform {supported}. Current platform is {sys.platform}.",
                             hint=f"Run this build on a supported host ({supported}).")
    for key, node in nodes.items():
        saved_steps = saved_nodes.get(key, {}).get("steps", {})
        if any(step["uses"] == "apt.install" and step["id"] not in saved_steps for step in node["ordered"]):
            target = ForgeHome(Path(apt_profile["path"])) if apt_profile else active_home(home)
            _require_spack_profile(target, node["config"]["id"])
            return {"backend": "spack", "path": str(target.root)}
    return apt_profile


def _configure_fingerprint(argv, script, source, environment):
    effective = cmake.capture(script, environment=environment, cwd=source)
    for key in ("_", "SHLVL", "PWD", "OLDPWD"):
        effective.pop(key, None)
    return fingerprint({"configure": argv, "environment": effective})


def _matches_legacy_configure(saved, requested, argv, logs, source, environment):
    if len(logs.parents) < 3:
        return False
    attempts = logs.parents[2]
    if attempts.name != "attempts":
        return False
    for attempt in attempts.iterdir():
        folder = attempt / logs.parent.name / logs.name
        archived = folder / "environment.sh"
        if not archived.is_file():
            continue
        if fingerprint({"configure": argv, "environment": digest(archived)}) != saved:
            continue
        runtime = folder / "runtime-environment.sh"
        script = runtime if runtime.is_file() else archived
        return _configure_fingerprint(argv, script, source, environment) == requested
    return False


def execute(home, reference=None, *, parameters=None, environment_name=None, jobs=1, resume=None, source=None, progress=None, verbose=False, as_json=False):
    from . import toolchains, unified_profiles
    from .apt_profiles import active_home
    from .env_entries import prepare_build_environment, require_outputs_off

    if jobs < 1:
        raise ForgeError("Jobs must be positive.")
    run_id = identifier(resume) if resume else None
    directory = home.root / "runs" / run_id if run_id else None
    if resume:
        home.require_initialized()
        request = read_json(directory / "request.json")
        if request.get("kind") != "build":
            raise ForgeError("This run is not a project build.")
        if source is not None:
            raise ForgeError("Resume uses the saved source checkout.")
        selected_graph = read_json(directory / "graph.json")
        environment_name = request.get("environment_name")
    else:
        selected_graph = graph(home, reference, parameters=parameters)
        _apply_source_override(selected_graph, source)
        apt_profile = _check_host(home, selected_graph)
        home.require_initialized()
        request = {"kind": "build", "run_id": run_id, "suite_id": selected_graph["nodes"][selected_graph["root"]]["config"]["id"],
                   "started_at": now(), "environment_name": environment_name,
                   "workspace": selected_graph["nodes"][selected_graph["root"]].get("workspace")}
        if apt_profile:
            request["apt_profile"] = apt_profile
        target = ForgeHome(Path(apt_profile["path"])) if apt_profile else active_home(home)
        if managed := unified_profiles.record(target):
            unified_profiles.verify(target, managed)
            request.update(profile_instance=managed["id"], profile_path=str(target.root))
            request["toolchain"] = managed["toolchain"]
        elif toolchain := toolchains.binding(target):
            request["toolchain"] = toolchain
    apt_profile = request.get("apt_profile")
    toolchain = request.get("toolchain")
    managed = None
    if request.get("profile_instance"):
        from .package_operations import _inside

        managed_home = ForgeHome(_inside(home, Path(request["profile_path"])))
        managed = unified_profiles.record(managed_home)
        if not managed or managed["id"] != request["profile_instance"] or toolchain != managed["toolchain"]:
            raise ForgeError("The saved build profile is unavailable or has changed.")
        if resume:
            unified_profiles.verify(managed_home, managed)
    excluded_outputs = {app_name(name) for node in selected_graph["nodes"].values()
                        for name in node["config"].get("environment", {}).get("outputs_off", ())}
    if excluded_outputs:
        runtime_profile = managed_home if managed else active_home(home)
        for node in selected_graph["nodes"].values():
            for name, child in node["requirements"].items():
                required = selected_graph["nodes"][child]["config"]
                output = required.get("outputs", {}).get(node["config"]["requires"][name]["output"], {})
                producer = next((step for step in required["steps"] if step["id"] == output.get("step")), {})
                package = producer.get("with", {}).get("export", {}).get("package", "")
                if excluded_outputs.intersection({app_name(required["id"]), app_name(package)}):
                    raise ForgeError(f"Build graph requires excluded output {required['id']}.")
    if not resume:
        run_id = new_id("build", root=home.root / "runs")
        directory = home.root / "runs" / run_id
        request["run_id"] = run_id
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / "run.lock"):
        state_path = directory / "graph.lock.json"
        state = read_json(state_path) if resume and state_path.exists() else {"schema_version": 1, "nodes": {}}
        if resume:
            _check_host(home, selected_graph, state["nodes"], apt_profile)
        if not resume:
            write_json(directory / "request.json", request)
            write_json(directory / "graph.json", selected_graph)
        attempts = directory / "attempts" / new_id("attempt", root=directory / "attempts")
        attempts.mkdir(parents=True)
        result = {**request, "path": str(directory), "attempt": str(attempts), "status": "running", "nodes": {}}
        write_json(directory / "result.json", result)
        from .build_logging import ProcessLog
        process_log = ProcessLog(attempts / "build.log", "Jarvis-Forge.Build", progress if verbose else None)
        active_step = None
        built_nodes = {}
        prepared_environment = None
        bindings_by_parent = state.setdefault("dependency_bindings", {})

        def event(message, component="Build", *, echo=True, display=None):
            with (attempts / "events.jsonl").open("a") as out:
                out.write(json.dumps({"at": now(), "message": message}) + "\n")
            if echo:
                process_log(message, module="Jarvis-Forge." + component)
                if progress and not verbose:
                    progress(display if display is not None else message)
            else:
                process_log.write(message, module="Jarvis-Forge." + component)

        def run_node(key, policy="reuse-or-build", requirement=None, locked_artifact=None):
            nonlocal active_step, prepared_environment
            node = selected_graph["nodes"][key]
            config = node["config"]
            config_env = config.get("environment", {})
            old = state["nodes"].get(key, {})
            node_dir = object_path(attempts, config["id"], {"node": key}, create=True)
            node_dir.mkdir(exist_ok=True)
            (node_dir / "build.yaml").write_text(node["text"])
            if old.get("environment"):
                environment = old["environment"]
                environment_origin = old.get("environment_origin", {})
                observed = cmake.tools(environment)
                if observed != old["tools"]:
                    raise ForgeError("Selected compiler/Python changed since the saved run.", code=1)
            else:
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
                if excluded_outputs and managed and prepared_environment is None:
                    prepared_environment, suppressed = prepare_build_environment(
                        home, runtime_profile, managed, excluded_outputs, os.environ, attempts / "profile-build.sh")
                    if suppressed:
                        event("Temporarily disabled for this build: " + ", ".join(suppressed))
                captured = cmake.capture(original, environment=prepared_environment)
                instance = managed["id"] if managed else None
                if (loaded := captured.get("JFORGE_PROFILE_INSTANCE")) and loaded != instance:
                    raise ForgeError(f"Shell profile {loaded} differs from the build profile {instance or 'legacy'}.",
                                     hint="Load the selected profile in a fresh shell before running this build.")
                if excluded_outputs:
                    require_outputs_off(home, runtime_profile, excluded_outputs, captured)
                environment = safe_environment({**captured, **variables}, variables)
                if managed:
                    environment = unified_profiles.build_environment(managed_home, managed, environment)
                    environment_origin["profile_instance"] = managed["id"]
                elif toolchain:
                    environment = toolchains.environment(toolchain, environment)
                    environment_origin["toolchain"] = toolchain["id"]
                observed = cmake.tools(environment)
            if excluded_outputs:
                require_outputs_off(home, runtime_profile, excluded_outputs, environment)
            context = {"tools": observed, "platform": sys.platform, "machine": os.uname().machine,
                       "cxx_standard": str(node["parameters"].get("cxx_standard", "unspecified"))}
            if managed:
                context["profile_instance"] = managed["id"]
            node_profile = apt_profile if any(s["uses"] == "apt.install" for s in node["ordered"]) else None
            apt = None
            if node_profile and not old.get("instance"):
                apt = _apt_snapshot(node_profile)
            if requirement:
                # A consumer's selected C++ standard is an explicit ABI requirement.
                if context["cxx_standard"] != requirement["context"]["cxx_standard"] or context["tools"] != requirement["context"]["tools"]:
                    raise ForgeError(f"Incompatible compiler/Python/C++ standard for dependency {config['id']}.")
                req = {**requirement, "build_id": config["id"]}
                existing = None
                if locked_artifact:
                    existing = artifact(home, locked_artifact)
                    if not compatible(existing, req, context):
                        raise ForgeError("Locked artifact is incompatible with its consumer.")
                elif req.get("artifact"):
                    existing = find_artifact(home, req, context)
                elif key in built_nodes:
                    return built_nodes[key]
                elif req["output"] in old.get("outputs", {}):
                    existing = artifact(home, old["outputs"][req["output"]]["artifact_id"])
                    if not compatible(existing, req, context):
                        raise ForgeError("Locked artifact is incompatible with its consumer.")
                elif policy != "rebuild":
                    existing = find_artifact(home, req, context, apt=apt)
                if existing:
                    row = {"outputs": {req["output"]: existing}, "status": "reused"}
                    result["nodes"].setdefault(key, row)
                    event(f"Reuse {config['id']}: {existing['artifact_id']}")
                    return row
                if policy == "reuse-only":
                    raise ForgeError(f"No compatible installed artifact for {config['id']}.")
            dependencies = {}
            locked = bindings_by_parent.setdefault(key, {})
            for name, child in node["requirements"].items():
                req = config["requires"][name]
                provided = run_node(child, req.get("policy", "reuse-or-build"), {**req, "context": context},
                                    locked_artifact=locked.get(name))
                if req["output"] not in provided["outputs"]:
                    raise ForgeError(f"Dependency does not export {req['output']}.")
                dependencies[name] = provided["outputs"][req["output"]]
                if not compatible(dependencies[name], {**req, "build_id": selected_graph["nodes"][child]["config"]["id"]}, context):
                    raise ForgeError(f"Dependency {name} is incompatible with this consumer.")
                locked[name] = dependencies[name]["artifact_id"]
                write_json(state_path, state)
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
            apt_input = build_identity = None
            if node_profile:
                identity["apt_profile"] = node_profile
                build_identity = fingerprint(identity)
                if old.get("instance"):
                    apt_input = old["apt_input"]
                else:
                    # Other nodes may have published dependencies since this node was entered.
                    apt = _apt_snapshot(node_profile)
                    apt_input = _find_apt_instance(home, config, identity, apt)
                identity["apt_input"] = apt_input
            instance = fingerprint(identity)
            if old.get("instance") and old["instance"] != instance:
                raise ForgeError("Build inputs changed; start a new run instead of resuming.")
            work = _instance_directory(
                workspace / "instances" if workspace else home.root / "builds/work",
                config, instance, installs=None if workspace else home.root / "builds/installs",
            )
            if workspace:
                from .build_workspace import link_run
                link_run(workspace, directory)
                event(f"Workspace: {workspace}; instance: {work}", echo=verbose)
            with file_lock(work / "instance.lock"):
                existing_outputs = {}
                for name in config.get("outputs", {}):
                    aid = artifact_key(home, config["id"], instance, name)
                    if aid is None:
                        continue
                    manifest = home.root / "builds/artifacts" / aid / "manifest.json"
                    if not manifest.exists():
                        continue
                    saved = read_json(manifest)
                    if workspace:
                        prefix = Path(saved["prefix"]) if isinstance(saved.get("prefix"), str) else Path(".")
                        # A deleted workspace leaves its manifest in Forge home.
                        # Keep it until a successful build replaces the output.
                        if (saved.get("artifact_id") == aid and saved.get("instance") == instance
                                and prefix.is_absolute() and prefix.resolve().is_relative_to(workspace / "instances")
                                and not prefix.exists() and not prefix.is_symlink()):
                            continue
                    try:
                        checked = artifact(home, aid)
                    except ForgeError:
                        # A manual rebuild in the same checkout changes the cache
                        # or required files. Rebuild in place and register again.
                        if not saved.get("in_place"):
                            raise
                        continue
                    if apt and checked.get("apt") != apt:
                        raise ForgeError("The apt dependency selection changed during this build. Start a new run.")
                    existing_outputs[name] = checked
                if existing_outputs and len(existing_outputs) == len(config.get("outputs", {})):
                    row = {"status": "reused", "outputs": existing_outputs, "instance": instance}
                    state["nodes"][key] = {**old, **row, "apt_input": apt_input,
                                           "environment": environment, "environment_origin": environment_origin,
                                           "tools": observed}
                    write_json(state_path, state)
                    result["nodes"][key] = row
                    built_nodes[key] = row
                    return row
                row = {"instance": instance, "environment": environment, "tools": observed, "steps": old.get("steps", {}),
                       "outputs": old.get("outputs", {}), "rebuild": identity.get("rebuild"),
                       "environment_origin": environment_origin, "apt_input": apt_input}
                state["nodes"][key] = row
                write_json(state_path, state)
                python = observed.get("python", {}).get("path", "python3")
                bindings = {"build_dir": str(parent), "workflow_dir": str(parent), "work": str(work),
                            "python": python, "project_python": python, "controller_python": sys.executable}
                for resource in node.get("card_sources", []):
                    if "script_id" not in resource:
                        continue
                    content = resource["content"].encode("utf-8")
                    if hashlib.sha256(content).hexdigest() != resource["sha256"]:
                        raise ForgeError("Modified companion script snapshot.")
                    frozen = object_path(work / "check-scripts", "check", {"sha256": resource["sha256"]}, create=True) / Path(resource["path"]).name
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
                        target = named_path(work / "sources", name, create=True)
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
                    total = len(node["ordered"])
                    title = _step_title(step)
                    active_step = {"build": config["id"], "id": step_id, "title": title,
                                   "index": index, "total": total}
                    project = app_name(config["id"]) + " · " if config["id"] != request["suite_id"] else ""
                    event(f"[{index}/{total}] {config['id']} / {step_id}",
                          display=f"[{index}/{total}] {project}{title}")
                    # Download sources only when the first consuming step is ready;
                    # ROOT preflight and apt dependencies run before its large archive.
                    for name, (source_spec, target) in list(pending_archives.items()):
                        if "${sources." + name + "}" not in json.dumps(step["with"]):
                            continue
                        marker = target / ".jforge-extracted"
                        if not marker.is_file():
                            log = node_dir / (name + "-download")
                            detail = f" ({log.with_suffix('.stderr.log')})" if verbose else ""
                            event(f"Download source {name}{detail}")
                            archive = _source_archive(
                                home, source_spec, log, name,
                                show_progress=progress is not None and not verbose and not as_json,
                            )
                            if workspace:
                                local_archive = object_path(work / "downloads", name, source_spec, create=True) / "archive"
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
                    step_dir = named_path(node_dir, step_id, create=True)
                    step_dir.mkdir()
                    try:
                        record = _step(home, step, options, work, step_dir, environment, observed,
                                       context, records, dependencies, jobs, event, row["steps"].get(step_id),
                                       workspace=workspace, verbose=verbose, as_json=as_json, apt_profile=node_profile,
                                       toolchain=toolchain, build_id=config["id"],
                                       output_exclusions=(runtime_profile, excluded_outputs) if excluded_outputs else None)
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
                    active_step = None
                outputs = {}
                for name, spec in config.get("outputs", {}).items():
                    producer = records[spec["step"]]
                    if not producer.get("export"):
                        raise ForgeError("Output has no installation export.")
                    metadata = producer["export"]
                    artifact_id = artifact_key(home, config["id"], instance, name, create=True)
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
                                 "evidence": evidence,
                                 "files": producer["files"] if producer.get("in_place") else cmake.manifest(Path(producer["prefix"]))}
                    if managed:
                        installed["profile_instance"] = managed["id"]
                    if producer.get("in_place"):
                        installed.update(in_place=True, cmake_cache=producer["cmake_cache"],
                                         cmake_cache_sha256=producer["cmake_cache_sha256"])
                    if node_profile:
                        last_apt = next(records[step["id"]] for step in reversed(node["ordered"])
                                        if step["uses"] == "apt.install")
                        installed.update(apt={"profile": node_profile, "signature": last_apt["apt_signature"]},
                                         build_identity=build_identity, apt_input=apt_input)
                    write_json(home.root / "builds/artifacts" / artifact_id / "manifest.json", installed)
                    outputs[name] = installed
                row["outputs"] = outputs
                write_json(state_path, state)
                finished = {"status": "passed", "outputs": outputs, "instance": instance}
                result["nodes"][key] = finished
                built_nodes[key] = finished
                return finished

        try:
            event(("Resume " if resume else "Build ") + app_name(request["suite_id"]), echo=not verbose)
            final = run_node(selected_graph["root"])
            from .shell_repair import artifact_activation

            outputs = {name: {**value, "activation": str(artifact_activation(home, value))}
                       for name, value in final["outputs"].items()}
            result.update(status="passed", outputs=outputs)
            if request.get("workspace"):
                from .build_workspace import publish
                publish(Path(request["workspace"]), result)
            try:
                from .env_entries import attach_root_build

                attached = attach_root_build(home, result)
                if attached:
                    result["environment_entry"] = attached["activation"]
            except ForgeError as exc:
                result["environment_entry_error"] = str(exc)
        except KeyboardInterrupt:
            result.update(status="interrupted", error="Interrupted.")
        except (ForgeError, OSError, ValueError) as exc:
            result.update(status="failed", error=str(exc))
            if isinstance(exc, ForgeError) and exc.hint:
                result["hint"] = exc.hint
        finally:
            if active_step is not None and result["status"] in {"failed", "interrupted"}:
                result["failed_step"] = active_step
            result["finished_at"] = now()
            process_log.write("Build " + result["status"] + (": " + result["error"] if result.get("error") else ""),
                              level="ERROR" if result["status"] == "failed" else "WARNING" if result["status"] == "interrupted" else "INFO")
            write_json(attempts / "result.json", result)
            write_json(directory / "result.json", result)
        return result


def _step(home, step, options, work, logs, environment, observed, context, records, dependencies, jobs, progress,
          previous, *, workspace=None, verbose=False, as_json=False, apt_profile=None, toolchain=None, build_id=None,
          output_exclusions=None):
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
    exported_dependencies = [path for v in used if (path := v.get("runtime_activation", v.get("activation")))]
    paths = {k: list(v) for k, v in options.get("paths", {}).items()}
    prefix_paths = [v["prefix"] for v in used if v.get("prefix")]
    prefix_paths.extend(package["prefix"] for dependency in used for package in dependency.get("packages", []))
    if prefix_paths:
        paths.setdefault("CMAKE_PREFIX_PATH", []).extend(prefix_paths)
    if step["uses"] == "cmake.project":
        write_json(logs / "dependency-paths.json", {key: list(dict.fromkeys(values)) for key, values in paths.items()})
        progress("Dependency paths: " + str(logs / "dependency-paths.json"), component="CMake", echo=verbose)
    archived_script = cmake.write_environment(logs / "environment.sh", base=environment, scripts=scripts, paths=paths)
    runtime_scripts = [str(cmake.runtime_activation(Path(path)))
                       if Path(path).name not in cmake.CHILD_ENVIRONMENT_FILES else path for path in scripts]
    script = archived_script
    if runtime_scripts != scripts:
        script = cmake.write_environment(logs / "runtime-environment.sh", base=environment, scripts=runtime_scripts, paths=paths)
    if output_exclusions and (scripts or paths):
        from .env_entries import require_outputs_off

        profile, names = output_exclusions
        if packages := sorted(names & {app_name(name) for name in selected_packages}):
            raise ForgeError(f"Package dependencies would activate excluded output {packages[0]}.")
        require_outputs_off(home, profile, names, cmake.capture(script, environment=environment))
    number = 0

    def invoke(argv, cwd, title="Command"):
        nonlocal number
        number += 1
        progress("Command: " + shlex.join(argv), echo=verbose,
                 component="CMake" if step["uses"] == "cmake.project" else "Checker" if step["uses"] == "check" else "Calculator")
        try:
            run_command(argv, cwd, script, logs, str(number), step.get("timeout_seconds", 86400),
                        progress=progress if verbose else None, process_env=environment, combined_logs=logs)
        except ForgeError as exc:
            command_record = logs / f"command-{number}.json"
            if not command_record.is_file():
                raise
            command = read_json(command_record)
            code = command.get("exit_code")
            if command["status"] != "failed" or not isinstance(code, int) or code <= 0:
                raise
            message = f"{title} failed (exit {code})."
            if not verbose:
                detail = (logs / f"command-{number}.stderr.log").read_text(errors="replace").strip()
                if detail:
                    message += "\n" + detail
            raise ForgeError(message, code=exc.code) from exc
    if step["uses"] == "apt.install":
        from . import spack_backend as backend

        project_python = observed.get("python", {}).get("path", "python3")
        if previous:
            for file, sha in previous["evidence"].items():
                if not Path(file).is_file() or digest(Path(file)) != sha:
                    raise ForgeError(f"Locked apt dependency changed: {file}")
            with process_environment(environment):
                backend.validate_project_python(ForgeHome(Path(previous["profile"])), previous["packages"],
                                                options["specs"], project_python)
            return previous
        from .apt_profiles import ensure_active
        from .apt_progress import AptProgress
        from .cli import _console
        from .toolchains import binding
        # Package dependencies belong in the selected apt profile. By default
        # that is spack-default, the same store and compilers as `jforge apt`.
        # The screen uses the same phase and package display as `jforge apt install`.
        display = AptProgress(_console(stderr=True), verbose=verbose, as_json=as_json, indent="  ")
        reporter = _AptInstallProgress(progress, display, logs)
        try:
            with process_environment(environment):
                target = ensure_active(home, progress=reporter,
                                       selected=ForgeHome(Path(apt_profile["path"])) if apt_profile else None)
                _require_spack_profile(target)
                if apt_profile and binding(target) != toolchain:
                    raise ForgeError("The project's toolchain binding changed since the build started.",
                                     hint="Start a new build run with the selected toolchain.")
                installed = backend.transact(target, "install", options["specs"], jobs=jobs,
                                             timeout=step.get("timeout_seconds", 86400), progress=reporter,
                                             project_python=project_python)
            display.close(success=True)
        finally:
            display.close()
        lock = Path(installed["lockfile"])
        activation = installed["activation"]
        adapter = Path(activation).parent / "spack-environment.sh"
        if adapter.is_file():
            activation = str(adapter)
        evidence = {str(lock): digest(lock)}
        evidence.update({str(p): digest(p) for p in Path(activation).parent.iterdir() if p.is_file()})
        return {"status": "passed", "packages": installed["packages"], "activation": activation,
                "apt_signature": _apt_signature(installed.get("roots", []), installed["packages"]),
                "evidence": evidence,
                "profile": str(target.root)}
    if step["uses"] == "check":
        from .standard_checks import execute
        report = execute(options["spec"], observed["python"]["path"], lambda argv: invoke(argv, work, "Check"), logs)
        if not verbose:
            detail = f" · Python {report['python']['version']}" if options["spec"].get("python") else ""
            progress("  Checks passed" + detail, component="Checker")
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
        exported = None
        if exported_dependencies:
            exported = str(cmake.write_environment(logs / "command-activate.sh", scripts=[
                cmake.runtime_activation(Path(path)) for path in exported_dependencies
            ]))
        return {"status": "passed", "artifacts": artifacts, "activation": str(script), "runtime_activation": exported}
    source = Path(options["source"])
    if not source.is_dir():
        raise ForgeError(f"CMake source not found: {source}")
    if options.get("binary_dir") == "preset":
        preset_name = options.get("configure_preset")
        if not isinstance(preset_name, str) or not preset_name:
            raise ForgeError("binary_dir preset requires configure_preset.")
        build = cmake.preset_binary_dir(source, preset_name)
    else:
        build = named_path(work / "build", step["id"], create=True)
    # An in-place artifact is the source tree. CBS writes CBS into that tree.
    prefix = source if options.get("in_place") else (
        named_path(work / "install", step["id"], create=True) if workspace else named_path(home.root / "builds/installs", step["id"], create=True) / work.name)
    lock = file_lock(source_cmake_lock(home, source), blocking=True,
                     message=f"CMake source is busy: {source}") if options.get("lock") == "source" else nullcontext()
    with lock:
        if options.get("in_place"):
            profile_instance = context.get("profile_instance")
            owner = build / ".jforge-profile.json"
            if owner.is_file():
                if read_json(owner).get("id") != profile_instance:
                    raise ForgeError("This checkout was configured under a different profile. Use a separate checkout.")
            elif profile_instance:
                if (build / "CMakeCache.txt").is_file():
                    raise ForgeError("This checkout already has a CMake build. Use a clean checkout for this profile.")
                write_json(owner, {"id": profile_instance})
        build.mkdir(parents=True, exist_ok=True)
        configured = json.loads(json.dumps(options))
        cache = configured.setdefault("cache", {})
        if toolchain:
            from .toolchains import cmake_runtime_paths

            cmake_runtime_paths(toolchain, cache)
            if "cmake" in toolchain.get("tools", {}):
                selected_cmake = toolchain["tools"]["cmake"]["commands"]["cmake"]
                requested = options.get("executable", "cmake")
                if requested != "cmake" and Path(requested).resolve() != Path(selected_cmake).resolve():
                    raise ForgeError(f"The build asks for {requested}, but the profile uses {selected_cmake}.")
                configured["executable"] = selected_cmake
        python_artifacts = {}
        for dependency in used:
            python = dependency.get("report", {}).get("python", {})
            if (python.get("development") and python["path"] == observed.get("python", {}).get("path")
                    and cache.get("Python3_EXECUTABLE", {}).get("value") == python["path"]):
                for variable, kind, path in (
                    ("Python3_INCLUDE_DIR", "PATH", str(Path(python["header"]).parent)),
                    ("Python3_LIBRARY", "FILEPATH", python["library"]),
                ):
                    if variable not in cache:
                        cache[variable] = {"type": kind, "value": path}
                        python_artifacts[variable] = path
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
        marker = build / ".jforge-configure.json"
        for phase in options.get("phases", ["configure", "build", "install"]):
            if phase == "configure":
                argv = invocation["configure"]
                requested = _configure_fingerprint(argv, script, source, environment)
                if marker.exists():
                    saved = read_json(marker)
                    if saved["fingerprint"] == requested or (saved.get("schema_version") is None
                            and _matches_legacy_configure(saved["fingerprint"], requested, argv, logs, source, environment)):
                        if saved.get("schema_version") != 2:
                            write_json(marker, {"schema_version": 2, "fingerprint": requested})
                        progress("Reuse CMake configuration")
                        continue
                    # A preset binary directory is shared by the checkout, so a source
                    # change reconfigures it instead of demanding a private build tree.
                    if options.get("binary_dir") != "preset":
                        raise ForgeError("CMake configuration changed; use a new build instance.")
                query = build / ".cmake/api/v1/query"
                query.mkdir(parents=True, exist_ok=True)
                for kind in ("codemodel-v2", "cache-v2", "toolchains-v1"):
                    (query / kind).touch()
            title = f"CMake {phase}"
            progress("  " + title, component="CMake", echo=not verbose)
            invoke(invocation[phase], source, title)
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
                for variable, resolved in (("Python3_INCLUDE_DIR", "_Python3_INCLUDE_DIR"),
                                           ("Python3_LIBRARY", "_Python3_LIBRARY_RELEASE")):
                    if variable in python_artifacts:
                        path = actual.get(resolved) or actual.get(variable)
                        if path and Path(path).resolve() != Path(python_artifacts[variable]).resolve():
                            raise ForgeError(f"CMake selected {path} for {variable}, expected {python_artifacts[variable]}.", code=1)
                write_json(logs / "cmake-cache.json", actual)
                shutil.copyfile(build / "CMakeCache.txt", logs / "CMakeCache.txt")
                write_json(marker, {"schema_version": 2, "fingerprint": requested})
        reply = build / ".cmake/api/v1/reply"
        if reply.is_dir():
            shutil.copytree(reply, logs / "cmake-file-api", dirs_exist_ok=True)
        cache_record = {}
        for name in options.get("required_files", []):
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ForgeError(f"Required file must stay inside the build prefix: {name}")
            file = prefix / name
            if not file.is_file():
                raise ForgeError(f"Installed file missing: {file}", code=1)
            if options.get("in_place"):
                cache_record.setdefault("files", {})[name] = (
                    {"link": os.readlink(file)} if file.is_symlink() else {"sha256": digest(file)})
        if options.get("in_place"):
            cache_file = build / "CMakeCache.txt"
            if not cache_file.is_file():
                raise ForgeError(f"In-place build has no CMake cache: {cache_file}", code=1)
            cache_record.update(in_place=True, files=cache_record.get("files", {}),
                                cmake_cache=str(cache_file), cmake_cache_sha256=digest(cache_file))
    metadata = options.get("export")
    if not metadata:
        return {"status": "passed", "prefix": str(prefix), "activation": str(script), **cache_record}
    if context["cxx_standard"] == "unspecified":
        actual_standard = cmake.cache_values(build).get("CMAKE_CXX_STANDARD")
        if actual_standard:
            raise ForgeError("Exported CMake projects must declare a matching cxx_standard parameter.")
    from .shell_repair import PATH_LISTS, _path_status, _variable_kind

    exported_paths = {}
    for name, values in metadata.get("paths", {}).items():
        paths = [str(prefix / value) for value in values]
        if name in PATH_LISTS:
            # Package layouts can have just lib or lib64. Check after install.
            kind = _variable_kind(name)
            paths = [path for path in paths if _path_status(path, kind)[0] != "invalid"]
        exported_paths[name] = paths
    activation_scripts = [cmake.runtime_activation(Path(path)) for path in exported_dependencies]
    if metadata.get("activation"):
        activation_scripts.append(prefix / metadata["activation"])
    for name in [metadata.get("cmake_dir"), metadata.get("activation")]:
        if name and not (prefix / name).exists():
            raise ForgeError(f"Export path missing: {prefix / name}", code=1)
    if options.get("in_place"):
        exported_paths.setdefault("PATH", []).insert(0, str(prefix))
    else:
        exported_paths.setdefault("CMAKE_PREFIX_PATH", []).insert(0, str(prefix))
    # Consumer environments must not inherit the producer's compiler/Python/PATH.
    # Dependency activation scripts and explicit paths carry the runtime closure.
    activation = work / (step["id"] + "-activate.sh")
    cmake.write_environment(activation, scripts=activation_scripts, paths=exported_paths,
                            profile_instance=context.get("profile_instance"), build_id=build_id)
    evidence = {str(activation): digest(activation)}
    for exported in activation_scripts:
        if exported.is_file():
            evidence[str(exported)] = digest(exported)
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
            "export": metadata, "evidence": evidence, **cache_record}


def step_path(home, run_id, step_name, *, pending=False):
    """Resolve a step in the latest attempt; qualify duplicates as build.step."""
    directory = home.run_path(run_id)
    result = read_json(directory / "result.json")
    attempt = Path(result["attempt"])
    graph_value = read_json(directory / "graph.json")
    matches = []
    for key, node in graph_value["nodes"].items():
        for step in node["ordered"]:
            if step_name in {step["id"], node["config"]["id"] + "." + step["id"]}:
                node_directory = object_path(attempt, node["config"]["id"], {"node": key})
                if node_directory is None:
                    continue
                candidate = named_path(node_directory, step["id"])
                if candidate.is_dir() or pending:
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
