"""Sequential local runs with dependency ordering and immutable evidence files."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

from . import __version__
from .storage import ForgeError, ForgeHome, digest, new_id, now, write_json
from .suite import load_suite, make_plan


def _stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    # A child may have survived its parent, so always finish the owned group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _artifact(source: Path, cwd: Path, destination: Path) -> dict:
    resolved = source.resolve()
    if not resolved.is_relative_to(cwd.resolve()) or not resolved.is_file():
        raise ForgeError(f"Missing artifact or path outside working directory: {source}", code=1)
    before = digest(resolved)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(resolved, destination)
    if digest(destination) != before or digest(resolved) != before:
        raise ForgeError(f"Artifact changed during archival: {source}", code=1)
    return {"source": str(source), "snapshot": str(destination),
            "sha256": before, "bytes": destination.stat().st_size}


def execute(home: ForgeHome, suite_path: Path, progress=None) -> dict:
    if os.name != "posix":
        raise ForgeError("The local runner currently supports POSIX systems; use WSL on Windows.")
    config = home.require_initialized()
    suite = load_suite(suite_path)
    run_id = new_id("run")
    run_dir = home.root / "runs" / run_id
    plan = make_plan(suite, run_dir)
    run_dir.mkdir(parents=True, mode=0o700)
    request = {"schema_version": 1, "run_id": run_id, "suite_id": plan["suite_id"],
               "host_id": config["host_id"], "hostname": socket.gethostname(),
               "forge_version": __version__, "started_at": now(),
               "suite_path": str(suite_path.resolve()), "invocation_cwd": str(Path.cwd())}
    write_json(run_dir / "request.json", request)
    records = []
    result = {**request, "status": "running", "steps": records, "path": str(run_dir)}

    def event(kind: str, **values) -> None:
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": now(), "event": kind, **values}) + "\n")
        if progress is not None:
            progress({"run_id": run_id, "event": kind, **values})

    try:
        (run_dir / "suite.snapshot.yaml").write_text(suite["text"], encoding="utf-8")
        write_json(run_dir / "plan.json", plan)
        shell = plan["environment_shell"]
        if shell:
            snapshot = run_dir / "environment.sh"
            shutil.copyfile(shell["path"], snapshot)
            if digest(snapshot) != shell["sha256"]:
                raise ForgeError("Environment script changed while preparing the run.", code=1)
        for item in plan["inputs"].values():
            target = Path(item["snapshot"])
            target.parent.mkdir(parents=True, mode=0o700)
            shutil.copyfile(item["source"], target)
            if digest(target) != item["sha256"]:
                raise ForgeError(f"Input changed while preparing the run: {item['source']}", code=1)
        write_json(run_dir / "environment.json", {
            "schema_version": 1, "hostname": socket.gethostname(),
            "system": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "python": sys.version,
            "python_executable": sys.executable, "cpu_count": os.cpu_count(),
            "environment_shell": shell,
            "recorded_variables": {key: os.environ[key] for key in
                                   ("PATH", "CC", "CXX", "CMAKE_PREFIX_PATH", "LOADEDMODULES")
                                   if key in os.environ},
            "scope": "Controller interpreter and selected inherited variables; shell text is snapshotted.",
        })
        event("run_started", path=str(run_dir))
        statuses = {}
        for step in plan["steps"]:
            step_id = step["id"]
            step_dir = run_dir / "steps" / step_id
            step_dir.mkdir(parents=True, mode=0o700)
            record = {"id": step_id, "started_at": now(), "status": "running", "artifacts": {}}
            records.append(record)
            write_json(step_dir / "command.json", step)
            if any(statuses[dep] != "passed" for dep in step["needs"]):
                record.update(status="blocked", reason="A prerequisite did not pass.")
            else:
                _execute_step(step, plan, run_dir, step_dir, record, event)
            record["finished_at"] = now()
            write_json(step_dir / "result.json", record)
            statuses[step_id] = record["status"]
            event("step_finished", step_id=step_id, status=record["status"])
        result["status"] = "passed" if all(r["status"] == "passed" for r in records) else "failed"
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        if records and records[-1]["status"] == "running":
            records[-1].update(status="interrupted", finished_at=now())
            write_json(run_dir / "steps" / records[-1]["id"] / "result.json", records[-1])
    except (OSError, ForgeError) as exc:
        result.update(status="failed", error=str(exc))
        if records and records[-1]["status"] == "running":
            records[-1].update(status="failed", reason=str(exc), finished_at=now())
            write_json(run_dir / "steps" / records[-1]["id"] / "result.json", records[-1])
    finally:
        result["finished_at"] = now()
        write_json(run_dir / "result.json", result)
    return result


def _execute_step(step, plan, run_dir, step_dir, record, event):
    shell = plan["environment_shell"]
    if shell and digest(Path(shell["path"])) != shell["sha256"]:
        raise ForgeError("Environment script changed after planning.", code=1)
    cwd = Path(step["cwd"])
    if cwd.is_relative_to(run_dir):
        cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = ["#!/usr/bin/env bash", "set -e", "set -o pipefail"]
    if shell:
        lines.append("source " + shlex.quote(shell["path"]))
    probe = (
        "import json,os,shutil,sys; "
        "from pathlib import Path; "
        "keys=['PATH','CC','CXX','CMAKE_PREFIX_PATH','LOADEDMODULES']; "
        "data={'schema_version':1,'variables':{k:os.environ[k] for k in keys if k in os.environ},"
        "'tools':{k:shutil.which(k) for k in ['python','python3','cmake','make','cc','c++']}}; "
        "Path(sys.argv[1]).write_text(json.dumps(data,indent=2)+'\\n')"
    )
    lines.append(shlex.join([sys.executable, "-c", probe, str(step_dir / "activation.json")]))
    # Record resolution inside the activated shell, not the controller's PATH.
    if "argv" in step:
        lines.append('command -v -- "$1" > ' + shlex.quote(str(step_dir / "executable.txt")))
        lines.append('exec "$@"')
        arguments = step["argv"]
    else:
        lines.append(step["script"])
        arguments = []
    script = step_dir / "execute.sh"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    command = [plan["bash"], "--noprofile", "--norc", str(script), *arguments]
    record["launcher_argv"] = command
    event("step_started", step_id=step["id"])
    started = time.monotonic()
    with (step_dir / "stdout.log").open("wb") as stdout, (step_dir / "stderr.log").open("wb") as stderr:
        try:
            process = subprocess.Popen(command, cwd=cwd, stdout=stdout, stderr=stderr,
                                       env={**os.environ, **step["variables"]}, start_new_session=True)
        except OSError as exc:
            record.update(status="failed", reason=str(exc))
            return
        try:
            record["exit_code"] = process.wait(timeout=step["timeout_seconds"])
            record["status"] = "passed" if record["exit_code"] == step["expect_exit_code"] else "failed"
        except subprocess.TimeoutExpired:
            _stop(process)
            record.update(status="failed", reason="timeout", exit_code=process.returncode)
        except BaseException:
            _stop(process)
            raise
        finally:
            record["elapsed_seconds"] = time.monotonic() - started
    if record["status"] == "passed":
        try:
            for name in step["artifacts"]:
                destination = run_dir / "artifacts" / step["id"] / name
                record["artifacts"][name] = _artifact(cwd / name, cwd, destination)
        except (ForgeError, OSError) as exc:
            record.update(status="failed", reason=str(exc))

