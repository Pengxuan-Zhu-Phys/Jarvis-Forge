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


def _config(home, repo, python):
    directory = root(home) / "config"
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "config": {
            "install_tree": {
                "root": str(root(home) / "store"),
                "padded_length": max(128, len(str(root(home) / "store")) + 32),
            },
            "build_stage": [str(root(home) / "stage")],
            "source_cache": str(root(home) / "sources"),
            "environments_root": str(root(home) / "environments"),
        },
        "repos": {"builtin": str(repo)},
        "bootstrap": {"root": str(root(home) / "bootstrap")},
        "packages": {
            "python": {
                "externals": [
                    {"spec": "python@" + python["version"], "prefix": python["prefix"]}
                ],
                "buildable": False,
            }
        },
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


def bootstrap(home, *, python=None, progress=None):
    from .package_operations import locked, operation

    with (
        locked(home),
        operation(home, "spack-bootstrap", {"python": python}) as (op, result),
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
        if configured(home) and python is None and current(home).get("environment"):
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
        executable = python or sys.executable
        try:
            probe = subprocess.run(
                [
                    executable,
                    "-c",
                    'import json,sys;print(json.dumps({"executable":sys.executable,"version":sys.version.split()[0],"prefix":sys.prefix}))',
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
        base = root(home)
        base.mkdir(parents=True, exist_ok=True)
        engine = base / "engine" / ENGINE_VERSION
        repo = base / "repositories" / REPO_COMMIT
        _unpack(home, ENGINE, engine, op, "engine-download")
        _unpack(home, REPOSITORY, repo, op, "repository-download")
        previous = settings(home) if configured(home) else None
        if previous and python is None:
            selected = previous["python"]
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
        _config(home, repo_path, selected)
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


def _candidate(home, op, roots, previous):
    directory = root(home) / "environments" / op.name
    directory.mkdir(parents=True)
    (directory / "spack.yaml").write_text(
        yaml.safe_dump(
            {
                "spack": {
                    "specs": roots,
                    "view": False,
                    "concretizer": {"unify": True, "reuse": True},
                }
            },
            sort_keys=False,
        )
    )
    shutil.copytree(root(home) / "config", directory / ".jforge-config")
    if previous and (Path(previous) / "spack.lock").is_file():
        shutil.copy2(Path(previous) / "spack.lock", directory / "spack.lock")
    return directory


def _snapshot(home, envdir, op, label="snapshot"):
    bridge = Path(__file__).with_name("spack_bridge.py")
    return _json(
        run(home, ["python", str(bridge), "installed" if label == "installed" else "snapshot"],
            op, label, envdir=envdir), label
    )


def _publish(home, op, envdir, roots, snapshot):
    from .shell_activation import activation
    from .package_operations import key_for

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
        envdir = _candidate(
            home,
            op,
            roots,
            before.get("environment")
            if action not in {"upgrade", "full-upgrade"}
            else None,
        )
        committed = False
        try:
            if progress:
                progress("[1/4] Resolve the Spack dependency graph")
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
                run(
                    home,
                    flags,
                    op,
                    "concretize",
                    envdir=envdir,
                    timeout=timeout,
                    progress=progress,
                )
                plan = _snapshot(home, envdir, op, "plan")
                write_json(op / "plan.json", plan)
                if progress:
                    progress(f"[2/4] Concrete plan: {len(plan['packages'])} packages")
            else:
                plan = {"packages": [], "modifications": []}
            write_json(op / "plan.json", plan)
            if action == "check":
                result.update(
                    backend="spack",
                    lockfile=str(op / "candidate-environment/spack.lock")
                    if (envdir / "spack.lock").is_file()
                    else None,
                    plan=str(op / "plan.json"),
                    packages=plan["packages"],
                    ready_to_install=True,
                    builds_executed=False,
                    state_committed=False,
                )
                return result
            if progress:
                progress(
                    "[3/4] Install dependencies and requested packages"
                    if action != "remove"
                    else "[3/4] Retain installed store; update selected roots"
                )
            if roots and action != "remove":
                if cache != "never":
                    _cache_keys(home, op, envdir, progress)

                args = [
                    "install",
                    "--only-concrete",
                    "--verbose",
                    "--show-log-on-error",
                    "-j",
                    str(jobs),
                    "--use-buildcache",
                    cache,
                ]
                if action == "reinstall":
                    args += ["--overwrite", "-y", *[name_of(s) for s in translated]]
                run(
                    home,
                    args,
                    op,
                    "install",
                    envdir=envdir,
                    timeout=timeout,
                    progress=progress,
                )
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
                progress("[4/4] Save the lockfile and publish the shell environment")
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
        old_repo = (root(home) / "config/repos.yaml").read_bytes()
        repo = destination / "repos/spack_repo/builtin"
        (root(home) / "config/repos.yaml").write_text(
            yaml.safe_dump({"repos": {"builtin": str(repo)}})
        )
        try:
            rows = _refresh_catalog(home, op)
        except BaseException:
            (root(home) / "config/repos.yaml").write_bytes(old_repo)
            raise
        value.update(
            repository=str(repo), repository_commit=head, repository_archive=pin
        )
        write_json(home.root / "apt/spack.json", value)
        result.update(
            backend="spack",
            available=len(rows),
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
