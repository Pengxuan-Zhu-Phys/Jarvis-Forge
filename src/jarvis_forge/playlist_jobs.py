"""Local verification queue. Test and command sources retain their own runners."""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import quote

from . import cmake, playlist_collections as collections, playlist_commands, playlist_library as library, testing
from .builds import file_lock
from .paths import named_path
from .storage import ForgeError, ForgeHome, digest, identifier, new_id, now, read_json, write_json


ACTIVE = {"queued", "running"}
ARTIFACT_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".html", ".pdf",
                     ".js", ".css", ".json", ".xml", ".csv", ".txt", ".log", ".md", ".woff", ".woff2"}


def _base(home, selected):
    return library._state(home, selected) / "jobs"


def _directory(home, selected, job):
    path = _base(home, selected) / identifier(job)
    if not (path / "state.json").is_file():
        raise ForgeError(f"Unknown playlist job: {job}", hint="jforge playlist jobs")
    return path


def get(home, selected, job):
    return read_json(_directory(home, selected, job) / "state.json")


def list_jobs(home, selected, limit=None):
    paths = sorted(_base(home, selected).glob("*/state.json"), reverse=True)
    rows = sorted((read_json(path) for path in paths), key=lambda row: row["submitted_at"], reverse=True)
    return rows if limit is None else rows[:limit]


def submit(home, selected, *, name=None, cases=(), jobs=1, environment=None, fresh=False):
    with file_lock(library._state(home, selected, create=True) / "submissions.lock", blocking=True):
        if name and any(row.get("playlist") == name and row["status"] in ACTIVE
                        for row in list_jobs(home, selected)):
            raise ForgeError("This album is already running or queued.", code=409)
        return _submit(home, selected, name=name, cases=cases, jobs=jobs, environment=environment, fresh=fresh)


