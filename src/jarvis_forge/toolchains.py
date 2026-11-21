"""Versioned, user-local GCC toolchains for Linux Spack profiles."""

import os
from pathlib import Path
import re
import subprocess
import sys

from .package_operations import _inside
from .paths import named_path
from .storage import ForgeError, digest, identifier, read_json

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
        raise ForgeError(f"Toolchain installation changed: {record['id']}", hint="jforge profile")
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
        raise ForgeError(f"No managed toolchain named {name}.", hint="jforge profile")
    return _verify(read_json(file))


def binding(profile):
    from .unified_profiles import record, base_home, platform_record
    data=record(profile)
    if data:
        selected=platform_record(base_home(profile),data['platform'])
        return _verify(selected['toolchain']) if selected['backend']=='spack' else None
    return None


def environment(record, base):
    path = base.get("PATH", os.defpath)
    return {**base, **record["drivers"], "PATH": os.pathsep.join(dict.fromkeys([*bin_dirs(record), *path.split(os.pathsep)]))}


def bin_dirs(record):
    paths = [Path(path).parent for tool in record.get("tools", {}).values() for path in tool["commands"].values()]
    if not record.get("external"):
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


def install(*args, **kwargs):
    raise ForgeError('Independent toolchains have been removed.',hint='jforge profile create NAME --platform ubuntu24')


def use(*args, **kwargs):
    raise ForgeError('A profile uses the compiler of its platform.',hint='jforge profile use NAME')


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
    from .paths import temporary_directory
    with temporary_directory(Path(record["profile"])/"checks", "compiler") as directory:
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


def install_platform(scope, versions, *, target, stage, jobs=None, timeout=14400, cache='never', progress=None,confirm=None):
    """Install the platform suite into its own store, using host compilers once."""
    from . import spack_backend
    stage.mkdir(parents=True,exist_ok=True)
    spack_backend.bootstrap(scope,extensions=False,progress=progress)
    specs=[f'gcc@={versions["gcc"]} languages=c,c++,fortran target={target}']
    specs += [f'{name}@={version} target={target}' for name,version in versions.items() if name!='gcc']
    result=spack_backend.transact(scope,'install',specs,jobs=jobs,timeout=timeout,cache=cache,progress=progress,
                                 build_stage=stage,target=target,deprecated_versions=versions,confirm=confirm)
    package=_root_package(result,'gcc',versions['gcc'])
    if package['external']:
        raise ForgeError('The platform compiler must be installed in the platform store.',code=1)
    prefix=Path(package['prefix']).resolve()
    record={'deprecated_versions':versions,'schema_version':3,'id':scope.root.name,'version':versions['gcc'],'target':target,
            'prefix':str(prefix),'profile':str(scope.root),'drivers':{key:str(prefix/'bin'/name) for key,name in DRIVERS.items()},
            'spec_sha256':digest(prefix/'.spack/spec.json'),
            'tools':_suite_tools(result,{key:value for key,value in versions.items() if key!='gcc'})}
    _verify(record)
    _,libraries=_inspect(record)
    record['runtimes']={name:str(path) for name,path in libraries.items()}
    check_tools(record)
    return record
