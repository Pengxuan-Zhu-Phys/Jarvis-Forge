"""Versioned, user-local GCC toolchains for Linux Spack profiles."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from .package_operations import _inside, locked
from .paths import physical_name, named_path
from .storage import ForgeError, ForgeHome, digest, identifier, read_json, write_json

DRIVERS = {"CC": "gcc", "CXX": "g++", "FC": "gfortran"}
RUNTIMES = {"libgcc_s.so.1": "CC", "libstdc++.so.6": "CXX", "libgfortran.so.5": "FC"}
TOOL_COMMANDS = {"cmake": ("cmake", "ctest"), "gmake": ("make", "gmake"),
                 "autoconf": ("autoconf", "autoreconf"), "automake": ("automake", "aclocal")}


def _linux():
    if sys.platform != "linux":
        raise ForgeError("Managed GCC toolchains require Linux and Spack.")


def _manifest(home, name, *, create=False):
    return _inside(home, named_path(home.root / "toolchains", identifier(name), create=create) / "toolchain.json")


def _run(argv, *, env=None, timeout=60):
    try:
        result = subprocess.run([str(part) for part in argv], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForgeError(f"Toolchain command failed: {argv[0]}\n{exc}", code=1) from exc
    if result.returncode:
        raise ForgeError(f"Toolchain command failed: {argv[0]} (exit {result.returncode})\n"
                         + (result.stderr or result.stdout).strip(), code=1)
    return result.stdout.strip()


def _verify(record):
    prefix = Path(record["prefix"])
    spec = prefix / ".spack/spec.json"
    if record.get("external"):
        if any(not Path(path).is_file() or digest(Path(path)) != value for path, value in record["fingerprints"].items()):
            raise ForgeError(f"External toolchain changed: {record['id']}")
    elif not spec.is_file() or digest(spec) != record["spec_sha256"]:
        raise ForgeError(f"Toolchain installation changed: {record['id']}", hint="jforge apt toolchain list")
    for path in record["drivers"].values():
        file = Path(path)
        if not file.is_file() or not os.access(file, os.X_OK) or not file.resolve().is_relative_to(prefix):
            raise ForgeError(f"Toolchain compiler is unavailable: {file}")
    for path in record.get("runtimes", {}).values():
        file = Path(path)
        if not file.is_file() or not file.resolve().is_relative_to(prefix):
            raise ForgeError(f"Toolchain runtime is unavailable: {file}")
    for tool in record.get("tools", {}).values():
        prefix = Path(tool["prefix"])
        spec = prefix / ".spack/spec.json"
        if tool.get("spec_sha256") and (not spec.is_file() or digest(spec) != tool["spec_sha256"]):
            raise ForgeError(f"Toolchain package changed: {prefix}")
        for name, path in tool["commands"].items():
            file = Path(path)
            if not file.is_file() or not file.resolve().is_relative_to(prefix) or not os.access(file, os.X_OK):
                raise ForgeError(f"Toolchain executable is unavailable: {name} at {file}")
            if tool.get("fingerprints") and digest(file) != tool["fingerprints"][name]:
                raise ForgeError(f"External toolchain executable changed: {file}")
    return record


def list_installed(home):
    return [read_json(_inside(home, file)) for file in sorted((home.root / "toolchains").glob("*/toolchain.json"))]


def installed(home, name):
    file = _manifest(home, name)
    if not file.is_file():
        raise ForgeError(f"No managed toolchain named {name}.", hint="jforge apt toolchain list")
    return _verify(read_json(file))


def binding(profile):
    file = _inside(profile, profile.root / "apt/toolchain.json")
    return _verify(read_json(file)) if file.is_file() else None


def environment(record, base):
    path = base.get("PATH", os.defpath)
    return {**base, **record["drivers"], "PATH": os.pathsep.join(dict.fromkeys([*bin_dirs(record), *path.split(os.pathsep)]))}


def bin_dirs(record):
    paths = [Path(path).parent for tool in record.get("tools", {}).values() for path in tool["commands"].values()]
    paths.extend(Path(path).parent for path in record["drivers"].values())
    return list(dict.fromkeys(map(str, paths)))


def _suite_tools(result, specs):
    tools = {}
    for name, version in specs.items():
        package = _root_package(result, name, version)
        prefix = Path(package["prefix"]).resolve()
        commands = {command: str(prefix / "bin" / command) for command in TOOL_COMMANDS[name]}
        tool = {"prefix": str(prefix), "version": version, "commands": commands}
        if not all(Path(path).is_file() for path in commands.values()):
            raise ForgeError(f"Required toolchain executables are missing under {prefix}.", code=1)
        if package["external"]:
            tool["fingerprints"] = {command: digest(Path(path)) for command, path in commands.items()}
        else:
            tool["spec_sha256"] = digest(prefix / ".spack/spec.json")
        tools[name] = tool
    return tools


def _root_package(result, name, version):
    package = next((row for row in result["packages"] if row["name"] == name and row["root"]
                    and row["version"] == version), None)
    if package is None:
        raise ForgeError(f"Spack did not prepare {name}@{version}.", code=1, hint=f"Operation logs: {result['path']}")
    return package


def install(home, spec, *, target=None, stage=None, jobs=1, timeout=14400, cache="never", progress=None,
            instance=None, tools=None, deprecated_versions=None):
    from . import spack_backend
    from .builds import file_lock

    _linux()
    match = re.fullmatch(r"gcc@=?([0-9]+\.[0-9]+\.[0-9]+)", spec)
    if not match:
        raise ForgeError("Select an exact GCC version, for example gcc@14.3.0.")
    if cache not in {"auto", "only", "never"}:
        raise ForgeError("Build cache mode must be auto, only or never.")
    version = match[1]
    allowed = spack_backend._deprecated_permissions(deprecated_versions or {})
    suite_versions = {"gcc": version, **(tools or {})}
    if any(suite_versions.get(name) != value for name, value in allowed.items()):
        raise ForgeError("Deprecated-version permissions must match this tool suite's exact versions.")
    if target is not None:
        identifier(target)
    if stage is not None:
        stage = stage.expanduser().resolve()
        if not stage.is_dir() or not os.access(stage, os.W_OK | os.X_OK):
            raise ForgeError(f"Toolchain build stage is not writable: {stage}")
    name = identifier(instance) if instance else physical_name(home.root / "toolchains", "gcc",
            {"version": version, "target": target, "tools": tools or {}}, create=True)
    manifest = _manifest(home, name, create=True)
    home.initialize()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(manifest.parent / "install.lock"):
        if manifest.is_file():
            record = installed(home, name)
            if instance and (record["version"] != version or record["target"] != target
                    or {key: tool["version"] for key, tool in record.get("tools", {}).items()} != (tools or {})):
                raise ForgeError(f"Toolchain definition changed for {instance}; use a new preset revision.")
            if allowed and record.get("deprecated_versions") != allowed:
                record["deprecated_versions"] = allowed
                write_json(manifest, record)
            return {**record, "status": "reused"}
        if cache != "only":
            missing = [program for program in ("make", "tar", "xz") if not shutil.which(program)]
            if missing:
                raise ForgeError("Missing compiler bootstrap tools: " + ", ".join(missing),
                                 hint="Load the site tool module or add a user-local installation to PATH.")
        # A bootstrap is a private backend scope within this toolchain, not
        # another recursively nested package profile.
        profile = manifest.parent
        bootstrap_home = ForgeHome(profile)
        bootstrap_home.initialize()
        spack_backend.bootstrap(bootstrap_home, extensions=False, progress=progress)
        request = f"gcc@={version} languages=c,c++,fortran" + (f" target={target}" if target else "")
        tool_specs = [f"{name}@={value}" + (f" target={target}" if target else "")
                      for name, value in (tools or {}).items()]
        result = spack_backend.transact(ForgeHome(profile), "install", [request, *tool_specs], jobs=jobs, timeout=timeout,
                                        cache=cache, progress=progress, build_stage=stage, target=target,
                                        deprecated_versions=allowed)
        gcc = _root_package(result, "gcc", version)
        if gcc["external"] and not instance:
            raise ForgeError("Spack did not install the requested user-local GCC.", code=1,
                             hint=f"Operation logs: {result['path']}")
        prefix = Path(gcc["prefix"]).resolve()
        drivers = {variable: str(prefix / "bin" / binary) for variable, binary in DRIVERS.items()}
        record = {"schema_version": 1, "id": name, "version": version, "target": target, "prefix": str(prefix),
                  "profile": str(profile), "drivers": drivers, "path": result["path"]}
        if allowed:
            record["deprecated_versions"] = allowed
        if gcc["external"]:
            selected = gcc.get("node", {}).get("external", {}).get("extra_attributes", {}).get("compilers", {})
            drivers.update({variable: str(Path(selected[key]).absolute())
                            for variable, key in (("CC", "c"), ("CXX", "cxx"), ("FC", "fortran"))
                            if selected.get(key)})
            if not all(Path(path).is_file() for path in drivers.values()):
                raise ForgeError(f"External GCC suite is incomplete under {prefix}.", code=1)
            record.update(external=True, fingerprints={path: digest(Path(path)) for path in drivers.values()})
        else:
            record["spec_sha256"] = digest(prefix / ".spack/spec.json")
        if tools:
            record["tools"] = _suite_tools(result, tools)
        _verify(record)
        _, libraries = _inspect(record)
        record["runtimes"] = {name: str(path) for name, path in libraries.items()}
        write_json(manifest, record)
        return {**record, "status": "installed"}


def use(home, name, *, progress=None):
    from .apt_profiles import active_home, backend_name, ensure_active
    from .spack_backend import current
    from .unified_profiles import record as profile_record

    _linux()
    record = installed(home, name)
    if "runtimes" not in record:
        _, libraries = _inspect(record)
        record["runtimes"] = {soname: str(path) for soname, path in libraries.items()}
    target = active_home(home)
    if profile_record(target):
        raise ForgeError("The selected profile already defines its compiler suite.",
                         hint="Use jforge apt profile build and jforge apt profile global to select another suite.")
    if backend_name(target) != "spack" or target.root.name.startswith("spack-toolchain-"):
        raise ForgeError("Select a separate project Spack profile before binding this toolchain.")
    target = ensure_active(home, progress=progress, selected=target)
    with locked(target):
        existing = binding(target)
        if existing != record:
            if current(target)["roots"]:
                raise ForgeError("Changing toolchains requires a project profile with no package roots.",
                                 hint="jforge apt profile NAME --create --backend spack")
            write_json(_inside(target, target.root / "apt/toolchain.json"), record)
    return {"toolchain": name, "profile": target.root.name, "prefix": record["prefix"]}


def spack_requirements(record, packages):
    compiler = "gcc@=" + record["version"]
    drivers = record["drivers"]
    external = {"spec": compiler + " languages=c,c++,fortran", "prefix": record["prefix"],
                "extra_attributes": {"compilers": {"c": drivers["CC"], "cxx": drivers["CXX"],
                                                    "fortran": drivers["FC"]}}}
    gcc = packages.setdefault("gcc", {})
    gcc["externals"] = [external]
    gcc["buildable"] = False
    for language in ("c", "cxx", "fortran"):
        packages.setdefault(language, {})["require"] = [compiler]
    if record.get("target"):
        packages.setdefault("all", {})["target"] = [record["target"]]
    for name, tool in record.get("tools", {}).items():
        spec = f"{name}@={tool['version']}"
        packages.setdefault(name, {}).update(
            externals=[{"spec": spec, "prefix": tool["prefix"]}], buildable=False, require=[spec])


def validate_plan(record, packages):
    selected = {"gcc": record, **record.get("tools", {})}
    for package in packages:
        tool = selected.get(package["name"])
        if tool and (not package["external"] or package["version"] != tool["version"]
                     or Path(package["prefix"]).resolve() != Path(tool["prefix"])):
            raise ForgeError(f"Spack selected {package['name']} at {package['prefix']}, expected {tool['prefix']}.")


def check_tools(record):
    env, _ = _inspect(record)
    for tool in record.get("tools", {}).values():
        for path in tool["commands"].values():
            version = _run([path, "--version"], env=env).splitlines()[0]
            if not re.search(r"(?<![\d.])" + re.escape(tool["version"]) + r"(?![\d.])", version):
                raise ForgeError(f"Expected {tool['version']} from {path}; got {version}.", code=1)


def cmake_runtime_paths(record, cache):
    directories = list(dict.fromkeys(str(Path(path).parent) for path in record.get("runtimes", {}).values()))
    if directories:
        for name in ("CMAKE_BUILD_RPATH", "CMAKE_INSTALL_RPATH"):
            value = cache.get(name, {}).get("value", "")
            paths = [*directories, *filter(None, str(value).split(";"))]
            cache[name] = {"type": "STRING", "value": ";".join(dict.fromkeys(paths))}


def _inspect(record):
    prefix = Path(record["prefix"])
    env = {key: value for key, value in os.environ.items()
           if key not in {"LD_LIBRARY_PATH", "LD_PRELOAD", "LIBRARY_PATH", "COMPILER_PATH", "GCC_EXEC_PREFIX"}}
    env = environment(record, env)
    for driver in record["drivers"].values():
        if _run([driver, "-dumpfullversion"], env=env) != record["version"]:
            raise ForgeError(f"Unexpected compiler version at {driver}.", code=1)
    libraries = {}
    for library, driver in RUNTIMES.items():
        path = Path(_run([record["drivers"][driver], "-print-file-name=" + library], env=env)).resolve()
        if not path.is_file() or not path.is_relative_to(prefix):
            raise ForgeError(f"Compiler runtime {library} is outside the toolchain: {path}", code=1)
        libraries[library] = path
    return env, libraries


def check(home, name):
    _linux()
    record = installed(home, name)
    env, libraries = _inspect(record)
    with TemporaryDirectory(prefix="jforge-gcc-check-") as directory:
        work = Path(directory)
        sources = {
            "CC": ("c", "extern int cpp_value(void), f_value(void);\n"
                         "int main(void) { return cpp_value() + f_value() != 42; }\n"),
            "CXX": ("cc", '#include <iostream>\nextern "C" int cpp_value() { std::cout << "C++\\n"; return 21; }\n'),
            # The function prefix cannot use c_int before the module is imported.
            "FC": ("f90", "function f_value() bind(C) result(value)\n"
                          "use iso_c_binding, only: c_int\n"
                          "implicit none\n"
                          "integer(c_int) :: value\n"
                          'print *, "Fortran"\n'
                          "value = 21\n"
                          "end function\n"),
        }
        objects = []
        for driver, (suffix, content) in sources.items():
            source, obj = work / ("probe." + suffix), work / (driver + ".o")
            source.write_text(content)
            _run([env[driver], "-c", source, "-o", obj], env=env)
            objects.append(obj)
        directories = list(dict.fromkeys(str(path.parent) for path in libraries.values()))
        link = [flag for directory in directories for flag in ("-L", directory)]
        runtime = [flag for directory in directories for flag in ("-Xlinker", "-rpath", "-Xlinker", directory)]
        binary = work / "probe"
        _run([env["CXX"], *objects, *link, "-Wl,--no-as-needed", "-lgfortran", "-lquadmath", "-lgcc_s",
              *runtime, "-o", binary], env=env)
        output = _run([binary], env=env)
        linked = _run(["ldd", binary], env=env)
        for name, path in libraries.items():
            match = re.search(r"^\s*" + re.escape(name) + r"\s+=>\s+(.+?)\s+\(", linked, re.MULTILINE)
            if not match or Path(match[1]).resolve() != path:
                raise ForgeError(f"Smoke test loaded an unexpected {name}.\n{linked}", code=1)
    return {"toolchain": record["id"], "version": record["version"], "node": os.uname().nodename,
            "libc": os.confstr("CS_GNU_LIBC_VERSION"), "drivers": record["drivers"],
            "runtimes": {name: str(path) for name, path in libraries.items()}, "output": output, "status": "passed"}
