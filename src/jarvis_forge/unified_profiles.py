"""Built-in compiler environments and host-local default selection."""

from __future__ import annotations

from importlib.resources import files
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import sys

from .storage import (
    ForgeError,
    ForgeHome,
    digest,
    identifier,
    read_json,
    write_json,
    write_text,
    now,
)

PYTHON_PROBE = (
    "import json,os,sys,sysconfig;print(json.dumps(dict(executable=os.path.abspath(sys.executable),"
    "version=sys.version.split()[0],prefix=sys.prefix,base_prefix=sys.base_prefix,abi=sysconfig.get_config_var('SOABI'))))"
)
X86_LEVELS = {"x86_64_v2", "x86_64_v3", "x86_64_v4"}
X86_LOADERS = ("/lib64/ld-linux-x86-64.so.2", "/lib/ld-linux-x86-64.so.2")


def presets():
    data = json.loads(
        files("jarvis_forge")
        .joinpath("card/apt_profiles.json")
        .read_text(encoding="utf-8")
    )
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
        return {
            "platform": "linux",
            "system": release.get("ID", "linux"),
            "version": release.get("VERSION_ID", ""),
            "machine": machine,
            "libc": libc,
        }
    return {
        "platform": sys.platform,
        "system": sys.platform,
        "version": platform.mac_ver()[0],
        "machine": machine,
        "libc": "",
    }


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
        raise ForgeError(
            f"Unsupported CPU architecture for built-in Linux profiles: {machine}."
        )
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
        raise ForgeError(
            f"Requires {preset['platform']}, current platform is {current['platform']}."
        )
    backend = preset["backend"]
    if backend == "spack":
        if not current["libc"].startswith("glibc "):
            raise ForgeError("Built-in Ubuntu presets require a glibc host.")
        target = _target(current, target)
        commands = ("bash", "cc", "c++", "make", "tar", "xz") if source else ("bash",)
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
    if any(
        Path(python[key]).resolve().is_relative_to(base.root.resolve())
        for key in ("prefix", "base_prefix")
    ):
        raise ForgeError("Select an external Python with pyenv.")
    return target, python


def _commands(data):
    chain = data["toolchain"]
    compiler_names = (
        {"CC": "gcc", "CXX": "g++", "FC": "gfortran"}
        if data["backend"] == "spack"
        else {"CC": "clang", "CXX": "clang++"}
    )
    commands = {compiler_names[key]: path for key, path in chain["drivers"].items()}
    commands.update(cc=chain["drivers"]["CC"], **{"c++": chain["drivers"]["CXX"]})
    commands.update(
        {
            command: path
            for tool in chain["tools"].values()
            for command, path in tool["commands"].items()
        }
    )
    return commands


def _runtime_dirs(data):
    chain = data["toolchain"]
    if chain.get("external"):
        return []
    return list(
        dict.fromkeys(
            str(Path(path).parent) for path in chain.get("runtimes", {}).values()
        )
    )


def _mac_install(profile, base, preset, jobs, timeout, progress, confirm=None):
    from . import homebrew_backend as brew
    from .toolchains import TOOL_COMMANDS, _run

    if not brew.configured(profile):
        brew.initialize(profile, base, brew.discover())
    result = brew.transact(
        profile,
        "install",
        preset["formulae"],
        jobs=jobs,
        timeout=timeout,
        progress=progress,
        confirm=confirm,
    )
    drivers = {
        key: shutil.which(command)
        for key, command in (("CC", "clang"), ("CXX", "clang++"))
    }
    tools = {}
    for package in result["packages"]:
        formula = package["name"]
        if formula not in preset["formulae"]:
            continue
        prefix = Path(package["prefix"])
        key = "gmake" if formula == "make" else formula
        commands = {
            command: str(
                prefix / ("libexec/gnubin" if command == "make" else "bin") / command
            )
            for command in TOOL_COMMANDS[key]
        }
        tools[key] = {
            "prefix": str(prefix),
            "version": package["version"],
            "commands": commands,
        }
    if len(tools) != len(preset["formulae"]):
        raise ForgeError("Homebrew did not publish every requested build tool.", code=1)
    paths = [
        *drivers.values(),
        *(path for tool in tools.values() for path in tool["commands"].values()),
    ]
    for path in paths:
        _run([path, "--version"])
    return {"external": True, "drivers": drivers, "tools": tools}, {
        path: digest(Path(path)) for path in paths
    }


