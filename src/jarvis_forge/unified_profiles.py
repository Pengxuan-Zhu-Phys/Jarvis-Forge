"""Built-in compiler environments and host-local default selection."""

from __future__ import annotations

from importlib.resources import files
from itertools import count
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys

from .package_operations import _inside
from .storage import ForgeError, ForgeHome, digest, identifier, read_json, write_json, write_text

PYTHON_PROBE = (
    "import json,os,sys,sysconfig;print(json.dumps(dict(executable=os.path.abspath(sys.executable),"
    "version=sys.version.split()[0],prefix=sys.prefix,base_prefix=sys.base_prefix,abi=sysconfig.get_config_var('SOABI'))))"
)
X86_LEVELS = {"x86_64_v2", "x86_64_v3", "x86_64_v4"}
X86_LOADERS = ("/lib64/ld-linux-x86-64.so.2", "/lib/ld-linux-x86-64.so.2")


def presets():
    data = json.loads(files("jarvis_forge").joinpath("card/apt_profiles.json").read_text(encoding="utf-8"))
    if data["schema_version"] != 1:
        raise ForgeError("Unsupported built-in profile schema.")
    return data["profiles"]


def host():
    machine = platform.machine()
    if sys.platform == "linux":
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
        try:
            libc = os.confstr("CS_GNU_LIBC_VERSION") or ""
        except (OSError, ValueError):
            libc = ""
        return {"platform": "linux", "system": release.get("ID", "linux"),
                "version": release.get("VERSION_ID", ""), "machine": machine,
                "libc": libc}
    return {"platform": sys.platform, "system": sys.platform,
            "version": platform.mac_ver()[0], "machine": machine, "libc": ""}


def _host_id(current):
    label = "-".join(filter(None, (current["system"], current["version"],
                                  current["machine"], current["libc"])))
    return identifier(re.sub(r"[^A-Za-z0-9_.-]+", "-", label))


def selection_path(base):
    return _inside(base, base.root / "apt/profile-defaults" / (_host_id(host()) + ".json"))


def selection(base):
    path = selection_path(base)
    if path.is_file():
        data = read_json(path)
        if data.get("schema_version") != 1 or not isinstance(data.get("id"), str) or data.get("platform") != host()["platform"]:
            raise ForgeError(f"Invalid default profile record: {path}")
        identifier(data["id"])
        return data
    return None


def clear_default(base):
    selection_path(base).unlink(missing_ok=True)


def record(profile):
    path = _inside(profile, profile.root / "apt/profile.json")
    if not path.is_file():
        return None
    data = read_json(path)
    if data.get("schema_version") != 1 or data.get("id") != profile.root.name:
        raise ForgeError(f"Invalid unified profile record: {path}")
    return data


def _python(executable=None):
    executable = executable or shutil.which("python3")
    if not executable:
        raise ForgeError("Select an external Python with pyenv.")
    from .toolchains import _run

    return json.loads(_run([executable, "-I", "-c", PYTHON_PROBE]))


def _target(current, requested):
    machine = current["machine"]
    default = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(machine)
    if not default:
        raise ForgeError(f"Unsupported CPU architecture for built-in Linux profiles: {machine}.")
    if requested in (None, default):
        return default
    if machine == "x86_64" and requested in X86_LEVELS:
        from .toolchains import _run

        pattern = rf"(?m)^\s*{re.escape(requested.replace('_', '-'))}\s+\(supported,"
        for loader in X86_LOADERS:
            if Path(loader).is_file() and re.search(pattern, _run([loader, "--help"])):
                return requested
    raise ForgeError(f"CPU target {requested!r} could not be verified on this host.")


def _prerequisites(base, preset, current, target=None, *, source=True):
    if current["platform"] != preset["platform"]:
        raise ForgeError(f"Requires {preset['platform']}, current platform is {current['platform']}.")
    backend = preset["backend"]
    if backend == "spack":
        if not current["libc"].startswith("glibc "):
            raise ForgeError("Built-in Ubuntu presets require a glibc host.")
        target = _target(current, target)
        commands = ("bash", "cc", "c++", "make", "patch", "tar", "xz") if source else ("bash",)
    else:
        if target is not None:
            raise ForgeError("--target is supported for Linux profiles.")
        from .homebrew_backend import find_brew

        if not find_brew():
            raise ForgeError("Homebrew is unavailable.")
        commands = ("bash", "clang", "clang++")
    if missing := [command for command in commands if not shutil.which(command)]:
        raise ForgeError("Missing host tools: " + ", ".join(missing))
    python = _python()
    if any(Path(python[key]).resolve().is_relative_to(base.root.resolve()) for key in ("prefix", "base_prefix")):
        raise ForgeError("Select an external Python with pyenv.")
    return target, python


