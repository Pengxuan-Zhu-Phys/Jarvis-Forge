"""Versioned, interpreter-independent records for user-selected environments."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
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
    """Check the exact planned files, including wrappers from retained revisions."""
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
                  "activation": str(activation), "description": "Active user-local packages",
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
            record["status"] = "legacy_python" if record.get("python") else "ready"
        except ForgeError:
            record["status"] = "modified_or_missing"
    if strict:
        if name == "apt" and record["status"] == "ready":
            from .package_operations import environment_path

            environment_path(home)
        if record["status"] == "legacy_python":
            raise ForgeError(f"Environment {name!r} has a retired Python override.",
                             hint="Select Python with pyenv and register the shell script again with env add --replace.")
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


def _publish(home, name, script, origin, description, parent=None):
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
    files["activate.sh"] = ("\n".join(lines) + "\n").encode()
    checks = {}
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(content)
        path.chmod(0o400)
        checks[relative] = hashlib.sha256(content).hexdigest()
    record = {"schema_version": 1, "managed_by": "jforge-env", "name": name, "revision": revision,
              "created_at": now(), "parent_revision": parent, "description": description,
              "source": origin, "files": checks}
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
                        (previous or {}).get("revision"))