def _host_id(current):
    return "|".join(
        str(current[key])
        for key in ("platform", "system", "version", "machine", "libc")
    )


def base_home(home):
    marker = home.root / "config.json"
    value = read_json(marker) if marker.is_file() else {}
    return ForgeHome(
        Path(value.get("forge_home") or value.get("apt_base_home") or home.root)
    )


def selection_path(base):
    return base_home(base).root / "profiles/selection.json"


def reject_old(base):
    base = base_home(base)
    config = base.root / "config.json"
    if config.is_file() and read_json(config).get("layout_version") != 4:
        raise ForgeError(
            f"Unsupported Forge layout in {base.root}.",
            hint="Create a fresh Forge home: jforge init --home /path/to/new-forge",
        )
    if (base.root / "apt/selection.json").exists() or (
        base.root / "apt/profile-defaults"
    ).exists():
        raise ForgeError(
            f"Unsupported profile layout in {base.root}.",
            hint="Create a fresh Forge home: jforge init --home /path/to/new-forge",
        )


def selection(base):
    reject_old(base)
    path = selection_path(base)
    if not path.is_file():
        return None
    value = read_json(path)
    if value.get("schema_version") != 3 or not isinstance(value.get("hosts"), dict):
        raise ForgeError(f"Invalid profile selection index: {path}")
    return value["hosts"].get(_host_id(host()))


def clear_default(base):
    from .paths import directory_lock

    path = selection_path(base)
    if not path.is_file():
        return
    with (
        directory_lock(base_home(base).root / "profiles", ".profiles.lock"),
        directory_lock(path.parent, ".selection.lock"),
    ):
        if not path.is_file():
            return
        index = read_json(path)
        if index.get("schema_version") != 3 or not isinstance(index.get("hosts"), dict):
            raise ForgeError(f"Invalid profile selection index: {path}")
        index["hosts"].pop(_host_id(host()), None)
        write_json(path, index)


def record(profile):
    file = profile.root / "metadata.json"
    if not file.is_file():
        return None
    value = read_json(file)
    if value.get("kind") != "profile":
        return None
    if value.get("schema_version") != 3 or value.get("id") != profile.root.name or value.get("model") != "builtin-profile/1":
        raise ForgeError(f"Unsupported profile metadata: {file}")
    return value


def tools_record(profile):
    file = profile.root / "tools/metadata.json"
    value = read_json(file)
    if value.get("schema_version") != 3 or value.get("kind") != "tool-suite" or value.get("id") != profile.root.name:
        raise ForgeError(f"Invalid profile tool-suite metadata: {file}")
    return value


def compatible(data, current=None):
    current = current or host()
    saved = data["host"]
    if (
        saved["platform"] != current["platform"]
        or saved["machine"] != current["machine"]
    ):
        raise ForgeError(
            f"Profile {data['id']} requires {saved['platform']}/{saved['machine']}."
        )
    if current["platform"] == "linux":

        def version(value):
            match = re.fullmatch(r"glibc (\d+)\.(\d+)(?:\.(\d+))?", value)
            if not match:
                raise ForgeError("A compatible glibc host is required.")
            return tuple(int(part or 0) for part in match.groups())

        if version(current["libc"]) < version(saved["libc"]):
            raise ForgeError(
                f"Profile {data['id']} requires {saved['libc']}; current host has {current['libc']}.",
                hint="Select a compatible profile or a separate Forge home on this host.",
            )
        _target(current, data["target"])


def verify(profile, data):
    platform_data = tools_record(profile)
    compatible(platform_data)
    if platform_data.get("status") != "ready" or data.get("status") != "ready":
        raise ForgeError(f"Profile {data['id']} is not ready.")
    if data["backend"] == "spack":
        from .toolchains import _verify

        _verify(platform_data["toolchain"])
    else:
        for path, checksum in platform_data["fingerprints"].items():
            if not Path(path).is_file() or digest(Path(path)) != checksum:
                raise ForgeError(f"Profile tool changed: {path}")
    if _python(data["python"]["executable"]) != data["python"]:
        raise ForgeError(f"External Python changed for profile {data['id']}.")
    from .apt_profiles import require_healthy_profile

    require_healthy_profile(profile, data)


