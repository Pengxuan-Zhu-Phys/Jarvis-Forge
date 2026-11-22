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

import yaml

from .paths import object_path, record_path, semantic_name, metadata
from .storage import ForgeError, ForgeHome, digest, identifier, now, read_json, write_json, write_text

ENGINE_VERSION = "1.2.2"
INSTALL_PROJECTION = "{name}-{version}"
PYTHON_PACKAGES = ("python", "python-venv")
ENGINE = {
    "name":"spack", "version":ENGINE_VERSION,
    "url": "https://github.com/spack/spack/releases/download/v1.2.2/spack-1.2.2.tar.gz",
    "sha256": "ed39d08bc295571cdec23a4566cbd8aa7ef4ebd582013d43874471a2b1257bf5",
}
REPO_COMMIT = "d4f7c711a6a42f1c4d551c8fd10fce9a11340a81"
REPO_BRANCH = "releases/v2026.06"
REPOSITORY = {
    "name":"builtin", "version":"2026.06",
    "url": f"https://github.com/spack/spack-packages/archive/{REPO_COMMIT}.tar.gz",
    "sha256": "06f6bbd84f040e1da916c0f81ec1292422d5fc17e352f565459a853f9d3d7361",
}

def root(home):
    from .package_operations import _inside

    return _inside(home, home.root / "spack")


def configured(home):
    return (home.root / "spack/backend.json").is_file()


def settings(home):
    data = read_json(home.root / "spack/backend.json")
    if data.get("schema_version") != 1 or data.get("backend") != "spack":
        raise ForgeError("Invalid Spack backend settings.")
    return data


def _saved_install_tree(home):
    file=root(home)/'config/config.yaml'
    saved=None
    if file.is_file():
        try:saved=yaml.safe_load(file.read_text())['config']['install_tree']
        except (TypeError,KeyError,yaml.YAMLError) as exc:
            raise ForgeError(f'Invalid existing Spack install layout: {file}') from exc
    elif configured(home):
        saved=settings(home).get('install_tree')
    if saved is not None:
        if not isinstance(saved,dict) or saved.get('projections',{}).get('all')!=INSTALL_PROJECTION or saved.get('padded_length') is not False or saved.get('root')!=str(home.root/'pkg'):
            raise ForgeError(f'Unsupported Spack store layout: {file}',hint='Recreate the profile in a fresh Forge home.')
        return saved
    if (root(home)/'store').exists():
        raise ForgeError(f'Unsupported Spack store layout: {root(home)/"store"}',hint='Create a fresh Forge home.')
    return None


