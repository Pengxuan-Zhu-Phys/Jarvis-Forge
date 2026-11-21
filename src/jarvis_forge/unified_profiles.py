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
    if config.is_file() and read_json(config).get("layout_version") != 3:
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
    if value.get("schema_version") != 3 or value.get("id") != profile.root.name:
        raise ForgeError(f"Unsupported profile metadata: {file}")
    return value


def platform_record(base, name):
    file = base_home(base).root / "platforms" / identifier(name) / "metadata.json"
    if not file.is_file():
        raise ForgeError(f"No platform named {name}.", hint="jforge profile check")
    value = read_json(file)
    if (
        value.get("schema_version") != 3
        or value.get("kind") != "platform"
        or value.get("id") != name
    ):
        raise ForgeError(f"Invalid platform metadata: {file}")
    return value


def compatible(data, current=None):
    current = current or host()
    saved = data["host"]
    if (
        saved["platform"] != current["platform"]
        or saved["machine"] != current["machine"]
    ):
        raise ForgeError(
            f"Platform {data['id']} requires {saved['platform']}/{saved['machine']}."
        )
    if current["platform"] == "linux":

        def version(value):
            match = re.fullmatch(r"glibc (\d+)\.(\d+)(?:\.(\d+))?", value)
            if not match:
                raise ForgeError("A compatible glibc host is required.")
            return tuple(int(part or 0) for part in match.groups())

        if version(current["libc"]) < version(saved["libc"]):
            raise ForgeError(
                f"Platform {data['id']} requires {saved['libc']}; current host has {current['libc']}.",
                hint=f"Create a different platform: jforge profile create NAME --platform {data['id']}-{current['system']} --preset {data['preset']}",
            )
        _target(current, data["target"])


def verify(profile, data):
    platform_data = platform_record(base_home(profile), data["platform"])
    compatible(platform_data)
    if platform_data.get("status") != "ready" or data.get("status") != "ready":
        raise ForgeError(f"Profile {data['id']} or its platform is not ready.")
    if data["backend"] == "spack":
        from .toolchains import _verify

        _verify(platform_data["toolchain"])
    else:
        for path, checksum in platform_data["fingerprints"].items():
            if not Path(path).is_file() or digest(Path(path)) != checksum:
                raise ForgeError(f"Platform tool changed: {path}")
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
    platform_data = data.get("platform_metadata", data)
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
    (scope.root / "locks/apps.lock").touch(exist_ok=True)
    config = read_json(scope.root / "config.json")
    config.update(
        forge_home=str(base_home(base).root),
        apt_base_home=str(base_home(base).root),
        layout_version=3,
    )
    write_json(scope.root / "config.json", config)