def validate_python(data, environment):
    from .toolchains import _run

    executable = shutil.which("python3", path=environment.get("PATH", os.defpath))
    if (
        not executable
        or json.loads(_run([executable, "-I", "-c", PYTHON_PROBE], env=environment))
        != data["python"]
    ):
        raise ForgeError(
            f"Select Python {data['python']['executable']} with pyenv for {data['id']}."
        )


def activation_checks(data):
    # Checks run before modifying the calling shell; failures return, never exit.
    expected = shlex.quote(json.dumps(data["python"]))
    lines = [
        f'if [ "$(command python3 -I -c {shlex.quote(PYTHON_PROBE)})" != {expected} ]; then',
        f"  printf '%s\\n' {shlex.quote('Select Python ' + data['python']['executable'] + ' with pyenv for ' + data['id'] + '.')} >&2",
        "  return 1",
        "fi",
    ]
    platform_data = data.get("tools", data)
    script = (
        "import os,platform,sys; h=" + repr(platform_data["host"]) + "; "
        'assert sys.platform==h["platform"] and platform.machine()==h["machine"], "Incompatible platform architecture"; '
    )
    if platform_data["host"]["platform"] == "linux":
        script += (
            'v=lambda s:tuple(map(int,s.split()[1].split("."))); '
            'assert v(os.confstr("CS_GNU_LIBC_VERSION"))>=v(h["libc"]), "Incompatible glibc"'
        )
    lines += [f"command python3 -I -c {shlex.quote(script)} || return 1"]
    if data["target"] in X86_LEVELS:
        target = data["target"].replace("_", "-")
        lines += [
            "if ! (",
            f"  for _jf_loader in {shlex.join(X86_LOADERS)}; do",
            '    if [ -x "$_jf_loader" ]; then "$_jf_loader" --help; fi',
            "  done",
            f") | command grep -E {shlex.quote('^[[:space:]]*' + target + '[[:space:]]+[(]supported,')} >/dev/null; then",
            f"  printf '%s\\n' 'CPU target {data['target']} is unavailable.' >&2",
            "  return 1",
            "fi",
        ]
    return lines


def _scoped_home(base, parent, name):
    from .paths import named_path
    from .package_operations import _inside

    base = base_home(base)
    return ForgeHome(_inside(base, named_path(base.root / parent, identifier(name))))


def _initialize_scope(base, scope):
    scope.initialize()
    from .scope_locks import initialize
    initialize(scope)
    (scope.root / "locks/apps.lock").touch(exist_ok=True)
    config = read_json(scope.root / "config.json")
    config.update(
        forge_home=str(base_home(base).root),
        apt_base_home=str(base_home(base).root),
        layout_version=4,
    )
    write_json(scope.root / "config.json", config)


def default_stage(scope):
    """Node-local disposable compilation data; persistent state stays in Forge."""
    import tempfile

    parent = (
        Path(
            os.environ.get("SLURM_TMPDIR")
            or os.environ.get("TMPDIR")
            or tempfile.gettempdir()
        )
        .expanduser()
        .resolve()
    )
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise ForgeError(
            f"Node temporary directory is not writable: {parent}",
            hint="Use profile create --stage DIR.",
        )
    root = parent / f"jforge-stages-{os.getuid()}"
    if root.is_symlink():
        raise ForgeError(f"Node stage root must not be a symbolic link: {root}")
    root.mkdir(mode=0o700, exist_ok=True)
    import stat

    if root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise ForgeError(
            f"Node stage root is not owner-private: {root}",
            hint="Use profile create --stage DIR.",
        )
    from .paths import directory_lock
    from .package_operations import _inside

    with directory_lock(root, ".stages.lock"):
        from .paths import record_path
        group = record_path(root, "stage", {"home": str(base_home(scope).root)}, create=True)
        owner_scope = scope.root.parent if scope.root.name == "tools" and scope.root.parent.parent.name == "profiles" else scope.root
        relative = Path("profiles") / owner_scope.name
        if scope.root != owner_scope:
            relative /= "tools"
        target = _inside(
            ForgeHome(root), group / relative
        )
        if target.is_symlink():
            raise ForgeError(f"Node stage must not be a symbolic link: {target}")
        target.mkdir(parents=True, exist_ok=True)
        marker = target / "metadata.json"
        owner = str(scope.root)
        if marker.is_symlink():
            raise ForgeError(
                f"Node stage metadata must not be a symbolic link: {marker}"
            )
        if marker.is_file() and read_json(marker).get("scope") != owner:
            raise ForgeError(
                f"Node stage belongs to another Forge home: {target}",
                hint="Use profile create --stage DIR to choose a separate stage root.",
            )
        write_json(marker, {"schema_version": 1, "kind": "node-stage", "scope": owner})
    return target