def _submit(home, selected, *, name=None, cases=(), jobs=1, environment=None, fresh=False):
    home.require_initialized()
    if bool(name) == bool(cases):
        raise ForgeError("Choose one playlist or a selection of cases.")
    if type(jobs) is not int or not 1 <= jobs <= 8 or type(fresh) is not bool:
        raise ForgeError("Jobs must be an integer from 1 to 8 and fresh must be a boolean.")
    if environment is not None:
        identifier(library._text(environment, "Job environment"))
    inventory = library.inventory(home, selected)
    by_id = {case["id"]: case for case in inventory["cases"]}
    item = None
    if name:
        items, _ = collections.catalog(home, selected, inventory=inventory)
        item = next((entry for entry in items if entry["id"] == name), None)
        if item is None:
            raise ForgeError(f"Unknown playlist: {name}")
        if item.get("problem"):
            raise ForgeError(item["problem"])
        cases = item["members"]
    members = collections._closure(cases, by_id)
    if not members:
        raise ForgeError("Select at least one case to run.")
    units = _units(inventory, by_id, members, environment or (item or {}).get("environment"))
    # A live profile entry must retain its gate semantics in jforge test run.
    profile_ref = name if item and not item.get("stored") else None
    if item and len(item.get("entries", [])) == 1:
        profile_ref = item["entries"][0].get("profile")
    if profile_ref and len(units) == 1:
        scope, profile = profile_ref.rsplit("@", 1)
        if scope in {f"{units[0]['project']}/{units[0]['source']['id']}", "library", by_id[members[0]]["shelf"]}:
            units[0]["profile"] = profile
    job = new_id("job", root=_base(home, selected))
    directory = _base(home, selected) / job
    initial = {"id": job, "label": item["title"] if item else "Selected cases", "playlist": name,
               "status": "queued", "submitted_at": now(), "jobs": jobs,
               "cases": [{**{key: by_id[case][key] for key in ("id", "project", "kind")},
                          "title": by_id[case].get("title", case.rsplit("/", 1)[1]),
                          "status": "queued", "unit": index}
                         for index, unit in enumerate(units) for case in unit["cases"]]}
    write_json(directory / "request.json", {"units": units, "jobs": jobs, "fresh": fresh})
    write_json(directory / "state.json", initial)
    try:
        with (directory / "runner.log").open("wb") as log:
            subprocess.Popen([sys.executable, "-m", "jarvis_forge.playlist_jobs", str(directory.parent.parent), str(home.root)],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except OSError as exc:
        _finish(directory, initial, "failed", str(exc))
        raise ForgeError(f"Could not start playlist runner: {exc}", code=1) from exc
    return initial


def _units(inventory, by_id, members, environment):
    projects = {project["id"]: project for project in inventory["projects"]}
    grouped = defaultdict(list)
    for case_id in members:
        case = by_id[case_id]
        grouped[(case["project"], case["source"])].append(case_id)
    units = []
    for (pid, sid), ids in grouped.items():
        project = projects[pid]
        source = next(item for item in project["sources"] if item["id"] == sid)
        unit = {"project": pid, "root": project["root"], "source": source,
                "environment": environment or source.get("env") or project.get("environment"), "cases": ids}
        if source["kind"] == "test":
            card = (Path(project["root"]) / source["path"]).resolve()
            if not card.is_relative_to(project["root"]):
                raise ForgeError(f"Test card leaves the checkout: {card}")
            revision = by_id[ids[0]]["sha256"]
            if digest(card) != revision:
                raise ForgeError(f"Test card changed during playlist submission: {card}")
            loaded = testing.load_card(card)
            names = [item.rsplit("/", 1)[1] for item in ids]
            testing.check_intersections(loaded, testing.select(loaded, groups=names)["ids"])
            unit.update(card=str(card), sha256=revision)
        units.append(unit)
    return units


def cancel(home, selected, job, *, only_if_queued=False):
    directory = _directory(home, selected, job)
    with file_lock(directory / "state.lock", blocking=True):
        state = read_json(directory / "state.json")
        if only_if_queued and state["status"] != "queued":
            raise ForgeError("This album has started. Open its run to stop it.", code=409)
        if state["status"] == "queued":
            _finish(directory, state, "interrupted", "Cancelled in queue.")
        elif state["status"] == "running":
            (directory / "cancel").touch(mode=0o600)
        return state


def wait_job(home, selected, job):
    directory = _directory(home, selected, job)
    while (state := read_json(directory / "state.json"))["status"] in ACTIVE:
        time.sleep(0.15)
    return state


def tail(home, selected, job, case):
    directory = _directory(home, selected, job)
    state = read_json(directory / "state.json")
    row = next((item for item in state["cases"] if item["id"] == case), None)
    if row is None:
        raise ForgeError(f"Case is not part of this job: {case}")
    logs = directory / f"unit-{row['unit']}" / "output.log"
    additional = []
    if row.get("run_id") and row["kind"] != "command":
        step = identifier(case.rsplit("/", 1)[1])
        step_dir = named_path(home.run_path(row.get("reused_from") or row["run_id"]) / "steps", step)
        for stream in ("stdout", "stderr"):
            path = step_dir / f"{stream}.log"
            if not path.is_file():
                path = step_dir / f"command-1.{stream}.log"
            if path.is_file():
                additional.append(path)
    texts = []
    for path in [logs, *additional]:
        if path.is_file():
            with path.open("rb") as stream:
                chunk = stream.read(24_000) if path.stat().st_size <= 24_000 else _tail_bytes(stream)
            data = chunk.decode("utf-8", "replace")
            if data.strip():
                texts.append(data)
    return "\n".join(texts)


def _tail_bytes(stream):
    stream.seek(-24_000, os.SEEK_END)
    return stream.read()


def _artifact_roots(home, selected, job):
    directory = _directory(home, selected, job)
    state = read_json(directory / "state.json")
    result = []
    for row in state["cases"]:
        root = directory / f"unit-{row['unit']}"
        if row.get("run_id") and row["kind"] != "command":
            step = identifier(row["id"].rsplit("/", 1)[1])
            run = identifier(row.get("reused_from") or row["run_id"])
            root = named_path(home.run_path(run) / "steps", step)
        # Only runner-owned output can be served, never paths in project checkout.
        if root.resolve().is_relative_to(home.root.resolve()):
            result.append((row["id"], root))
    return result


def artifact_path(home, selected, job, index, relative):
    roots = _artifact_roots(home, selected, job)
    if not index.isdecimal() or int(index) >= len(roots):
        raise ForgeError("Unknown artifact case.", code=404)
    root = roots[int(index)][1].resolve()
    path = root / relative
    if (not relative or Path(relative).is_absolute() or ".." in Path(relative).parts
            or path.suffix.lower() not in ARTIFACT_SUFFIXES
            or not path.resolve().is_relative_to(root) or not path.is_file()):
        raise ForgeError("Artifact is outside this job or unavailable.", code=404)
    return path


def artifacts(home, selected, job):
    result = []
    hidden = {".cache", "pytest-cache", "__pycache__", "registry", "jarvis-home", ".git"}
    media = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".html", ".pdf"}
    for index, (case, root) in enumerate(_artifact_roots(home, selected, job)):
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in hidden and not (Path(directory) / name).is_symlink())
            for name in sorted(names):
                path = Path(directory) / name
                if path.suffix.lower() not in media and name not in {"result.json", "junit.xml", "checks.json"}:
                    continue
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    continue
                try:
                    if path.suffix.lower() == ".png":
                        with path.open("rb") as stream:
                            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                                continue
                    size = path.stat().st_size
                except OSError:
                    # Test cleanup can remove a temporary output during a poll.
                    continue
                # PNG is the gallery preview; alternate formats stay available by URL.
                if path.suffix.lower() in {".svg", ".pdf", ".jpeg", ".jpg"} and path.with_suffix(".png").exists():
                    continue
                relative = path.relative_to(root).as_posix()
                result.append({"case": case, "name": relative, "bytes": size,
                               "preview": path.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"},
                               "url": f"/api/jobs/{quote(job, safe='')}/artifacts/{index}/{quote(relative, safe='/')}"})
    return result


