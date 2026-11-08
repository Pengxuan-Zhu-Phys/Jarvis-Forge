"""A user-owned Spack backend. Spack owns resolution, builds and binary reuse."""

from __future__ import annotations

import fnmatch
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import uuid

import yaml

from .storage import ForgeError, digest, now, read_json, write_json

ENGINE_VERSION = "1.2.2"
INSTALL_PROJECTION = "{name}"
PYTHON_PACKAGES = ("python", "python-venv")
ENGINE = {
    "url": "https://github.com/spack/spack/releases/download/v1.2.2/spack-1.2.2.tar.gz",
    "sha256": "ed39d08bc295571cdec23a4566cbd8aa7ef4ebd582013d43874471a2b1257bf5",
}
REPO_COMMIT = "d4f7c711a6a42f1c4d551c8fd10fce9a11340a81"
REPO_BRANCH = "releases/v2026.06"
REPOSITORY = {
    "url": f"https://github.com/spack/spack-packages/archive/{REPO_COMMIT}.tar.gz",
    "sha256": "06f6bbd84f040e1da916c0f81ec1292422d5fc17e352f565459a853f9d3d7361",
}
ALIASES = {
    "libeigen3-dev": "eigen",
    "libgsl-dev": "gsl",
    "libboost-dev": "boost",
    "libboost-filesystem-dev": "boost",
    "libboost-system-dev": "boost",
    "libboost-program-options-dev": "boost",
    "libboost-thread-dev": "boost",
    "libhdf5-dev": "hdf5",
    "libyaml-cpp-dev": "yaml-cpp",
    "libsqlite3-dev": "sqlite",
    "sqlite3": "sqlite",
    "liblapack-dev": "netlib-lapack",
    "libblas-dev": "netlib-lapack",
    "libopenblas-dev": "openblas",
    "libfftw3-dev": "fftw",
    "libfastjet-dev": "fastjet",
    "libhepmc3-dev": "hepmc3",
    "liblhapdf-dev": "lhapdf",
    "libpythia8-dev": "pythia8",
    "root-system": "root",
    "libtbb-dev": "intel-tbb",
    "libvdt-dev": "vdt",
    "libpcre2-dev": "pcre2",
    "zlib1g-dev": "zlib",
    "libbz2-dev": "bzip2",
    "liblzma-dev": "xz",
    "liblz4-dev": "lz4",
    "libzstd-dev": "zstd",
    "libssl-dev": "openssl",
    "libxxhash-dev": "xxhash",
    "nlohmann-json3-dev": "nlohmann-json",
    "gfortran": "gcc languages=c,c++,fortran",
    "g++": "gcc languages=c,c++,fortran",
    "ninja-build": "ninja",
    "pkg-config": "pkgconf",
}


def root(home):
    from .package_operations import _inside

    return _inside(home, home.root / "spack")


def configured(home):
    return (home.root / "apt/spack.json").is_file()


def settings(home):
    data = read_json(home.root / "apt/spack.json")
    if data.get("schema_version") != 1 or data.get("backend") != "spack":
        raise ForgeError("Invalid Spack backend settings.")
    return data


def _saved_install_tree(home):
    file = root(home) / "config/config.yaml"
    if file.is_file():
        try:
            data = yaml.safe_load(file.read_text())
            tree = data["config"]["install_tree"]
        except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
            raise ForgeError(f"Cannot read the existing Spack install layout: {file}") from exc
        if not isinstance(tree, dict) or not isinstance(tree.get("root"), str):
            raise ForgeError(f"Invalid Spack install layout: {file}")
        return tree
    if configured(home) or (root(home) / "store").exists():
        saved = settings(home).get("install_tree") if configured(home) else None
        if saved:
            if not isinstance(saved, dict) or not isinstance(saved.get("root"), str):
                raise ForgeError("Invalid saved Spack install layout.")
            return saved
        # Old deployments used this exact padding rule. Never change their
        # layout in place, even when repairing a missing config file.
        return {"root": str(root(home) / "store"),
                "padded_length": max(128, len(str(root(home) / "store")) + 32)}
    return None


def _new_install_tree(home):
    from .package_operations import _inside
    from .storage import ForgeHome, identifier
    config = read_json(home.root / "config.json")
    base = ForgeHome(Path(config.get("apt_base_home", str(home.root))))
    if base.root != home.root:
        if not home.root.is_relative_to(base.root / "apt/profiles"):
            raise ForgeError("Spack profile is outside its Forge home.")
        name = identifier(home.root.name.removeprefix("spack-"))
    else:
        name = "_direct"
    store = _inside(base, base.root / "apps" / name)
    return {"root": str(store), "padded_length": False,
            "projections": {"all": INSTALL_PROJECTION}}


def store_root(home):
    """Report the native store without initializing it or changing old layouts."""
    tree = _saved_install_tree(home) or _new_install_tree(home)
    return Path(tree["root"])


def normalize(text):
    text = text.strip()
    if not text or text.startswith("-") or any(c in text for c in "\x00\r\n"):
        raise ForgeError(
            "Supply a package name or a quoted Spack spec, without command options."
        )
    for alias in sorted(ALIASES, key=len, reverse=True):
        if text.startswith(alias) and (
            len(text) == len(alias) or text[len(alias)] in "@=<> %~+"
        ):
            replacement = ALIASES[alias].split(" ", 1)
            text = replacement[0] + text[len(alias) :]
            if len(replacement) == 2:
                text += " " + replacement[1]
            break
    match = re.fullmatch(
        r"([a-z0-9][a-z0-9_-]*)(==|=|>=|<=)([A-Za-z0-9_.-]+)( .*)?", text
    )
    if match:
        name, relation, version, suffix = match.groups()
        text = (
            name
            + "@"
            + (
                {
                    "=": "=" + version,
                    "==": "=" + version,
                    ">=": version + ":",
                    "<=": ":" + version,
                }[relation]
            )
        )
    # Native Spack variants follow the root version constraint.
    if match:
        text += suffix or ""
    if not re.match(r"^[a-z0-9][a-z0-9_-]*(?:$|[@+~%\s])", text):
        raise ForgeError(
            f"Invalid package/spec: {text}",
            hint="Use gsl, gsl@2.8, or a quoted native Spack spec.",
        )
    return text