def _prepare_tools(base, profile, *, target=None, stage=None, jobs=None, timeout=14400, cache=None, progress=None, confirm=None):
    from .paths import directory_lock
    from . import scope_cleanup
    from .scope_locks import lease

    name = profile.root.name
    preset = name
    path = ForgeHome(profile.root / "tools")
    with directory_lock(profile.root, ".tools.lock"):
        marker = path.root / "metadata.json"
        previous = None
        if marker.is_file():
            data = tools_record(profile)
            compatible(data)
            if target is not None and data["target"] != target:
                raise ForgeError(
                    f"Profile {name} uses target {data['target']}, not {target}."
                )
            if preset and data["preset"] != preset:
                raise ForgeError(f"Profile {name} uses preset {data['preset']}, not {preset}.")
            if data.get("status") == "ready":
                if data["backend"] == "spack":
                    from .toolchains import _verify
                    _verify(data["toolchain"])
                return data
            preset = preset or data["preset"]
            target = target or data["target"]
            if stage is None and data.get("stage_root"):
                stage = data["stage_root"]
            if progress:
                progress(f"Resume tools for profile {name}; reuse completed packages")
            previous = data
        elif path.root.exists() and not scope_cleanup.owned(base, path):
            if scope_cleanup.lock_residue(path):
                with lease(path, exclusive=True):
                    scope_cleanup.delete(base, path)
            else:
                raise ForgeError(f"Unregistered tool directory is preserved: {path.root}")
        preset = preset or name
        if preset not in presets():
            raise ForgeError(
                f"Unknown built-in profile: {preset}.", hint="jforge profile check"
            )
        definition = presets()[preset]
        target, python = _prerequisites(
            base, definition, host(), target, source=cache != "only"
        )
        if stage is not None:
            stage = Path(stage).expanduser().resolve()
            if not stage.is_dir() or not os.access(stage, os.W_OK | os.X_OK):
                raise ForgeError(f"Build stage is not writable: {stage}")
        _initialize_scope(base, path)
        data = {
            "schema_version": 3,
            "kind": "tool-suite",
            "id": name,
            "name": name,
            "preset": preset,
            "revision": definition["revision"],
            "backend": definition["backend"],
            "host": host(),
            "target": target,
            "created_at": previous.get("created_at", now()) if previous else now(),
            "attempts": (previous.get("attempts", 1) + 1) if previous else 1,
            "status": "building",
            "bootstrap_python": python,
            "stage_mode": "explicit" if stage else "node",
            "stage_root": str(stage) if stage else None,
            "stage": str(stage / name / "tools" if stage else default_stage(path)),
        }
        write_json(marker, data)
        try:
            if data["backend"] == "spack":
                from .toolchains import install_suite

                data["toolchain"] = install_suite(
                    path,
                    definition["versions"],
                    target=target,
                    stage=Path(data["stage"]),
                    jobs=jobs,
                    timeout=timeout,
                    cache=cache or "never",
                    progress=progress,
                    confirm=confirm,
                )
            else:
                data["toolchain"], data["fingerprints"] = _mac_install(
                    path, base, definition, jobs, timeout, progress, confirm
                )
            bin_dir = path.root / "bin"
            bin_dir.mkdir(exist_ok=True)
            for command, executable in _commands(data).items():
                link = bin_dir / command
                link.unlink(missing_ok=True)
                link.symlink_to(executable)
            script = [
                "#!/usr/bin/env bash",
                "export PATH=" + shlex.quote(str(bin_dir)) + "${PATH:+:$PATH}",
            ]
            script += [
                f"export {key}={shlex.quote(value)}"
                for key, value in data["toolchain"]["drivers"].items()
            ]
            if paths := _runtime_dirs(data):
                script += [
                    "export LD_LIBRARY_PATH="
                    + shlex.quote(":".join(paths))
                    + "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
                ]
            write_text(path.root / "activate.sh", "\n".join(script) + "\nreturn 0\n")
            data.update(status="ready", activation=str(path.root / "activate.sh"))
            write_json(marker, data)
            return data
        except BaseException:
            data["status"] = "failed"
            write_json(marker, data)
            raise


