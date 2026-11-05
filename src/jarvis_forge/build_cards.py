"""Readable project cards normalized to the explicit build execution schema."""

from copy import deepcopy
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import re
import shlex

import yaml

from .storage import ForgeError, digest, identifier
from .suite import UniqueLoader


def default_card(name):
    resource = files("jarvis_forge").joinpath("card/" + name + ".yaml")
    text = resource.read_text()
    return yaml.load(text, Loader=UniqueLoader), hashlib.sha256(text.encode()).hexdigest()


def read_library(path):
    try:
        config = yaml.load(Path(path).read_text(), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read library card {path}: {exc}") from exc
    if not isinstance(config, dict) or set(config) != {"Library"}:
        raise ForgeError("Library card requires a Library section.")
    value = config["Library"]
    if (not isinstance(value, dict) or set(value) - {"name", "version", "build", "workflow"}
            or sum(key in value for key in ("build", "workflow")) != 1
            or not {"name", "version"}.issubset(value)
            or not isinstance(value["name"], str) or not isinstance(value["version"], str)
            or not isinstance(value.get("build", value.get("workflow")), dict)):
        raise ForgeError("Library card requires name, string version and build template.")
    return {"name": value["name"], "version": value["version"], "build": value.get("build", value.get("workflow"))}


def library_path(recipe, path, home=None, library_files=None):
    if library_files and recipe in library_files:
        return Path(library_files[recipe])
    for candidate in (path.parent / "library" / (recipe + ".yaml"), path.parent / recipe):
        if candidate.is_file():
            return candidate
    if home:
        from .build_catalog import libraries
        for entry in libraries(home):
            if entry["id"] == recipe:
                candidate = home.root / "builds/recipes" / entry["sha256"] / "library.yaml"
                if candidate.is_file() and digest(candidate) == entry["sha256"]:
                    return candidate
                raise ForgeError(f"Missing or modified library snapshot: {recipe}")
    raise ForgeError(f"Library card not found: {recipe}", hint="jforge build update; or place it in library/ next to your card")


def _keys(value, allowed, where):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ForgeError(f"Unknown or invalid fields in {where}.")


def _cache(options):
    if not isinstance(options, dict):
        raise ForgeError("installation.cmake.options must be a mapping.")
    result = {}
    for name, value in options.items():
        result[name] = value if isinstance(value, dict) else {
            "type": "BOOL" if isinstance(value, bool) else "STRING", "value": value}
    return result


def native_card(config):
    """Readable Forge project card; legacy module spelling stays an adapter detail."""
    _keys(config, {"name", "path", "environment", "platforms", "dependencies", "requires", "build", "checks"}, "project build")
    workflow = {"name": config["name"]}
    for key in ("path", "environment", "platforms"):
        if key in config:
            workflow[key] = config[key]
    checks = config.get("checks", {})
    _keys(checks, {"before", "after"}, "checks")
    def check_commands(stage):
        value = checks.get(stage)
        if not isinstance(value, dict) or not set(value).intersection({"python", "tools", "scripts"}):
            return value
        _keys(value, {"python", "tools", "scripts"}, f"checks.{stage}")
        commands = []
        standard = {k: v for k, v in value.items() if k != "scripts"}
        if standard:
            from .standard_checks import validate
            validate(standard)
            commands.append({"check": standard})
        scripts = value.get("scripts", [])
        if isinstance(scripts, str):
            scripts = [scripts]
        if not isinstance(scripts, list):
            raise ForgeError("Check scripts must be a list of relative paths or path/args mappings.")
        for item in scripts:
            item = {"path": item} if isinstance(item, str) else item
            _keys(item, {"path", "args"}, "check script")
            if not isinstance(item.get("path"), str) or not isinstance(item.get("args", []), list) or not all(isinstance(x, (str, int, float)) for x in item.get("args", [])):
                raise ForgeError("Check scripts require a path and scalar args.")
            args = [str(x).replace("${build.version}", str(config.get("build", {}).get("version", "")))
                    .replace("${build.features}", json.dumps(config.get("build", {}).get("features", []))) for x in item.get("args", [])]
            commands.append({"script": item["path"], "args": args})
        return commands
    before, after = check_commands("before"), check_commands("after")
    libraries, calculators, prerequisite_names = [], [], []
    if before:
        libraries.append({"name": "preflight", "execution": {"commands": before}})
        prerequisite_names.append("preflight")
    dependencies = config.get("dependencies", [])
    if dependencies:
        libraries.append({"name": "packages", "required_modules": list(prerequisite_names),
                          "installation": {"packages": dependencies}})
        prerequisite_names.append("packages")
    requires = config.get("requires", {})
    if not isinstance(requires, dict):
        raise ForgeError("requires must map dependency names to build references.")
    for name, request in requires.items():
        _keys(request, {"build", "workflow", "output", "version", "artifact"}, "build requirement")
        if name in prerequisite_names:
            raise ForgeError(f"Build requirement uses a reserved name: {name}")
        if "build" in request and "workflow" in request:
            raise ForgeError("Select build or the legacy workflow reference, not both.")
        reference = request.get("build", request.get("workflow"))
        if not isinstance(reference, str) or not reference:
            raise ForgeError("Build requirements must name a recipe or file.")
        libraries.append({"name": name, **{k: v for k, v in request.items() if k not in {"build", "workflow"}}, "workflow": reference})
        prerequisite_names.append(name)
    build = config.get("build")
    if build is not None:
        _keys(build, {"name", "version", "source", "cmake", "environment", "cmake_dir", "features", "required_files"}, "build")
        module = {"name": build.get("name", "project"), "version": str(build.get("version", "0")),
                  "required_modules": prerequisite_names,
                  "installation": {k: v for k, v in build.items() if k not in {"name", "version"}}}
        module["installation"].setdefault("cmake", {})
        if after:
            module["execution"] = {"commands": after}
        calculators.append(module)
    elif after:
        calculators.append({"name": "checks", "required_modules": prerequisite_names,
                            "execution": {"commands": after}})
    return {"Workflow": workflow, "LibDeps": {"Modules": libraries}, "Calculators": {"Modules": calculators}}


def normalize(config, path, home=None, library_files=None):
    """Expand native cards and compatible module aliases into explicit execution steps."""
    if "name" in config and "Workflow" not in config:
        config = native_card(config)
    _keys(config, {"Workflow", "LibDeps", "Calculators"}, "build card")
    workflow = config.get("Workflow", {})
    _keys(workflow, {"name", "path", "environment", "cxx_standard", "platforms"}, "Workflow")
    defaults, default_sha = default_card("build_default")
    provenance = [{"card": "jforge:build_default", "sha256": default_sha}]
    project = (path.parent / str(workflow.get("path", "."))).resolve()
    result = {"schema_version": 1, "kind": "build", "id": identifier(workflow.get("name", path.stem)),
              "steps": [], "sources": {}, "outputs": {}, "requires": {}, "parameters": {}}
    if workflow.get("environment"):
        result["environment"] = {"name": workflow["environment"]}
    if workflow.get("platforms"):
        result["platforms"] = workflow["platforms"]
    ends, module_deps, groups, calculator_outputs = {}, {}, [], []
    for group_name in ("LibDeps", "Calculators"):
        group = config.get(group_name, {})
        _keys(group, {"Modules"}, group_name)
        if not isinstance(group.get("Modules", []), list):
            raise ForgeError(f"{group_name}.Modules must be a list.")
        for module in group.get("Modules", []):
            _keys(module, {"name", "recipe", "version", "workflow", "output", "artifact", "path",
                           "required_modules", "installation", "execution"}, "module")
            name = identifier(module.get("name", ""))
            if name in ends:
                raise ForgeError(f"Duplicate module name: {name}")
            dependencies = module.get("required_modules", [])
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            if not isinstance(dependencies, list) or not all(isinstance(n, str) for n in dependencies):
                raise ForgeError("required_modules must contain module names.")
            module_deps[name] = dependencies
            if module.get("workflow"):
                if module.get("recipe") or module.get("installation") or module.get("execution"):
                    raise ForgeError("A build reference cannot also define a local installation/execution.")
                req = {"build": module["workflow"], "output": module.get("output", "installation")}
                if module.get("version"):
                    version = str(module["version"])
                    req["version"] = version if version.startswith(("=", ">", "<", "!", "~")) else "==" + version
                if module.get("artifact"):
                    req["artifact"] = module["artifact"]
                result["requires"][name] = req
                ends[name] = "requires." + name
                continue
            installation = module.get("installation", {})
            _keys(installation, {"cmake", "source", "packages", "environment", "cmake_dir", "features", "required_files"}, "installation")
            settings = installation.get("cmake", {})
            has_cmake = "cmake" in installation
            _keys(settings, {"preset", "build_preset", "test_preset", "options", "targets", "phases", "includes", "libraries", "runtime"}, "installation.cmake")
            overrides = _cache(settings.get("options", {}))
            if module.get("recipe"):
                recipe = module["recipe"]
                identifier(recipe)
                selected_path = library_path(recipe, path, home, library_files)
                library = read_library(selected_path)
                if module.get("version") and str(module["version"]) != library["version"]:
                    raise ForgeError(f"Library {recipe} supports {library['version']}; no checksum-pinned card for {module['version']}.")
                expanded = deepcopy(library["build"])
                if expanded.get("requires"):
                    raise ForgeError("Library templates with external requirements must use an explicit build reference.")
                provenance.append({"card": recipe, "sha256": digest(selected_path), "content": selected_path.read_text()})
                for step in expanded.get("steps", []):
                    step.setdefault("timeout_seconds", defaults["command" if step["uses"] == "command" else "build"]["timeout_seconds"])
                    if step.get("uses") == "cmake.project":
                        step["with"]["cache"] = {**deepcopy(defaults["cmake"]["cache"]), **step["with"].get("cache", {})}
                        step["with"].setdefault("phases", deepcopy(defaults["cmake"]["phases"]))
                        if step["with"].get("export"):
                            for variable, values in defaults["cmake"]["export"]["paths"].items():
                                step["with"]["export"].setdefault("paths", {}).setdefault(variable, deepcopy(values))
                        step["with"].setdefault("cache", {}).update(overrides)
                        for key in ("build_preset", "test_preset", "targets", "phases"):
                            if key in settings:
                                step["with"][key] = settings[key]
                        if settings.get("preset"):
                            step["with"]["configure_preset"] = settings["preset"]
                standard = workflow.get("cxx_standard", overrides.get("CMAKE_CXX_STANDARD", {}).get("value", expanded.get("parameters", {}).get("cxx_standard")))
                if standard is not None:
                    expanded.setdefault("parameters", {})["cxx_standard"] = standard
                    for step in expanded["steps"]:
                        if step["uses"] == "cmake.project":
                            step["with"]["cache"]["CMAKE_CXX_STANDARD"] = {"type": "STRING", "value": "${parameters.cxx_standard}"}
                step_ids = {s["id"] for s in expanded["steps"]}
                source_ids = set(expanded.get("sources", {}))
                # Rewrite only engine placeholders, never arbitrary command text.
                def namespace(value):
                    if isinstance(value, str):
                        for old in step_ids:
                            value = value.replace("${steps." + old + ".", "${steps." + name + "." + old + ".")
                        for old in source_ids:
                            value = value.replace("${sources." + old + "}", "${sources." + name + "." + old + "}")
                        return value
                    if isinstance(value, list):
                        return [namespace(v) for v in value]
                    if isinstance(value, dict):
                        return {k: namespace(v) for k, v in value.items()}
                    return value
                expanded = namespace(expanded)
                for step in expanded["steps"]:
                    step["id"] = name + "." + step["id"]
                    step["needs"] = [name + "." + n for n in step.get("needs", [])]
                    if "dependencies" in step["with"]:
                        step["with"]["dependencies"] = [name + "." + n for n in step["with"]["dependencies"]]
                for key, value in expanded.get("parameters", {}).items():
                    if key in result["parameters"] and result["parameters"][key] != value:
                        raise ForgeError(f"Modules request conflicting parameter {key}.")
                    result["parameters"][key] = value
                result["sources"].update({name + "." + k: v for k, v in expanded.get("sources", {}).items()})
                if expanded.get("platforms"):
                    supported = set(expanded["platforms"])
                    supported &= set(result.get("platforms", supported))
                    if not supported:
                        raise ForgeError("Modules have incompatible supported platforms.")
                    result["platforms"] = sorted(supported)
                output = deepcopy(expanded["outputs"]["installation"])
                output["step"] = name + "." + output["step"]
                output["publish_after"] = [name + "." + x for x in output["publish_after"]]
                result["outputs"][name] = output
                result["steps"].extend(expanded["steps"])
                ends[name] = output["publish_after"][-1]
                groups.append((name, [s["id"] for s in expanded["steps"]], output["step"]))
            else:
                module_steps = []
                source = installation.get("source", module.get("path"))
                if installation.get("packages"):
                    if has_cmake or source or module.get("execution"):
                        raise ForgeError("A package module cannot also define a project build or commands.")
                    packages = installation["packages"]
                    if not isinstance(packages, list) or not packages or not all(isinstance(p, str) for p in packages):
                        raise ForgeError("installation.packages must be a nonempty list of Spack specs.")
                    module_steps.append({"id": name + ".install", "uses": "apt.install",
                                         "timeout_seconds": defaults["build"]["timeout_seconds"], "with": {"specs": packages}})
                if has_cmake:
                    if not source:
                        raise ForgeError(f"CMake module {name} requires path or installation.source.")
                    if isinstance(source, dict):
                        _keys(source, {"url", "sha256", "subdir", "timeout_seconds"}, "installation.source")
                        source = deepcopy(source)
                        source.setdefault("timeout_seconds", defaults["source"]["timeout_seconds"])
                        result["sources"][name] = source
                    else:
                        source = str(source).replace("&J", str(project))
                        source = str((project / source).resolve())
                        result["sources"][name] = {"path": source}
                    base_cache = {} if settings.get("preset") else deepcopy(defaults["cmake"]["cache"])
                    options = {"source": "${sources." + name + "}", "cache": {**base_cache, **overrides},
                               "phases": settings.get("phases", defaults["cmake"]["phases"]),
                               "export": {**deepcopy(defaults["cmake"]["export"]), "package": name, "version": str(module.get("version", "0"))}}
                    export = options["export"]
                    for key, target_key in (("environment", "activation"), ("cmake_dir", "cmake_dir"), ("features", "features")):
                        if key in installation:
                            export[target_key] = installation[key]
                    if installation.get("required_files"):
                        options["required_files"] = installation["required_files"]
                    runtime_packages, runtime_paths = [], []
                    for field, variable in (("includes", "CPATH"), ("libraries", "LIBRARY_PATH"), ("runtime", "LD_LIBRARY_PATH")):
                        values = settings.get(field, [])
                        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                            raise ForgeError(f"installation.cmake.{field} must be a list of package names or package:subdirectory entries.")
                        paths = []
                        for entry in values:
                            package, separator, relative = entry.partition(":")
                            from .storage import package_name
                            package_name(package)
                            if separator and (not relative or Path(relative).is_absolute() or ".." in Path(relative).parts):
                                raise ForgeError("Dependency subdirectories must stay inside their package prefix.")
                            subdirs = [relative] if separator else ["include"] if field == "includes" else ["lib64", "lib"]
                            paths.extend("${packages." + package + ".prefix}/" + subdir for subdir in subdirs)
                            runtime_packages.append(package)
                        if paths:
                            options.setdefault("paths", {})[variable] = paths
                            cmake_variable = {"includes": "CMAKE_INCLUDE_PATH", "libraries": "CMAKE_LIBRARY_PATH"}.get(field)
                            if cmake_variable:
                                options["paths"][cmake_variable] = list(paths)
                        if field == "runtime":
                            runtime_paths = paths
                    if runtime_packages:
                        export["runtime_packages"] = list(dict.fromkeys(runtime_packages))
                    if runtime_paths:
                        for variable in ("CMAKE_BUILD_RPATH", "CMAKE_INSTALL_RPATH"):
                            options["cache"].setdefault(variable, {"type": "STRING", "value": runtime_paths})
                        options["cache"].setdefault("CMAKE_INSTALL_RPATH_USE_LINK_PATH", {"type": "BOOL", "value": True})
                        export["paths"]["LD_LIBRARY_PATH"] = [*runtime_paths, *export["paths"].get("LD_LIBRARY_PATH", [])]
                    standard = workflow.get("cxx_standard", overrides.get("CMAKE_CXX_STANDARD", {}).get("value", result["parameters"].get("cxx_standard")))
                    if standard is not None:
                        result["parameters"]["cxx_standard"] = standard
                        options["cache"]["CMAKE_CXX_STANDARD"] = {"type": "STRING", "value": "${parameters.cxx_standard}"}
                    for old, new in (("preset", "configure_preset"), ("build_preset", "build_preset"), ("test_preset", "test_preset"), ("targets", "targets")):
                        if old in settings:
                            options[new] = settings[old]
                    step_id = name + ".install"
                    module_steps.append({"id": step_id, "uses": "cmake.project", "timeout_seconds": defaults["build"]["timeout_seconds"], "with": options})
                    result["outputs"][name] = {"step": step_id, "publish_after": [step_id]}
                execution = module.get("execution", {})
                _keys(execution, {"path", "commands"}, "execution")
                def command_path(value):
                    value = str(value).replace("&J", str(project))
                    return value if "${" in value else str((project / value).resolve())
                cwd = command_path(execution.get("path", module.get("path", "${work}" if has_cmake else ".")))
                commands = execution.get("commands", [])
                if not isinstance(commands, list):
                    commands = [commands]
                for index, command in enumerate(commands, 1):
                    command_cwd = cwd
                    if isinstance(command, dict) and set(command) == {"check"}:
                        from .standard_checks import validate
                        validate(command["check"])
                        options = {"spec": command["check"]}
                        if module_steps and has_cmake:
                            options["dependencies"] = [module_steps[0]["id"]]
                        needs = [module_steps[-1]["id"]] if module_steps else []
                        if module_steps and has_cmake:
                            needs = list(dict.fromkeys([module_steps[0]["id"], *needs]))
                        module_steps.append({"id": name + ".command-" + str(index), "uses": "check",
                            "needs": needs, "with": options, "timeout_seconds": defaults["command"]["timeout_seconds"]})
                        continue
                    if isinstance(command, dict) and "script" in command:
                        _keys(command, {"script", "args"}, "check script")
                        relative = command["script"]
                        if not isinstance(relative, str) or "\\" in relative or Path(relative).suffix != ".py":
                            raise ForgeError("Companion checks require relative Python script paths.")
                        source_script = path.parent / relative
                        if (Path(relative).is_absolute() or ".." in Path(relative).parts
                                or not source_script.resolve().is_relative_to(path.parent.resolve()) or not source_script.is_file()):
                            raise ForgeError(f"Companion check script must be an existing relative file: {relative}")
                        try:
                            content = source_script.read_bytes().decode("utf-8")
                        except (OSError, UnicodeError) as exc:
                            raise ForgeError(f"Cannot read companion Python script: {relative}: {exc}") from exc
                        script_id = "check-" + hashlib.sha256(relative.encode()).hexdigest()[:16]
                        if not any(item.get("script_id") == script_id for item in provenance):
                            provenance.append({"card": "script", "script_id": script_id, "path": relative,
                                               "sha256": digest(source_script), "content": content})
                        command = ["${python}", "${scripts." + script_id + "}", *command.get("args", [])]
                    if isinstance(command, dict):
                        _keys(command, {"cmd", "command", "cwd"}, "execution command")
                        command_cwd = command_path(command.get("cwd", cwd))
                        command = command.get("cmd", command.get("command"))
                    if isinstance(command, list) and command and all(isinstance(x, str) for x in command):
                        argv = command
                    elif isinstance(command, str) and command.strip():
                        argv = ["bash", "-e", "-o", "pipefail", "-c", command]
                        parts = shlex.split(command)
                        if len(parts) == 2 and parts[0] == "cd":
                            cwd = (command_cwd.rstrip("/") + "/" + parts[1]) if "${" in command_cwd else str((Path(command_cwd) / parts[1]).resolve())
                    else:
                        raise ForgeError("execution.commands entries must be shell strings, argv lists or cmd/cwd mappings.")
                    module_path = "${steps." + name + ".install.prefix}" if has_cmake else command_cwd
                    source_value = "${sources." + name + "}" if isinstance(source, dict) else str((project / str(source).replace("&J", str(project))).resolve()) if source else command_cwd
                    argv = [x.replace("&J", str(project)).replace("${path}", module_path)
                            .replace("${source}", source_value) for x in argv]
                    options = {"argv": argv, "cwd": command_cwd}
                    if module_steps:
                        options["dependencies"] = [module_steps[0]["id"]] if has_cmake else []
                    module_steps.append({"id": name + ".command-" + str(index), "uses": "command",
                        "needs": [module_steps[-1]["id"]] if module_steps else [],
                        "timeout_seconds": defaults["command"]["timeout_seconds"], "with": options})
                    if has_cmake and "dependencies" in options:
                        module_steps[-1]["needs"] = list(dict.fromkeys([module_steps[0]["id"], *module_steps[-1]["needs"]]))
                if not module_steps:
                    raise ForgeError(f"Module {name} requires a recipe, build reference, CMake installation or execution.commands.")
                result["steps"].extend(module_steps)
                ends[name] = module_steps[-1]["id"]
                if name in result["outputs"]:
                    result["outputs"][name]["publish_after"] = [ends[name]]
                    if group_name == "Calculators":
                        calculator_outputs.append(name)
                groups.append((name, [s["id"] for s in module_steps], module_steps[0]["id"]))
    for name, step_ids, installation_step in groups:
        needed = module_deps[name]
        if set(needed) - set(ends):
            raise ForgeError(f"Unknown required_modules for {name}: {set(needed) - set(ends)}")
        for step in result["steps"]:
            if step["id"] in step_ids:
                step.setdefault("needs", []).extend(ends[n] for n in needed if not ends[n].startswith("requires."))
                if needed and step["uses"] != "apt.install":
                    refs = ["requires." + n if ends[n].startswith("requires.") else
                            result["outputs"][n]["step"] if n in result["outputs"] else ends[n] for n in needed]
                    step["with"].setdefault("dependencies", []).extend(refs)
                    step["needs"].extend(r for r in refs if not r.startswith("requires."))
    if len(result["outputs"]) == 1:
        result["outputs"] = {"installation": next(iter(result["outputs"].values()))}
    elif len(calculator_outputs) == 1:
        result["outputs"]["installation"] = deepcopy(result["outputs"][calculator_outputs[0]])
    return result, provenance