def name_of(text):
    return re.match(r"^[a-z0-9][a-z0-9_-]*", normalize(text))[0]


def current(home):
    file = home.root / "apt/state.json"
    value = read_json(file).get("spack", {}) if file.exists() else {}
    if file.exists() and not value:
        raise ForgeError(
            "This is an archived source package environment.",
            hint="jforge apt switch spack; jforge list --archived",
        )
    return value or {
        "schema_version": 1,
        "roots": [],
        "packages": [],
        "environment": None,
    }


def environment(home):
    from .package_operations import _inside

    result = current(home).get("environment")
    if not result:
        raise ForgeError(
            "No Spack environment is active.", hint="jforge apt import spack"
        )
    return _inside(home, Path(result))


def _process_environment(home):
    env = dict(os.environ)
    for key in [
        "SPACK_ENV",
        "SPACK_ENV_VIEW",
        "SPACK_ROOT",
        "SPACK_DISABLE_LOCAL_CONFIG",
    ]:
        env.pop(key, None)
    env.update(
        SPACK_PYTHON=sys.executable,
        SPACK_COLOR="never",
        SPACK_USER_CACHE_PATH=str(root(home) / "cache"),
        SPACK_USER_CONFIG_PATH=str(root(home) / "discovery"),
        SPACK_SYSTEM_CONFIG_PATH=str(root(home) / "system"),
        SPACK_GNUPGHOME=str(root(home) / "gpg"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def _executable(home):
    from .package_operations import _inside

    value = settings(home)
    executable = _inside(home, Path(value["engine"]) / "bin/spack")
    if not executable.is_file():
        raise ForgeError("Managed Spack is missing.", hint="jforge apt import spack")
    return executable


def command(home, args, envdir=None):
    result = [
        sys.executable,
        str(_executable(home)),
        "--color=never",
        "-C",
        str(
            Path(envdir) / ".jforge-config"
            if envdir is not None and (Path(envdir) / ".jforge-config").is_dir()
            else root(home) / "config"
        ),
    ]
    if envdir is not None:
        result += ["-e", str(envdir)]
    return result + list(args)


def run(home, args, op, label, *, envdir=None, timeout=3600, progress=None):
    """Reuse Forge's streamed, separately archived stdout/stderr command runner."""
    from .package_operations import run_command

    script = op / "spack-context.sh"
    env = _process_environment(home)
    keys = [
        "SPACK_PYTHON",
        "SPACK_COLOR",
        "SPACK_USER_CACHE_PATH",
        "SPACK_USER_CONFIG_PATH",
        "SPACK_SYSTEM_CONFIG_PATH",
        "SPACK_GNUPGHOME",
        "PYTHONDONTWRITEBYTECODE",
    ]
    script.write_text(
        "unset SPACK_ENV SPACK_ENV_VIEW SPACK_ROOT SPACK_DISABLE_LOCAL_CONFIG\n"
        + "\n".join("export " + k + "=" + shlex.quote(env[k]) for k in keys)
        + "\n"
    )
    if progress:
        progress("Spack " + label + ": " + shlex.join(args))
    run_command(
        command(home, args, envdir),
        home.root,
        script,
        op,
        label,
        timeout,
        progress,
    )
    return (op / f"command-{label}.stdout.log").read_text(
        encoding="utf-8", errors="replace"
    )


def _json(text, label):
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ForgeError(
            f"Spack returned invalid JSON during {label}.", code=1
        ) from exc


def _python_externals(python):
    return {
        "python": {
            "externals": [{"spec": "python@" + python["version"],
                           "prefix": python.get("base_prefix", python["prefix"])}],
            "buildable": False,
        },
        # Spack's Python extension recipes require this node. They can use
        # the existing interpreter without creating a second environment.
        "python-venv": {
            "externals": [{"spec": "python-venv@1.0", "prefix": python["prefix"]}],
            "buildable": False,
        },
    }


def _check_python_location(home, selected):
    config = read_json(home.root / "config.json")
    forge_home = Path(config.get("apt_base_home", home.root)).resolve()
    prefixes = (selected["prefix"], selected.get("base_prefix", selected["prefix"]))
    if any(Path(prefix).resolve().is_relative_to(forge_home) for prefix in prefixes):
        raise ForgeError("The selected Python is inside the Forge data directory.",
                         hint="Select an external Python with pyenv and initialize a fresh apt profile.")


def _python_reuse(*, buildcache=True):
    exclude = [*PYTHON_PACKAGES, *(f"^{name}" for name in PYTHON_PACKAGES)]
    sources = [{"type": "local", "exclude": exclude}, {"type": "external"}]
    if buildcache:
        sources.append({"type": "buildcache", "exclude": exclude})
    return {"roots": True, "from": sources}


def _config(home, repo, python):
    directory = root(home) / "config"
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "config": {
            "install_tree": _saved_install_tree(home) or _new_install_tree(home),
            "build_stage": [str(root(home) / "stage")],
            "source_cache": str(root(home) / "sources"),
            "environments_root": str(root(home) / "environments"),
        },
        "repos": {"builtin": str(repo)},
        "bootstrap": {"root": str(root(home) / "bootstrap")},
        "packages": _python_externals(python),
    }
    for name, content in data.items():
        (directory / (name + ".yaml")).write_text(
            yaml.safe_dump({name: content}, sort_keys=False)
        )
    if not (directory / "mirrors.yaml").exists():
        (directory / "mirrors.yaml").write_text(
            yaml.safe_dump(
                {
                    "mirrors": {
                        "spack-public": {
                            "url": "https://mirror.spack.io",
                            "binary": False,
                        },
                        "spack-public-binaries": {
                            "url": "https://binaries.spack.io/develop",
                            "binary": True,
                            "source": False,
                            "signed": True,
                        },
                    }
                }
            )
        )
    return data["config"]["install_tree"]


def _unpack(home, source, directory, op, label):
    from .package_operations import verified_archive
    from .downloads import unpack

    if directory.exists():
        return
    archive = verified_archive(home, source, op / label)
    temporary = directory.with_name(directory.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        unpack(archive, temporary)
        entries = list(temporary.iterdir())
        if len(entries) != 1 or not entries[0].is_dir():
            raise ForgeError("Unexpected Spack archive layout.")
        directory.parent.mkdir(parents=True, exist_ok=True)
        entries[0].rename(directory)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def bootstrap(home, *, progress=None, extensions=None):
    from .package_operations import locked, operation

    with (
        locked(home),
        operation(home, "spack-bootstrap", {}) as (op, result),
    ):
        current(home)  # Refuse to overwrite archived source state.
        if not configured(home) and any(
            (home.root / "apt" / name).exists()
            for name in ("official.json", "sources.json")
        ):
            raise ForgeError(
                "Source package data is archived; select a Spack profile.",
                hint="jforge apt switch spack",
            )
        if configured(home) and current(home).get("environment"):
            value = settings(home)
            _executable(home)
            result.update(
                backend="spack",
                engine_version=value["engine_version"],
                repository_commit=value["repository_commit"],
                python=value["python"],
                activation=current(home)["activation"],
                state_committed=True,
            )
            return result
        if not shutil.which("bash"):
            raise ForgeError("Spack backend requires Bash on PATH.")
        previous = settings(home) if configured(home) else None
        executable = (previous["python"]["executable"] if previous
                      else shutil.which("python") or shutil.which("python3") or sys.executable)
        try:
            probe = subprocess.run(
                [
                    executable,
                    "-c",
                    'import json,sys;print(json.dumps({"executable":sys.executable,"version":sys.version.split()[0],"prefix":sys.prefix,"base_prefix":sys.base_prefix}))',
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=True,
            )
            selected = _json(probe.stdout, "Python selection")
        except (OSError, subprocess.SubprocessError) as exc:
            raise ForgeError(f"Cannot use selected Python: {executable}") from exc
        _check_python_location(home, selected)
        base = root(home)
        base.mkdir(parents=True, exist_ok=True)
        engine = base / "engine" / ENGINE_VERSION
        repo = base / "repositories" / REPO_COMMIT
        _unpack(home, ENGINE, engine, op, "engine-download")
        _unpack(home, REPOSITORY, repo, op, "repository-download")
        if previous:
            repo_path = Path(previous["repository"])
            pin = previous["repository_archive"]
            commit = previous["repository_commit"]
        else:
            repo_path, pin, commit = (
                repo / "repos/spack_repo/builtin",
                REPOSITORY,
                REPO_COMMIT,
            )
        install_tree = _config(home, repo_path, selected)
        for name in ["discovery", "system", "environments"]:
            (base / name).mkdir(exist_ok=True)
        value = {
            "schema_version": 1,
            "backend": "spack",
            "engine": str(engine),
            "engine_version": ENGINE_VERSION,
            "engine_archive": ENGINE,
            "repository": str(repo_path),
            "repository_commit": commit,
            "repository_archive": pin,
            **(
                {"official_cache_keys_ready": True}
                if previous and previous.get("official_cache_keys_ready")
                else {}
            ),
            "python": selected,
            "install_tree": install_tree,
            "extension_recipes": (previous.get("extension_recipes", True) if previous else True)
            if extensions is None else extensions,
        }
        write_json(home.root / "apt/spack.json", value)
        version = run(home, ["--version"], op, "version", progress=progress).strip()
        if version != ENGINE_VERSION:
            raise ForgeError(f"Expected Spack {ENGINE_VERSION}; found {version}.")
        run(
            home,
            ["compiler", "find", "--scope", "user"],
            op,
            "compiler-discovery",
            progress=progress,
        )
        result["compilers"] = run(
            home, ["compiler", "list"], op, "compilers", progress=None
        ).strip()
        from .recipe_releases import ensure

        if value["extension_recipes"]:
            ensure(home, op)
        _refresh_catalog(home, op)
        if not current(home)["environment"]:
            envdir = _candidate(home, op, [], previous=None)
            _publish(home, op, envdir, [], {"packages": [], "modifications": []})
        result.update(
            backend="spack",
            engine_version=version,
            repository_commit=commit,
            python=selected,
            activation=current(home).get("activation"),
            state_committed=True,
        )
        return result


def _extension_repository(home):
    from .recipe_releases import register

    return register(home)


def _refresh_catalog(home, op):
    output = run(home, ["list", "--format", "name_only"], op, "catalog", progress=None)
    names = sorted(
        {
            word
            for line in output.splitlines()
            if not line.startswith("==>")
            for word in line.split()
            if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", word)
        }
    )
    write_json(
        root(home) / "catalog.json",
        {"schema_version": 1, "packages": names, "updated_at": now()},
    )
    return names


def catalog(home, pattern="*", query=None):
    cache = root(home) / "catalog.json"
    names = read_json(cache)["packages"] if cache.exists() else []
    active = {p["name"]: p for p in current(home)["packages"]}
    return [
        {
            **{"name": name, "version": "", "status": "available", "backend": "spack"},
            **active.get(name, {}),
        }
        for name in names
        if fnmatch.fnmatchcase(name, pattern)
        and (query is None or query.lower() in name.lower())
    ]


def _spec_node(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ForgeError(f"Cannot read installed package spec: {path}") from exc
    spec = data.get("spec", data) if isinstance(data, dict) else None
    if isinstance(spec, dict) and isinstance(spec.get("nodes"), list) and spec["nodes"]:
        return spec["nodes"][0]
    return spec


def _spec_target(path):
    node = _spec_node(path)
    arch = node.get("arch") if isinstance(node, dict) else None
    target = arch.get("target") if isinstance(arch, dict) else None
    if isinstance(target, dict):
        target = target.get("name")
    if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", target):
        raise ForgeError(f"Installed package spec has no CPU target: {path}")
    return target


def _generic_host_target(home):
    """Best generic CPU target, matching Spack build-cache binaries."""
    vendor = root(home) / "engine" / ENGINE_VERSION / "lib/spack/spack/vendor"
    if not (vendor / "archspec/cpu").is_dir():
        return None
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = str(vendor)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import archspec.cpu as cpu\n"
                "host = cpu.host()\n"
                "print((host.generic or host).name)\n",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ForgeError("Cannot detect the host CPU target for this Spack store.") from exc
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        raise ForgeError(
            "Cannot detect the host CPU target for this Spack store.",
            hint=detail[-1] if detail else None,
        )
    name = probe.stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ForgeError(f"Unexpected host CPU target: {name!r}")
    return name


def _gcc_runtime_target(home):
    """Keep one gcc-runtime in a package-name store.

    A reused binary may be the host microarchitecture while another package is
    the generic target. Each target plans its own gcc-runtime, and both would
    occupy the same prefix. Use the runtime already installed, otherwise the
    host's generic target.
    """
    layout = _saved_install_tree(home) or _new_install_tree(home)
    if not isinstance(layout, dict) or layout.get("projections") != {"all": INSTALL_PROJECTION}:
        return None
    installed = Path(layout["root"]) / "gcc-runtime" / ".spack" / "spec.json"
    if installed.is_file():
        return _spec_target(installed)
    return _generic_host_target(home)


def _candidate(home, op, roots, previous):
    from .recipe_releases import ensure

    profile = settings(home)
    _check_python_location(home, profile["python"])
    if profile.get("extension_recipes", True):
        ensure(home, op)
    directory = root(home) / "environments" / op.name
    directory.mkdir(parents=True)
    document = {
        "spack": {
            "specs": roots,
            "view": False,
            "concretizer": {"unify": True, "reuse": _python_reuse()},
        }
    }
    target = _gcc_runtime_target(home)
    if target:
        document["spack"]["packages"] = {"gcc-runtime": {"require": [f"target={target}"]}}
    (directory / "spack.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
    shutil.copytree(root(home) / "config", directory / ".jforge-config")
    package_config = directory / ".jforge-config/packages.yaml"
    config = yaml.safe_load(package_config.read_text()) if package_config.is_file() else {}
    config.setdefault("packages", {}).update(_python_externals(profile["python"]))
    package_config.write_text(yaml.safe_dump(config, sort_keys=False))
    if previous and (Path(previous) / "spack.lock").is_file():
        shutil.copy2(Path(previous) / "spack.lock", directory / "spack.lock")
    return directory


def _snapshot(home, envdir, op, label="snapshot", *, cache=None, overwrite=(),
              omit_cached_build_deps=False):
    bridge = Path(__file__).with_name("spack_bridge.py")
    if cache is not None:
        args = ["installation-plan", cache, json.dumps(list(overwrite))]
        if omit_cached_build_deps:
            args.append("omit-cached-build-deps")
    else:
        args = ["installed" if label == "installed" else "snapshot"]
    return _json(
        run(
            home,
            [
                "python",
                str(bridge),
                *args,
            ],
            op,
            label,
            envdir=envdir,
        ),
        label,
    )


def _name_only_layout(envdir):
    file = envdir / ".jforge-config/config.yaml"
    if not file.is_file():
        return None
    layout = yaml.safe_load(file.read_text()).get("config", {}).get("install_tree", {})
    if not isinstance(layout, dict) or layout.get("projections") != {"all": "{name}"}:
        return None
    return layout


def _pin_installed_gcc_runtime(plan, envdir):
    layout = _name_only_layout(envdir)
    if layout is None:
        return None
    runtime_hashes = {
        package["hash"] for package in plan["packages"]
        if package["name"] == "gcc-runtime" and not package["external"]
    }
    spec_file = Path(layout["root"]) / "gcc-runtime/.spack/spec.json"
    if not runtime_hashes or not spec_file.is_file():
        return None
    node = _spec_node(spec_file)
    installed = node.get("hash") if isinstance(node, dict) else None
    if not isinstance(installed, str) or not re.fullmatch(r"[a-z2-7]{32}", installed):
        return None
    conflicting = runtime_hashes - {installed}
    if not conflicting:
        return None
    arch = node.get("arch")
    if node.get("name") != "gcc-runtime" or not isinstance(arch, dict):
        return None
    version, platform, os_name = node.get("version"), arch.get("platform"), arch.get("platform_os")
    if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", value)
               for value in (version, platform, os_name)):
        return None
    target = _spec_target(spec_file)
    manifest = envdir / "spack.yaml"
    document = yaml.safe_load(manifest.read_text())
    packages = document["spack"].setdefault("packages", {})
    architecture = f"platform={platform} os={os_name} target={target}"
    # Spack generates runtime dependencies during solving. Constrain their
    # inputs here and verify the concrete runtime hash in the resulting plan.
    packages.setdefault("gcc-runtime", {})["require"] = [f"@={version} {architecture}"]
    # A runtime constraint alone can lead Spack to choose a different compiler.
    # Keep the GCC languages of the packages that used the conflicting runtime.
    for package in plan["packages"]:
        if package["installed"] or package["external"]:
            continue
        dependencies = package.get("node", {}).get("dependencies", [])
        if not any(dep["name"] == "gcc-runtime" and dep["hash"] in conflicting for dep in dependencies):
            continue
        languages = sorted({
            language
            for dep in dependencies if dep["name"] == "gcc"
            for language in dep["parameters"]["virtuals"]
            if language in {"c", "cxx", "fortran"}
        })
        if languages:
            requirement = f"{architecture} %{','.join(languages)}=gcc@={version}"
            requirements = packages.setdefault(package["name"], {}).setdefault("require", [])
            if requirement not in requirements:
                requirements.append(requirement)
    # Reused binary DAGs can bring their already-fixed runtime dependencies.
    # The retry resolves against local specs. Installation can still use a
    # cached binary that matches the resulting concrete hash.
    document["spack"]["concretizer"]["reuse"] = _python_reuse(buildcache=False)
    manifest.write_text(yaml.safe_dump(document, sort_keys=False))
    return installed


def _pending_install_hashes(plan):
    """Split a plan into binary installs and source builds.

    Cached specs are installed from their binaries and must not pull the tools
    that created those archives. Specs with no binary are built afterwards.
    """
    cached, source = [], []
    for package in plan["packages"]:
        if package.get("external") or package.get("installed"):
            continue
        digest = package["hash"]
        if not isinstance(digest, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", digest):
            raise ForgeError(f"Unexpected Spack package hash: {digest!r}")
        (cached if package.get("cached") else source).append(digest)
    return cached, source


def _install_args(jobs, policy, hashes=()):
    return [
        "install",
        "--only-concrete",
        "--verbose",
        "--show-log-on-error",
        "-j",
        str(jobs),
        "--use-buildcache",
        policy,
        *[f"/{digest}" for digest in hashes],
    ]


def _validate_name_only_plan(plan, envdir):
    """Reject conflicting required prefixes; Spack owns dependency selection."""
    layout = _name_only_layout(envdir)
    if layout is None:
        return  # Existing profiles retain their original multi-instance policy.
    builds = {}
    for package in plan["packages"]:
        if package["external"]:
            continue  # Externals do not occupy this store's package directories.
        builds.setdefault(package["name"], set()).add(package["hash"])
    conflicts = sorted(name for name, hashes in builds.items() if len(hashes) > 1)
    if conflicts:
        raise ForgeError("This environment allows one build per package; the plan contains multiple instances of: " +
                         ", ".join(conflicts), code=1,
                         hint="Choose compatible Spack specs that require one instance of each package.")
    store = Path(layout["root"])
    for package in plan["packages"]:
        if package["external"]:
            continue
        prefix = Path(package["prefix"])
        if prefix != store / package["name"]:
            raise ForgeError(f"Spack returned an unexpected name-only prefix: {prefix}", code=1)
        if (prefix.exists() or prefix.is_symlink()) and not package["installed"] and not package.get("prefix_matches"):
            raise ForgeError(f"{package['name']} already occupies {prefix} with another build; its metadata and files are preserved.",
                             code=1, hint="jforge apt switch spack --name <new-name>; then install the requested packages there.")


def _validate_external_python(home, packages):
    for package in packages:
        name = package["name"]
        if name not in PYTHON_PACKAGES:
            continue
        selected = settings(home)["python"]
        prefix = selected.get("base_prefix", selected["prefix"]) if name == "python" else selected["prefix"]
        if not package["external"] or Path(package["prefix"]).resolve() != Path(prefix).resolve():
            raise ForgeError(
                f"The Spack plan uses {name} from {package['prefix']}. Forge requires the external Python at {prefix}.",
                code=1,
                hint="Select Python with pyenv and use a fresh apt profile if the current profile has a locked Python dependency.",
            )


def _publish(home, op, envdir, roots, snapshot):
    from .shell_activation import activation
    from .package_operations import key_for

    _validate_external_python(home, snapshot["packages"])
    python_prefixes = {Path(row["prefix"]) for row in snapshot["packages"] if row["name"] in PYTHON_PACKAGES}
    modifications = []
    for modification in snapshot["modifications"]:
        if python_prefixes and modification["type"] in {"PrependPath", "AppendPath"}:
            path = Path(modification["value"])
            if modification["name"] == "PATH" and path.parent in python_prefixes and path.name in {"bin", "Scripts"}:
                continue
            if modification["name"] == "PYTHONPATH" and any(path.is_relative_to(prefix) for prefix in python_prefixes):
                continue
        modifications.append(modification)
    snapshot = {**snapshot, "modifications": modifications}
    # Header-only consumers may inspect these legacy hints before find_package.
    for package in snapshot["packages"]:
        if package["name"] == "eigen":
            prefix = Path(package["prefix"])
            for include in [prefix / "include/eigen3", prefix / "include"]:
                if (include / "Eigen/Core").is_file():
                    for name, value in [
                        ("EIGEN3_INCLUDE_DIR", include),
                        ("EIGEN3_ROOT", prefix),
                    ]:
                        snapshot["modifications"].append(
                            {"type": "SetEnv", "name": name, "value": str(value)}
                        )
                    break

    revision = home.root / "environments/apt/revisions" / op.name
    revision.mkdir(parents=True, exist_ok=True)
    bridge = revision / "spack_bridge.py"
    shutil.copy2(Path(__file__).with_name("spack_bridge.py"), bridge)
    mods = revision / "modifications.json"
    write_json(revision / "snapshot.json", snapshot)
    mods.write_text(json.dumps(snapshot["modifications"], indent=2) + "\n")
    adapter = revision / "spack-environment.sh"
    argv = command(home, ["python", str(bridge), "render", str(mods)], envdir)
    values = _process_environment(home)
    exports = [
        "SPACK_USER_CACHE_PATH",
        "SPACK_USER_CONFIG_PATH",
        "SPACK_SYSTEM_CONFIG_PATH",
        "SPACK_GNUPGHOME",
    ]
    # Render against the invoking shell's environment at source time. Never
    # capture the install-time PATH in a persistent activation file.
    invoke = [
        "env",
        "-u",
        "SPACK_ENV",
        "-u",
        "SPACK_ENV_VIEW",
        "-u",
        "SPACK_ROOT",
        "-u",
        "SPACK_DISABLE_LOCAL_CONFIG",
        *["%s=%s" % (k, values[k]) for k in exports],
        *argv,
    ]
    adapter.write_text(
        "_JFORGE_SPACK_CODE=$("
        + shlex.join(invoke)
        + ') || return $?\neval "$_JFORGE_SPACK_CODE"\nunset _JFORGE_SPACK_CODE\n'
    )
    variables = {m["name"] for m in snapshot["modifications"]}
    paths = {m["name"] for m in snapshot["modifications"] if "Path" in m["type"]}
    destination = revision / "activate.sh"
    destination.write_text(
        activation(home, [adapter], extra_variables=variables, extra_paths=paths)
    )
    records = []
    active = {}
    manual = []
    for row in snapshot["packages"]:
        record = {
            **row,
            "schema_version": 1,
            "managed_by": "jforge-spack",
            "install_id": row["hash"],
            "backend": "spack",
            "active": True,
            "automatic": not row["root"],
            "prefix_exists": Path(row["prefix"]).is_dir(),
            "operation_id": op.name,
            "environment": str(envdir),
            "context": {"python": settings(home)["python"]},
            "provenance": {
                "kind": "spack",
                "spec": row["spec"],
                "dag_hash": row["hash"],
                "external": row["external"],
            },
        }
        from .package_operations import _inside

        file = _inside(
            home,
            home.root
            / "packages"
            / row["name"]
            / row["version"]
            / row["hash"]
            / "manifest.json",
        )
        if not file.exists():
            write_json(file, record)
        records.append(record)
        key = key_for(record)
        active[row["name"]] = key
        if row["root"]:
            manual.append(key)
    state = {
        "schema_version": 1,
        "active": active,
        "manual": manual,
        "activation": str(destination),
        "last_operation_id": op.name,
        "updated_at": now(),
        "backend": "spack",
        "spack": {
            "schema_version": 1,
            "roots": roots,
            "packages": records,
            "environment": str(envdir),
            "activation": str(destination),
            "operation_id": op.name,
        },
    }
    write_json(home.root / "apt/state.json", state)
    return records


def _requested_roots(before, action, translated):
    if action == "remove":
        names = {name_of(s) for s in translated}
        missing = names - {name_of(s) for s in before["roots"]}
        if missing:
            raise ForgeError(
                "Packages are not explicit roots: " + ", ".join(sorted(missing))
            )
        roots = [s for s in before["roots"] if name_of(s) not in names]
    elif action in {"upgrade", "full-upgrade"}:
        if translated:
            raise ForgeError(
                "Spack upgrade updates the locked environment; use install SPEC to change a specific root."
            )
        roots = before["roots"]
    else:
        names = {name_of(s) for s in translated}
        roots = [s for s in before["roots"] if name_of(s) not in names] + translated
    if action == "check" and not translated:
        roots = before["roots"]
    return roots


def transact(
    home,
    action,
    specs=(),
    *,
    jobs=1,
    timeout=3600,
    cache="auto",
    dry_run=False,
    progress=None,
):
    from .package_operations import locked, operation

    if cache not in {"auto", "only", "never"}:
        raise ForgeError("Build cache mode must be auto, only or never.")
    if jobs < 1 or not math.isfinite(timeout) or timeout <= 0:
        raise ForgeError("Jobs and timeout must be positive.")
    translated = list(dict.fromkeys(normalize(s) for s in specs))
    if action != "remove" and any(name_of(s) in PYTHON_PACKAGES for s in translated):
        raise ForgeError("Select Python and virtual environments with pyenv before running Forge.")
    before = current(home)
    roots = _requested_roots(before, action, translated)
    if dry_run:
        return {
            "backend": "spack",
            "dry_run": True,
            "roots": roots,
            "requires_concretization": True,
            "note": "No downloads or solver execution; use apt check for a concrete dependency plan.",
        }
    if not configured(home):
        bootstrap(home, progress=progress)
    with (
        locked(home),
        operation(
            home,
            "spack-" + action,
            {"specs": list(specs), "roots": roots, "cache": cache, "jobs": jobs},
        ) as (op, result),
    ):
        before = current(home)
        roots = _requested_roots(before, action, translated)
        result["roots"] = roots
        previous = before.get("environment") if action not in {"upgrade", "full-upgrade"} else None
        envdir = _candidate(home, op, roots, previous)
        # In a flat store, validate binary and source work separately. Historical
        # build dependencies of installed/binary packages need no local prefix.
        cache_first = (bool(roots) and action != "remove" and cache == "auto"
                       and _name_only_layout(envdir) is not None)
        plan_cache = "only" if cache_first or action == "remove" else cache
        overwrite = [name_of(s) for s in translated] if action == "reinstall" else []
        committed = False
        runtime_retried = False

        def resolve_plan(policy, *, suffix="", omit_cached_build_deps=False):
            run(home, flags, op, "concretize" + suffix, envdir=envdir,
                timeout=timeout, progress=progress)
            dag = _snapshot(home, envdir, op, "dag" + suffix)
            write_json(op / "dag.json", dag)
            run(home, ["python", str(Path(__file__).with_name("spack_bridge.py")), "tree"],
                op, "dependency-tree" + suffix, envdir=envdir, timeout=timeout,
                progress=progress if getattr(progress, "verbose", False) else None)
            return _snapshot(home, envdir, op, "plan" + suffix, cache=policy,
                             overwrite=overwrite, omit_cached_build_deps=omit_cached_build_deps)

        def validate_plan(plan, label, policy, *, omit_cached_build_deps=False):
            nonlocal runtime_retried
            write_json(op / f"{label}.json", {**plan, "cache_policy": policy})
            _validate_external_python(home, plan["packages"])
            try:
                _validate_name_only_plan(plan, envdir)
            except ForgeError:
                if runtime_retried or action not in {"install", "check", "reinstall"}:
                    raise
                installed = _pin_installed_gcc_runtime(plan, envdir)
                if installed is None:
                    raise
                runtime_retried = True
                if progress:
                    progress("Retry dependency resolution with the installed gcc-runtime")
                (op / f"{label}.json").rename(op / f"{label}-before-runtime-retry.json")
                lock = envdir / "spack.lock"
                lock.rename(op / "spack-before-runtime-retry.lock")
                if previous and (Path(previous) / "spack.lock").is_file():
                    shutil.copy2(Path(previous) / "spack.lock", lock)
                plan = resolve_plan(policy, suffix="-runtime-retry",
                                    omit_cached_build_deps=omit_cached_build_deps)
                write_json(op / f"{label}.json", {**plan, "cache_policy": policy})
                _validate_external_python(home, plan["packages"])
                _validate_name_only_plan(plan, envdir)
                if any(p["name"] == "gcc-runtime" and not p["external"] and p["hash"] != installed
                       for p in plan["packages"]):
                    raise ForgeError("Spack could not reuse the installed gcc-runtime.", code=1)
            return plan

        try:
            if progress:
                progress("[1/4] Resolve dependencies")
            if roots:
                flags = ["concretize"] + (
                    ["--fresh", "--force"]
                    if action in {"upgrade", "full-upgrade"}
                    else []
                )
                previous_roots = {name_of(r): r for r in before["roots"]}
                changed = any(
                    name_of(r) in previous_roots and previous_roots[name_of(r)] != r
                    for r in translated
                )
                if changed and "--force" not in flags:
                    flags.append("--force")
                plan = resolve_plan(plan_cache)
                if progress:
                    progress(f"[2/4] Check installation plan: {len(plan['packages'])} packages")
            else:
                plan = {"packages": [], "modifications": []}
            plan = validate_plan(plan, "plan", plan_cache)
            if action == "check":
                result.update(
                    backend="spack",
                    lockfile=str(op / "candidate-environment/spack.lock")
                    if (envdir / "spack.lock").is_file()
                    else None,
                    plan=str(op / "plan.json"),
                    packages=plan["packages"],
                    ready_to_install=True,
                    source_fallback_requires_validation=cache_first,
                    builds_executed=False,
                    state_committed=False,
                )
                if cache_first:
                    result["note"] = (
                        "Binary-only installation plan; if a cache install fails, "
                        "remaining source dependencies are validated before building."
                    )
                return result
            if progress:
                progress(
                    "[3/4] Install packages"
                    if action != "remove"
                    else "[3/4] Retain installed store; update selected roots"
                )
            if roots and action != "remove":
                show_plan = getattr(progress, "installation_plan", None)
                if show_plan is not None:
                    show_plan(plan["packages"], overwrite)
                if cache != "never":
                    _cache_keys(home, op, envdir, progress)

                args = _install_args(jobs, plan_cache)
                if action == "reinstall":
                    args += ["--overwrite", "-y", *[name_of(s) for s in translated]]
                label = "install-cache" if cache_first else "install"
                try:
                    run(home, args, op, label, envdir=envdir,
                        timeout=timeout, progress=progress)
                except ForgeError:
                    # Timeouts, interruptions and launch failures must propagate.
                    # Only a completed failed Spack cache attempt can fall back.
                    record = op / "command-install-cache.json"
                    failure = read_json(record) if record.is_file() else {}
                    exit_code = failure.get("exit_code")
                    if (not cache_first or failure.get("status") != "failed"
                            or not isinstance(exit_code, int) or exit_code <= 0):
                        raise
                    if progress:
                        progress("Binary installation incomplete; validate remaining source dependencies")
                    # Reinstall still asks Spack for build tools of the overwritten specs.
                    # A normal cache miss installs remaining binaries first, then builds
                    # only the specs that have no binary.
                    source_plan = _snapshot(
                        home, envdir, op, "source-plan", cache="auto", overwrite=overwrite,
                        omit_cached_build_deps=not overwrite,
                    )
                    source_plan = validate_plan(
                        source_plan, "source-plan", "auto", omit_cached_build_deps=not overwrite,
                    )
                    if show_plan is not None:
                        show_plan(source_plan["packages"], overwrite)
                    if overwrite:
                        source_args = list(args)
                        source_args[source_args.index("--use-buildcache") + 1] = "auto"
                        run(home, source_args, op, "install", envdir=envdir,
                            timeout=timeout, progress=progress)
                    else:
                        cached_hashes, source_hashes = _pending_install_hashes(source_plan)
                        if cached_hashes:
                            if progress:
                                progress(
                                    f"Install {len(cached_hashes)} remaining "
                                    "packages from the binary cache"
                                )
                            run(home, _install_args(jobs, "only", cached_hashes), op,
                                "install-binaries", envdir=envdir, timeout=timeout, progress=progress)
                        if source_hashes:
                            if progress:
                                progress(f"Build {len(source_hashes)} packages from source")
                            run(home, _install_args(jobs, "auto", source_hashes), op,
                                "install", envdir=envdir, timeout=timeout, progress=progress)
            final = _snapshot(home, envdir, op, "installed") if roots else plan
            missing = [
                p["name"]
                for p in final["packages"]
                if not p["installed"] or not Path(p["prefix"]).is_dir()
            ]
            if missing:
                raise ForgeError(
                    "Spack packages are not ready: " + ", ".join(missing), code=1
                )
            if progress:
                progress("[4/4] Save environment")
            records = _publish(home, op, envdir, roots, final)
            committed = True
            result.update(
                backend="spack",
                packages=records,
                activation=current(home)["activation"],
                environment=str(envdir),
                lockfile=str(envdir / "spack.lock")
                if (envdir / "spack.lock").is_file()
                else None,
                state_committed=True,
                store_retained=action == "remove",
            )
            return result
        finally:
            if not committed and envdir.exists():
                envdir.rename(op / "candidate-environment")


def details(home, spec):
    from .package_operations import locked, operation

    translated = normalize(spec)
    name = name_of(translated)
    if not configured(home):
        return {
            "name": name,
            "backend": "spack",
            "status": "metadata-not-initialized",
            "requested_spec": translated,
            "hint": "jforge apt import spack",
        }
    with locked(home), operation(home, "spack-show", {"spec": spec}) as (op, result):
        _extension_repository(home)
        rows = _json(
            run(home, ["list", "--format", "version_json", name], op, "details"),
            "package details",
        )
        matching = [row for row in rows if row["name"] == name]
        if not matching:
            raise ForgeError(f"No Spack package matches {name}.")
        return {**matching[0], "backend": "spack", "requested_spec": translated}


def _cache_keys(home, op, envdir, progress):
    mirrors = yaml.safe_load((root(home) / "config/mirrors.yaml").read_text()).get(
        "mirrors", {}
    )
    if "spack-public-binaries" not in mirrors or settings(home).get(
        "official_cache_keys_ready"
    ):
        return
    # Trust only the named official cache here. Arbitrary mirrors require an
    # explicitly supplied signing key through `apt sources --trust-key`.
    try:
        run(
            home,
            [
                "buildcache",
                "keys",
                "--install",
                "--trust",
                "--yes-to-all",
                "spack-public-binaries",
            ],
            op,
            "official-cache-keys",
            envdir=envdir,
            progress=progress,
        )
    except ForgeError as exc:
        write_json(
            op / "binary-cache-warning.json",
            {"message": str(exc), "source_builds_may_be_needed": True},
        )
        if progress:
            progress(
                "Official binary cache keys are unavailable; Spack may build from source. Use --cache only to require binaries."
            )
    else:
        value = settings(home)
        value["official_cache_keys_ready"] = True
        write_json(home.root / "apt/spack.json", value)


def update(home, *, progress=None):
    from .package_operations import locked, operation
    from .downloads import fetch

    if not configured(home):
        return bootstrap(home, progress=progress)
    with locked(home), operation(home, "spack-update", {}) as (op, result):
        # Resolve a commit on the engine-compatible release branch. Archives
        # avoid mutating an installed checkout or creating local Git branches.
        api = "https://api.github.com/repos/spack/spack-packages/commits/" + REPO_BRANCH
        response = op / "repository-head.json"
        fetch(api, response, op / "repository-head-download")
        head = read_json(response).get("sha", "")
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise ForgeError("Invalid official Spack recipe commit.")
        archive = op / "repository.tar.gz"
        url = f"https://github.com/spack/spack-packages/archive/{head}.tar.gz"
        fetch(url, archive, op / "repository-download")
        pin = {"url": url, "sha256": digest(archive)}
        cached = root(home).parent / "cache/downloads" / pin["sha256"]
        cached.parent.mkdir(parents=True, exist_ok=True)
        if not cached.exists():
            shutil.copy2(archive, cached)
        destination = root(home) / "repositories" / head
        _unpack(home, pin, destination, op, "repository-verified-download")
        value = settings(home)
        from . import recipe_releases

        extension = recipe_releases.prepare(home, op)
        old_repo = (root(home) / "config/repos.yaml").read_bytes()
        catalog_file = root(home) / "catalog.json"
        old_catalog = catalog_file.read_bytes() if catalog_file.exists() else None
        repo = destination / "repos/spack_repo/builtin"
        (root(home) / "config/repos.yaml").write_text(
            yaml.safe_dump({"repos": {"builtin": str(repo)}})
        )
        try:
            recipe_releases.register(home, extension)
            rows = _refresh_catalog(home, op)
        except BaseException:
            (root(home) / "config/repos.yaml").write_bytes(old_repo)
            if old_catalog is None:
                catalog_file.unlink(missing_ok=True)
            else:
                catalog_file.write_bytes(old_catalog)
            raise
        write_json(home.root / "apt/recipe-release.json", extension)
        value.update(
            repository=str(repo), repository_commit=head, repository_archive=pin
        )
        write_json(home.root / "apt/spack.json", value)
        result.update(
            backend="spack",
            available=len(rows),
            recipe_release=extension["version"],
            repository_commit=head,
            updated_at=now(),
            note="Recipe metadata refreshed; installed environments retain their lockfiles.",
        )
        return result


def validate_sources(*, add=None, url=None, remove=None, trust_key=None):
    if add and (not url or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", add)):
        raise ForgeError("Adding a mirror requires a simple name and --url.")
    if url:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if (
            parsed.scheme not in {"https", "file", "oci"}
            or parsed.username
            or parsed.password
        ):
            raise ForgeError(
                "Use an HTTPS, file:// or OCI mirror URL without embedded credentials."
            )
    if url and not add:
        raise ForgeError("--url requires --add NAME.")
    if add in {"spack-public", "spack-public-binaries"}:
        raise ForgeError(
            "The official mirror names are reserved; choose a custom name."
        )
    if trust_key and not Path(trust_key).expanduser().is_file():
        raise ForgeError(f"Signing key does not exist: {trust_key}")


def sources(home, *, add=None, url=None, remove=None, trust_key=None, progress=None):
    from .package_operations import locked, operation

    validate_sources(add=add, url=url, remove=remove, trust_key=trust_key)
    if not configured(home):
        if add or remove or trust_key:
            bootstrap(home, progress=progress)
        else:
            return {
                "backend": "spack",
                "sources": [
                    {
                        "name": "spack",
                        "url": "https://github.com/spack/spack-packages",
                        "description": "Spack recipes and explicitly configured build-cache mirrors",
                    }
                ],
                "aliases": ALIASES,
            }
    path = root(home) / "config/mirrors.yaml"
    mirrors = yaml.safe_load(path.read_text()).get("mirrors", {})
    if not any([add, remove, trust_key]):
        return {
            "backend": "spack",
            "repository_commit": settings(home)["repository_commit"],
            "sources": [
                {"name": name, **(value if isinstance(value, dict) else {"url": value})}
                for name, value in mirrors.items()
            ],
            "aliases": ALIASES,
        }
    with (
        locked(home),
        operation(
            home, "spack-sources", {"add": add, "url": url, "remove": remove}
        ) as (op, result),
    ):
        if trust_key:
            key = Path(trust_key).expanduser().resolve()
            if not key.is_file():
                raise ForgeError(f"Signing key does not exist: {key}")
            run(
                home,
                ["gpg", "trust", "--yes-to-all", str(key)],
                op,
                "trust-key",
                progress=progress,
            )
        if add:
            mirrors[add] = {"url": url, "binary": True, "source": False, "signed": True}
        if remove:
            if remove not in mirrors:
                raise ForgeError(f"Mirror not found: {remove}")
            del mirrors[remove]
        path.write_text(yaml.safe_dump({"mirrors": mirrors}, sort_keys=False))
        result.update(
            backend="spack",
            sources=[
                {"name": k, **(v if isinstance(v, dict) else {"url": v})}
                for k, v in mirrors.items()
            ],
        )
        return result


def compilers(home, *, find=False, progress=None):
    from .package_operations import locked, operation

    if not configured(home):
        bootstrap(home, progress=progress)
    with (
        locked(home),
        operation(home, "spack-compilers", {"find": find}) as (op, result),
    ):
        if find:
            run(
                home,
                ["compiler", "find", "--scope", "user"],
                op,
                "compiler-discovery",
                progress=progress,
            )
        result.update(
            backend="spack", compilers=run(home, ["compiler", "list"], op, "compilers")
        )
        return result


def autoremove(home, *, dry_run=False, progress=None):
    from .package_operations import locked, operation

    if dry_run:
        return {
            "backend": "spack",
            "dry_run": True,
            "note": "Spack will retain all saved environment revisions and their build dependencies.",
        }
    with locked(home), operation(home, "spack-autoremove", {}) as (op, result):
        run(
            home,
            [
                "gc",
                "--except-any-environment",
                "--keep-build-dependencies",
                "--yes-to-all",
            ],
            op,
            "gc",
            progress=progress,
        )
        result.update(backend="spack", historical_environments_retained=True)
        return result
