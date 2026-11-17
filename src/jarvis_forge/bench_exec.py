"""Slurm executor, offline prefetch and coverage for bench cards.

The controller submits one array job per case and step. Each array task runs
``python -m jarvis_forge bench exec-task``. Job state is read back from
``state.json``; Slurm is only asked who is still in the queue.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time

import yaml

from .package_operations import verified_archive
from .storage import ForgeError, identifier, now, read_json, write_json, write_text
from .suite import UniqueLoader


EXECUTOR_FIELDS = {"kind", "partition", "account", "max_queued", "array_limit", "data_root",
                   "preamble", "machine_class"}
LOST = {"NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "CANCELLED", "FAILED", "COMPLETED", "MISSING", "DEADLINE"}
FAILED_INFRA = {"OUT_OF_MEMORY", "TIMEOUT"}
ACTIVE = {"RUNNING", "PENDING", "CONFIGURING", "COMPLETING", "SUSPENDED"}


def load_executor(home, name):
    """The named executor, or the built-in local one when no file is stored."""
    name = name or "local"
    identifier(name)
    path = home.root / "executors" / f"{name}.yaml"
    if not path.is_file():
        if name != "local":
            raise ForgeError(f"Executor not found: {name}", hint="jforge bench executor add NAME --file site.yaml")
        return {"name": "local", "kind": "local", "machine_class": None, "data_root": None}
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read executor {path}: {exc}") from exc
    spec = _validate_executor(raw, name)
    spec["name"] = name
    spec["path"] = str(path)
    return spec


def add_executor(home, name, source):
    home.require_initialized()
    identifier(name)
    source = Path(source).expanduser()
    try:
        raw = yaml.load(source.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read {source}: {exc}") from exc
    spec = _validate_executor(raw, name)
    target = home.root / "executors" / f"{name}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text(target, yaml.safe_dump(spec, sort_keys=False))
    return {"name": name, "kind": spec["kind"], "path": str(target),
            "text": f"{name}: {spec['kind']}\n"}


def list_executors(home):
    home.require_initialized()
    root = home.root / "executors"
    rows = []
    if root.is_dir():
        for path in sorted(root.glob("*.yaml")):
            spec = load_executor(home, path.stem)
            rows.append({"name": spec["name"], "kind": spec["kind"], "machine_class": spec.get("machine_class")})
    lines = [f"{row['name']}  {row['kind']}  {row['machine_class'] or ''}".rstrip() for row in rows]
    return {"executors": rows, "text": ("\n".join(lines) + "\n") if lines else "No executors.\n"}


def show_executor(home, name):
    spec = load_executor(home, name)
    text = yaml.safe_dump({key: spec[key] for key in ("kind", "partition", "account", "max_queued", "array_limit",
                                                      "data_root", "preamble", "machine_class") if spec.get(key) not in (None, "")},
                          sort_keys=False)
    return {"name": name, "executor": spec, "text": text}


def cached_fetch(home, card):
    """Paths of fetched items. Missing items fail preflight."""
    found = {}
    for item in card.get("fetch") or []:
        path = _fetch_path(home, item)
        if path is None:
            raise ForgeError(f"Fetch {item['id']} is not in the cache.",
                             hint="jforge bench prefetch CARD")
        found[item["id"]] = {"path": str(path), "sha256": item["sha256"]}
    return found


def prefetch(home, path):
    """Download every fetch item into the shared download cache and unpack it."""
    from . import bench_cards
    home.require_initialized()
    card = bench_cards.load(path)
    if not card["fetch"]:
        return {"fetched": [], "text": "No fetch items.\n"}
    rows = []
    logs = home.root / "cache" / "fetch-logs"
    logs.mkdir(parents=True, exist_ok=True)
    for item in card["fetch"]:
        archive = verified_archive(home, {"url": item["url"], "sha256": item["sha256"]}, logs / item["id"])
        if item.get("unpack"):
            target = home.root / "cache" / "fetch" / f"{item['id']}-{item['sha256']}"
            marker = target / ".jforge-fetch"
            if not marker.is_file():
                if target.exists():
                    shutil.rmtree(target)
                from .downloads import unpack
                unpack(archive, target)
                marker.write_text(item["sha256"] + "\n")
            stored = target
        else:
            stored = archive
        rows.append({"id": item["id"], "path": str(stored), "sha256": item["sha256"]})
    lines = [f"{row['id']}  {row['path']}" for row in rows]
    return {"fetched": rows, "text": "\n".join(lines) + "\n"}


def coverage_report(card, *, required):
    """Run the coverage list. A gap is an error. A missing program is an error only for a gate."""
    spec = card.get("coverage")
    if not spec:
        return None
    argv = []
    root = Path(card["dir"])
    for part in spec["list"]:
        path = Path(part)
        if path.is_absolute() or "/" in part:
            path = path if path.is_absolute() else root / path
            argv.append(str(path))
        else:
            argv.append(part)
    if shutil.which(argv[0]) is None and not Path(argv[0]).is_file():
        if required:
            raise ForgeError(f"Coverage command is not runnable: {argv[0]}")
        return {"status": "not checked", "reason": f"cannot run {argv[0]}"}
    result = subprocess.run(argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        raise ForgeError(f"Coverage command failed (exit {result.returncode}).\n{result.stdout}")
    from . import bench_cards
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    cases = {item["name"] for item in bench_cards._discover(card)}
    exclude = spec.get("exclude") or {}
    missing = [name for name in names if name not in cases and name not in exclude]
    if missing:
        raise ForgeError("Coverage gap: " + ", ".join(missing),
                         hint="Add a case or an exclude reason.")
    return {"status": "checked", "names": names, "excluded": sorted(exclude)}


def run_slurm(home, directory, prepared, *, detach, progress=None):
    """Submit ready tasks. Detach returns after the first wave; otherwise wait."""
    spec = prepared["executor_spec"]
    _save_jobs(directory, {"jobs": [], "tasks": {}})
    while True:
        try:
            outcome = reconcile(home, directory, prepared, progress=progress)
        except KeyboardInterrupt:
            outcome = _public_running(directory, prepared)
            outcome["interrupted"] = True
            return outcome
        if outcome["status"] != "running" or detach:
            if detach and outcome["status"] == "running":
                outcome["detached"] = True
            return outcome
        time.sleep(_poll())


def reconcile(home, directory, prepared, progress=None):
    """One pass: collect finished tasks, retry lost ones, submit what is now ready."""
    from . import bench
    with bench.builds.file_lock(directory / "run.lock", blocking=True, message=f"Bench run is busy: {directory.name}"):
        record = _load_jobs(directory)
        _collect(prepared, directory, record)
        _skip_after_failure(prepared)
        _run_ready_compares(home, directory, prepared)
        _collect(prepared, directory, record)
        if _finished(prepared):
            status = bench._finish(home, directory, prepared, None)
            return bench._public_result(directory, status)
        _submit_ready(home, directory, prepared, record, progress)
        _save_jobs(directory, record)
        bench.write_json(directory / "result.json", {**read_json(directory / "result.json"), "status": "running"})
        return _public_running(directory, prepared)


def exec_task(run_dir, task_id):
    """Run one array task on a compute node. The directory layout is the state."""
    from . import bench
    from .storage import ForgeHome
    run_dir = Path(run_dir).resolve()
    home = ForgeHome(run_dir.parent.parent)
    request = read_json(run_dir / "bench.json")
    case_name, step_name, index_text = task_id.split("/")
    index = int(index_text)
    prepared = bench._prepare_saved(home, request)
    prepared["run_id"] = run_dir.name
    case = next(item for item in prepared["cases"] if item["name"] == case_name)
    step = next(item for item in prepared["card"]["steps"] if item["name"] == step_name)
    unit = next(item for item in prepared["units"] if item["case"] == case_name and item["step"] == step_name)
    repeat = next(item for item in unit["repeats"] if item["index"] == index)
    root = bench._step_dir(prepared, case_name, step_name)
    work_root = root.with_name(root.name + ".new") if unit["replace"] else root
    env = bench._base_environment(prepared)
    status = bench._run_repeat(home, run_dir, prepared, case, step, unit, repeat, work_root, env, None)
    if status != "passed":
        raise SystemExit(1)


def watch(home, run_id, progress=None):
    from . import bench
    directory = bench._run_dir(home, run_id)
    request = read_json(directory / "bench.json")
    while True:
        result = read_json(directory / "result.json")
        if result.get("status") != "running":
            return bench.status(home, run_id)
        prepared = bench._prepare_saved(home, request)
        prepared["run_id"] = run_id
        try:
            outcome = reconcile(home, directory, prepared, progress=progress)
        except KeyboardInterrupt:
            result = read_json(directory / "result.json") if (directory / "result.json").is_file() else {}
            return {"run_id": run_id, "status": result.get("status", "running"), "interrupted": True,
                    "text": f"{request['name']} {request.get('profile', '')} {result.get('status', 'running')}\n"}
        if outcome["status"] != "running":
            return outcome
        time.sleep(_poll())


def cancel(home, run_id, *, yes=False):
    from . import bench
    if not yes:
        raise ForgeError("Refusing to cancel a bench run without --yes.")
    directory = bench._run_dir(home, run_id)
    request = read_json(directory / "bench.json")
    result = read_json(directory / "result.json") if (directory / "result.json").is_file() else {}
    if result.get("status") != "running":
        raise ForgeError(f"Run is not running: {run_id}")
    record = _load_jobs(directory)
    for job in record.get("jobs") or []:
        subprocess.run(["scancel", str(job["id"])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    prepared = bench._prepare_saved(home, request)
    status = bench._finish(home, directory, prepared, "cancelled")
    return bench._public_result(directory, status)


def forget_failed(directory):
    """Drop failed task state so resume --retry-failed can submit those tasks again."""
    record = _load_jobs(directory)
    tasks = directory / "tasks"
    if tasks.is_dir():
        for case_dir in tasks.iterdir():
            if not case_dir.is_dir():
                continue
            for task in case_dir.iterdir():
                state_path = task / "state.json"
                if not state_path.is_file():
                    continue
                try:
                    state = read_json(state_path)
                except ForgeError:
                    continue
                if state.get("status") != "failed":
                    continue
                state_path.unlink()
                step, _, index = task.name.rpartition(".")
                if not index.isdigit():
                    continue
                key = f"{case_dir.name}/{step}/{int(index)}"
                (record.get("tasks") or {}).pop(key, None)
    _save_jobs(directory, record)


def _validate_executor(raw, name):
    if not isinstance(raw, dict):
        raise ForgeError(f"Executor {name} must be a mapping.")
    unknown = set(raw) - EXECUTOR_FIELDS
    if unknown:
        raise ForgeError("Unknown executor fields: " + ", ".join(sorted(unknown)))
    kind = raw.get("kind", "local")
    if kind not in {"local", "slurm"}:
        raise ForgeError(f"Executor kind must be local or slurm: {kind}")
    spec = {"kind": kind, "machine_class": raw.get("machine_class"), "data_root": raw.get("data_root"),
            "partition": raw.get("partition"), "account": raw.get("account"), "preamble": raw.get("preamble") or "",
            "max_queued": raw.get("max_queued", 400), "array_limit": raw.get("array_limit", 1000)}
    if spec["machine_class"] is not None and (not isinstance(spec["machine_class"], str) or not spec["machine_class"].strip()):
        raise ForgeError("machine_class must be a string.")
    if spec["data_root"] is not None and (not isinstance(spec["data_root"], str) or not spec["data_root"]):
        raise ForgeError("data_root must be a path.")
    if not isinstance(spec["preamble"], str):
        raise ForgeError("preamble must be a string.")
    for key in ("max_queued", "array_limit"):
        if isinstance(spec[key], bool) or not isinstance(spec[key], int) or spec[key] < 1:
            raise ForgeError(f"{key} must be a positive integer.")
    if kind == "slurm":
        if not isinstance(spec["partition"], str) or not spec["partition"]:
            raise ForgeError("A Slurm executor needs a partition.")
        if not spec["data_root"]:
            raise ForgeError("A Slurm executor needs data_root on the shared filesystem.")
        if not spec["machine_class"]:
            raise ForgeError("A Slurm executor needs machine_class.")
    return spec


def _fetch_path(home, item):
    archive = home.root / "cache" / "downloads" / item["sha256"]
    if item.get("unpack"):
        target = home.root / "cache" / "fetch" / f"{item['id']}-{item['sha256']}"
        if (target / ".jforge-fetch").is_file() and archive.is_file():
            return target
        return None
    if archive.is_file():
        return archive
    return None


def _collect(prepared, directory, record):
    """Copy task state from disk into the in-memory units and apply Slurm outcomes."""
    from . import bench
    tasks = record.setdefault("tasks", {})
    for unit in prepared["units"]:
        if unit.get("compare"):
            continue
        step = _step(prepared, unit["step"])
        statuses = []
        for repeat in unit["repeats"]:
            key = _task_key(unit["case"], unit["step"], repeat["index"])
            slot = _slot(prepared, unit, repeat["index"])
            state = _task_state(directory, unit["case"], unit["step"], repeat["index"])
            done = bench._read_done(slot)
            if repeat["action"] in {"reuse", "keep"} and done:
                statuses.append("reused")
                repeat["directory"] = str(slot)
                repeat["done"] = done
                bench._record_task(directory, unit["case"], unit["step"], repeat["index"], "reused", slot)
                continue
            if state and state.get("status") in {"passed", "failed"}:
                statuses.append("reused" if state["status"] == "passed" and repeat["action"] == "reuse" else state["status"])
                if state["status"] == "passed":
                    statuses[-1] = "passed"
                    repeat["directory"] = str(slot)
                    repeat["done"] = done or bench._read_done(slot)
                else:
                    statuses[-1] = "failed"
                continue
            outcome = _slurm_outcome(record, key)
            info = tasks.get(key) or {}
            if outcome in ACTIVE or outcome is None and info.get("job"):
                if outcome in ACTIVE:
                    statuses.append("pending")
                    continue
            if outcome in FAILED_INFRA:
                _fail_task(directory, unit, repeat, f"{outcome}: adjust resources")
                statuses.append("failed")
                continue
            if outcome in LOST or (info.get("job") and outcome is None):
                attempts = info.get("attempts") or 0
                if info.get("resubmit"):
                    statuses.append("pending")
                    continue
                if attempts <= step.get("retries", 1):
                    info["resubmit"] = True
                    tasks[key] = info
                    statuses.append("pending")
                    continue
                if info.get("job"):
                    _fail_task(directory, unit, repeat, "lost after retries")
                    statuses.append("failed")
                    continue
            statuses.append("pending" if repeat["action"] == "run" else "pending")
        if statuses and all(item in {"passed", "reused"} for item in statuses):
            if unit["replace"]:
                bench._commit_new(bench._step_dir(prepared, unit["case"], unit["step"]))
                for repeat in unit["repeats"]:
                    slot = bench._step_dir(prepared, unit["case"], unit["step"]) / f"r{repeat['index']:03d}"
                    repeat["directory"] = str(slot)
                    repeat["done"] = bench._read_done(slot)
            unit["status"] = "passed"
        elif "failed" in statuses:
            unit["status"] = "failed"
        else:
            unit["status"] = None


def _slurm_outcome(record, key):
    """Scheduler state for one task, or None when it has not been submitted."""
    info = (record.get("tasks") or {}).get(key)
    if not info or not info.get("job"):
        return None
    job = next((item for item in record.get("jobs") or [] if item["id"] == info["job"]), None)
    if job is None:
        return None
    if _queued(job["id"]):
        return "RUNNING"
    states = _accounted(job["id"], job.get("indexes") or [])
    index = int(key.rsplit("/", 1)[-1])
    return states.get(index) or states.get(None) or "MISSING"


def _queued(job_id):
    result = subprocess.run(["squeue", "-h", "-j", str(job_id), "-o", "%i"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return bool(result.stdout.strip())


def _accounted(job_id, indexes):
    result = subprocess.run(["sacct", "-n", "-P", "-j", str(job_id), "--format=JobID,State"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    found = {}
    parent = None
    for line in result.stdout.splitlines():
        if "|" not in line:
            continue
        ident, state = line.split("|", 1)
        state = state.split()[0]
        if "_" in ident:
            suffix = ident.split("_", 1)[1].split(".", 1)[0]
            if suffix.isdigit():
                found[int(suffix)] = state
        elif ident.split(".")[0] == str(job_id):
            parent = state
    if not found and parent:
        for index in indexes:
            found[index] = parent
        found[None] = parent
    return found


def _submit_ready(home, directory, prepared, record, progress):
    spec = prepared["executor_spec"]
    active = _active_jobs(record)
    tasks = record.setdefault("tasks", {})
    for unit in prepared["units"]:
        if unit.get("compare") or unit.get("status") in {"passed", "failed", "skipped"}:
            continue
        if not _upstream_done(prepared, unit):
            continue
        pending = []
        for repeat in unit["repeats"]:
            key = _task_key(unit["case"], unit["step"], repeat["index"])
            info = tasks.get(key) or {}
            if repeat["action"] not in {"run", "mismatch"} and not info.get("resubmit"):
                continue
            state = _task_state(directory, unit["case"], unit["step"], repeat["index"])
            if state and state.get("status") in {"passed", "failed"}:
                continue
            if info.get("job") and not info.get("resubmit") and _slurm_outcome(record, key) in ACTIVE.union({None}):
                if _slurm_outcome(record, key) == "RUNNING":
                    continue
            if info.get("resubmit") or not info.get("job"):
                pending.append(repeat["index"])
        if not pending:
            continue
        chunks = [pending[start:start + spec["array_limit"]] for start in range(0, len(pending), spec["array_limit"])]
        for indexes in chunks:
            if len(active) >= spec["max_queued"]:
                return
            job_id = _sbatch(directory, prepared, unit, indexes)
            active.add(job_id)
            record.setdefault("jobs", []).append({"id": job_id, "case": unit["case"], "step": unit["step"],
                                                  "indexes": indexes, "submitted_at": now()})
            for index in indexes:
                key = _task_key(unit["case"], unit["step"], index)
                info = tasks.get(key) or {}
                info.update(job=job_id, attempts=(info.get("attempts") or 0) + 1, resubmit=False)
                tasks[key] = info
            if progress:
                progress(f"submitted {unit['case']} {unit['step']} job {job_id} ({len(indexes)} tasks)")


def _sbatch(directory, prepared, unit, indexes):
    spec = prepared["executor_spec"]
    step = _step(prepared, unit["step"])
    script = directory / "slurm" / f"{unit['case']}-{unit['step']}-{indexes[0]}.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#!/bin/bash", f"#SBATCH --job-name=jforge-{unit['case']}-{unit['step']}",
             f"#SBATCH --partition={spec['partition']}", f"#SBATCH --cpus-per-task={step['resources']['cores']}"]
    if spec.get("account"):
        lines.append(f"#SBATCH --account={spec['account']}")
    memory = step["resources"].get("memory")
    if memory:
        lines.append(f"#SBATCH --mem={memory}")
    from . import bench_cards
    seconds = bench_cards.duration_seconds(step["resources"].get("time"))
    if seconds:
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)
        lines.append(f"#SBATCH --time={hours:02d}:{minutes:02d}:{secs:02d}")
    if step["resources"].get("gpus"):
        lines.append(f"#SBATCH --gpus={step['resources']['gpus']}")
    if len(indexes) > 1 or indexes != [0]:
        lines.append("#SBATCH --array=" + ",".join(str(index) for index in indexes))
        task = f"{unit['case']}/{unit['step']}/${{SLURM_ARRAY_TASK_ID}}"
    else:
        task = f"{unit['case']}/{unit['step']}/{indexes[0]}"
    log_dir = directory / "slurm"
    lines.append(f"#SBATCH --output={log_dir}/%A_%a.out")
    lines.append(f"#SBATCH --error={log_dir}/%A_%a.err")
    if spec.get("preamble"):
        lines.append(spec["preamble"].rstrip())
    import sys
    # The task id stays unquoted so bash expands SLURM_ARRAY_TASK_ID.
    lines.append(f"exec {shlex.quote(sys.executable)} -m jarvis_forge bench exec-task {shlex.quote(str(directory))} {task}")
    write_text(script, "\n".join(lines) + "\n")
    script.chmod(0o700)
    result = subprocess.run(["sbatch", "--parsable", str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        raise ForgeError(f"sbatch failed for {unit['case']} {unit['step']}.\n{result.stdout}")
    return result.stdout.strip().split(";")[0]


def _run_ready_compares(home, directory, prepared):
    from . import bench
    produced = {}
    for unit in prepared["units"]:
        produced.setdefault(unit["case"], {})[unit["step"]] = unit
    for unit in prepared["units"]:
        if not unit.get("compare") or unit.get("status") in {"passed", "failed"}:
            continue
        if not _upstream_done(prepared, unit):
            continue
        case = next(item for item in prepared["cases"] if item["name"] == unit["case"])
        step = _step(prepared, unit["step"])
        bench._run_compare(directory, prepared, case, step, unit, produced[unit["case"]])


def _upstream_done(prepared, unit):
    step = _step(prepared, unit["step"])
    for name in step["after"]:
        upstream = next(item for item in prepared["units"] if item["case"] == unit["case"] and item["step"] == name)
        if upstream.get("status") != "passed":
            return False
    return True


def _skip_after_failure(prepared):
    for case in {unit["case"] for unit in prepared["units"]}:
        failed = False
        for unit in prepared["units"]:
            if unit["case"] != case:
                continue
            if failed and unit.get("status") not in {"passed", "failed"}:
                unit["status"] = "skipped"
            if unit.get("status") == "failed":
                failed = True


def _finished(prepared):
    return all(unit.get("status") in {"passed", "failed", "skipped"} for unit in prepared["units"])


def _fail_task(directory, unit, repeat, reason):
    from . import bench
    task_dir = directory / "tasks" / unit["case"] / f"{unit['step']}.{repeat['index']}"
    task_dir.mkdir(parents=True, exist_ok=True)
    bench.write_json(task_dir / "state.json", {"schema": bench.TASK_SCHEMA, "status": "failed", "error": reason})


def _task_state(directory, case, step, index):
    path = directory / "tasks" / case / f"{step}.{index}" / "state.json"
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except ForgeError:
        return None


def _slot(prepared, unit, index):
    from . import bench
    root = bench._step_dir(prepared, unit["case"], unit["step"])
    if unit["replace"] and unit.get("status") != "passed":
        root = root.with_name(root.name + ".new")
    return root / f"r{index:03d}"


def _step(prepared, name):
    return next(item for item in prepared["card"]["steps"] if item["name"] == name)


def _task_key(case, step, index):
    return f"{case}/{step}/{index}"


def _active_jobs(record):
    active = set()
    for job in record.get("jobs") or []:
        if _queued(job["id"]):
            active.add(job["id"])
    return active


def _load_jobs(directory):
    path = directory / "slurm.json"
    if not path.is_file():
        return {"jobs": [], "tasks": {}}
    return read_json(path)


def _save_jobs(directory, record):
    write_json(directory / "slurm.json", record)


def _public_running(directory, prepared):
    from . import bench
    text = f"{prepared['card']['name']} {prepared['profile']['name']} running\n"
    return {"run_id": directory.name, "status": "running", "path": str(directory), "cases": [], "text": text}


def _poll():
    try:
        return max(0.0, float(os.environ.get("JFORGE_BENCH_POLL", "2")))
    except ValueError:
        return 2.0
