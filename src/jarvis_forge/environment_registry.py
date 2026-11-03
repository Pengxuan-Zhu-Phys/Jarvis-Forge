"""Versioned, interpreter-independent records for user-selected environments."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess

from .storage import ForgeError, ForgeHome, digest, identifier, new_id, now, read_json, write_json


def _inside(home, path):
    if not path.resolve().is_relative_to(home.root.resolve()):
        raise ForgeError(f"Environment path escapes the Forge home: {path}")
    for parent in (path, *path.parents):
        if parent == home.root:
            break
        if parent.is_symlink():
            raise ForgeError(f"Managed environment paths must not be symbolic links: {parent}")
    return path


def _base(home, name):
    identifier(name)
    if name.lower() == "apt":
        raise ForgeError("The apt environment is managed by jforge apt; choose another name.")
    return _inside(home, home.root / "environments" / name)


@contextmanager
def _locked(home):
    _inside(home, home.root / "environments")
    _inside(home, home.root / "locks")
    home.initialize()
    with _inside(home, home.root / "locks/environments.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ForgeError("Another environment update is in progress.") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def verify(record):
    """Check the exact planned files, including sh content and Python wrappers."""
    root = Path(record["path"])
    home = ForgeHome(Path(record["home"]))
    for relative, expected in record["files"].items():
        part = Path(relative)
        if part.is_absolute() or ".." in part.parts:
            raise ForgeError("Invalid environment file record.")
        path = _inside(home, root / part)
        if (not path.is_file() or digest(path) != expected
                or (part.parts[0] == "bin" and not os.access(path, os.X_OK))):
            raise ForgeError(f"Managed environment file is missing or modified: {path}",
                             hint="Register a new revision with jforge env add NAME --shell PATH --replace.")


def show(home, name, revision=None, *, strict=False):
    if name == "apt":
        from .apt_profiles import active_home
        home = active_home(home)
        if revision:
            raise ForgeError("Use jforge apt env for the current apt activation script.")
        state_path = home.root / "apt/state.json"
        state = read_json(state_path) if state_path.exists() else {}
        if not state.get("activation"):
            raise ForgeError("No apt environment has been published.", hint="jforge apt install PACKAGE")
        activation = _inside(home, Path(state["activation"]))
        record = {"schema_version": 1, "managed_by": "jforge-apt", "home": str(home.root),
                  "name": "apt", "revision": state["last_operation_id"], "path": str(activation.parent),
                  "activation": str(activation), "python": None, "description": "Active user-local packages",
                  "files": {activation.name: digest(activation)} if activation.is_file() else {}}
        record["status"] = "ready" if activation.is_file() else "missing"
    else:
        base = _base(home, name)
        if revision is None:
            pointer = base / "current.json"
            if not pointer.is_file():
                raise ForgeError(f"Environment not found: {name}", hint="jforge env list")
            current = read_json(pointer)
            if current.get("schema_version") != 1 or current.get("name") != name:
                raise ForgeError(f"Invalid environment pointer: {pointer}")
            revision = current.get("revision")
        if not isinstance(revision, str):
            raise ForgeError("Invalid environment revision.")
        identifier(revision)
        directory = _inside(home, base / "revisions" / revision)
        manifest = directory / "manifest.json"
        record = read_json(manifest)
        if (record.get("schema_version") != 1 or record.get("name") != name
                or record.get("revision") != revision or not isinstance(record.get("files"), dict)
                or not {"activate.sh", "source.sh"}.issubset(record["files"])):
            raise ForgeError(f"Invalid environment manifest: {manifest}")
        record = {**record, "home": str(home.root), "path": str(directory),
                  "activation": str(directory / "activate.sh")}
        try:
            verify(record)
            record["status"] = "ready"
        except ForgeError:
            record["status"] = "modified_or_missing"
    if strict:
        if record["status"] != "ready":
            raise ForgeError(f"Environment {name!r} has missing or modified files.")
        verify(record)
    return record


def list_environments(home):
    from .apt_profiles import active_home
    records = [show(home, path.parent.name)
               for path in sorted((home.root / "environments").glob("*/current.json"))
               if path.parent.name != "apt"]
    state_path = active_home(home).root / "apt/state.json"
    if state_path.exists() and read_json(state_path).get("activation"):
        records.append(show(home, "apt"))
    return sorted(records, key=lambda item: item["name"])


def _publish(home, name, script, origin, description, python, parent=None):
    revision = new_id("env")
    directory = _inside(home, _base(home, name) / "revisions" / revision)
    directory.mkdir(parents=True, mode=0o700)
    files = {"source.sh": script}
    lines = ["#!/usr/bin/env bash", "# Managed by Jarvis-Forge; explicitly source this file to activate.",
             "source " + shlex.quote(str(directory / "source.sh")),
             "_jforge_env_status=$?", 'if [ "$_jforge_env_status" -ne 0 ]; then return "$_jforge_env_status"; fi',
             "unset _jforge_env_status",
             "export JFORGE_ENV_NAME=" + shlex.quote(name),
             "export JFORGE_ENV_REVISION=" + shlex.quote(revision)]
    if python:
        lines.append("unset PYTHONHOME")
        if python["mode"] == "pyenv":
            lines += ["export PYENV_ROOT=" + shlex.quote(python["root"]),
                      "export PYENV_VERSION=" + shlex.quote(python["selection"])]
            executable = [python["program"], "exec", "python"]
            extra_path = [str(Path(python["executable"]).parent), str(Path(python["root"]) / "shims"),
                          str(Path(python["program"]).parent)]
        else:
            executable = [python["executable"]]
            extra_path = [str(Path(python["executable"]).parent)]
        for alias in ("python", "python3"):
            files[f"bin/{alias}"] = ("#!/bin/sh\nexec " + shlex.join(executable) + ' "$@"\n').encode()
        info = python["observed"]
        if info["prefix"] != info["base_prefix"]:
            lines.append("export VIRTUAL_ENV=" + shlex.quote(info["prefix"]))
        else:
            lines.append("unset VIRTUAL_ENV")
        lines.append("export PATH=" + shlex.quote(":".join([str(directory / "bin"), *extra_path])) + ':"$PATH"')
        lines.append("hash -r")
    files["activate.sh"] = ("\n".join(lines) + "\n").encode()
    checks = {}
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(content)
        path.chmod(0o500 if relative.startswith("bin/") else 0o400)
        checks[relative] = hashlib.sha256(content).hexdigest()
    record = {"schema_version": 1, "managed_by": "jforge-env", "name": name, "revision": revision,
              "created_at": now(), "parent_revision": parent, "description": description,
              "source": origin, "python": python, "files": checks}
    write_json(directory / "manifest.json", record)
    write_json(_base(home, name) / "current.json", {"schema_version": 1, "name": name, "revision": revision})
    return show(home, name, revision, strict=True)


def add(home, name, shell, *, replace=False, description=None):
    base = _base(home, name)
    shell = shell.expanduser().resolve()
    if not shell.is_file():
        raise ForgeError(f"Environment script is not a file: {shell}")
    content = shell.read_bytes()
    bash = shutil.which("bash")
    if not bash:
        raise ForgeError("Bash is required to check environment script syntax.")
    check = subprocess.run([bash, "--noprofile", "--norc", "-n"], input=content,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    if check.returncode:
        raise ForgeError("Invalid Bash environment script: " + check.stderr.decode(errors="replace"))
    with _locked(home):
        previous = show(home, name) if (base / "current.json").exists() else None
        if previous and not replace:
            raise ForgeError(f"Environment {name!r} already exists.", hint="Use --replace to save a new revision.")
        return _publish(home, name, content, {"path": str(shell), "sha256": hashlib.sha256(content).hexdigest()},
                        description if description is not None else (previous or {}).get("description", ""),
                        (previous or {}).get("python"), (previous or {}).get("revision"))


def _command(argv, env, timeout):
    try:
        result = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForgeError(f"Cannot inspect selected Python: {exc}") from exc
    if result.returncode:
        raise ForgeError("Selected Python environment is unavailable: " + (result.stderr or result.stdout).strip())
    return result.stdout.strip()


def set_python(home, name, *, executable=None, pyenv=None, pyenv_root=None, inherit=False, timeout=30):
    _base(home, name)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ForgeError("--timeout must be finite and positive.")
    if sum((executable is not None, pyenv is not None, inherit)) != 1 or (pyenv_root and not pyenv):
        raise ForgeError("Choose exactly one of --python, --pyenv or --inherit; --pyenv-root requires --pyenv.")
    show(home, name, strict=True)
    # Resolve the requested interpreter only. Do not source scripts or execute a
    # global pyenv switch while modifying a profile.
    selection = None
    if not inherit:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        if pyenv is not None:
            if not pyenv.strip() or ":" in pyenv or any(c.isspace() for c in pyenv):
                raise ForgeError("Select one existing pyenv version or environment name.")
            if pyenv == "system":
                raise ForgeError("Use --python with an explicit system interpreter path, or --inherit for the script's PATH.")
            root = Path(pyenv_root or os.environ.get("PYENV_ROOT") or "~/.pyenv").expanduser().resolve()
            local_program = root / "bin/pyenv"
            program = str(local_program) if local_program.is_file() else shutil.which("pyenv")
            if not program:
                raise ForgeError("pyenv is not available on PATH or under the selected root.")
            program = os.path.abspath(program)
            env.update(PYENV_ROOT=str(root), PYENV_VERSION=pyenv)
            requested = _command([str(program), "which", "python"], env, timeout)
            selection = {"mode": "pyenv", "root": str(root), "program": program, "selection": pyenv}
        else:
            requested = os.path.expanduser(str(executable))
            if os.sep not in requested:
                requested = shutil.which(requested) or requested
            selection = {"mode": "executable", "requested": str(executable)}
        path = Path(os.path.abspath(requested))  # Preserve venv symlinks and pyvenv.cfg lookup.
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ForgeError(f"Python executable is not available: {path}")
        env.pop("PYTHONHOME", None)
        text = _command([str(path), "-c", "import json,sys; print(json.dumps({'executable':sys.executable, 'version':sys.version.split()[0], 'prefix':sys.prefix, 'base_prefix':sys.base_prefix, 'major':sys.version_info[0]}))"], env, timeout)
        try:
            info = json.loads(text)
            if info["major"] != 3 or not all(isinstance(info[k], str) for k in ("executable", "version", "prefix", "base_prefix")):
                raise ValueError("Expected a Python 3 interpreter")
        except (ValueError, KeyError, TypeError) as exc:
            raise ForgeError("Selected executable did not report a valid Python 3 environment.") from exc
        selection.update(executable=os.path.abspath(info["executable"]), observed=info)
    with _locked(home):
        previous = show(home, name, strict=True)
        script = (Path(previous["path"]) / "source.sh").read_bytes()
        return _publish(home, name, script, previous["source"], previous["description"], selection, previous["revision"])