def _finish(directory, state, status, reason=None):
    for item in state["cases"]:
        if item["status"] in ACTIVE:
            item.update(status="interrupted" if status == "interrupted" else "not_run", reason=reason)
    state.update(status=status, finished_at=now(), reason=reason)
    write_json(directory / "state.json", state)


def _signal(process, value):
    try:
        os.killpg(process.pid, value)
    except ProcessLookupError:
        pass


def _invoke(argv, directory, cwd, env, *, test, observe=None):
    log = directory / "output.log"
    output = directory / "test.json" if test else log
    with output.open("wb") as stdout, log.open("ab") as stderr:
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=stdout, stderr=stderr, start_new_session=True)
        stopped = None
        try:
            while child.poll() is None:
                if observe:
                    observe()
                if (directory.parent / "cancel").exists() and stopped is None:
                    stopped = time.monotonic()
                    _signal(child, signal.SIGINT)
                if stopped is not None and time.monotonic() - stopped > 5:
                    _signal(child, signal.SIGKILL)
                time.sleep(0.12)
        finally:
            if child.poll() is None:
                _signal(child, signal.SIGKILL)
                child.wait()
    if observe:
        observe()
    return child.returncode


def _observe_test(home, work, unit, acceptance_id):
    """Mirror the existing checker records without changing its execution protocol."""
    pointer = work / "run.json"
    if not pointer.exists():
        for path in sorted((home.root / "runs").glob("*/request.json"), reverse=True):
            request = read_json(path)
            if request.get("acceptance_id") == acceptance_id:
                write_json(pointer, {"run_id": request["run_id"]})
                break
        else:
            return
    run_id = read_json(pointer)["run_id"]
    root = home.run_path(run_id)
    if not (root / "result.json").exists():
        return
    result = read_json(root / "result.json")
    rows = {row["id"]: row for row in result.get("steps", [])}
    with file_lock(work.parent / "state.lock", blocking=True):
        state = read_json(work.parent / "state.json")
        for row in state["cases"]:
            if row["id"] not in unit["cases"]:
                continue
            name = row["id"].rsplit("/", 1)[1]
            step = rows.get(name)
            row["run_id"] = run_id
            row["status"] = step["status"] if step else "running" if named_path(root / "steps", name).exists() else "queued"
            if step:
                row.update(seconds=step.get("duration_seconds"), reason=step.get("reason"),
                           counts=step.get("counts"), reused_from=step.get("reused_from"))
        state["warnings"] = list(dict.fromkeys([*state.get("warnings", []), *result.get("warnings", [])]))
        if unit.get("profile") in {"acceptance", "release"}:
            report = home.root / "checker" / "reports" / f"{acceptance_id}.json"
            if report.exists():
                summary = read_json(report)
                state["acceptance"] = {"status": "accepted" if summary["accepted"] else "not accepted",
                                       "reasons": [reason for entry in summary["entries"] for reason in entry.get("reasons", [])]}
        write_json(work.parent / "state.json", state)


def _steps(home, directory, unit, fresh, jobs):
    source = unit["source"]
    test = source["kind"] == "test"
    env = None
    ids = [case.rsplit("/", 1)[1] for case in unit["cases"]]
    if test:
        if digest(Path(unit["card"])) != unit["sha256"]:
            raise ForgeError(f"Test card changed after submission: {unit['card']}")
        argv = [sys.executable, "-m", "jarvis_forge", "--home", str(home.root), "--json", "test", "run", unit["card"]]
        if unit.get("profile"):
            argv.extend(("--profile", unit["profile"]))
        else:
            for case in ids:
                argv.extend(("--group", case))
        acceptance_id = f"{directory.parent.name}-{directory.name}"
        argv.extend(("--acceptance-id", acceptance_id))
        if unit["environment"]:
            argv.extend(("--env", unit["environment"]))
        if fresh:
            argv.append("--full")
    else:
        request = directory / "request.json"
        write_json(request, {"cases": ids, "mode": "verify", "jobs": jobs, "fresh": fresh})
        replacements = {"${request}": str(request), "${result_dir}": str(directory)}
        argv = playlist_commands.command_argv([replacements.get(part, part) for part in source["run"]])
        if unit["environment"]:
            from .environment_registry import show
            env = cmake.capture(show(home, unit["environment"], strict=True)["activation"])
    code = _invoke(argv, directory, unit["root"], env, test=test,
                   observe=(lambda: _observe_test(home, directory, unit, acceptance_id)) if test else None)
    if code in (130, -signal.SIGINT) or (directory.parent / "cancel").exists():
        return {name: {"status": "interrupted", "reason": "Cancelled."} for name in ids}
    if test:
        value = read_json(directory / "test.json")
        if not value.get("runs"):
            error = value.get("error") or {}
            raise ForgeError(error.get("message") or f"Test runner exited {code}.")
        if unit.get("profile") in {"acceptance", "release"}:
            from . import testing_run
            testing_run.summarize(home, acceptance_id, missing_ok=True)
            _observe_test(home, directory, unit, acceptance_id)
        run = value["runs"][0]
        rows = {item["id"]: {"status": item["status"], "seconds": item.get("duration_seconds"),
                             "reason": item.get("reason"), "run_id": run.get("run_id"),
                             "counts": item.get("counts"), "reused_from": item.get("reused_from")}
                for item in run.get("steps", [])}
    else:
        value = read_json(directory / "result.json")
        rows = _command_rows(value.get("cases"), ids)
        if code != 0 and all(row["status"] in {"passed", "reused", "skipped"} for row in rows.values()):
            raise ForgeError(f"Command runner exited {code}.")
    return {name: rows.get(name, {"status": "not_run", "reason": "No result recorded."}) for name in ids}


