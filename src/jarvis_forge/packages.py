"""User-prefix package transactions with immutable receipts and activation files."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import selectors
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid

from packaging.version import Version

from . import __version__
from .catalog import Catalog, requirement, source_config, update_catalog
from .downloads import fetch, unpack
from .runner import _stop
from .storage import ForgeError, ForgeHome, digest, new_id, now, read_json, write_json
from .suite import expand


def key_for(record):
    return f"{record['name']}/{record['version']}/{record['install_id']}/manifest.json"


def state(home: ForgeHome) -> dict:
    path = home.root / "apt/state.json"
    value = read_json(path) if path.exists() else {"schema_version": 1, "active": {}, "manual": []}
    if (value.get("schema_version") != 1 or not isinstance(value.get("active"), dict)
            or not isinstance(value.get("manual"), list)
            or not all(isinstance(v, str) for v in value["active"].values())
            or not all(isinstance(v, str) for v in value["manual"])):
        raise ForgeError(f"Invalid package state: {path}")
    return value


def installed(home: ForgeHome) -> list[dict]:
    current = state(home)
    return [dict(record, active=current["active"].get(record["name"]) == key_for(record),
                 automatic=key_for(record) not in current["manual"])
            for record in home.packages()]


def _inside(home, path):
    if not path.resolve().is_relative_to(home.root.resolve()):
        raise ForgeError(f"Managed path escapes the Forge home: {path}")
    for candidate in (path, *path.parents):
        if candidate == home.root:
            break
        if candidate.is_symlink():
            raise ForgeError(f"Managed paths must not contain symbolic links: {candidate}")
    return path


@contextmanager
def locked(home: ForgeHome):
    if os.name != "posix":
        raise ForgeError("The user-prefix package manager requires POSIX; use WSL on Windows.")
    # Check existing path components before initialization can follow a symlink.
    for name in ("apt", "packages", "environments", "operations", "cache", "work", "locks"):
        _inside(home, home.root / name)
    home.initialize()
    lock = _inside(home, home.root / "locks/apt.lock")
    with lock.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ForgeError(f"Another package operation holds {lock}.") from exc
        try:
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps({"pid": os.getpid(), "hostname": socket.gethostname(), "started_at": now()}))
            stream.flush()
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def operation(home, action, request):
    operation_id = new_id("apt")
    directory = _inside(home, home.root / "operations" / operation_id)
    directory.mkdir(mode=0o700)
    write_json(directory / "request.json", {"schema_version": 1, "operation_id": operation_id,
               "action": action, "request": request, "started_at": now(), "forge_version": __version__})
    result = {"operation_id": operation_id, "path": str(directory)}
    try:
        yield directory, result
        result["status"] = "passed"
    except BaseException as exc:
        result.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=str(exc))
        if isinstance(exc, ForgeError):
            exc.hint = (exc.hint + "\n" if exc.hint else "") + f"Operation logs: {directory}"
        raise
    finally:
        write_json(directory / "result.json", {**result, "finished_at": now()})


def _context(recipes):
    tools = {"bash": shutil.which("bash")}
    commands = {"bash": [tools["bash"]]}
    for recipe in recipes:
        for tool in recipe.get("requires_tools", []):
            variable = {"cc": "CC", "c++": "CXX", "gfortran": "FC"}.get(tool)
            selected = os.environ.get(variable, tool) if variable else tool
            command = shlex.split(selected)
            tools[tool] = shutil.which(command[0]) if command else None
            commands[tool] = [tools[tool], *command[1:]]
    missing = [name for name, path in tools.items() if not path]
    if missing:
        raise ForgeError(f"Required build tools are missing: {', '.join(missing)}.",
                         hint="Load the site's compiler/CMake modules, then retry.")
    probes = {}
    for name, path in tools.items():
        try:
            probe = subprocess.run([*commands[name], "--version"], capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise ForgeError(f"Build-tool probe timed out: {path}") from exc
        probes[name] = {"path": path, "argv": [*commands[name], "--version"],
                        "version_output": probe.stdout[:4096], "exit_code": probe.returncode}
    return {"system": platform.system(), "release": platform.release(), "machine": platform.machine(), "tools": probes,
            "python": {"executable": sys.executable, "version": platform.python_version()},
            "loaded_modules": os.environ.get("LOADEDMODULES", ""),
            "flags": {k: os.environ[k] for k in ("CC", "CXX", "FC", "CFLAGS", "CXXFLAGS", "FFLAGS", "FCFLAGS", "CPPFLAGS", "LDFLAGS") if k in os.environ}}


def plan_install(home, specs, *, jobs=1, mode="install", operation_id="planned-operation", verify_external=False, external_log=None):
    if jobs < 1:
        raise ForgeError("--jobs must be at least 1.")
    previous = state(home)
    records = {key_for(r): r for r in home.packages()}
    if (set(previous["active"].values()) | set(previous["manual"])) - set(records):
        raise ForgeError("Package state references missing installation receipts.")
    if mode in {"upgrade", "full-upgrade", "reinstall"}:
        normalized = []
        for spec in specs:
            req = requirement(spec)
            name = req.name
            if name not in previous["active"]:
                name = next((r["name"] for key, r in records.items()
                             if key in previous["active"].values() and req.name in r.get("provenance", {}).get("binary_versions", {})), name)
            normalized.append(name + str(req.specifier))
        specs = normalized
    requested = [requirement(spec).name for spec in specs]
    roots = {name: records[key] for name, key in previous["active"].items()
             if key in previous["manual"] and key in records}
    official = home.root / "cache/apt/official.json"
    if official.exists():
        pending = read_json(official).get("pending", [])
        relevant = set(requested or roots) if mode in {"upgrade", "full-upgrade"} else {
            requirement(s).name for s in specs if not requirement(s).specifier}
        blocked = [p for p in pending if p["name"] in relevant]
        if blocked:
            raise ForgeError("Official recipe updates need an adapter: " +
                             "; ".join(f"{p['name']}: {p['reason']}" for p in blocked))
    if mode in {"upgrade", "full-upgrade"}:
        names = requested or list(roots)
        unknown = set(names) - set(previous["active"])
        if unknown:
            raise ForgeError(f"Packages are not active: {', '.join(sorted(unknown))}")
        selected_specs = [f"{name}>={records[previous['active'][name]]['version']}" for name in names]
        selected_specs += specs
        selected_specs += [f"{name}=={r['version']}" for name, r in roots.items() if name not in names]
        requested = list(roots)
    else:
        selected_specs = list(specs)
        if mode == "reinstall":
            selected_specs = [f"{requirement(s).name}=={records[previous['active'][requirement(s).name]]['version']}"
                              if not requirement(s).specifier and requirement(s).name in previous["active"] else s for s in specs]
        selected_specs += [f"{name}=={r['version']}" for name, r in roots.items() if name not in requested]
    recipes = Catalog(home).resolve(selected_specs) if selected_specs else []
    context = _context(recipes) if recipes else {}
    if verify_external and any(r.get("provenance", {}).get("external_dependencies") for r in recipes):
        from .official_dependencies import check_external
        context["external_dependencies"] = check_external(recipes, external_log)
    planned, selected = [], {}
    for recipe in recipes:
        dependencies = [selected[requirement(dep).name]["key"] for dep in recipe.get("depends", [])]
        recipe_context = {**context, "tools": {name: item for name, item in context["tools"].items()
                                             if name == "bash" or name in recipe.get("requires_tools", [])}}
        if "external_dependencies" in recipe_context:
            recipe_context["external_dependencies"] = [{k: evidence[k] for k in ("dependency", "argv", "version", "compared_version", "version_policy")}
                for evidence in context["external_dependencies"] if evidence["consumer"] == recipe["name"]]
        payload = {"recipe": recipe, "context": recipe_context, "dependencies": dependencies}
        install_id = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
        if mode == "reinstall" and (recipe["name"] in requested or set(requested) & set(recipe.get("provenance", {}).get("binary_versions", {}))):
            install_id += "-" + uuid.uuid4().hex[:8]
        package_dir = _inside(home, home.root / "packages" / recipe["name"] / recipe["version"] / install_id)
        prefix = package_dir / "prefix"
        key = f"{recipe['name']}/{recipe['version']}/{install_id}/manifest.json"
        old = records.get(key)
        if old and (old.get("managed_by") != "jforge-apt" or not old["prefix_exists"]):
            raise ForgeError(f"Existing installation is incomplete or unmanaged: {package_dir}")
        work = _inside(home, home.root / "work" / operation_id / recipe["name"])
        bindings = {"prefix": str(prefix), "source": str(work / "source" / recipe["source"]["subdir"]),
                    "build": str(work / "build"), "jobs": str(jobs), "python": sys.executable}
        bindings.update({f"dep:{requirement(d).name}": selected[requirement(d).name]["prefix"]
                         for d in recipe.get("depends", [])})
        for dep in recipe.get("depends", []):
            provider = selected[requirement(dep).name]
            for alias in provider["recipe"].get("provenance", {}).get("binary_versions", {}):
                key_name = f"dep:{alias}"
                if key_name in bindings and bindings[key_name] != provider["prefix"]:
                    raise ForgeError(f"Ambiguous dependency provider for {alias} in {recipe['name']}")
                bindings[key_name] = provider["prefix"]
        commands = {phase: [[expand(arg, bindings) for arg in cmd] for cmd in recipe.get(phase, [])]
                    for phase in ("build", "install", "check", "probe", "dependency_check")}
        # Validate export placeholders before any download or build starts.
        for section, entries in recipe.get("environment", {}).items():
            for value in entries.values():
                for arg in value if isinstance(value, list) else [value]:
                    expand(arg, bindings)
        item = {"name": recipe["name"], "version": recipe["version"], "install_id": install_id,
                "key": key, "prefix": str(prefix), "work": str(work), "bindings": bindings,
                "dependencies": dependencies, "recipe": recipe, "commands": commands,
                "external_verification_pending": bool(recipe.get("provenance", {}).get("external_dependencies")) and not verify_external,
                "action": "reuse" if old else "install", "manual": recipe["name"] in requested or recipe["name"] in roots or any(
                    spec.split("=", 1)[0] in recipe.get("provenance", {}).get("binary_versions", {})
                    for spec in requested)}
        planned.append(item)
        selected[recipe["name"]] = item
    return {"mode": mode, "requested": requested, "specs": specs, "context": context,
            "packages": planned, "state_before": previous}


def _shell_environment(recipe, bindings):
    lines = ["#!/usr/bin/env bash", "# Generated by Jarvis-Forge; source this file."]
    for name, value in recipe.get("environment", {}).get("set", {}).items():
        lines.append(f"export {name}={shlex.quote(expand(value, bindings))}")
    for name, paths in recipe.get("environment", {}).get("prepend", {}).items():
        if paths:
            value = ":".join(expand(p, bindings) for p in paths)
            lines.append(f'export {name}={shlex.quote(value)}"${{{name}:+:${{{name}}}}}"')
    return "\n".join(lines) + "\n"


def _activation(home, keys, records):
    lines, done, visiting = ["#!/usr/bin/env bash", "# Generated by Jarvis-Forge."], set(), set()

    def add(key):
        if key in done:
            return
        if key in visiting or key not in records:
            raise ForgeError(f"Missing or cyclic installed dependency: {key}")
        visiting.add(key)
        item = records[key]
        for dep in item.get("dependencies", []):
            add(dep)
        env_file = _inside(home, Path(item["environment_file"]))
        if not env_file.is_file():
            raise ForgeError(f"Missing installed environment file: {env_file}")
        lines.append("source " + shlex.quote(str(env_file)))
        visiting.remove(key)
        done.add(key)
    for key in keys:
        add(key)
    return "\n".join(lines) + "\n"


def _publish(home, current, operation_id):
    records = {key_for(r): r for r in home.packages()}
    text = _activation(home, current["active"].values(), records)
    path = _inside(home, home.root / "environments/apt/revisions" / operation_id / "activate.sh")
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(text, encoding="utf-8")
    current = dict(current, activation=str(path), updated_at=now(), last_operation_id=operation_id)
    write_json(_inside(home, home.root / "apt/state.json"), current)
    return current


def _run_command(argv, cwd, environment_script, logs, number, timeout, context, progress=None):
    script = logs / f"command-{number}.sh"
    probe = logs / f"command-{number}.json"
    record = {"argv": argv, "cwd": str(cwd), "environment": str(environment_script), "started_at": now(),
              "timeout_seconds": timeout, "status": "running"}
    write_json(probe, record)
    script.write_text("set -e\nset -o pipefail\nsource " + shlex.quote(str(environment_script)) + '\nexec "$@"\n')
    started = time.monotonic()
    try:
        with (logs / f"command-{number}.stdout.log").open("wb") as out, (logs / f"command-{number}.stderr.log").open("wb") as err:
            # Separate pipes preserve the original log files; display both on stderr
            # so the final --json result on stdout remains machine-readable.
            process = subprocess.Popen([context["tools"]["bash"]["path"], "--noprofile", "--norc", str(script), *argv],
                                       cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, out)
                    selector.register(process.stderr, selectors.EVENT_READ, err)
                    while selector.get_map():
                        remaining = timeout - (time.monotonic() - started)
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(argv, timeout)
                        for key, _ in selector.select(min(remaining, 1.0)):
                            data = os.read(key.fileobj.fileno(), 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                                continue
                            key.data.write(data)
                            key.data.flush()
                            if progress is not None:
                                if hasattr(sys.stderr, "buffer"):
                                    sys.stderr.buffer.write(data)
                                    sys.stderr.buffer.flush()
                                else:
                                    sys.stderr.write(data.decode("utf-8", errors="replace"))
                                    sys.stderr.flush()
                    remaining = timeout - (time.monotonic() - started)
                    code = process.wait(timeout=max(0, remaining))
                record.update(exit_code=code, status="passed" if code == 0 else "failed")
            except subprocess.TimeoutExpired as exc:
                _stop(process)
                record.update(status="timeout", exit_code=process.returncode)
                raise ForgeError(f"Build/check command timed out: {argv[0]}", code=1) from exc
            except BaseException:
                _stop(process)
                record.update(status="interrupted", exit_code=process.returncode)
                raise
            finally:
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
        if code:
            raise ForgeError(f"Package command failed with exit {code}: {shlex.join(argv)}; logs: {logs}", code=1)
    except OSError as exc:
        record.update(status="failed", error=str(exc))
        raise ForgeError(f"Cannot start package command: {exc}", code=1) from exc
    finally:
        record.update(finished_at=now(), elapsed_seconds=time.monotonic() - started)
        write_json(probe, record)


def install(home, specs, *, jobs=1, timeout=3600, mode="install", progress=None, official=True):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ForgeError("--timeout must be finite and positive.")
    with locked(home), operation(home, mode, {"specs": specs, "jobs": jobs, "timeout_seconds": timeout,
                                             "official_lookup": official}) as (op, result):
        if official:
            from .official import ensure_recipes
            previous = state(home)
            installed_records = {key_for(r): r for r in home.packages()}
            roots = [name for name, key in previous["active"].items() if key in previous["manual"] and key in installed_records]
            lookup_specs = list(specs) + [name for name in roots if name not in {requirement(s).name for s in specs}]
            result["official_imports"] = ensure_recipes(home, lookup_specs, op, progress)
        plan = plan_install(home, specs, jobs=jobs, mode=mode, operation_id=op.name, verify_external=True, external_log=op / "external-dependencies.json")
        write_json(op / "plan.json", plan)
        write_json(op / "external-dependencies.json", {"providers": plan["context"].get("external_dependencies", [])})
        current = dict(plan["state_before"], active=dict(plan["state_before"]["active"]), manual=list(plan["state_before"]["manual"]))
        records = {key_for(r): r for r in home.packages()}
        completed = []
        result["packages"] = completed
        readiness = {item["name"]: {"status": "planned", "dependencies": list(item["dependencies"])} for item in plan["packages"]}
        def save_readiness():
            write_json(op / "dependency-status.json", {"schema_version": 1, "packages": readiness})
        save_readiness()
        ready_keys = set()
        if progress:
            progress("Dependency order: " + ", ".join(item["name"] for item in plan["packages"]))
        for index, item in enumerate(plan["packages"], 1):
            if progress:
                progress(f"Package {index}/{len(plan['packages'])}: {item['action']} {item['name']}={item['version']}")
            try:
                if not set(item["dependencies"]) <= ready_keys:
                    raise ForgeError(f"Dependencies are not ready for {item['name']}.", code=1)
                readiness[item["name"]]["status"] = "installing" if item["action"] == "install" else "verifying"
                save_readiness()
                if item["action"] == "install":
                    record = _install_one(home, item, plan["context"], op, timeout, records, progress)
                    records[item["key"]] = record
                else:
                    _verify_existing(home, item, plan["context"], op, timeout, records, progress)
                ready_keys.add(item["key"])
                readiness[item["name"]]["status"] = "ready"
                save_readiness()
                if progress:
                    progress(f"{item['name']}: verified and ready")
            except BaseException as exc:
                readiness[item["name"]].update(status="failed", error=str(exc))
                for entry in readiness.values():
                    if entry["status"] == "planned":
                        entry.update(status="blocked", reason=f"Transaction stopped at {item['name']}")
                save_readiness()
                raise
            aliases = item["recipe"].get("provenance", {}).get("binary_versions", {})
            for old_name in list(current["active"]):
                if old_name != item["name"] and old_name in aliases:
                    del current["active"][old_name]
            current["active"][item["name"]] = item["key"]
            if item["manual"] and item["key"] not in current["manual"]:
                current["manual"].append(item["key"])
            completed.append({"name": item["name"], "version": item["version"], "install_id": item["install_id"],
                              "prefix": item["prefix"], "action": item["action"]})
        current = _publish(home, current, op.name)
        result["activation"] = current["activation"]
        result["state_committed"] = True
        return result


def _verify_existing(home, item, context, op, timeout, records, progress):
    """Revalidate reused instances in this operation, without modifying their history."""
    logs = op / item["name"]
    logs.mkdir(mode=0o700)
    record = records[item["key"]]
    for field, digest_field in (("environment_file", "environment_sha256"), ("activation", "activation_sha256")):
        path = _inside(home, Path(record[field]))
        if not path.is_file() or (record.get(digest_field) and digest(path) != record[digest_field]):
            raise ForgeError(f"Installed environment is missing or modified: {path}", code=1,
                             hint=f"jforge apt reinstall {item['name']}")
    for name, expected in record.get("key_files", {}).items():
        path = _inside(home, Path(record["prefix"]) / name)
        if not path.is_file() or digest(path) != expected:
            raise ForgeError(f"Installed artifact is missing or modified: {path}", code=1,
                             hint=f"jforge apt reinstall {item['name']}")
    activation = logs / "dependency-environment.sh"
    activation.write_text(_activation(home, [item["key"]], records), encoding="utf-8")
    commands = item["commands"]["probe"] or item["commands"]["check"]
    if progress:
        progress(f"{item['name']}: verifying existing prefix {item['prefix']}")
    for index, argv in enumerate(commands, 1):
        _run_command(argv, Path(item["prefix"]), activation, logs, f"{index:03d}-reuse-probe", timeout, context, progress)
    write_json(logs / "readiness.json", {"status": "ready", "prefix": item["prefix"], "install_id": item["install_id"],
               "verification": "probe" if item["commands"]["probe"] else "legacy-check", "verified_at": now()})


def _install_one(home, item, context, op, timeout, records, progress=None):
    recipe, package_dir = item["recipe"], Path(item["prefix"]).parent
    logs = op / item["name"]
    logs.mkdir(mode=0o700)
    write_json(logs / "recipe.json", recipe)
    work = Path(item["work"])
    _inside(home, work).mkdir(parents=True, exist_ok=False, mode=0o700)
    if progress:
        progress(f"{item['name']}: fetching and unpacking source")
    archive = verified_archive(home, recipe["source"], logs / "download")
    unpack(archive, work / "source")
    source = Path(item["bindings"]["source"])
    if not source.is_dir():
        raise ForgeError(f"Archive lacks the declared source directory: {recipe['source']['subdir']}", code=1)
    patches = []
    if recipe["source"].get("patch_archive"):
        patch_archive = verified_archive(home, recipe["source"]["patch_archive"], logs / "patch-download")
        unpack(patch_archive, work / "packaging")
        patches = quilt_patches(work / "packaging")
    Path(item["bindings"]["build"]).mkdir(mode=0o700)
    package_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    prefix = Path(item["prefix"])
    prefix.mkdir(mode=0o700)
    try:
        env_file = package_dir / "environment.sh"
        env_file.write_text(_shell_environment(recipe, item["bindings"]), encoding="utf-8")
        record = {"schema_version": 1, "managed_by": "jforge-apt", "name": item["name"],
                  "version": item["version"], "install_id": item["install_id"], "prefix": str(prefix),
                  "dependencies": item["dependencies"], "environment_file": str(env_file),
                  "activation": str(package_dir / "activate.sh"), "source": recipe["source"],
                  "recipe_sha256": hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest(),
                  "provenance": recipe.get("provenance", {}),
                  "context": context, "operation_id": op.name, "installed_at": now(), "status": "installed"}
        activation = package_dir / "activate.sh"
        activation.write_text(_activation(home, [item["key"]], {**records, item["key"]: record}), encoding="utf-8")
        number = 0
        total = len(patches) + sum(len(item["commands"][phase]) for phase in ("dependency_check", "build", "install", "check", "probe"))
        for patch_path, strip in patches:
            number += 1
            if progress:
                progress(f"{item['name']}: step {number}/{total} (patch): {patch_path.name}")
            _run_command([context["tools"]["patch"]["path"], "-t", "-N", f"-p{strip}", "-i", str(patch_path)], source, activation,
                         logs, f"{number:03d}-patch", timeout, context, progress)
        for phase in ("dependency_check", "build", "install", "check", "probe"):
            for argv in item["commands"][phase]:
                number += 1
                if progress:
                    shown = shlex.join(argv[:2]) + " <script>" if len(argv) > 2 and argv[1] == "-c" else shlex.join(argv)
                    progress(f"{item['name']}: step {number}/{total} ({phase}): {shown}")
                _run_command(argv, source, activation, logs, f"{number:03d}-{phase}", timeout, context, progress)
        if not any(prefix.iterdir()):
            raise ForgeError(f"Install produced an empty prefix: {prefix}", code=1)
        record["environment_sha256"] = digest(env_file)
        record["activation_sha256"] = digest(activation)
        key_files = {}
        for relative in recipe.get("artifacts", []):
            path = _inside(home, prefix / relative)
            if not path.is_relative_to(prefix) or not path.is_file():
                raise ForgeError(f"Required installed artifact is missing: {path}", code=1)
            key_files[relative] = digest(path)
        record["key_files"] = key_files
        record["probe"] = item["commands"]["probe"]
        write_json(logs / "readiness.json", {"status": "ready", "prefix": str(prefix), "verified_at": now()})
        write_json(package_dir / "manifest.json", record)
        return record
    except BaseException:
        shutil.rmtree(package_dir)
        raise


def verified_archive(home, source, log_path):
    archive = _inside(home, home.root / "cache/downloads" / source["sha256"])
    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not archive.is_file() or digest(archive) != source["sha256"]:
        temporary = archive.with_name(archive.name + "." + uuid.uuid4().hex + ".part")
        try:
            fetch(source["url"], temporary, log_path)
            if digest(temporary) != source["sha256"]:
                raise ForgeError(f"SHA-256 mismatch for source archive: {source['url']}", code=1)
            os.replace(temporary, archive)
        finally:
            temporary.unlink(missing_ok=True)
    return archive


def quilt_patches(packaging):
    base = packaging / "debian/patches"
    series = base / "series"
    if not series.exists():
        return []
    result = []
    for line in series.read_text(encoding="utf-8").splitlines():
        fields = shlex.split(line, comments=True)
        if not fields:
            continue
        if len(fields) > 2 or (len(fields) == 2 and fields[1] not in {"-p0", "-p1"}):
            raise ForgeError(f"Unsupported quilt series entry: {line}")
        path = base / fields[0]
        if not path.resolve().is_relative_to(base.resolve()) or not path.is_file():
            raise ForgeError(f"Unsafe or missing quilt patch: {fields[0]}")
        strip = int(fields[1][-1]) if len(fields) == 2 else 1
        headers = 0
        for text in path.read_text(encoding="utf-8").splitlines():
            if text.startswith(("--- ", "+++ ")):
                headers += 1
                name = text[4:].split("\t", 1)[0].split(" ", 1)[0]
                if name == "/dev/null":
                    continue
                parts = Path(name).parts
                if Path(name).is_absolute() or ".." in parts or len(parts) <= strip or name.startswith(('"', "'")):
                    raise ForgeError(f"Unsafe patch target: {name}")
        if headers < 2:
            raise ForgeError(f"Only unified-diff quilt patches are supported: {path.name}")
        result.append((path, strip))
    return result


def environment_path(home):
    current = state(home)
    if not current.get("activation") or not Path(current["activation"]).is_file():
        raise ForgeError("No apt environment is available.", hint="jforge apt install <package>")
    return str(_inside(home, Path(current["activation"])))


def plan_remove(home, specs, *, automatic=False):
    records = {key_for(r): r for r in home.packages()}
    current = state(home)
    if automatic:
        retained, visiting = set(), set()
        def keep(key):
            if key in retained:
                return
            if key in visiting or key not in records:
                raise ForgeError(f"Missing or cyclic installed dependency: {key}")
            visiting.add(key)
            for dep in records[key].get("dependencies", []):
                keep(dep)
            visiting.remove(key)
            retained.add(key)
        for key in current["manual"]:
            keep(key)
        selected = {key for key, item in records.items() if key not in retained and item.get("managed_by") == "jforge-apt"}
    else:
        selected = set()
        for spec in specs:
            req = requirement(spec)
            matches = {key for key, item in records.items() if item["name"] == req.name and Version(item["version"]) in req.specifier}
            if not matches:
                raise ForgeError(f"No installed package matches {spec}.")
            selected.update(matches)
    for key in selected:
        item = records[key]
        package_dir = _inside(home, home.root / "packages" / Path(key).parent)
        if (item.get("managed_by") != "jforge-apt" or package_dir.is_symlink()
                or Path(item["prefix"]) != package_dir / "prefix"):
            raise ForgeError(f"Refusing to remove an unmanaged/invalid installation: {key}")
    for key, item in records.items():
        if key not in selected and set(item.get("dependencies", [])) & selected:
            raise ForgeError(f"Cannot remove a dependency still needed by {item['name']}={item['version']}.")
    return {"packages": [records[key] for key in sorted(selected)], "keys": sorted(selected)}


def remove(home, specs, *, automatic=False, expected_keys=None):
    with locked(home), operation(home, "autoremove" if automatic else "remove", {"specs": specs}) as (op, result):
        plan = plan_remove(home, specs, automatic=automatic)
        if expected_keys is not None and plan["keys"] != expected_keys:
            raise ForgeError("Installed packages changed after the removal preview; review a new plan.")
        write_json(op / "removed-receipts.json", plan)
        current = state(home)
        chosen = set(plan["keys"])
        current["active"] = {name: key for name, key in current["active"].items() if key not in chosen}
        current["manual"] = [key for key in current["manual"] if key not in chosen]
        # Generate the surviving environment before deleting any installation.
        _publish(home, current, op.name)
        for key in plan["keys"]:
            shutil.rmtree(home.root / "packages" / Path(key).parent)
        result["packages"] = plan["packages"]
        return result


def update(home, *, official=True):
    with locked(home), operation(home, "update", {"official": official}) as (op, result):
        result.update(update_catalog(home, op))
        from .official import refresh_official
        if official:
            result.update(refresh_official(home, op))
        result["available"] = len(Catalog(home).list())
        return result


def edit_sources(home, *, add=None, remove_source=None):
    with locked(home), operation(home, "edit-sources", {"add": add, "remove": remove_source}) as (op, result):
        config = source_config(home)
        if add:
            source = add if add.startswith("https://") else str(Path(add).expanduser().resolve())
            if source not in config["sources"]:
                config["sources"].append(source)
        if remove_source:
            source = remove_source if remove_source.startswith("https://") else str(Path(remove_source).expanduser().resolve())
            if source not in config["sources"]:
                raise ForgeError(f"Source is not configured: {source}")
            config["sources"].remove(source)
        write_json(_inside(home, home.root / "apt/sources.json"), config)
        result.update(config)
        return result