def _instances(base, current):
    for path in sorted((base.root / "apt/profiles").glob("*/apt/profile.json")):
        profile = ForgeHome(path.parent.parent)
        data = record(profile)
        if data["host"] == current:
            yield profile, data


def _reserve_instance(base, stem, key):
    from .builds import file_lock

    with file_lock(base.root / "apt/profile-builds" / (stem + ".lock"), blocking=True):
        for index in count(1):
            name = identifier(stem if index == 1 else f"{stem}-{index}")
            profile = ForgeHome(_inside(base, base.root / "apt/profiles" / name))
            inputs = profile.root / "apt/profile-input.json"
            if inputs.is_file():
                if read_json(inputs).get("fingerprint") == key:
                    return profile
            elif not profile.root.exists():
                write_json(inputs, {"schema_version": 1, "fingerprint": key, "layout": 2})
                return profile


def _commands(data):
    chain = data["toolchain"]
    compiler_names = {"CC": "gcc", "CXX": "g++", "FC": "gfortran"} if data["backend"] == "spack" else {
        "CC": "clang", "CXX": "clang++"}
    commands = {compiler_names[key]: path for key, path in chain["drivers"].items()}
    commands.update(cc=chain["drivers"]["CC"], **{"c++": chain["drivers"]["CXX"]})
    commands.update({command: path for tool in chain["tools"].values() for command, path in tool["commands"].items()})
    return commands


def _runtime_dirs(data):
    chain = data["toolchain"]
    if chain.get("external"):
        return []
    return list(dict.fromkeys(str(Path(path).parent) for path in chain.get("runtimes", {}).values()))


def build_environment(profile, data, environment):
    validate_python(data, environment)
    paths = [str(profile.root / "env/bin"), *environment.get("PATH", os.defpath).split(os.pathsep)]
    result = {**environment, **data["toolchain"]["drivers"], "JFORGE_PROFILE_INSTANCE": data["id"],
              "PATH": os.pathsep.join(dict.fromkeys(paths))}
    if paths := _runtime_dirs(data):
        paths.extend(filter(None, environment.get("LD_LIBRARY_PATH", "").split(os.pathsep)))
        result["LD_LIBRARY_PATH"] = os.pathsep.join(dict.fromkeys(paths))
    return result


def _activation(profile, data):
    script = profile.root / "env/toolchain.sh"
    bin_dir = profile.root / "env/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, path in _commands(data).items():
        link = bin_dir / name
        if not link.is_symlink() or os.readlink(link) != path:
            link.unlink(missing_ok=True)
            link.symlink_to(path)
    lines = [f"export JFORGE_PROFILE_INSTANCE={shlex.quote(data['id'])}",
             'export PATH=' + shlex.quote(str(bin_dir)) + '${PATH:+:$PATH}']
    if paths := _runtime_dirs(data):
        lines.append('export LD_LIBRARY_PATH=' + shlex.quote(os.pathsep.join(paths)) + '${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}')
    lines.extend(f"export {key}={shlex.quote(value)}" for key, value in data["toolchain"]["drivers"].items())
    content = "\n".join(lines) + "\n"
    if not script.is_file() or script.read_text() != content:
        write_text(script, content)
    return digest(script)


def activation_checks(data):
    expected = shlex.quote(json.dumps({key: data["python"][key] for key in
                                      ("executable", "version", "prefix", "base_prefix", "abi")}))
    probe = shlex.quote(PYTHON_PROBE)
    message = shlex.quote(f"Select Python {data['python']['executable']} with pyenv for {data['id']}.")
    lines = [f'if [ "$(command python3 -I -c {probe})" != {expected} ]; then',
             f"  printf '%s\\n' {message} >&2", "  return 1", "fi"]
    if data["target"] in X86_LEVELS:
        target = data["target"].replace("_", "-")
        pattern = shlex.quote(rf"^[[:space:]]*{target}[[:space:]]+\(supported,")
        lines.extend(["if ! (", f"  for _jforge_loader in {shlex.join(X86_LOADERS)}; do",
                      '    if [ -x "$_jforge_loader" ]; then "$_jforge_loader" --help; fi',
                      "  done", f") | command grep -E {pattern} >/dev/null; then",
                      f"  printf '%s\\n' 'CPU target {data['target']} is unavailable on this node.' >&2",
                      "  return 1", "fi"])
    return lines