def _new_install_tree(home):
    from .package_operations import _inside
    return {"root": str(_inside(home, home.root / "pkg")), "padded_length": False,
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
    file = home.root / "packages.json"
    value = read_json(file).get("spack", {}) if file.exists() else {}
    if file.exists() and not value:
        raise ForgeError(
            "This is an archived source package environment.",
            hint="jforge apt import spack\njforge apt instances --archived",
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
    engine = value.get("engine")
    if not isinstance(engine, str) or not Path(engine).is_absolute():
        raise ForgeError(f"Invalid Spack engine path for profile {home.root.name}.")
    from .unified_profiles import base_home
    executable = _inside(base_home(home), Path(engine) / "bin/spack")
    if not executable.is_file():
        raise ForgeError("Managed Spack is missing.", hint="jforge apt import spack")
    return executable


def command(home, args, envdir=None):
    result = [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).with_name("spack_entry.py")),
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


def run(home, args, op, label, *, envdir=None, timeout=3600, progress=None, patch_jobs=None, planning=None):
    """Reuse Forge's streamed, separately archived stdout/stderr command runner."""
    from .package_operations import run_command
    from . import spack_planning

    cached = spack_planning.query(op, envdir, args, label)
    if cached is not None:
        (op / f"command-{label}.stdout.log").write_text(cached)
        (op / f"command-{label}.stderr.log").write_text("")
        write_json(op / f"command-{label}.json", {"status": "passed", "exit_code": 0,
                   "executed": False, "source": str(op / "planning.json")})
        if progress:
            progress("Spack " + label + ": " + shlex.join(args))
            if output := getattr(progress, "output", None):
                output("stdout", cached.encode("utf-8"))
        return cached
    original_args = args
    if planning is not None:
        args = ["python", str(Path(__file__).with_name("spack_bridge.py")), "resolve-plan",
                json.dumps({**planning, "flags": list(original_args)})]

    script = op / "spack-context.sh"
    env = _process_environment(home)
    env["_JFORGE_CACHE_TRUST_LOG"] = str(op / "cache-trust-failures.jsonl")
    keys = [
        "SPACK_PYTHON",
        "SPACK_COLOR",
        "SPACK_USER_CACHE_PATH",
        "SPACK_USER_CONFIG_PATH",
        "SPACK_SYSTEM_CONFIG_PATH",
        "SPACK_GNUPGHOME",
        "PYTHONDONTWRITEBYTECODE",
        "_JFORGE_CACHE_TRUST_LOG",
    ]
    if args[0] == "install" and args[args.index("--use-buildcache") + 1] != "only":
        patch_jobs = args[args.index("-j") + 1]
    if sys.platform == "linux" and patch_jobs is not None:
        from .bootstrap_patch import bin_dir

        if patch_dir := bin_dir(home, op, env, jobs=patch_jobs, timeout=timeout, progress=progress):
            env["PATH"] = str(patch_dir) + os.pathsep + env.get("PATH", os.defpath)
            keys.append("PATH")
            script = op / "spack-source-context.sh"
    script.write_text(
        "unset SPACK_ENV SPACK_ENV_VIEW SPACK_ROOT SPACK_DISABLE_LOCAL_CONFIG\n"
        + "\n".join("export " + k + "=" + shlex.quote(env[k]) for k in keys)
        + "\n"
    )
    if progress:
        progress("Spack " + label + ": " + shlex.join(original_args))
    with (op / "spack-calls.jsonl").open("a") as calls:
        calls.write(json.dumps({"label": label, "args": args}) + "\n")
    try:
        run_command(
            command(home, args, envdir), home.root, script, op, label, timeout, progress,
            combined_logs=getattr(progress, "combined_logs", None),
        )
    except ForgeError as exc:
        _reject_cache_security_failure(op, label, exc)
        lookup_error = re.compile(r"UnknownPackageError|\b(?:Unknown package|No such package)\b|Package .+ not found", re.I)
        missing = bool(lookup_error.search(str(exc)))
        for stream in ("stdout", "stderr"):
            path = op / f"command-{label}.{stream}.log"
            if path.is_file():
                with path.open(encoding="utf-8", errors="replace") as lines:
                    missing = missing or any(lookup_error.search(line) for line in lines)
        if missing:
            raise ForgeError(str(exc), code=exc.code,
                             hint="Use native Spack package names. Run jforge apt search <term>."
                             + ("\n" + exc.hint if exc.hint else "")) from exc
        raise
    if (op / "cache-trust-failures.jsonl").is_file():
        _reject_cache_security_failure(op, label, ForgeError("Binary-cache trust verification failed.", code=1))
    output = (op / f"command-{label}.stdout.log").read_text(
        encoding="utf-8", errors="replace"
    )
    if planning is not None:
        spack_planning.save(op, envdir, planning, output)
    return output


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
    controller_venvs = Path.home() / ".jarvis-pyenv/jforge-bootstrap/venvs"
    if Path(selected["prefix"]).resolve().is_relative_to(controller_venvs.resolve()):
        raise ForgeError("The selected Python belongs to the JForge launcher.",
                         hint="Select a project Python with pyenv and initialize a fresh apt profile.")


def _python_reuse(*, buildcache=True):
    exclude = [*PYTHON_PACKAGES, *(f"^{name}" for name in PYTHON_PACKAGES)]
    sources = [{"type": "local", "exclude": exclude}, {"type": "external"}]
    if buildcache:
        sources.append({"type": "buildcache", "exclude": exclude})
    return {"roots": True, "from": sources}


def _config(home, repo, python):
    from .parallelism import policy

    parallel = policy()
    marker=home.root/'metadata.json'
    scope_data=read_json(marker) if marker.is_file() else {}
    if scope_data.get('stage_mode')=='node':
        from .unified_profiles import default_stage
        stage=str(default_stage(home))
    else:stage=scope_data.get('stage')
    directory = root(home) / "config"
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "config": {
            "build_jobs": parallel["build_jobs"],
            "concurrent_packages": parallel["concurrent_packages"],
            "installer": "new",
            "install_tree": _saved_install_tree(home) or _new_install_tree(home),
            "build_stage": [str(stage or home.root / "stage")],
            "stage_name": "{name}",
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
    from .paths import directory_lock
    with directory_lock(directory.parent,'.unpack.lock'):
        marker=directory/'metadata.json'
        record=read_json(marker) if marker.is_file() else {}
        if record.get('unpacked_sha256')==source['sha256']:
            return
        archive=verified_archive(home,source,op/label)
        temporary=op/'unpack'
        if temporary.exists():shutil.rmtree(temporary)
        try:
            unpack(archive,temporary)
            entries=list(temporary.iterdir())
            if len(entries)!=1 or not entries[0].is_dir():raise ForgeError('Unexpected Spack archive layout.')
            directory.mkdir(parents=True,exist_ok=True)
            for child in list(directory.iterdir()):
                if child.name=='metadata.json':continue
                if child.is_dir() and not child.is_symlink():shutil.rmtree(child)
                else:child.unlink()
            for child in entries[0].iterdir():child.rename(directory/child.name)
            write_json(marker,{**record,'unpacked_sha256':source['sha256']})
        finally:
            if temporary.exists():shutil.rmtree(temporary)


def _missing_system_pyenv_python(executable):
    pyenv = shutil.which("pyenv")
    shim = Path(executable).resolve()
    if not pyenv or shim.parent.name != "shims":
        return False
    options = dict(capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3)
    try:
        root = subprocess.run([pyenv, "root"], check=True, **options).stdout.strip()
        if not Path(root).is_absolute() or shim != (Path(root) / "shims/python").resolve():
            return False
        version = subprocess.run([pyenv, "version-name"], check=True, **options).stdout.strip()
        return version == "system" and subprocess.run([pyenv, "which", "python"], **options).returncode == 127
    except (OSError, subprocess.SubprocessError):
        return False


def _probe_external_python(executable, *, system_python3=None):
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
        return _json(probe.stdout, "Python selection")
    except (OSError, subprocess.SubprocessError) as exc:
        if (system_python3 and isinstance(exc, subprocess.CalledProcessError)
                and exc.returncode == 127 and _missing_system_pyenv_python(executable)):
            return _probe_external_python(system_python3)
        raise ForgeError(f"Cannot use selected Python: {executable}") from exc


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
                hint="jforge apt import spack",
            )
        if configured(home) and current(home).get("environment"):
            value = settings(home)
            _executable(home)
            _prepare_backend(home, op, progress)
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
        from .unified_profiles import record as managed_record, _python
        managed=managed_record(home)
        if managed:
            selected=_python(managed["python"]["executable"])
            if selected!=managed["python"]:raise ForgeError("The profile external Python changed.")
        elif previous:
            selected = _probe_external_python(previous["python"]["executable"])
        else:
            python = shutil.which("python")
            python3 = shutil.which("python3")
            if not (python or python3):
                raise ForgeError("No external Python is available on PATH.",
                                 hint="Select a project Python with pyenv before creating this apt profile.")
            selected = _probe_external_python(
                python or python3, system_python3=python3 if python else None,
            )
        _check_python_location(home, selected)
        base = root(home)
        base.mkdir(parents=True, exist_ok=True)
        from .unified_profiles import base_home
        shared = base_home(home).root / "spack"
        engine = object_path(shared, "spack", ENGINE, create=True, version=ENGINE_VERSION)
        repo = object_path(shared, "builtin", REPOSITORY, create=True, version="2026.06")
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
        write_json(home.root / "spack/backend.json", value)
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
        _prepare_backend(home, op, progress)
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


def _node_target(node):
    arch = node.get("arch") if isinstance(node, dict) else None
    target = arch.get("target") if isinstance(arch, dict) else None
    if isinstance(target, dict):
        target = target.get("name")
    return target if isinstance(target, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", target) else None


def _spec_target(path):
    target = _node_target(_spec_node(path))
    if not target:
        raise ForgeError(f"Installed package spec has no CPU target: {path}")
    return target


def _require_target(packages, target):
    requirement = f"target={target}"
    packages.setdefault("all", {})["target"] = [target]
    # Spack treats all.require as a default: a package-specific require replaces it.
    # Keep it on real-package overrides as well; c/cxx/fortran constrain providers.
    for name, policy in packages.items():
        if name in {"c", "cxx", "fortran"} or not isinstance(policy, dict):
            continue
        rules = policy.get("require", [])
        if name == "all" or rules:
            rules = [rules] if isinstance(rules, str) else rules
            policy["require"] = list(rules) + ([requirement] if requirement not in rules else [])


def _validate_target(packages, target):
    if not target:
        return
    incompatible = []
    for pkg in packages:
        if pkg["external"]:
            continue  # OS and user-owned external binaries cannot be rebuilt by Forge.
        actual = _node_target(pkg.get("node"))
        if actual != target:
            incompatible.append(f"{pkg['name']} ({actual or 'unknown'})")
    if incompatible:
        raise ForgeError(f"Spack profile requires target={target} throughout the build graph; "
                         + "incompatible specs: " + ", ".join(sorted(set(incompatible))) + ".",
                         code=1, hint="Remove conflicting target requests or build a fresh profile.")


def _generic_host_target(home):
    """Best generic CPU target, matching Spack build-cache binaries."""
    vendor = Path(settings(home)["engine"]) / "lib/spack/spack/vendor"
    if not (vendor / "archspec/cpu").is_dir():
        return None
    try:
        probe = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1])\n"
                "import archspec.cpu as cpu\n"
                "host = cpu.host()\n"
                "print((host.generic or host).name)\n",
                str(vendor),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
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
    if not isinstance(layout, dict) or layout.get("projections", {}).get("all") != INSTALL_PROJECTION:
        return None
    active=next((row for row in current(home)["packages"] if row["name"]=="gcc-runtime" and not row["external"]),None)
    matches=list(Path(layout["root"]).glob("gcc-runtime-*/.spack/spec.json"))
    installed = Path(active["prefix"])/".spack/spec.json" if active else matches[0] if len(matches)==1 else Path(layout["root"])/"missing"
    if installed.is_file():
        return _spec_target(installed)
    return _generic_host_target(home)