def create(base, name, *, stage=None, target=None, jobs=None, timeout=14400, cache=None, progress=None, confirm=None):
    from .paths import directory_lock
    from . import spack_backend, homebrew_backend, scope_cleanup
    from .scope_locks import lease
    import yaml
    from contextlib import ExitStack

    base = base_home(base)
    reject_old(base)
    if name not in presets():
        raise ForgeError(f"Unknown built-in profile: {name}.", hint="Choose ubuntu22, ubuntu24, ubuntu26 or macos.")
    definition = presets()[name]
    target, python = _prerequisites(base, definition, host(), target, source=cache != "only")
    with ExitStack() as lifetime:
        with directory_lock(base.root / "profiles", ".profiles.lock"):
            profile = _scoped_home(base, "profiles", name)
            if scope_cleanup.registered_removal(base, profile):
                if profile.root.exists():
                    with lease(profile, exclusive=True):
                        scope_cleanup.delete(base, profile)
                else:
                    scope_cleanup.receipt(base, profile).unlink()
            previous = record(profile)
            if previous and previous.get("status") == "ready":
                verify(profile, previous)
                validate_python(previous, os.environ)
                if progress:
                    progress(f"Profile {name} is already ready")
                return {**previous, "activation": str(profile.root / "activate.sh")}
            if profile.root.exists() and not previous and not scope_cleanup.owned(base, profile):
                if scope_cleanup.lock_residue(profile):
                    with lease(profile, exclusive=True):
                        scope_cleanup.delete(base, profile)
                else:
                    raise ForgeError(f"Unregistered profile is preserved: {profile.root}")
            if previous:
                compatible(previous)
                if previous.get("target") != target or previous.get("python") != python:
                    raise ForgeError("The interrupted profile uses different CPU/Python inputs; use its original environment.")
                if stage is None and previous.get("stage_root"):
                    stage = previous["stage_root"]
                if progress:
                    progress(f"Resume profile {name}; reuse completed packages")
            if stage is not None:
                stage = Path(stage).expanduser().resolve()
                if not stage.is_dir() or not os.access(stage, os.W_OK | os.X_OK):
                    raise ForgeError(f"Build stage is not writable: {stage}")
            profile.root.mkdir(parents=True, exist_ok=True)
            lifetime.enter_context(lease(profile, exclusive=True))
            _initialize_scope(base, profile)
            data = {"schema_version": 3, "kind": "profile", "model": "builtin-profile/1",
                    "id": name, "name": name, "preset": name, "revision": definition["revision"],
                    "backend": definition["backend"], "host": host(), "target": target,
                    "python": python, "created_at": previous.get("created_at", now()) if previous else now(),
                    "attempts": previous.get("attempts", 1) + 1 if previous else 1,
                    "status": "creating", "stage_mode": "explicit" if stage else "node",
                    "stage_root": str(stage) if stage else None,
                    "stage": str(stage / name if stage else default_stage(profile)), "apps": []}
            write_json(profile.root / "metadata.json", data)
        try:
            tools = _prepare_tools(base, profile, target=target, stage=stage, jobs=jobs, timeout=timeout,
                                   cache=cache, progress=progress, confirm=confirm)
            data.update(tools=tools, toolchain=tools["toolchain"])
            write_json(profile.root / "metadata.json", data)
            if data["backend"] == "spack":
                spack_backend.bootstrap(profile, progress=progress)
            else:
                homebrew_backend.initialize(profile, base, homebrew_backend.discover())
            data["status"] = "ready"
            write_json(profile.root / "metadata.json", data)
            write_text(profile.root / "profile.yaml", yaml.safe_dump({
                "schema_version": 3, "name": name, "preset": name,
                "python": python["version"], "packages": [], "apps": []}, sort_keys=False))
            refresh_activation(profile)
            return {**data, "activation": str(profile.root / "activate.sh")}
        except BaseException:
            data["status"] = "failed"
            write_json(profile.root / "metadata.json", data)
            raise