def validate_python(data, environment):
    executable = shutil.which("python3", path=environment.get("PATH", os.defpath))
    from .toolchains import _run

    if not executable or json.loads(_run([executable, "-I", "-c", PYTHON_PROBE], env=environment)) != data["python"]:
        raise ForgeError(f"Select Python {data['python']['executable']} with pyenv for {data['id']}.")


def verify(profile, data):
    from .apt_profiles import require_healthy_profile

    require_healthy_profile(profile, data)
    if data["host"] != host():
        raise ForgeError(f"Profile {data['id']} belongs to a different host environment.")
    if data["backend"] == "spack":
        from .spack_backend import store_root
        from .toolchains import binding

        if data.get("layout") == 2:
            _inside(profile, store_root(profile))
            _inside(profile, Path(data["toolchain"]["profile"]))
        _target(data["host"], data["target"])
        if binding(profile) != data["toolchain"]:
            raise ForgeError(f"Compiler binding changed for profile {data['id']}.")
    else:
        for path, expected in data["fingerprints"].items():
            if not Path(path).is_file() or digest(Path(path)) != expected:
                raise ForgeError(f"Profile executable changed: {path}")
    if _python(data["python"]["executable"]) != data["python"]:
        raise ForgeError(f"External Python changed for profile {data['id']}.")
    state_path = profile.root / "apt/state.json"
    state = read_json(state_path) if state_path.is_file() else {}
    if not state.get("activation") or not _inside(profile, Path(state["activation"])).is_file():
        raise ForgeError(f"Package activation is missing for profile {data['id']}.")
    script = profile.root / "env/toolchain.sh"
    if not script.is_file() or digest(script) != data["activation_sha256"]:
        raise ForgeError(f"Profile toolchain activation has changed: {script}",
                         hint=f"jforge apt profile build {data['preset']}")
    for name, path in _commands(data).items():
        link = profile.root / "env/bin" / name
        if not link.is_symlink() or os.readlink(link) != path:
            raise ForgeError(f"Profile executable link has changed: {link}",
                             hint=f"jforge apt profile build {data['preset']}")


def check(base, name=None):
    current = host()
    choices = presets()
    instances = list(_instances(base, current))
    if name and name not in choices and not any(data["id"] == name for _, data in instances):
        raise ForgeError(f"Unknown profile preset or instance: {name}.")
    rows = []
    for key, preset in choices.items():
        matches = [(profile, data) for profile, data in instances if data["preset"] == key
                   and name in (None, key, data["id"])]
        if matches:
            for profile, data in matches:
                try:
                    verify(profile, data)
                except (ForgeError, OSError) as exc:
                    status, reason = "broken", str(exc)
                else:
                    try:
                        validate_python(data, os.environ)
                    except ForgeError as exc:
                        status, reason = "blocked", str(exc)
                    else:
                        status, reason = "ready", None
                rows.append({"name": key, "id": data["id"], "backend": data["backend"],
                             "status": status, "reason": reason, "target": data["target"]})
        elif name in (None, key):
            try:
                _prerequisites(base, preset, current)
            except ForgeError as exc:
                status = "blocked" if current["platform"] == preset["platform"] else "unsupported"
                reason = str(exc)
            else:
                status, reason = "buildable", None
            rows.append({"name": key, "backend": preset["backend"], "status": status, "reason": reason,
                         "versions": preset.get("versions")})
    return {"host": current, "profiles": rows, "default": (selection(base) or {}).get("id")}


def catalog(base):
    current = host()
    return {"presets": [{"name": name, **value} for name, value in presets().items()],
            "instances": [data for _, data in _instances(base, current)],
            "default": (selection(base) or {}).get("id")}


def _mac_install(profile, base, preset, jobs, timeout, progress):
    from . import homebrew_backend as brew
    from .toolchains import TOOL_COMMANDS, _run

    if not brew.configured(profile):
        brew.initialize(profile, base, brew.discover())
    result = brew.transact(profile, "install", preset["formulae"], jobs=jobs, timeout=timeout, progress=progress)
    drivers = {key: shutil.which(command) for key, command in (("CC", "clang"), ("CXX", "clang++"))}
    tools = {}
    for package in result["packages"]:
        formula = package["name"]
        if formula not in preset["formulae"]:
            continue
        prefix = Path(package["prefix"])
        key = "gmake" if formula == "make" else formula
        commands = {command: str(prefix / ("libexec/gnubin" if command == "make" else "bin") / command)
                    for command in TOOL_COMMANDS[key]}
        tools[key] = {"prefix": str(prefix), "version": package["version"], "commands": commands}
    if len(tools) != len(preset["formulae"]):
        raise ForgeError("Homebrew did not publish every requested build tool.", code=1)
    paths = [*drivers.values(), *(path for tool in tools.values() for path in tool["commands"].values())]
    for path in paths:
        _run([path, "--version"])
    return {"drivers": drivers, "tools": tools}, {path: digest(Path(path)) for path in paths}


