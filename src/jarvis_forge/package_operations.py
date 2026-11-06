"""Shared package I/O: locking, evidence, verified archives and command logs.

This module does not resolve dependencies or build packages; Spack owns those.
"""

from __future__ import annotations
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from . import __version__
from .downloads import fetch
from .storage import ForgeError, ForgeHome, digest, new_id, now, read_json, write_json


def key_for(record):
    return f"{record['name']}/{record['version']}/{record['install_id']}/manifest.json"


def _inside(home, path):
    if not path.resolve().is_relative_to(home.root.resolve()):
        raise ForgeError(f"Managed path escapes the Forge home: {path}")
    for candidate in (path, *path.parents):
        if candidate == home.root:
            break
        if candidate.is_symlink():
            raise ForgeError(
                f"Managed paths must not contain symbolic links: {candidate}"
            )
    return path


@contextmanager
def locked(home: ForgeHome):
    if os.name != "posix":
        raise ForgeError(
            "The user-prefix package manager requires POSIX; use WSL on Windows."
        )
    # Check existing path components before initialization can follow a symlink.
    for name in (
        "apt",
        "packages",
        "environments",
        "operations",
        "cache",
        "work",
        "locks",
    ):
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
            stream.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "hostname": socket.gethostname(),
                        "started_at": now(),
                    }
                )
            )
            stream.flush()
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def operation(home, action, request):
    operation_id = new_id("apt")
    directory = _inside(home, home.root / "operations" / operation_id)
    directory.mkdir(mode=0o700)
    write_json(
        directory / "request.json",
        {
            "schema_version": 1,
            "operation_id": operation_id,
            "action": action,
            "request": request,
            "started_at": now(),
            "forge_version": __version__,
        },
    )
    result = {"operation_id": operation_id, "path": str(directory)}
    try:
        yield directory, result
        result["status"] = "passed"
    except BaseException as exc:
        result.update(
            status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
            error=str(exc),
        )
        if isinstance(exc, ForgeError):
            exc.hint = (
                exc.hint + "\n" if exc.hint else ""
            ) + f"Operation logs: {directory}"
        raise
    finally:
        write_json(directory / "result.json", {**result, "finished_at": now()})


def run_command(argv, cwd, environment_script, logs, number, timeout, progress=None, *, process_env=None):
    from .runner import _stop

    script = logs / f"command-{number}.sh"
    probe = logs / f"command-{number}.json"
    requested_argv = list(argv)
    record = {
        "argv": argv,
        "requested_argv": requested_argv,
        "cwd": str(cwd),
        "environment": str(environment_script),
        "started_at": now(),
        "timeout_seconds": timeout,
        "status": "running",
        "stdin": "devnull",
    }
    write_json(probe, record)
    script_lines = [
        "set -e",
        "set -o pipefail",
        "source " + shlex.quote(str(environment_script)),
    ]
    script.write_text("\n".join(script_lines) + '\nexec "$@"\n')
    bash = shutil.which("bash")
    if not bash:
        raise ForgeError("Spack commands require Bash on PATH.")
    started = time.monotonic()
    try:
        with (
            (logs / f"command-{number}.stdout.log").open("wb") as out,
            (logs / f"command-{number}.stderr.log").open("wb") as err,
        ):
            # Separate pipes preserve the original log files; display both on stderr
            # so the final --json result on stdout remains machine-readable.
            process = subprocess.Popen(
                [bash, "--noprofile", "--norc", str(script), *argv],
                cwd=cwd,
                # The detached session cannot own the caller's SSH terminal.
                # Spack commands are noninteractive; CLI confirmations happen first.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=process_env,
            )
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
                                output = getattr(progress, "output", None)
                                if output is not None:
                                    output("stdout" if key.data is out else "stderr", data)
                                elif hasattr(sys.stderr, "buffer"):
                                    sys.stderr.buffer.write(data)
                                    sys.stderr.buffer.flush()
                                else:
                                    sys.stderr.write(
                                        data.decode("utf-8", errors="replace")
                                    )
                                    sys.stderr.flush()
                    remaining = timeout - (time.monotonic() - started)
                    code = process.wait(timeout=max(0, remaining))
                record.update(
                    exit_code=code, status="passed" if code == 0 else "failed"
                )
            except subprocess.TimeoutExpired as exc:
                _stop(process)
                record.update(status="timeout", exit_code=process.returncode)
                raise ForgeError(
                    f"Build/check command timed out: {argv[0]}", code=1
                ) from exc
            except BaseException:
                _stop(process)
                record.update(status="interrupted", exit_code=process.returncode)
                raise
            finally:
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
        if code:
            failure = getattr(progress, "command_failure", None)
            raise ForgeError(
                failure(number, code, logs / f"command-{number}.stderr.log") if failure else
                f"Package command failed with exit {code}: {shlex.join(argv)}; logs: {logs}",
                code=1,
            )
    except OSError as exc:
        record.update(status="failed", error=str(exc))
        raise ForgeError(f"Cannot start package command: {exc}", code=1) from exc
    finally:
        finished = getattr(progress, "output_finished", None)
        if finished is not None:
            finished()
        record.update(finished_at=now(), elapsed_seconds=time.monotonic() - started)
        write_json(probe, record)


def verified_archive(home, source, log_path, *, progress=None):
    archive = _inside(home, home.root / "cache/downloads" / source["sha256"])
    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not archive.is_file() or digest(archive) != source["sha256"]:
        temporary = archive.with_name(archive.name + "." + uuid.uuid4().hex + ".part")
        try:
            options = {}
            if source.get("timeout_seconds"):
                options["timeout"] = source["timeout_seconds"]
            if progress is not None:
                options["progress"] = progress
            fetch(source["url"], temporary, log_path, **options)
            if digest(temporary) != source["sha256"]:
                raise ForgeError(
                    f"SHA-256 mismatch for source archive: {source['url']}", code=1
                )
            os.replace(temporary, archive)
        finally:
            temporary.unlink(missing_ok=True)
    return archive


def environment_path(home):
    file = home.root / "apt/state.json"
    state = read_json(file) if file.exists() else {}
    if not state.get("spack"):
        raise ForgeError(
            "No Spack package environment is available.", hint="jforge apt import spack"
        )
    path = state.get("activation")
    if not path or not _inside(home, Path(path)).is_file():
        raise ForgeError(
            "The Spack activation script is missing.", hint="jforge apt import spack"
        )
    return str(Path(path))