def _namespace(base, name, kind):
    base = base_home(base)
    other = "profiles" if kind == "platform" else "platforms"
    if (base.root / other / name).exists():
        raise ForgeError(
            f"{name} is already a {other[:-1]} name; platform and profile names share one namespace."
        )


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
        target = _inside(
            ForgeHome(root), root / scope.root.parent.name / scope.root.name
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


def prepare_platform(
    base,
    name,
    *,
    preset=None,
    target=None,
    stage=None,
    jobs=None,
    timeout=14400,
    cache=None,
    progress=None,
    confirm=None,
):
    from .paths import directory_lock

    base = base_home(base)
    name = identifier(name)
    _namespace(base, name, "platform")
    path = _scoped_home(base, "platforms", name)
    with directory_lock(base.root / "platforms", ".platforms.lock"):
        marker = path.root / "metadata.json"
        if marker.is_file():
            data = platform_record(base, name)
            compatible(data)
            if data.get("status") != "ready":
                raise ForgeError(
                    f"Platform {name} is incomplete; remove it explicitly before rebuilding."
                )
            if target is not None and data["target"] != target:
                raise ForgeError(
                    f"Platform {name} uses target {data['target']}, not {target}."
                )
            if data["backend"] == "spack":
                from .toolchains import _verify

                _verify(data["toolchain"])
            if preset and data["preset"] != preset:
                raise ForgeError(
                    f"Platform {name} uses preset {data['preset']}, not {preset}."
                )
            return data
        if path.root.exists():
            raise ForgeError(
                f"Unregistered platform directory is preserved: {path.root}"
            )
        preset = preset or name
        if preset not in presets():
            raise ForgeError(
                f"Unknown platform preset: {preset}.", hint="jforge profile check"
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
            "kind": "platform",
            "id": name,
            "name": name,
            "preset": preset,
            "revision": definition["revision"],
            "backend": definition["backend"],
            "host": host(),
            "target": target,
            "created_at": now(),
            "status": "building",
            "bootstrap_python": python,
            "stage_mode": "explicit" if stage else "node",
            "stage_root": str(stage) if stage else None,
            "stage": str(stage / name if stage else default_stage(path)),
        }
        write_json(marker, data)
        try:
            if data["backend"] == "spack":
                from .toolchains import install_platform

                data["toolchain"] = install_platform(
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


def create(
    base,
    name,
    *,
    platform=None,
    preset=None,
    stage=None,
    target=None,
    jobs=None,
    timeout=14400,
    cache=None,
    progress=None,
    confirm=None,
):
    from .paths import directory_lock
    from . import spack_backend, homebrew_backend
    import yaml

    base = base_home(base)
    reject_old(base)
    name = identifier(name)
    with directory_lock(base.root / "profiles", ".profiles.lock"):
        _namespace(base, name, "profile")
        profile = _scoped_home(base, "profiles", name)
        if profile.root.exists():
            raise ForgeError(f"Profile already exists: {name}")
        if platform is None:
            current = selection(base)
            if current:
                platform = record(_scoped_home(base, "profiles", current["id"]))[
                    "platform"
                ]
            elif host()["platform"] == "darwin":
                platform = "macos"
            elif host()["system"] == "ubuntu" and host()["version"] in {
                "22.04",
                "24.04",
                "26.04",
            }:
                platform = "ubuntu" + host()["version"][:2]
            else:
                raise ForgeError(
                    "Choose a platform explicitly on this host.",
                    hint="jforge profile create NAME --platform ubuntu24",
                )
        if platform == name:
            raise ForgeError("Platform and profile names must be different.")
        platform_data = prepare_platform(
            base,
            platform,
            preset=preset,
            stage=stage,
            target=target,
            jobs=jobs,
            timeout=timeout,
            cache=cache,
            progress=progress,
            confirm=confirm,
        )
        python = _python()
        if any(
            Path(python[key]).resolve().is_relative_to(base.root)
            for key in ("prefix", "base_prefix")
        ):
            raise ForgeError(
                "Select external Python with pyenv before creating a profile."
            )
        _initialize_scope(base, profile)
        data = {
            "schema_version": 3,
            "kind": "profile",
            "id": name,
            "name": name,
            "platform": platform,
            "preset": platform_data["preset"],
            "backend": platform_data["backend"],
            "host": host(),
            "target": platform_data["target"],
            "python": python,
            "toolchain": platform_data["toolchain"],
            "platform_metadata": platform_data,
            "created_at": now(),
            "status": "creating",
            "stage": str(
                Path(stage).expanduser().resolve() / name
                if stage
                else default_stage(profile)
            ),
            "stage_mode": "explicit" if stage else "node",
            "stage_root": str(Path(stage).expanduser().resolve()) if stage else None,
            "apps": [],
        }
        write_json(profile.root / "metadata.json", data)
        try:
            if data["backend"] == "spack":
                spack_backend.bootstrap(profile, progress=progress)
            else:
                homebrew_backend.initialize(profile, base, homebrew_backend.discover())
            data["status"] = "ready"
            write_json(profile.root / "metadata.json", data)
            write_text(
                profile.root / "profile.yaml",
                yaml.safe_dump(
                    {
                        "schema_version": 3,
                        "name": name,
                        "platform": platform,
                        "preset": data["preset"],
                        "python": python["version"],
                        "packages": [],
                        "apps": [],
                    },
                    sort_keys=False,
                ),
            )
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
    platform_data = platform_record(base_home(profile), data["platform"])
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
    compatible(platform_record(base_home(profile), data["platform"]))
    from .toolchains import environment as tool_environment

    return {
        **tool_environment(data["toolchain"], environment),
        "JFORGE_PROFILE_INSTANCE": data["id"],
    }


def _instances(base, current=None):
    for file in sorted((base_home(base).root / "profiles").glob("*/metadata.json")):
        profile = ForgeHome(file.parent)
        if data := record(profile):
            yield profile, data


def catalog(base):
    base = base_home(base)
    return {
        "presets": [{"name": name, **value} for name, value in presets().items()],
        "platforms": [
            read_json(file)
            for file in sorted((base.root / "platforms").glob("*/metadata.json"))
        ],
        "instances": [data for _, data in _instances(base)],
        "default": (selection(base) or {}).get("id"),
    }


def check(base, name=None):
    choices = presets()
    if name and name not in choices:
        raise ForgeError(f"Unknown platform preset: {name}")
    rows = []
    for key, preset in choices.items():
        if name and name != key:
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
        hint=f"jforge profile create NAME --platform {name}",
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
        scope = _scoped_home(base, "profiles", name)
        if scope.root.exists():
            if not record(scope):
                raise ForgeError(f"Unregistered profile is preserved: {scope.root}")
            return scope
        scope = _scoped_home(base, "platforms", name)
        if not scope.root.exists():
            raise ForgeError(f"No platform or profile named {name}.")
        platform_record(base, name)
        users = [row["id"] for _, row in _instances(base) if row["platform"] == name]
        if users:
            raise ForgeError("Platform is used by profiles: " + ", ".join(users))
        return scope

    if dry_run:
        target = inspect()
    else:
        with (
            directory_lock(base.root / "profiles", ".profiles.lock"),
            directory_lock(base.root / "platforms", ".platforms.lock"),
        ):
            target = inspect()
            if confirm is None:
                raise ForgeError(
                    "Removal requires confirmation.",
                    hint="jforge profile remove NAME --yes",
                )
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
            with app_locked(target), locked(target):
                shutil.rmtree(target.root)
                if index is not None:
                    write_json(file, index)
    return {"name": name, "path": str(target.root), "dry_run": dry_run}