def build(base, name, *, target=None, stage=None, jobs=1, timeout=14400, cache=None, progress=None):
    from . import apt_profiles, spack_backend, toolchains
    from .builds import file_lock, fingerprint
    from .env_entries import publish_apt
    from .package_operations import environment_path

    available = presets()
    if name not in available:
        raise ForgeError(f"Unknown profile preset: {name}.", hint="jforge apt profile")
    preset = available[name]
    current = host()
    backend = preset["backend"]
    if backend == "homebrew" and any(value is not None for value in (stage, cache, target)):
        raise ForgeError("--stage, --cache and --target apply to Linux profiles.")
    if cache is not None and cache not in {"auto", "only", "never"}:
        raise ForgeError("Choose auto, only or never for --cache.")
    target, python = _prerequisites(base, preset, current, target, source=cache != "only")
    if stage is not None:
        stage = stage.expanduser().resolve()
    if stage is not None and (not stage.is_dir() or not os.access(stage, os.W_OK | os.X_OK)):
        raise ForgeError(f"Profile build stage is not writable: {stage}")
    label = identifier(f"{name}-r{preset['revision']}-{_host_id(current)}" + (f"-{target}" if target else ""))
    inputs = {"preset": preset, "host": current, "target": target, "python": python}
    if backend == "spack":
        inputs["layout"] = 2
    key = fingerprint(inputs)
    profile = _reserve_instance(base, identifier(f"{backend}-{label}-py{python['version']}"), key)
    instance = profile.root.name
    with file_lock(profile.root / "profile-build.lock", blocking=True):
        if data := record(profile):
            _activation(profile, data)
            verify(profile, data)
            entry = publish_apt(base, profile, environment_path(profile))
            return {**data, "activation": entry["activation"], "status": "reused"}
        data = {"schema_version": 1, "id": instance, "preset": name, "revision": preset["revision"],
                "host": current, "target": target, "backend": backend, "python": python, "fingerprint": key}
        if backend == "spack":
            versions = preset["versions"]
            profile.initialize()
            data["layout"] = 2
            suite = toolchains.install(profile, "gcc@" + versions["gcc"], target=target, stage=stage,
                                       jobs=jobs, timeout=timeout, cache=cache or "never", progress=progress,
                                       instance="bootstrap", tools={k: v for k, v in versions.items() if k != "gcc"})
            toolchains.check(profile, "bootstrap")
            toolchains.check_tools(suite)
            apt_profiles.switch(base, name=instance.removeprefix("spack-"), select=False, progress=progress)
            selected = spack_backend.settings(profile)["python"]
            if any(Path(selected[field]).resolve() != Path(python[field]).resolve() for field in ("executable", "prefix")):
                raise ForgeError("Spack selected a different Python. Select it with pyenv before building the profile.")
            data["toolchain"] = {field: value for field, value in suite.items() if field != "status"}
            write_json(profile.root / "apt/toolchain.json", data["toolchain"])
        else:
            data["toolchain"], data["fingerprints"] = _mac_install(profile, base, preset, jobs, timeout, progress)
        data["activation_sha256"] = _activation(profile, data)
        verify(profile, data)
        entry = publish_apt(base, profile, environment_path(profile), profile_record=data)
        write_json(profile.root / "apt/profile.json", data)
        return {**data, "activation": entry["activation"], "status": "built"}


def set_default(base, name):
    from .env_entries import publish_apt
    from .package_operations import environment_path, locked

    identifier(name)
    matches = [(profile, data) for profile, data in _instances(base, host())
               if name in (data["id"], data["preset"])]
    if len(matches) != 1:
        hint = "jforge apt profile" if matches else f"jforge apt profile build {name}"
        message = "Choose an exact profile instance: " + ", ".join(data["id"] for _, data in matches) if matches else (
            f"No built profile matches {name!r} on this host.")
        raise ForgeError(message, hint=hint)
    profile, data = matches[0]
    with locked(base):
        verify(profile, data)
        validate_python(data, os.environ)
        entry = publish_apt(base, profile, environment_path(profile))
        write_json(profile.root / "env/selection.json", {"schema_version": 1, "name": "apt"})
        write_json(selection_path(base), {"schema_version": 1, "id": data["id"], "platform": data["host"]["platform"]})
    return {**data, "activation": entry["activation"], "status": "selected"}