def _command_rows(items, ids):
    if not isinstance(items, list):
        raise ForgeError("Command runner result must contain a cases array.")
    rows = {}
    for item in items:
        name = item.get("id") if isinstance(item, dict) else None
        if not isinstance(name, str) or name not in ids or name in rows:
            raise ForgeError(f"Unexpected or duplicated command result ID: {name!r}")
        status = item.get("status")
        if status not in ("passed", "failed", "skipped", "interrupted", "not_run"):
            raise ForgeError(f"Unknown command result status for {name}: {status!r}")
        reason, seconds = item.get("reason"), item.get("seconds")
        rows[name] = {"status": status, "reason": reason if isinstance(reason, str) else None,
                      "seconds": seconds if type(seconds) in {int, float} and 0 <= seconds < float("inf") else None}
    return rows


def _run_unit(home, directory, index, unit, request):
    work = directory / f"unit-{index}"
    work.mkdir(mode=0o700, exist_ok=True)
    try:
        return _steps(home, work, unit, request["fresh"], request["jobs"]), None
    except (ForgeError, OSError, ValueError) as exc:
        return {}, str(exc)


def _run(home, directory, state):
    request = read_json(directory / "request.json")
    pending = dict(enumerate(request["units"]))
    running = {}
    with ThreadPoolExecutor(max_workers=request["jobs"]) as pool:
        while pending or running:
            active_projects = {unit["project"] for _, unit in running.values()}
            if not (directory / "cancel").exists():
                for index, unit in list(pending.items()):
                    if len(running) >= request["jobs"]:
                        break
                    if unit["project"] in active_projects:
                        continue
                    active_projects.add(unit["project"])
                    del pending[index]
                    running[pool.submit(_run_unit, home, directory, index, unit, request)] = (index, unit)
                    with file_lock(directory / "state.lock", blocking=True):
                        state = read_json(directory / "state.json")
                        for case in state["cases"]:
                            if case["unit"] == index:
                                case.update(status="running", started_at=now())
                        write_json(directory / "state.json", state)
            if not running:
                break
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for task in finished:
                index, _ = running.pop(task)
                result, error = task.result()
                with file_lock(directory / "state.lock", blocking=True):
                    state = read_json(directory / "state.json")
                    for case in state["cases"]:
                        if case["unit"] == index:
                            case.update(result.get(case["id"].rsplit("/", 1)[1]) or
                                        {"status": "failed", "reason": error})
                    write_json(directory / "state.json", state)
    findings = {row["status"] for row in state["cases"]}
    if (directory / "cancel").exists() or "interrupted" in findings:
        status = "interrupted"
    else:
        status = "passed" if findings <= {"passed", "reused", "skipped"} else "failed"
    _finish(directory, state, status)


def _drain(base, home):
    with file_lock(base / "runner.lock", blocking=True):
        for path in sorted((base / "jobs").glob("*/state.json"), key=lambda file: read_json(file)["submitted_at"]):
            directory = path.parent
            with file_lock(directory / "state.lock", blocking=True):
                state = read_json(path)
                if state["status"] == "running":
                    _finish(directory, state, "interrupted", "Previous playlist runner exited.")
                    continue
                if state["status"] != "queued":
                    continue
                state.update(status="running", started_at=now())
                write_json(path, state)
            try:
                _run(home, directory, state)
            except Exception as exc:
                _finish(directory, state, "failed", str(exc))


if __name__ == "__main__":
    _drain(Path(sys.argv[1]), ForgeHome(Path(sys.argv[2])))