def refresh_activation(profile):
    from .env_entries import _activation_text
    from .package_operations import environment_path

    data = record(profile)
    if not data:
        return
    platform_data = tools_record(profile)
    scripts = [environment_path(profile), platform_data["activation"]]
    # External apps deliberately stay out of the profile activation.
    for app in data.get("apps", []):
        if app.get("enabled") and not app.get("external"):
            scripts.append(app["activation"])
    target = profile.root / "activate.sh"
    text = _activation_text(data["id"], scripts, managed=data, activation=target)
    if not target.is_file() or target.read_text() != text:
        write_text(target, text)
    return str(target)


def build_environment(profile, data, environment):
    validate_python(data, environment)
    compatible(data)
    from .toolchains import environment as tool_environment

    result = {
        **tool_environment(data["toolchain"], environment),
        "JFORGE_PROFILE_INSTANCE": data["id"],
    }
    result.setdefault("XDG_CACHE_HOME", str(profile.root / "cache"))
    result.setdefault("MPLCONFIGDIR", str(profile.root / "cache/matplotlib"))
    return result


def _instances(base, current=None):
    for file in sorted((base_home(base).root / "profiles").glob("*/metadata.json")):
        profile = ForgeHome(file.parent)
        if data := record(profile):
            yield profile, data


def catalog(base):
    base = base_home(base)
    selected = (selection(base) or {}).get("id")
    rows = []
    for name, definition in presets().items():
        data = record(_scoped_home(base, "profiles", name))
        rows.append({"id": name, "name": name, "backend": definition["backend"],
                     "status": data["status"] if data else "not installed", "selected": name == selected})
    return {"instances": rows, "default": selected}


def check(base, name=None):
    choices = presets()
    if name and name not in choices:
        raise ForgeError(f"Unknown built-in profile: {name}")
    rows = []
    for key, preset in choices.items():
        if name and name != key:
            continue
        installed = record(_scoped_home(base, "profiles", key))
        if installed and installed.get("status") == "ready":
            try:
                verify(_scoped_home(base, "profiles", key), installed)
                validate_python(installed, os.environ)
            except ForgeError as exc:
                rows.append({"name": key, "backend": preset["backend"], "status": "blocked", "reason": str(exc)})
            else:
                rows.append({"name": key, "backend": preset["backend"], "status": "ready", "reason": None})
            continue
        try:
            _prerequisites(base, preset, host())
        except ForgeError as exc:
            status = (
                "blocked" if host()["platform"] == preset["platform"] else "unsupported"
            )
            rows.append(
                {
                    "name": key,
                    "backend": preset["backend"],
                    "status": status,
                    "reason": str(exc),
                }
            )
        else:
            rows.append(
                {
                    "name": key,
                    "backend": preset["backend"],
                    "status": "buildable",
                    "reason": None,
                }
            )
    return {
        "host": host(),
        "profiles": rows,
        "default": (selection(base) or {}).get("id"),
    }


def set_default(base, name):
    from .paths import directory_lock
    from .build_apps import locked as app_locked

    base = base_home(base)
    profile = _scoped_home(base, "profiles", name)
    if not record(profile):
        raise ForgeError(f"No profile named {name}.", hint="jforge profile")
    # Creation/removal and selection share the namespace lock. Verify only after
    # taking it, so a concurrent removal cannot leave a dangling selection.
    with directory_lock(base.root / "profiles", ".profiles.lock"):
        data = record(profile)
        if not data:
            raise ForgeError(f"No profile named {name}.", hint="jforge profile")
        with app_locked(profile):
            verify(profile, data)
            validate_python(data, os.environ)
        with directory_lock(base.root / "profiles", ".selection.lock"):
            path = selection_path(base)
            index = (
                read_json(path)
                if path.is_file()
                else {"schema_version": 3, "hosts": {}}
            )
            if index.get("schema_version") != 3 or not isinstance(
                index.get("hosts"), dict
            ):
                raise ForgeError(f"Invalid profile selection index: {path}")
            index["hosts"][_host_id(host())] = {"id": name, "host": host()}
            write_json(path, index)
    return {
        **data,
        "activation": str(profile.root / "activate.sh"),
        "status": "selected",
    }