def _candidate(home, op, roots, previous, *, reuse_buildcache=True, target=None, expected_packages=None):
    from .recipe_releases import ensure
    from .toolchains import binding, spack_requirements

    profile = settings(home)
    _check_python_location(home, profile["python"])
    toolchain = binding(home)
    if profile.get("extension_recipes", True):
        ensure(home, op)
    directory = op / "candidate"
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "spack": {
            "specs": roots,
            "view": False,
            "concretizer": {"unify": True, "reuse": _python_reuse(buildcache=reuse_buildcache and not toolchain)},
        }
    }
    target = target or (toolchain and toolchain.get("target"))
    runtime = target or _gcc_runtime_target(home)
    if runtime:
        document["spack"]["packages"] = {"gcc-runtime": {"require": [f"target={runtime}"]}}
    (directory / "spack.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
    shutil.copytree(root(home) / "config", directory / ".jforge-config")
    package_config = directory / ".jforge-config/packages.yaml"
    config = yaml.safe_load(package_config.read_text()) if package_config.is_file() else {}
    packages = config.setdefault("packages", {})
    packages.update(_python_externals(profile["python"]))
    if toolchain:
        spack_requirements(toolchain, packages)
    if expected_packages is not None:
        from .profile_exports import constraints
        for name,requirement in constraints(expected_packages).items():
            rules=packages.setdefault(name,{}).setdefault('require',[])
            if isinstance(rules,str):rules=[rules];packages[name]['require']=rules
            rules.append(requirement)
    if target:
        _require_target(packages, target)
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
    if not isinstance(layout, dict) or layout.get("projections", {}).get("all") != INSTALL_PROJECTION:
        return None
    return layout


def _pin_installed_gcc_runtime(plan, envdir, *, expected_target=None):
    layout = _name_only_layout(envdir)
    if layout is None:
        return None
    runtime_hashes = {
        package["hash"] for package in plan["packages"]
        if package["name"] == "gcc-runtime" and not package["external"]
    }
    matches=list(Path(layout["root"]).glob("gcc-runtime-*/.spack/spec.json"))
    state=Path(layout['root']).parent/'packages.json'
    active=next((row for row in read_json(state).get('spack',{}).get('packages',[]) if row['name']=='gcc-runtime' and not row['external']),None) if state.is_file() else None
    spec_file=Path(active['prefix'])/'.spack/spec.json' if active else matches[0] if len(matches)==1 else Path(layout['root'])/'missing'
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
    if expected_target and target != expected_target:
        raise ForgeError(f"Installed gcc-runtime targets {target}, but this profile requires {expected_target}.",
                         hint="Build and select a fresh profile before installing more packages.")
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


def _completed_install_failure(op, label):
    record = op / f"command-{label}.json"
    failure = read_json(record) if record.is_file() else {}
    exit_code = failure.get("exit_code")
    return failure.get("status") == "failed" and isinstance(exit_code, int) and exit_code > 0


def _restore_candidate_lock(envdir, previous, archive):
    lock = envdir / "spack.lock"
    if lock.is_file():
        lock.rename(archive)
    if previous and (Path(previous) / "spack.lock").is_file():
        shutil.copy2(Path(previous) / "spack.lock", lock)


def _install_args(jobs, policy, hashes=()):
    return [
        "install",
        "--only-concrete",
        "--verbose",
        "--show-log-on-error",
        "-j",
        str(jobs),
        "--concurrent-packages",
        "16",
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
        if prefix != store / semantic_name(package["name"],package["version"]):
            raise ForgeError(f"Spack returned an unexpected name-only prefix: {prefix}", code=1)


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


def _deprecated_permissions(value):
    """Validate exact package/version permissions supplied by a fixed tool suite."""
    if not isinstance(value, dict) or any(
        not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.+-]*", name)
        or not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", version)
        for name, version in value.items()
    ):
        raise ForgeError("Deprecated-version permissions must name exact package versions.")
    return dict(value)


def _validate_deprecated_versions(packages, allowed):
    unexpected = sorted({f"{row['name']}@{row['version']}" for row in packages
                         if row.get("deprecated") and not row.get("external")
                         and allowed.get(row["name"]) != row["version"]})
    if unexpected:
        raise ForgeError("Dependency plan includes unapproved deprecated versions: " + ", ".join(unexpected),
                         code=1, hint="Only exact versions declared by this profile's tool suite are permitted.")


def validate_project_python(home, packages, roots, executable):
    """Check the runtime closure of this build's requested package roots."""
    if executable is None:
        return
    names = {name_of(spec) for spec in roots}
    by_hash = {package["hash"]: package for package in packages}
    pending = [package["hash"] for package in packages if package.get("root") and package["name"] in names]
    seen = set()
    while pending:
        digest = pending.pop()
        if digest in seen or digest not in by_hash:
            continue
        seen.add(digest)
        package = by_hash[digest]
        if package["name"] in PYTHON_PACKAGES:
            project = _probe_external_python(executable)
            selected = settings(home)["python"]
            if (project["version"] != selected["version"] or any(
                    Path(project[key]).resolve() != Path(selected.get(key, selected["prefix"])).resolve()
                    for key in ("prefix", "base_prefix"))):
                raise ForgeError(
                    f"Project Python {project['executable']} ({project['version']}, {project['prefix']}) "
                    f"does not match apt Python {selected['executable']} ({selected['version']}, {selected['prefix']}).",
                    code=1, hint="Select an apt profile created with the project Python before running this build.",
                )
            return
        pending.extend(dep["hash"] for dep in package.get("node", {}).get("dependencies", [])
                       if {"link", "run"}.intersection(dep["parameters"]["deptypes"]))


def _adapter_script(home, envdir, revision, *, modifications=None):
    bridge = revision / "spack_bridge.py"
    mods = modifications or revision / "modifications.json"
    argv = command(home, ["python", str(bridge), "render", str(mods)], envdir)
    launcher = os.environ.get("JFORGE_CONTROLLER_LAUNCHER")
    if launcher and Path(launcher).is_absolute():
        argv = [launcher, "__jforge_controller_python", *argv[3:]]
    values = _process_environment(home)
    exports = ["SPACK_USER_CACHE_PATH", "SPACK_USER_CONFIG_PATH", "SPACK_SYSTEM_CONFIG_PATH", "SPACK_GNUPGHOME"]
    # Render against the invoking shell's environment at source time.
    invoke = ["env", "-u", "SPACK_ENV", "-u", "SPACK_ENV_VIEW", "-u", "SPACK_ROOT",
              "-u", "SPACK_DISABLE_LOCAL_CONFIG", *[f"{key}={values[key]}" for key in exports], *argv]
    return ("_JFORGE_SPACK_CODE=$(" + shlex.join(invoke)
            + ') || return $?\neval "$_JFORGE_SPACK_CODE"\nunset _JFORGE_SPACK_CODE\n')


def refresh_runtime_adapter(path):
    """Regenerate a legacy adapter from its frozen inputs for a stable launcher."""
    launcher = os.environ.get("JFORGE_CONTROLLER_LAUNCHER")
    if not launcher or not Path(launcher).is_absolute():
        return path
    revision = path.parent
    revisions = revision.parent
    if tuple(item.name for item in (revisions, revisions.parent, revisions.parent.parent)) != (
            "revisions", "apt", "environments"):
        return path
    home = ForgeHome(revisions.parent.parent.parent)
    envdir = root(home) / "environments" / revision.name
    if not all(file.is_file() for file in (envdir / "spack.lock", revision / "spack_bridge.py",
                                            revision / "modifications.json")):
        return path
    content = _adapter_script(home, envdir, revision)
    if path.read_text() == content:
        return path
    from .package_operations import _inside
    refreshed = _inside(home, revision / "controller" / path.name)
    if not refreshed.is_file() or refreshed.read_text() != content:
        write_text(refreshed, content)
    return refreshed


def refresh_runtime_environment(path):
    adapter = path.with_name("spack-environment.sh")
    if not adapter.is_file():
        return path
    refreshed = refresh_runtime_adapter(adapter)
    if refreshed == adapter:
        return path
    original = "source " + shlex.quote(str(adapter)) + " || return $?"
    text = path.read_text()
    if original not in text:
        return path
    content = text.replace(original, "source " + shlex.quote(str(refreshed)) + " || return $?", 1)
    activation = refreshed.with_name(path.name)
    if not activation.is_file() or activation.read_text() != content:
        write_text(activation, content)
    return activation


def repaired_runtime_environment(home, state, *, materialize=True):
    """Resolve an apt repair while retaining the operation's frozen inputs."""
    from .package_operations import _inside

    repair = state["activation_repair"]
    if (not isinstance(repair, dict) or not isinstance(repair.get("id"), str)
            or not isinstance(repair.get("sha256"), str)):
        raise ForgeError("Invalid apt activation repair record.")
    revision = _inside(home, Path(state["activation"])).parent
    directory = _inside(home, revision / "repairs" / identifier(repair["id"]))
    modifications = _inside(home, directory / "modifications.json")
    activation = _inside(home, directory / "activate.sh")
    if not modifications.is_file() or digest(modifications) != repair["sha256"]:
        raise ForgeError(f"Repaired apt settings are missing or modified: {modifications}")
    if materialize:
        original = _inside(home, Path(state["activation"]))
        old_adapter = _inside(home, revision / "spack-environment.sh")
        envdir = _inside(home, Path(state["spack"]["environment"]))
        bridge = _inside(home, revision / "spack_bridge.py")
        if not bridge.is_file() or not envdir.is_dir():
            raise ForgeError(f"Spack activation inputs are missing for {original}.",
                             hint="Run jforge apt check to inspect the package environment.")
        adapter = _inside(home, directory / old_adapter.name)
        before = "source " + shlex.quote(str(old_adapter)) + " || return $?"
        text = original.read_text(encoding="utf-8")
        if before not in text.splitlines():
            raise ForgeError(f"Unrecognized Spack activation script: {original}")
        after = "source " + shlex.quote(str(adapter)) + " || return $?"
        content = _adapter_script(home, envdir, revision, modifications=modifications)
        if not adapter.is_file() or adapter.read_text(encoding="utf-8") != content:
            write_text(adapter, content)
        content = text.replace(before + "\n", after + "\n", 1)
        if not activation.is_file() or activation.read_text(encoding="utf-8") != content:
            write_text(activation, content)
    return activation, modifications


def _publish(home, op, envdir, roots, snapshot):
    from . import profile_fonts
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

    revision = root(home) / "runtime"
    revision.mkdir(parents=True, exist_ok=True)
    if envdir != root(home):
        for name in ('spack.yaml','spack.lock'):
            if (envdir/name).is_file():shutil.copy2(envdir/name, root(home)/name)
            elif (root(home)/name).exists():(root(home)/name).unlink()
        shutil.copytree(envdir/'.jforge-config', root(home)/'.jforge-config', dirs_exist_ok=True)
    envdir=root(home)
    config = profile_fonts.write_config(home, store_root(home), revision, snapshot["packages"])
    if config:
        modifications.append({"type": "SetEnv", "name": "FONTCONFIG_FILE", "value": str(config)})
    bridge = revision / "spack_bridge.py"
    shutil.copy2(Path(__file__).with_name("spack_bridge.py"), bridge)
    mods = revision / "modifications.json"
    write_json(revision / "snapshot.json", snapshot)
    mods.write_text(json.dumps(snapshot["modifications"], indent=2) + "\n")
    adapter = revision / "spack-environment.sh"
    adapter.write_text(_adapter_script(home, envdir, revision))
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

        record['manifest_key']=semantic_name(row['name'],row['version'])
        if not row['external']:
            metadata(Path(row['prefix']),'package',row['name'],version=row['version'],identity={'dag_hash':row['hash']},**{key:value for key,value in record.items() if key not in {'name','version','schema_version'}})
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
    for row in snapshot.get('inventory',[]):
        if row['external'] or any(record['hash']==row['hash'] for record in records):continue
        metadata(Path(row['prefix']),'package',row['name'],version=row['version'],identity={'dag_hash':row['hash']},
                 **{key:value for key,value in row.items() if key not in {'name','version','schema_version'}},automatic=True,active=False)
    write_json(home.root / "packages.json", state)
    return records


def _requested_roots(before, action, translated):
    if action == "remove":
        names = {name_of(s) for s in translated}
        missing = names - {row['name'] for row in before['packages']}
        if missing:raise ForgeError('Packages are not installed: '+', '.join(sorted(missing)))
        from .package_lifecycle import reverse_dependents
        names=reverse_dependents(before['packages'],names)
        roots=[spec for spec in before['roots'] if name_of(spec) not in names]
    elif action in {"upgrade", "full-upgrade"}:
        names={name_of(spec) for spec in translated}
        if any(spec!=name_of(spec) for spec in translated):raise ForgeError('Use native package names with upgrade; use install SPEC to change constraints.')
        if missing:=names-{row['name'] for row in before['packages']}:raise ForgeError('Packages are not installed: '+', '.join(sorted(missing)))
        if names:
            from .profile_exports import constraints,package_identity
            pinned=constraints([package_identity(row) for row in before['packages']])
            roots=[spec if name_of(spec) in names else name_of(spec)+pinned.get(name_of(spec),'') for spec in before['roots']]
        else:roots = before["roots"]
    else:
        names = {name_of(s) for s in translated}
        roots = [s for s in before["roots"] if name_of(s) not in names] + translated
    if action == "check" and not translated:
        roots = before["roots"]
    return roots


from .package_operations import profile_write_lock

@profile_write_lock
def transact(
    home,
    action,
    specs=(),
    *,
    jobs=None,
    timeout=3600,
    cache="auto",
    dry_run=False,
    progress=None,
    project_python=None,
    build_stage=None,
    target=None,
    deprecated_versions=None,
    confirm=None,
    from_history=None,
    expected_packages=None,
):
    from .package_operations import locked, operation
    from .toolchains import binding, validate_plan as validate_toolchain_plan
    from .unified_profiles import record as profile_record
    from .apt_profiles import validate_active
    from .parallelism import policy

    parallel = policy()
    jobs = parallel["build_jobs"] if jobs is None else min(jobs, parallel["build_jobs"])
    parallel.update(build_jobs=jobs, cmake_jobs=min(jobs, 16), homebrew_make_jobs=min(jobs, 16))

    # A binding carries only the exact permissions of its native tool suite.
    # This never changes the profile's persistent Spack configuration.
    requested_permissions = _deprecated_permissions(deprecated_versions) if deprecated_versions is not None else None

    if profile_record(home):
        validate_active(home)

    if cache not in {"auto", "only", "never"}:
        raise ForgeError("Build cache mode must be auto, only or never.")
    if jobs < 1 or not math.isfinite(timeout) or timeout <= 0:
        raise ForgeError("Jobs and timeout must be positive.")
    translated = list(dict.fromkeys(normalize(s) for s in specs))
    if action != "remove" and any(name_of(s) in PYTHON_PACKAGES for s in translated):
        raise ForgeError("Select Python and virtual environments with pyenv before running Forge.")
    before = current(home)
    roots = _requested_roots(before, action, translated)
    if not configured(home):
        bootstrap(home, progress=progress)
    with (
        locked(home),
        operation(
            home,
            "spack-" + action,
            {"specs": list(specs), "roots": roots, "cache": cache, "jobs": jobs,
             "deprecated_versions": requested_permissions},
        ) as (op, result),
    ):
        from .unified_profiles import host
        result["parallelism"] = parallel
        result["host"] = host()
        result["python"] = settings(home)["python"]
        from . import package_lifecycle as lifecycle
        selected_history=None
        if from_history is not None:
            selected_history,saved_history=lifecycle.historical(home,from_history)
            if saved_history.get('host'):
                from .unified_profiles import compatible
                compatible({'id':home.root.name,'preset':'ubuntu24','host':saved_history['host'],'target':saved_history.get('target') or target})
            if saved_history.get('python') and saved_history['python']!=settings(home)['python']:
                raise ForgeError('History uses a different external Python; select its original profile.')
        transaction=None
        history_dir=None
        removed=[]
        before = current(home)
        roots = _requested_roots(before, action, translated)
        toolchain = binding(home)
        allowed = (requested_permissions if requested_permissions is not None
                   else _deprecated_permissions((toolchain or {}).get("deprecated_versions", {})))
        result["deprecated_versions"] = allowed
        bound_target = toolchain.get("target") if toolchain else None
        if target and bound_target and target != bound_target:
            raise ForgeError(f"Requested CPU target {target} conflicts with profile target {bound_target}.")
        target = target or bound_target
        result["target"]=target
        roots = _requested_roots(before, action, translated)
        if selected_history:
            roots=yaml.safe_load((selected_history/'spack.yaml').read_text())['spack']['specs']
        if action=='remove':
            names=lifecycle.reverse_dependents(before['packages'],{name_of(spec) for spec in translated})
            roots=[spec for spec in before['roots'] if name_of(spec) not in names]
        result["roots"] = roots
        previous = before.get("environment") if action not in {"upgrade", "full-upgrade"} else None
        envdir = _candidate(home, op, roots, previous, reuse_buildcache=cache != "never", target=target,expected_packages=expected_packages)
        if selected_history:
            shutil.copy2(selected_history/'spack.lock',envdir/'spack.lock')
            document=yaml.safe_load((envdir/'spack.yaml').read_text())
            document['spack']['specs']=roots
            (envdir/'spack.yaml').write_text(yaml.safe_dump(document,sort_keys=False))
        config_file = envdir / ".jforge-config/config.yaml"
        config = yaml.safe_load(config_file.read_text()) if config_file.is_file() else {}
        config.setdefault("config", {})["deprecated"] = bool(allowed)
        config["config"].update(build_jobs=jobs, concurrent_packages=16, installer="new")
        if build_stage is None and (scope_data:=profile_record(home)):
            if scope_data.get('stage_mode')=='node':
                from .unified_profiles import default_stage
                build_stage=default_stage(home)
            else:build_stage=Path(scope_data['stage'])
        if build_stage is not None:
            config["config"]["build_stage"] = [str(build_stage)]
            result["stage"]=str(build_stage)
        config_file.write_text(yaml.safe_dump(config, sort_keys=False))
        write_json(op / "deprecation-policy.json", {"deprecated_versions": allowed})
        if allowed and progress:
            progress("Use pinned tool-suite versions: " + ", ".join(f"{name}@{version}" for name, version in allowed.items()))
        # In a flat store, validate binary and source work separately. Historical
        # build dependencies of installed/binary packages need no local prefix.
        cache_first = (bool(roots) and action != "remove" and cache == "auto"
                       and _name_only_layout(envdir) is not None)
        plan_cache = "only" if cache_first or action == "remove" else cache
        overwrite = [name_of(s) for s in translated] if action == "reinstall" else []
        committed = False
        runtime_retried = False

        # A fresh solver may itself have to build clingo-bootstrap from source.
        patch_jobs = jobs if cache != "only" and action not in {"check", "remove"} else None

        def resolve_plan(policy, flags, *, suffix="", omit_cached_build_deps=False):
            nonlocal cache_first, plan_cache
            run(home, flags, op, "concretize" + suffix, envdir=envdir,
                timeout=timeout, progress=progress, patch_jobs=patch_jobs,
                planning={"policy": policy, "overwrite": overwrite,
                          "omit_cached_build_deps": omit_cached_build_deps,
                          "source_if_uncached": cache_first and policy == "only",
                          "from_lock":bool(selected_history),
                          "database_checkpoint":str(op/'database-before.json')})
            from .spack_planning import current as combined_plan
            combined = combined_plan(op, envdir)
            if combined and combined["result"]["policy"] != policy:
                policy = combined["result"]["policy"]
                cache_first = False
                plan_cache = policy
            dag = _snapshot(home, envdir, op, "dag" + suffix)
            write_json(op / "dag.json", dag)
            _validate_deprecated_versions(dag["packages"], allowed)
            _validate_target(dag["packages"], target)
            run(home, ["python", str(Path(__file__).with_name("spack_bridge.py")), "tree"],
                op, "dependency-tree" + suffix, envdir=envdir, timeout=timeout,
                progress=progress if getattr(progress, "verbose", False) else None)
            return _snapshot(home, envdir, op, "plan" + suffix, cache=policy,
                             overwrite=overwrite, omit_cached_build_deps=omit_cached_build_deps)

        def validate_plan(plan, label, policy, flags, *, omit_cached_build_deps=False):
            nonlocal runtime_retried
            write_json(op / f"{label}.json", {**plan, "cache_policy": policy})
            _validate_deprecated_versions(plan["packages"], allowed)
            _validate_external_python(home, plan["packages"])
            if expected_packages is not None:
                from .profile_exports import compare
                compare(expected_packages,read_json(op/'dag.json')['packages'])
            if toolchain and roots:
                validate_toolchain_plan(toolchain, read_json(op / "dag.json")["packages"])
            try:
                _validate_name_only_plan(plan, envdir)
            except ForgeError:
                if selected_history or runtime_retried or action not in {"install", "check", "reinstall"}:
                    raise
                installed = _pin_installed_gcc_runtime(plan, envdir, expected_target=target)
                if installed is None:
                    raise
                runtime_retried = True
                if progress:
                    progress("Retry dependency resolution with the installed gcc-runtime")
                (op / f"{label}.json").rename(op / f"{label}-before-runtime-retry.json")
                _restore_candidate_lock(envdir, previous, op / "spack-before-runtime-retry.lock")
                plan = resolve_plan(policy, flags, suffix="-runtime-retry",
                                    omit_cached_build_deps=omit_cached_build_deps)
                write_json(op / f"{label}.json", {**plan, "cache_policy": policy})
                _validate_deprecated_versions(plan["packages"], allowed)
                _validate_external_python(home, plan["packages"])
                if toolchain:
                    validate_toolchain_plan(toolchain, read_json(op / "dag.json")["packages"])
                _validate_name_only_plan(plan, envdir)
                if any(p["name"] == "gcc-runtime" and not p["external"] and p["hash"] != installed
                       for p in plan["packages"]):
                    raise ForgeError("Spack could not reuse the installed gcc-runtime.", code=1)
            if project_python is not None and translated:
                validate_project_python(home, read_json(op / "dag.json")["packages"], translated, project_python)
            return plan

        flags = ["concretize"]
        try:
            if progress:
                progress("[1/4] Resolve dependencies")
            if roots:
                if action in {"upgrade", "full-upgrade"}:
                    flags += ["--fresh", "--force"]
                previous_roots = {name_of(r): r for r in before["roots"]}
                changed = any(
                    name_of(r) in previous_roots and previous_roots[name_of(r)] != r
                    for r in translated
                )
                if changed and "--force" not in flags:
                    flags.append("--force")
                plan = resolve_plan(plan_cache, flags)
                if progress:
                    progress(f"[2/4] Check installation plan: {len(plan['packages'])} packages")
            else:
                # Native concretization writes a valid empty lock, not the previous DAG.
                plan = resolve_plan(plan_cache, flags)
            plan = validate_plan(plan, "plan", plan_cache, flags)
            if action == "check" or dry_run:
                from .spack_planning import current as combined_plan
                combined=combined_plan(op,envdir)
                inventory=combined['result'].get('inventory',[]) if combined else []
                preview_removed=[row for row in inventory if action=='remove' and row['name'] in names and not row['external']]
                lifecycle.protect_apps(home,preview_removed)
                result['changes']={'install':[row for row in plan['packages'] if not row['installed'] and not row['external']],
                                   'rebuild':[old for old in inventory for row in plan['packages'] if not row['external'] and old['prefix']==row['prefix'] and (old['hash']!=row['hash'] or row['name'] in overwrite)],
                                   'remove':preview_removed}
                result.update(
                    backend="spack",
                    dry_run=dry_run,
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
                history_dir=lifecycle.history(home,action,envdir,{**result,'status':'planned'})
                result.update(history=history_dir.name.split('-')[0],history_path=str(history_dir))
                if cache_first:
                    result["note"] = (
                        "Binary-only installation plan; if a cache install fails, "
                        "remaining source dependencies are validated before building."
                    )
                return result
            from .spack_planning import current as combined_plan
            combined=combined_plan(op,envdir)
            inventory=combined['result'].get('inventory',[]) if combined else []
            if not (op/'database-before.json').is_file():
                raw=run(home,['python',str(Path(__file__).with_name('spack_bridge.py')),'inventory',str(op/'database-before.json')],op,'inventory',envdir=envdir)
                inventory=_json(raw,'native inventory')['packages']
            def database(options):
                return run(home,['python',str(Path(__file__).with_name('spack_bridge.py')),'database',json.dumps(options)],op,
                           'database-'+options['action'],envdir=envdir,timeout=timeout)
            transaction=lifecycle.Transaction(home,op,inventory,database,op/'database-before.json')
            replacements=transaction.changes(plan,overwrite)
            if action=='remove':
                removed=[row for row in inventory if row['name'] in names and not row['external']]
                lifecycle.protect_apps(home,removed)
            change_plan={'install':[p for p in plan['packages'] if not p['installed'] and not p['external']],
                         'rebuild':replacements,'remove':removed}
            result['changes']=change_plan
            if replacements or removed:
                if confirm is None:raise ForgeError('Package removal or in-place rebuilding requires confirmation.',hint='Repeat with --yes after reviewing the package plan.')
                confirm(change_plan)
            elif confirm and (any(not row.get('root') for row in change_plan['install']) or action=='upgrade' and change_plan['install']):
                confirm(change_plan)
            history_dir=lifecycle.history(home,action,envdir,{**result,'status':'installing'})
            result.update(history=history_dir.name.split('-')[0],history_path=str(history_dir))
            transaction.prepare(plan,overwrite)
            transaction.move(removed)
            if progress:
                progress(
                    "[3/4] Install packages"
                    if action != "remove"
                    else "[3/4] Remove packages and dependents"
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
                except ForgeError as exc:
                    # Timeouts, interruptions and launch failures must propagate.
                    # Only a completed failed Spack cache attempt can fall back.
                    _reject_cache_security_failure(op, label, exc)
                    if not cache_first or not _completed_install_failure(op, label):
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
                        source_plan, "source-plan", "auto", flags, omit_cached_build_deps=not overwrite,
                    )
                    extra=transaction.changes(source_plan,overwrite)
                    extra=[row for row in extra if row['prefix'] in transaction.before_prefixes and row['prefix'] not in transaction.moved]
                    if extra:
                        if confirm is None:raise ForgeError('Source fallback requires in-place rebuilding; confirm the new plan.')
                        confirm({'install':[],'rebuild':extra,'remove':[]})
                    transaction.prepare(source_plan,overwrite)
                    if show_plan is not None:
                        show_plan(source_plan["packages"], overwrite)
                    if overwrite:
                        source_args = list(args)
                        source_args[source_args.index("--use-buildcache") + 1] = "auto"
                        run(home, source_args, op, "install", envdir=envdir,
                            timeout=timeout, progress=progress)
                    else:
                        cached_hashes, source_hashes = _pending_install_hashes(source_plan)
                        try:
                            if cached_hashes:
                                if progress:
                                    progress(
                                        f"Install {len(cached_hashes)} remaining "
                                        "packages from the binary cache"
                                    )
                                run(home, _install_args(jobs, "only", cached_hashes), op,
                                    "install-binaries", envdir=envdir, timeout=timeout, progress=progress)
                        except ForgeError as exc:
                            _reject_cache_security_failure(op, "install-binaries", exc)
                            if not _completed_install_failure(op, "install-binaries"):
                                raise
                            if progress:
                                progress("Binary cache install incomplete; resolve again for source builds")
                            # The index can outlive an archive. Its frozen build DAG
                            # can also require tools incompatible with the local store.
                            manifest = envdir / "spack.yaml"
                            document = yaml.safe_load(manifest.read_text())
                            document["spack"]["concretizer"]["reuse"] = _python_reuse(buildcache=False)
                            manifest.write_text(yaml.safe_dump(document, sort_keys=False))
                            if not selected_history:
                                _restore_candidate_lock(envdir, previous, op / "spack-before-cache-retry.lock")
                            # Keep completed binaries eligible for local reuse, even on upgrade.
                            retry_flags = [flag for flag in flags if flag != "--fresh"]
                            source_plan = resolve_plan("never", retry_flags, suffix="-cache-retry")
                            source_plan = validate_plan(source_plan, "source-retry-plan", "never", retry_flags)
                            extra=[row for row in transaction.changes(source_plan,overwrite) if row['prefix'] in transaction.before_prefixes and row['prefix'] not in transaction.moved]
                            if extra:
                                if confirm is None:raise ForgeError('Source retry requires confirmed in-place rebuilding.')
                                confirm({'install':[],'rebuild':extra,'remove':[]})
                            transaction.prepare(source_plan,overwrite)
                            if show_plan is not None:
                                show_plan(source_plan["packages"], overwrite)
                            run(home, _install_args(jobs, "never"), op, "install-source-retry",
                                envdir=envdir, timeout=timeout, progress=progress)
                        else:
                            if source_hashes:
                                if progress:
                                    progress(f"Build {len(source_hashes)} packages from source")
                                run(home, _install_args(jobs, "auto", source_hashes), op,
                                    "install", envdir=envdir, timeout=timeout, progress=progress)
            final = _snapshot(home, envdir, op, "installed")
            _validate_deprecated_versions(final["packages"], allowed)
            _validate_target(final["packages"], target)
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
            from .unified_profiles import refresh_activation,sync_definition
            refresh_activation(home)
            sync_definition(home)
            committed = True
            transaction.finish()
            result.update(
                backend="spack",
                packages=records,
                activation=current(home)["activation"],
                environment=str(root(home)),
                lockfile=str(root(home) / "spack.lock")
                if (envdir / "spack.lock").is_file()
                else None,
                state_committed=True,
                store_retained=False,
                removed=removed,
            )
            return result
        finally:
            if not committed and transaction is not None:
                transaction.rollback()
            if history_dir is not None:
                for name in ('spack.yaml','spack.lock'):
                    if (envdir/name).is_file():shutil.copy2(envdir/name,history_dir/name)
                saved=read_json(history_dir/'result.json')
                saved.update(manifest_sha256=digest(history_dir/'spack.yaml'),
                             lock_sha256=digest(history_dir/'spack.lock') if (history_dir/'spack.lock').is_file() else None)
                write_json(history_dir/'result.json',{**saved,**result,'status':'passed' if committed else 'planned' if dry_run or action=='check' else 'failed'})
            calls = op / "spack-calls.jsonl"
            result["spack_invocations"] = len(calls.read_text().splitlines()) if calls.is_file() else 0
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
            raise ForgeError(f"No Spack package matches {name}.", hint=f"jforge apt search {shlex.quote(name)}")
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
        output = run(
            home,
            [
                "python", str(Path(__file__).with_name("spack_bridge.py")),
                "trust-cache-keys",
                "spack-public-binaries",
            ],
            op,
            "official-cache-keys",
            envdir=envdir,
            progress=progress,
        )
        imported = _json(output, "binary-cache key import").get("fingerprints")
        if not isinstance(imported, list) or not imported or any(not isinstance(key, str) or not key for key in imported):
            raise ForgeError("Binary-cache key import returned no signing keys.", code=1)
    except ForgeError as exc:
        raise ForgeError(str(exc), code=exc.code,
                         hint="Could not prepare trusted binary-cache keys. Check jforge apt sources and the key-import logs."
                         + ("\n" + exc.hint if exc.hint else "")) from exc
    else:
        value = settings(home)
        value["official_cache_keys_ready"] = True
        write_json(home.root / "spack/backend.json", value)


def _prepare_backend(home, op, progress):
    """Pay solver and signature-tool bootstrap costs before publishing a profile."""
    if settings(home).get("backend_prepared"):
        return
    from .parallelism import policy

    if progress:
        progress("Prepare dependency solver and binary-cache verification")
    run(home, ["python", str(Path(__file__).with_name("spack_bridge.py")), "prepare-backend"],
        op, "prepare-backend", progress=progress, patch_jobs=policy()["build_jobs"])
    _cache_keys(home, op, None, progress)
    value = settings(home)
    value["backend_prepared"] = True
    write_json(home.root / "spack/backend.json", value)


_CACHE_TRUST_ERROR = re.compile(
    r"\b(?:SpackGPGError|NoGpgException|NoKeyException|NoVerifyException|SignatureVerificationError|SignatureError|UnsignedPackageException)\b"
    r"|\[GNUPG:\]\s*(?:BADSIG|ERRSIG|NO_PUBKEY|KEYEXPIRED|SIGEXPIRED|REVKEYSIG)\b"
    r"|\b(?:bad|invalid|untrusted|expired)\s+(?:[a-z]+\s+)?signature\b"
    r"|\bsignature\s+(?:verification|check)\s+(?:failed|failure)\b"
    r"|\b(?:cannot|could not|unable to|failed to)\s+verify\s+(?:the\s+)?signature\b"
    r"|\bno public key\b|\bgpg(?:2)?:\s*(?:fatal|error)\b",
    re.IGNORECASE,
)


def _reject_cache_security_failure(op, label, error):
    # Search the entire separate logs, not just the error tail: the installer
    # can wrap a verification failure in a generic package-install exception.
    failed = bool(_CACHE_TRUST_ERROR.search(str(error)))
    failure_record = op / "cache-trust-failures.jsonl"
    if failure_record.is_file():
        failed = True
    for stream in ("stdout", "stderr"):
        path = op / f"command-{label}.{stream}.log"
        if path.is_file():
            with path.open(encoding="utf-8", errors="replace") as lines:
                failed = failed or any(_CACHE_TRUST_ERROR.search(line) for line in lines)
    if failed:
        hint = "Binary-cache trust verification failed. Check jforge apt sources and its trusted keys."
        if error.hint and hint in error.hint:
            raise error
        raise ForgeError(str(error), code=error.code,
                         hint=hint + ("\n" + error.hint if error.hint else "")) from error


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
        from .package_operations import archive_path
        cached = archive_path(home, pin, create=True)
        cached.parent.mkdir(parents=True, exist_ok=True)
        if not cached.exists():
            shutil.copy2(archive, cached)
        destination = object_path(record_path(root(home)/"recipes/history", "update-builtin", pin, create=True), "builtin", pin, create=True, version="2026.06")
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
        write_json(home.root / "spack/recipes.json", extension)
        value.update(
            repository=str(repo), repository_commit=head, repository_archive=pin
        )
        write_json(home.root / "spack/backend.json", value)
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
    from .toolchains import binding

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
        if toolchain := binding(home):
            result["toolchain"] = {"id": toolchain["id"], "prefix": toolchain["prefix"]}
        return result


@profile_write_lock
def autoremove(home, *, dry_run=False, progress=None, confirm=None):
    from . import package_lifecycle as lifecycle
    from .package_operations import locked,operation
    def inspect():
        state=current(home)
        keep={row['hash'] for row in state['packages']}
        inventory=[read_json(file) for file in (home.root/'pkg').glob('*/metadata.json') if read_json(file).get('kind')=='package']
        unused=[row for row in inventory if row['hash'] not in keep and not row.get('external')]
        lifecycle.protect_apps(home,unused)
        return state,inventory,unused,{'install':[],'rebuild':[],'remove':unused}
    if dry_run:
        state,inventory,unused,changes=inspect()
        return {'backend':'spack','dry_run':True,'changes':changes,'unused':unused}
    with locked(home),operation(home,'autoremove',{}) as (op,result):
        state,inventory,unused,changes=inspect()
        if unused:
            if confirm is None:raise ForgeError('Autoremove requires confirmation.',hint='jforge apt autoremove --yes')
            confirm(changes)
        def database(options):
            return run(home,['python',str(Path(__file__).with_name('spack_bridge.py')),'database',json.dumps(options)],op,'database-'+options['action'],envdir=root(home))
        run(home,['python',str(Path(__file__).with_name('spack_bridge.py')),'inventory',str(op/'database-before.json')],op,'inventory',envdir=root(home))
        tx=lifecycle.Transaction(home,op,inventory,database,op/'database-before.json')
        committed=False
        try:
            tx.move(unused)
            result.update(backend='spack',state_committed=True,removed=unused,changes=changes,activation=state.get('activation'))
            folder=lifecycle.history(home,'autoremove',root(home),{**result,'status':'passed'})
            result.update(history=folder.name.split('-')[0],history_path=str(folder))
            committed=True
            tx.finish()
            return result
        finally:
            if not committed:tx.rollback()



def locked_packages(home):
    """Read the complete concrete DAG without starting or changing Spack."""
    file=root(home)/'spack.lock'
    if not file.is_file():return []
    value=read_json(file)
    if 'packages' in value:return value['packages']
    rows=[]
    for hash,node in value.get('concrete_specs',{}).items():
        rows.append({'name':node['name'],'version':node['version'],'hash':hash,'external':bool(node.get('external')),
                     'node':node})
    return rows


def _read_native(home,args):
    """Native metadata queries do not initialize an environment or operation log."""
    try:
        result=subprocess.run(command(home,args,None),env=_process_environment(home),cwd=home.root,
                              stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=120,start_new_session=True)
    except (OSError,subprocess.SubprocessError) as exc:raise ForgeError(f'Cannot query the managed Spack engine: {exc}',code=1) from exc
    if result.returncode:raise ForgeError((result.stderr or result.stdout).strip() or 'Spack metadata query failed.',code=1)
    return result.stdout


def search(home,term):
    if not configured(home):return catalog(home,query=term)
    if not term or term.startswith('-') or any(char in term for char in ('\n','\x00')):raise ForgeError('Supply a package search term.')
    rows=_json(_read_native(home,['list','--search-description','--format','version_json',term]),'package search')
    if not isinstance(rows,list) or any(not isinstance(row,dict) or not isinstance(row.get('name'),str) for row in rows):raise ForgeError('Spack returned invalid package search records.',code=1)
    return [{**row,'backend':'spack','status':'available','version':row.get('latest_version','')} for row in rows]


def read_details(home,spec):
    name=name_of(spec)
    if not configured(home):return {'name':name,'status':'profile-not-initialized','hint':'jforge profile create ubuntu24'}
    rows=_json(_read_native(home,['list','--format','version_json',name]),'package details')
    result=next((row for row in rows if row['name']==name),None)
    if result is None:raise ForgeError(f'No native Spack package matches {name}.',hint=f'jforge apt search {name}')
    return result