def build(base, name, **kwargs):
    raise ForgeError(
        "Profile build was replaced by profile create.",
        hint=f"jforge profile create {name}",
    )


def sync_definition(profile):
    """Keep the editable profile description current with the committed state."""
    import yaml

    file = profile.root / "profile.yaml"
    if not file.is_file():
        return
    data = record(profile)
    state = read_json(profile.root / "packages.json").get(data["backend"], {})
    definition = yaml.safe_load(file.read_text())
    definition["packages"] = state.get("roots", [])
    definition["apps"] = [
        {
            "name": app["name"],
            "version": app.get("version"),
            "enabled": app.get("enabled", True),
        }
        for app in data.get("apps", [])
    ]
    write_text(file, yaml.safe_dump(definition, sort_keys=False))


def remove(base, name, *, dry_run=False, confirm=None):
    from .paths import directory_lock
    from .package_operations import locked
    from .build_apps import locked as app_locked

    base = base_home(base)
    name = identifier(name)

    def inspect():
        from . import scope_cleanup
        scope = _scoped_home(base, "profiles", name)
        if scope.root.exists():
            if not record(scope):
                from . import scope_cleanup
                if not (scope_cleanup.owned(base, scope) or scope_cleanup.lock_residue(scope)
                        or scope_cleanup.registered_removal(base, scope)):
                    raise ForgeError(f"Unregistered profile is preserved: {scope.root}")
            return scope
        if scope_cleanup.registered_removal(base, scope):
            return scope
        raise ForgeError(f"No profile named {name}.")

    if dry_run:
        target = inspect()
    else:
        with directory_lock(base.root / "profiles", ".profiles.lock"):
            target = inspect()
            if confirm:
                confirm()
            file = selection_path(base)
            index = read_json(file) if file.is_file() else None
            if index is not None:
                if index.get("schema_version") != 3 or not isinstance(
                    index.get("hosts"), dict
                ):
                    raise ForgeError(f"Invalid selection index: {file}")
                index["hosts"] = {
                    key: row for key, row in index["hosts"].items() if row["id"] != name
                }
            from .scope_locks import lease
            from .scope_cleanup import delete
            if target.root.exists():
                with lease(target, exclusive=True):
                    with app_locked(target), locked(target):
                        pass
                    delete(base, target)
            else:
                from .scope_cleanup import receipt
                receipt(base, target).unlink(missing_ok=True)
            if index is not None:
                write_json(file, index)
    return {"name": name, "path": str(target.root), "dry_run": dry_run}


def package_inventory(profile):
    """Read a profile-wide inventory without changing native stores or reuse."""
    def packages(scope, backend):
        if backend == "homebrew":
            from . import homebrew_backend
            return homebrew_backend.current(scope)["packages"] if homebrew_backend.configured(scope) else []
        return scope.packages()

    data = record(profile)
    if not data:
        from .apt_profiles import backend_name
        return packages(profile, backend_name(profile))
    from .build_apps import reading
    with reading(profile):
        tools = ForgeHome(profile.root / "tools")
        inventory = {}
        for source, scope in (("tools", tools), ("apt", profile)):
            for index, package in enumerate(packages(scope, data["backend"])):
                prefix = package.get("prefix")
                # Deduplicate references to one physical installation only.
                # Equal names/versions in different stores remain separate.
                key = (package["name"], str(package.get("version", "")),
                       str(Path(prefix).resolve()) if prefix else (source, index))
                category = ("base-tool" if package.get("root") or package.get("automatic") is False
                            else "bootstrap-dependency") if source == "tools" else "application"
                reference = {"source": source, **{key: package[key] for key in
                             ("hash", "install_id", "external", "root", "active") if key in package}}
                if key not in inventory:
                    inventory[key] = {**package, "category": category, "sources": [source],
                                      "references": [reference]}
                else:
                    row = inventory[key]
                    if source not in row["sources"]:
                        row["sources"].append(source)
                    if reference not in row["references"]:
                        row["references"].append(reference)
                    if package.get("active"):
                        row["active"] = True
        return list(inventory.values())
