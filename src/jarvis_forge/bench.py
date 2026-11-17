"""Run bench cards, reuse finished steps, and record acceptance evidence.

Design: Jarvis-Books Docs/Jarvis-Forge/BENCH_DESIGN.md. Local and Slurm
executors, prefetch, coverage and ``uses: bench`` live here. Baselines, run
comparison and trend do too.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import time

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from . import __version__, bench_cards, builds, cmake
from .storage import ForgeError, digest, identifier, new_id, now, read_json, write_json, write_text
from .suite import UniqueLoader


DONE_SCHEMA = "jforge.bench.done/1"
TASK_SCHEMA = "jforge.bench.task/1"


def validate(path):
    card = bench_cards.load(path)
    found = _all_cases(card)
    bench_cards.check_references(card, found)
    for name in card["profiles"]:
        bench_cards.select_cases(card, profile=name)
    runnable = [item["name"] for item in found if not item.get("skip")]
    coverage = None
    if card["coverage"]:
        from . import bench_exec
        report = bench_exec.coverage_report(card, required=False)
        coverage = report["status"] if report else "not checked"
    return {"name": card["name"], "path": card["path"], "sha256": card["sha256"],
            "cases": runnable,
            "skipped": [item["name"] for item in found if item.get("skip")],
            "steps": [step["name"] for step in card["steps"]],
            "profiles": sorted(card["profiles"]),
            "coverage": coverage,
            "text": f"{card['name']}: {len(runnable)} cases, {len(card['steps'])} steps\n"}


def plan(home, path, *, profile=None, cases=(), tags=(), redo=(), keep=(), verify_full=False,
         environment_name=None, no_build=False, executor="local"):
    prepared = _prepare(home, path, profile=profile, cases=cases, tags=tags, redo=redo, keep=keep,
                        verify_full=verify_full, environment_name=environment_name, intent="plan",
                        executor=executor)
    if prepared["blocked"]:
        raise ForgeError(_block_message(prepared))
    _coverage_gate(prepared)
    return _plan_view(prepared)


def run(home, path, *, profile=None, cases=(), tags=(), jobs=1, redo=(), keep=(), verify_full=False,
        environment_name=None, no_build=False, executor="local", progress=None, baseline=None,
        rerun_failed=None, detach=False):
    home.require_initialized()
    if jobs < 1:
        raise ForgeError("Jobs must be a positive integer.")
    partial = False
    rerun_of = None
    selected_cases = cases
    selected_profile = profile
    selected_tags = tags
    if rerun_failed:
        if profile or cases or tags:
            raise ForgeError("--rerun-failed takes its cases from the finished run.",
                             hint="Run the card again without --rerun-failed for a result that can pass the gate.")
        previous = _finished_bench(home, rerun_failed)
        card = bench_cards.load(path)
        if previous["name"] != card["name"]:
            raise ForgeError(f"Run {rerun_failed} belongs to {previous['name']}.")
        selected_cases = [item["name"] for item in previous["report"]["cases"]
                          if item["status"] in {"failed", "error", "incomplete"}]
        if not selected_cases:
            raise ForgeError(f"Run {rerun_failed} has no failed or incomplete cases.")
        selected_profile = previous["profile"]
        selected_tags = ()
        partial = True
        rerun_of = rerun_failed
    if baseline:
        baseline_request = read_json(_run_dir(home, baseline) / "bench.json")
        if baseline_request.get("kind") != "bench":
            raise ForgeError(f"Run {baseline} is not a bench run.")
    prepared = _prepare(home, path, profile=selected_profile, cases=selected_cases, tags=selected_tags,
                        redo=redo, keep=keep, verify_full=verify_full, environment_name=environment_name,
                        intent="dry" if no_build else "run", executor=executor)
    if prepared["blocked"]:
        raise ForgeError(_block_message(prepared))
    _coverage_gate(prepared)
    if baseline and baseline_request["name"] != prepared["card"]["name"]:
        raise ForgeError(f"Run {baseline} belongs to {baseline_request['name']}.")
    prepared["partial"] = partial
    prepared["rerun_of"] = rerun_of
    prepared["baseline_run"] = baseline
    run_id = new_id("bench")
    prepared["run_id"] = run_id
    directory = home.root / "runs" / run_id
    directory.mkdir(parents=True)
    request = _request(prepared, run_id, jobs=jobs, executor=executor, cases=selected_cases, tags=selected_tags)
    write_json(directory / "request.json", request)
    write_json(directory / "bench.json", {**request, "card_snapshot": prepared["card"]["sha256"]})
    write_json(directory / "plan.json", _plan_view(prepared))
    write_json(directory / "result.json", {**request, "status": "running"})
    if prepared["executor_spec"]["kind"] == "slurm":
        from . import bench_exec
        outcome = bench_exec.run_slurm(home, directory, prepared, detach=detach, progress=progress)
        if outcome.get("interrupted"):
            raise KeyboardInterrupt
        return outcome
    try:
        with builds.file_lock(directory / "run.lock", blocking=True, message=f"Bench run is busy: {run_id}"):
            _execute(home, directory, prepared, jobs=jobs, progress=progress)
    except KeyboardInterrupt:
        _finish(home, directory, prepared, "interrupted")
        raise
    status = _finish(home, directory, prepared, None)
    return _public_result(directory, status)


def verify(home, path, *, step, profile=None, cases=(), tags=(), environment_name=None):
    """Re-run verify on finished repeats. A failure marks that repeat missing."""
    home.require_initialized()
    if not step:
        raise ForgeError("Name the step to verify.")
    prepared = _prepare(home, path, profile=profile, cases=cases, tags=tags, redo=(), keep=(),
                        verify_full=False, environment_name=environment_name, intent="dry")
    spec = next((item for item in prepared["card"]["steps"] if item["name"] == step), None)
    if spec is None:
        raise ForgeError(f"Unknown step: {step}")
    if spec.get("compare"):
        raise ForgeError(f"{step} is a compare step; it runs on every bench run.")
    if not spec.get("verify"):
        raise ForgeError(f"{step} has no verify command.")
    checked = failed = 0
    for case in prepared["cases"]:
        if case.get("skip"):
            continue
        count = _repeat_count(prepared["card"], case, spec, prepared["software"])
        root = _step_dir(prepared, case["name"], step)
        for index in range(count):
            slot = root / f"r{index:03d}"
            done = _read_done(slot)
            if not done:
                continue
            checked += 1
            code, output = _invoke(spec["verify"], _task_bindings(
                prepared, case, spec, index, count, slot), slot, prepared, log=slot / ".jforge" / "verify-again.log")
            if code == 0:
                done.pop("verify_status", None)
                done["verify"] = {"exit": 0, "summary": _summary_line(output)}
            else:
                failed += 1
                done["verify_status"] = "failed"
                done["verify"] = {"exit": code, "summary": _summary_line(output)}
            write_json(slot / ".jforge" / "done.json", done)
    return {"step": step, "checked": checked, "failed": failed}


def status(home, run_id):
    home.require_initialized()
    directory = _run_dir(home, run_id)
    request = read_json(directory / "bench.json")
    if request.get("kind") != "bench":
        raise ForgeError("This run is not a bench run.")
    result = read_json(directory / "result.json") if (directory / "result.json").is_file() else {}
    if result.get("status") == "running" and request.get("executor_kind") == "slurm":
        from . import bench_exec
        prepared = _prepare_saved(home, request)
        prepared["run_id"] = run_id
        bench_exec.reconcile(home, directory, prepared)
        result = read_json(directory / "result.json") if (directory / "result.json").is_file() else {}
    tasks = _task_rows(directory)
    return {"run_id": run_id, "status": result.get("status", request.get("status", "running")),
            "name": request["name"], "profile": request["profile"], "tasks": tasks,
            "text": _status_text(request, result, tasks)}


def card_status(home, path, *, profile=None, environment_name=None):
    """Gate read: the latest run of this card and profile, without executing it."""
    card = bench_cards.load(path)
    selected = bench_cards.profile_of(card, profile)
    environment = environment_name if environment_name is not None else card.get("environment")
    rows = [item for item in history(home, card=card["name"], profile=selected["name"])
            if item.get("environment") == environment and not item.get("partial")]
    latest = None
    report = None
    for item in rows:
        report_path = home.root / "runs" / item["run_id"] / "report.json"
        if not report_path.is_file():
            continue
        latest = item
        report = read_json(report_path)
        break
    if latest is None:
        return {"accepted": False, "name": card["name"], "profile": selected["name"],
                "reason": "No bench run covers this profile.", "text": "No bench run covers this profile.\n"}
    accepted = _gate(report, selected.get("require"))
    reason = None if accepted else _gate_reason(report, selected.get("require"))
    return {"accepted": accepted, "name": card["name"], "profile": selected["name"],
            "run_id": latest["run_id"], "require": selected.get("require"), "reason": reason,
            "cases": report["cases"], "text": _gate_text(card["name"], selected["name"], report, accepted, reason)}


def acceptance_check(home, path, *, profile):
    """Pass when the latest full run covers the profile, the gate holds, and software matches.

    A newer covering run whose software id differs fails the check. An older
    matching run is not used in its place.
    """
    card = bench_cards.load(path)
    selected, found = bench_cards.select_cases(card, profile=profile)
    needed = {item["name"] for item in found if not item.get("skip")}
    chosen = None
    report = None
    for item in history(home, card=card["name"], profile=selected["name"]):
        if item.get("partial") or item.get("status") in {"running", "cancelled", "interrupted"}:
            continue
        if not needed <= set(item.get("cases") or []):
            continue
        report_path = home.root / "runs" / item["run_id"] / "report.json"
        if not report_path.is_file():
            continue
        chosen = item
        report = read_json(report_path)
        break
    if chosen is None:
        raise ForgeError(f"No bench run covers {card['name']} {selected['name']}.",
                         hint=f"jforge bench run {card['path']} --profile {selected['name']}")
    if not _gate(report, selected.get("require")):
        raise ForgeError(_gate_reason(report, selected.get("require")))
    current = _resolve_software(home, card, intent="dry")
    recorded = {item["name"]: item.get("id") for item in report.get("software") or []}
    names = [card["subject"]] if card.get("subject") else list(current)
    for name in names:
        if name not in current:
            raise ForgeError(f"subject {name!r} is not a software item.")
        if recorded.get(name) != current[name]["id"]:
            raise ForgeError(f"software changed: {name} {recorded.get(name)} -> {current[name]['id']}")
    return {"status": "passed", "run_id": chosen["run_id"], "name": card["name"], "profile": selected["name"]}


def resume(home, run_id, *, retry_failed=False, jobs=None, progress=None):
    home.require_initialized()
    directory = _run_dir(home, run_id)
    request = read_json(directory / "bench.json")
    if request.get("kind") != "bench":
        raise ForgeError("This run is not a bench run.")
    current = read_json(directory / "result.json").get("status")
    if current in {"passed", "reported", "failed", "error", "incomplete"} and not retry_failed:
        raise ForgeError(f"Run already finished: {run_id}", hint="Start a new bench run to apply a changed card.")
    prepared = _prepare_saved(home, request)
    prepared["run_id"] = run_id
    if jobs:
        request["jobs"] = jobs
    if prepared["executor_spec"]["kind"] == "slurm":
        from . import bench_exec
        if retry_failed:
            bench_exec.forget_failed(directory)
        while True:
            outcome = bench_exec.reconcile(home, directory, prepared, progress=progress)
            if outcome["status"] != "running":
                return outcome
            time.sleep(bench_exec._poll())
    with builds.file_lock(directory / "run.lock", blocking=True, message=f"Bench run is busy: {run_id}"):
        _execute(home, directory, prepared, jobs=request.get("jobs", 1), progress=progress,
                 retry_failed=retry_failed)
    status_name = _finish(home, directory, prepared, None)
    return _public_result(directory, status_name)


def history(home, *, card=None, case=None, profile=None, environment=None):
    path = home.root / "bench" / "index.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if card and item.get("name") != card:
            continue
        if profile and item.get("profile") != profile:
            continue
        if environment is not None and item.get("environment") != environment:
            continue
        if case and case not in item.get("cases", []):
            continue
        rows.append(item)
    return list(reversed(rows))


def compare_runs(home, run_a, run_b):
    """Align one finished run with another. Inputs other than the subject must match."""
    left = _finished_bench(home, run_a)
    right = _finished_bench(home, run_b)
    if left["name"] != right["name"]:
        raise ForgeError(f"Runs belong to different cards: {left['name']} and {right['name']}.")
    subject = left["report"].get("subject") or right["report"].get("subject")
    changes = _runs_input_changes(left, right, subject)
    same_machine = left["report"].get("machine_class") == right["report"].get("machine_class")
    rows = []
    names = []
    for case in left["report"]["cases"]:
        if case["name"] not in names:
            names.append(case["name"])
    for case in right["report"]["cases"]:
        if case["name"] not in names:
            names.append(case["name"])
    for case_name in names:
        left_case = next((item for item in left["report"]["cases"] if item["name"] == case_name), None)
        right_case = next((item for item in right["report"]["cases"] if item["name"] == case_name), None)
        steps = []
        for case in (left_case, right_case):
            if not case:
                continue
            for step in case.get("steps") or []:
                if step["name"] not in steps:
                    steps.append(step["name"])
        for step_name in steps:
            left_items = _step_items(left_case, step_name)
            right_items = _step_items(right_case, step_name)
            for name in list(left_items) + [item for item in right_items if item not in left_items]:
                before = left_items.get(name) or {}
                after = right_items.get(name) or {}
                actual_a, actual_b = before.get("actual"), after.get("actual")
                tolerance = after.get("tolerance") or before.get("tolerance") or {}
                performance = "max_ratio" in tolerance or "min_ratio" in tolerance
                reason = None
                if performance and not same_machine:
                    reason = "different machine class"
                delta = None
                ratio = None
                if reason is None and isinstance(actual_a, (int, float)) and isinstance(actual_b, (int, float)):
                    delta = actual_b - actual_a
                    ratio = None if actual_a == 0 else actual_b / actual_a
                rows.append({"case": case_name, "step": step_name, "item": name, "a": actual_a, "b": actual_b,
                             "delta": delta, "ratio": ratio, "reason": reason})
    only_a = sorted(set(item["name"] for item in left["report"]["cases"]) - set(item["name"] for item in right["report"]["cases"]))
    only_b = sorted(set(item["name"] for item in right["report"]["cases"]) - set(item["name"] for item in left["report"]["cases"]))
    comparable = not changes
    text = _compare_text(left, right, comparable, changes, rows, only_a, only_b, same_machine)
    return {"run_a": run_a, "run_b": run_b, "name": left["name"], "comparable": comparable,
            "machine_class": [left["report"].get("machine_class"), right["report"].get("machine_class")],
            "changes": changes, "rows": rows, "only_a": only_a, "only_b": only_b, "text": text}


def trend(home, path, *, case, step, item):
    """Values of one compared item across finished runs, oldest first."""
    card = bench_cards.load(path)
    if step not in {spec["name"] for spec in card["steps"]}:
        raise ForgeError(f"Unknown step: {step}")
    rows = []
    for record in reversed(history(home, card=card["name"], case=case)):
        if record.get("partial"):
            continue
        report_path = home.root / "runs" / record["run_id"] / "report.json"
        if not report_path.is_file():
            continue
        payload = read_json(report_path)
        found = None
        for case_record in payload.get("cases") or []:
            if case_record["name"] != case:
                continue
            found = _step_items(case_record, step).get(item)
        if not found or "actual" not in found:
            continue
        software = _subject_id(payload, card.get("subject"))
        rows.append({"at": record.get("at"), "run_id": record["run_id"], "software": software,
                     "actual": found.get("actual"), "error": found.get("actual_error"),
                     "status": found.get("status"), "machine_class": payload.get("machine_class")})
    lines = [f"{card['name']} {case} {step} {item}", "at,run_id,software,machine_class,actual,error,status"]
    for row in rows:
        lines.append(",".join(_plain(row[key]) for key in
                              ("at", "run_id", "software", "machine_class", "actual", "error", "status")))
    if len(lines) == 2:
        lines.append("(no values)")
    return {"name": card["name"], "case": case, "step": step, "item": item, "rows": rows,
            "text": "\n".join(lines) + "\n"}


def data_list(home, card=None):
    """Bytes used by each stored step directory."""
    home.require_initialized()
    _, name = _data_card(card)
    root = home.root / "bench" / "data"
    rows = []
    if root.is_dir():
        cards = [root / name] if name else sorted(path for path in root.iterdir() if path.is_dir())
        for card_dir in cards:
            if not card_dir.is_dir():
                continue
            for profile_dir in sorted(path for path in card_dir.iterdir() if path.is_dir()):
                for case_dir in sorted(path for path in profile_dir.iterdir() if path.is_dir()):
                    for step_dir in sorted(path for path in case_dir.iterdir() if path.is_dir()):
                        rows.append({"card": card_dir.name, "profile": profile_dir.name, "case": case_dir.name,
                                     "step": step_dir.name, "bytes": _tree_bytes(step_dir)})
    lines = [f"{row['card']}  {row['profile']}  {row['case']}  {row['step']}  {_size(row['bytes'])}" for row in rows]
    return {"rows": rows, "text": ("\n".join(lines) + "\n") if lines else "No bench data.\n"}


def data_prune(home, card=None, *, yes=False):
    """Delete leftover partial directories and step trees the card no longer names."""
    home.require_initialized()
    if not yes:
        raise ForgeError("Refusing to prune bench data without --yes.")
    loaded, name = _data_card(card)
    root = home.root / "bench" / "data"
    if not root.is_dir():
        return {"removed": [], "text": "No bench data.\n"}
    keep = _data_keep(loaded) if loaded else None
    targets = [root / name] if name else sorted(path for path in root.iterdir() if path.is_dir())
    removed = []
    for card_dir in targets:
        if not card_dir.is_dir():
            continue
        for path in _prune_dirs(card_dir, keep):
            shutil.rmtree(path)
            removed.append(str(path.relative_to(root)))
    lines = [f"removed {len(removed)}"]
    lines.extend(f"  {item}" for item in removed)
    return {"removed": removed, "text": "\n".join(lines) + "\n"}


def report(home, run_id, *, case=None):
    directory = _run_dir(home, run_id)
    payload = read_json(directory / "report.json")
    if case:
        selected = next((item for item in payload["cases"] if item["name"] == case), None)
        if selected is None:
            raise ForgeError(f"Case not in this run: {case}")
        text = (directory / "cases" / case / "report.md").read_text(encoding="utf-8")
        return {"run_id": run_id, "case": selected, "markdown": text}
    text = (directory / "report.md").read_text(encoding="utf-8")
    return {"run_id": run_id, "report": payload, "markdown": text}


def judge(actual, expected, actual_error, expected_error, tolerance):
    """One numeric comparison. No tolerance reports the values and does not pass or fail."""
    delta = None if actual is None or expected is None else actual - expected
    sigma = None
    if delta is not None and actual_error is not None and expected_error is not None:
        scale = (actual_error ** 2 + expected_error ** 2) ** 0.5
        sigma = None if scale == 0 and delta != 0 else (0.0 if scale == 0 else delta / scale)
    if not tolerance:
        return {"status": "reported", "delta": delta, "sigma": sigma}
    ok = True
    reason = None
    if "abs" in tolerance and abs(delta) > tolerance["abs"]:
        ok = False
    if "rel" in tolerance and abs(delta) > tolerance["rel"] * abs(expected):
        ok = False
    if "sigma" in tolerance:
        if actual_error is None or expected_error is None:
            ok = False
            reason = "missing error"
        elif abs(delta) > tolerance["sigma"] * (actual_error ** 2 + expected_error ** 2) ** 0.5:
            ok = False
    if "max_ratio" in tolerance:
        if expected == 0:
            ok = False
            reason = "reference is 0"
        elif actual / expected > tolerance["max_ratio"]:
            ok = False
    if "min_ratio" in tolerance:
        if expected == 0:
            ok = False
            reason = "reference is 0"
        elif actual / expected < tolerance["min_ratio"]:
            ok = False
    return {"status": "passed" if ok else "failed", "delta": delta, "sigma": sigma, "reason": reason}


def select_items(data, select):
    """Map a dotted path, or a list of paths, onto item names and values."""
    if isinstance(select, list):
        found = {}
        for path in select:
            parts = str(path).split(".")
            node = _dig(data, parts)
            if node is not _MISSING:
                found[parts[-1]] = node
        return found
    return _walk(data, str(select).split("."))


# Preparation

def _all_cases(card):
    items = []
    for item in bench_cards._discover(card):
        items.append({**item, "settings": {**card["settings"], **item.get("settings", {})}})
    return items


def _prepare(home, path, *, profile, cases, tags, redo, keep, verify_full, environment_name, intent,
             executor="local"):
    card = bench_cards.load(path)
    selected, found = bench_cards.select_cases(card, profile=profile, cases=cases, tags=tags)
    _check_flags(card, redo, keep)
    software = _resolve_software(home, card, intent=intent)
    bench_cards.check_references(card, found, software)
    environment = _environment(home, environment_name or card.get("environment"))
    from . import bench_exec
    spec = bench_exec.load_executor(home, executor)
    fetch = bench_exec.cached_fetch(home, card) if card["fetch"] else {}
    if spec.get("data_root"):
        data_root = str(Path(spec["data_root"]).expanduser().resolve())
    else:
        data_root = str(home.root / "bench" / "data")
    machine = spec.get("machine_class") or _machine_class(home)
    data_profile = selected.get("data") or selected["name"]
    prepared = {"card": card, "profile": selected, "data_profile": data_profile, "cases": found,
                "software": software, "environment": environment, "redo": list(redo), "keep": list(keep),
                "verify_full": verify_full, "home": home, "blocked": False, "blocks": [],
                "machine_class": machine, "data_root": data_root, "executor_spec": spec, "fetch": fetch,
                "partial": False, "rerun_of": None, "baseline_run": None, "run_id": None}
    prepared["units"] = _decide(prepared)
    prepared["blocked"] = any(unit["action"] == "blocked" for unit in prepared["units"])
    return prepared


def _prepare_saved(home, request):
    path = Path(request["card"])
    card = bench_cards.load(path)
    if card["sha256"] != request["card_sha256"]:
        raise ForgeError("The bench card changed after this run started.",
                         hint="Start a new bench run to use the edited card.")
    prepared = _prepare(home, path, profile=request["profile"], cases=request.get("case_filter") or (),
                        tags=request.get("tag_filter") or (), redo=request.get("redo") or (),
                        keep=request.get("keep") or (), verify_full=request.get("verify_full", False),
                        environment_name=request.get("environment"), intent="dry",
                        executor=request.get("executor") or "local")
    if request.get("data_root"):
        prepared["data_root"] = request["data_root"]
    if request.get("machine_class"):
        prepared["machine_class"] = request["machine_class"]
    prepared["partial"] = bool(request.get("partial"))
    prepared["rerun_of"] = request.get("rerun_of")
    prepared["baseline_run"] = request.get("baseline")
    prepared["run_id"] = request.get("run_id")
    prepared["units"] = _decide(prepared)
    prepared["blocked"] = any(unit["action"] == "blocked" for unit in prepared["units"])
    return prepared


def _coverage_gate(prepared):
    if not prepared["card"].get("coverage"):
        return
    from . import bench_exec
    prepared["coverage"] = bench_exec.coverage_report(
        prepared["card"], required=bool(prepared["profile"].get("require")))


def _check_flags(card, redo, keep):
    names = {step["name"] for step in card["steps"]}
    both = set(redo) & set(keep)
    if both:
        raise ForgeError("Choose either --redo or --keep for " + ", ".join(sorted(both)) + ".")
    unknown = (set(redo) | set(keep)) - names
    if unknown:
        raise ForgeError("Unknown step: " + ", ".join(sorted(unknown)))
    for step in card["steps"]:
        if step["compare"] and step["name"] in set(redo) | set(keep):
            raise ForgeError(f"{step['name']} is a compare step; it runs on every bench run.")


# Software

def _resolve_software(home, card, *, intent):
    resolved = {}
    for name, spec in card["software"].items():
        if spec.get("build"):
            resolved[name] = _resolve_build(home, card, name, spec, intent=intent)
        else:
            resolved[name] = _resolve_source(card, name, spec)
        _check_requirement(name, spec, resolved[name])
    return resolved


def _resolve_source(card, name, spec):
    path = _source_path(card, spec["source"])
    identity = builds.source_identity({"path": str(path)}, Path(card["dir"]))
    return {"name": name, "kind": "source", "action": "record", "path": identity["path"],
            "id": f"source:{identity['sha256']}", "version": (identity.get("git") or {}).get("version"),
            "activation": [], "detail": f"source:{identity['sha256'][:12]}", "label": Path(identity["path"]).name}


def _resolve_build(home, card, name, spec, *, intent):
    reference = _build_reference(card, spec["build"])
    source = _source_path(card, spec["source"]) if spec.get("source") else None
    if intent == "run" and spec["rebuild"] == "auto":
        result = builds.execute(home, reference, parameters=spec["set"] or None, source=source, jobs=1)
        if result.get("status") == "interrupted":
            raise KeyboardInterrupt
        if result.get("status") != "passed":
            raise ForgeError(result.get("error") or f"Build failed: {spec['build']}", hint=result.get("hint"))
        output = result["outputs"].get("installation")
        if not output:
            raise ForgeError(f"{spec['build']} does not publish an installation output.")
        node_status = next(iter(result.get("nodes", {}).values()), {}).get("status")
        action = "reused" if node_status == "reused" else "built"
        return _build_item(name, spec, output, action)
    found = _installed(home, reference, parameters=spec["set"] or None, source=source)
    if found:
        return _build_item(name, spec, found, "reuse")
    if intent == "plan" and spec["rebuild"] == "auto":
        return {"name": name, "kind": "build", "action": "build", "path": None, "id": None,
                "version": None, "activation": [], "detail": "no artifact", "label": spec["build"],
                "features": []}
    raise ForgeError(f"No compatible installed artifact for {spec['build']}.",
                     hint=f"jforge build run {reference}")


def _build_item(name, spec, output, action):
    artifact = output["artifact_id"]
    return {"name": name, "kind": "build", "action": action, "path": output.get("prefix"),
            "id": f"{output['build_id']}:{artifact}", "version": output.get("version"),
            "activation": [output["activation"]] if output.get("activation") else [],
            "detail": artifact, "label": output.get("build_id") or spec["build"],
            "features": output.get("features") or [], "artifact": artifact}


def _check_requirement(name, spec, item):
    if spec.get("artifact") and item.get("artifact") and item["artifact"] != spec["artifact"]:
        raise ForgeError(f"software.{name} artifact does not match the installed build.")
    if spec.get("features") and not set(spec["features"]) <= set(item.get("features") or []):
        raise ForgeError(f"software.{name} is missing features: " + ", ".join(spec["features"]))
    if spec.get("version"):
        if not item.get("version"):
            raise ForgeError(f"software.{name} has no version to compare with {spec['version']}.")
        try:
            spec_set = SpecifierSet(spec["version"])
        except InvalidSpecifier as exc:
            raise ForgeError(f"Invalid version constraint for software.{name}: {exc}") from exc
        if Version(str(item["version"])) not in spec_set:
            raise ForgeError(f"software.{name} version {item['version']} does not satisfy {spec['version']}.")


def _build_reference(card, text):
    if "/" in text or text.endswith((".yaml", ".yml")):
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path(card["dir"]) / path
        return str(path)
    return text


def _source_path(card, text):
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(card["dir"]) / path
    path = path.resolve()
    if not path.is_dir():
        raise ForgeError(f"Local source directory missing: {path}")
    return path


def _installed(home, reference, *, parameters, source):
    """Find the artifact the next build run would reuse, without starting one."""
    selected = builds.graph(home, reference, parameters=parameters)
    builds._apply_source_override(selected, source)
    builds._check_host(home, selected)
    node = selected["nodes"][selected["root"]]
    config = node["config"]
    if "installation" not in config.get("outputs", {}):
        raise ForgeError(f"{config['id']} does not publish an installation output.")
    parent = Path(node["path"]).parent
    sources = {name: builds.source_identity(value, parent) for name, value in config.get("sources", {}).items()}
    from .apt_profiles import active_home
    from . import toolchains
    target = active_home(home)
    toolchain = toolchains.binding(target)
    captured = cmake.capture(None)
    environment = builds.safe_environment(captured)
    origin = {"inherited": True}
    if toolchain:
        environment = toolchains.environment(toolchain, environment)
        origin["toolchain"] = toolchain["id"]
    observed = cmake.tools(environment)
    context = {"tools": observed, "platform": sys.platform, "machine": os.uname().machine,
               "cxx_standard": str(node["parameters"].get("cxx_standard", "unspecified"))}
    identity = {"recipe": selected["root"], "parameters": node["parameters"], "sources": sources,
                "context": context, "environment": environment, "environment_origin": origin, "requires": {}}
    instance = builds.fingerprint(identity)
    artifact_id = identifier(config["id"] + "-" + instance[:20] + "-installation")
    manifest = home.root / "builds/artifacts" / artifact_id / "manifest.json"
    if not manifest.is_file():
        return None
    return builds.artifact(home, artifact_id)


def _environment(home, name):
    if not name:
        return {"name": None}
    from .environment_registry import show
    record = show(home, name, strict=True)
    files = record.get("files") or {}
    return {"name": name, "revision": record.get("revision"),
            "files": "sha256:" + builds.fingerprint(files), "activation": record.get("activation")}


# Decisions

def _decide(prepared):
    units = []
    upstream_ran = {}
    upstream_outputs = {}
    for case in prepared["cases"]:
        if case.get("skip"):
            continue
        produced = {}
        ran = set()
        for step in prepared["card"]["steps"]:
            unit = _decide_step(prepared, case, step, produced, ran)
            units.append(unit)
            produced[step["name"]] = unit
            if unit["action"] in {"run", "blocked"} or any(item["action"] == "run" for item in unit["repeats"]):
                ran.add(step["name"])
        upstream_ran[case["name"]] = ran
        upstream_outputs[case["name"]] = produced
    return units


def _decide_step(prepared, case, step, produced, ran):
    count = 1 if step.get("compare") else _repeat_count(prepared["card"], case, step, prepared["software"])
    redo = step["name"] in prepared["redo"]
    keep = step["name"] in prepared["keep"]
    replace = (not step["reuse"] or redo) and not step.get("compare")
    forced = any(name in ran for name in step["after"])
    root = _step_dir(prepared, case["name"], step["name"])
    work_root = root.with_name(root.name + ".new") if replace else root
    repeats = []
    changes = []
    missing = []
    if step.get("compare"):
        for index in range(count):
            repeats.append({"index": index, "action": "run", "inputs": {}, "directory": None})
        return _unit(case, step, "run", repeats, count, missing, [], False, prepared, redo, replace=False, compare=True)
    for index in range(count):
        slot = work_root / f"r{index:03d}"
        current = _inputs(prepared, case, step, index, count, produced, task=slot)
        done = _read_done(slot)
        usable = bool(done) and _outputs_present(done, slot, prepared["verify_full"])
        if usable and replace:
            own = _own_changes(done.get("inputs", {}), current)
            if own:
                raise ForgeError(f"Incomplete regeneration of {case['name']} {step['name']} has different inputs.",
                                 hint=f"Remove {work_root} or resume the run that created it.")
            repeats.append({"index": index, "action": "reuse", "inputs": current, "directory": str(slot), "done": done})
            continue
        if not usable:
            missing.append(index)
            repeats.append({"index": index, "action": "run", "inputs": current, "directory": None})
            continue
        own = _own_changes(done.get("inputs", {}), current)
        upstream_changed = (done.get("inputs") or {}).get("upstream") != current.get("upstream")
        if own:
            changes.extend(own)
            repeats.append({"index": index, "action": "mismatch", "inputs": current, "directory": str(slot), "done": done})
            continue
        if forced or upstream_changed:
            missing.append(index)
            repeats.append({"index": index, "action": "run", "inputs": current, "directory": None, "done": done})
            continue
        repeats.append({"index": index, "action": "reuse", "inputs": current, "directory": str(slot), "done": done})
    if any(item["action"] == "mismatch" for item in repeats):
        if keep and not missing:
            for item in repeats:
                item["action"] = "keep"
            action = "keep"
        else:
            action = "blocked"
    elif missing:
        action = "run"
    else:
        action = "reuse"
    return _unit(case, step, action, repeats, count, missing, changes, action == "blocked", prepared, redo,
                 replace=replace, compare=False)


def _unit(case, step, action, repeats, count, missing, changes, blocked, prepared, redo, *, replace, compare):
    return {"case": case["name"], "step": step["name"], "action": action, "repeats": repeats, "count": count,
            "missing": missing, "changes": _unique_changes(changes), "blocked": blocked,
            "detail": _step_detail(step, count, missing, action, redo, prepared),
            "compare": compare, "replace": replace}


def _own_changes(old, new):
    changes = []
    if old.get("definition") != new.get("definition"):
        changes.append({"kind": "definition", "name": "definition", "old": old.get("definition"), "new": new.get("definition")})
    for key in sorted(set((old.get("variables") or {})) | set(new.get("variables") or {})):
        if (old.get("variables") or {}).get(key) != (new.get("variables") or {}).get(key):
            changes.append({"kind": "variables", "name": key, "old": (old.get("variables") or {}).get(key),
                            "new": (new.get("variables") or {}).get(key)})
    old_files, new_files = old.get("files") or {}, new.get("files") or {}
    for key in sorted(set(old_files) | set(new_files)):
        if old_files.get(key) != new_files.get(key):
            changes.append({"kind": "files", "name": key, "old": old_files.get(key), "new": new_files.get(key)})
    old_soft, new_soft = old.get("software") or {}, new.get("software") or {}
    for key in sorted(set(old_soft) | set(new_soft)):
        if old_soft.get(key) != new_soft.get(key):
            changes.append({"kind": "software", "name": key, "old": old_soft.get(key), "new": new_soft.get(key)})
    old_fetch, new_fetch = old.get("fetch") or {}, new.get("fetch") or {}
    for key in sorted(set(old_fetch) | set(new_fetch)):
        if old_fetch.get(key) != new_fetch.get(key):
            changes.append({"kind": "fetch", "name": key, "old": old_fetch.get(key), "new": new_fetch.get(key)})
    if old.get("environment") != new.get("environment"):
        changes.append({"kind": "environment", "name": "environment", "old": old.get("environment"),
                        "new": new.get("environment")})
    return changes


def _unique_changes(changes):
    seen = []
    for item in changes:
        if item not in seen:
            seen.append(item)
    return seen


def _step_detail(step, count, missing, action, redo, prepared):
    done = count - len(missing)
    if action == "blocked":
        text = "inputs differ"
    elif action == "keep":
        text = "keep existing results"
    elif not missing and action == "reuse":
        text = f"{count}/{count} done"
    elif missing and done:
        text = f"{done} of {count} ({_ranges(missing)} missing)"
    elif redo:
        text = "redo"
    else:
        text = ""
    seconds = bench_cards.duration_seconds((step.get("resources") or {}).get("time"))
    if step["reuse"] is False and not step.get("compare") and seconds and seconds > 3600 and step["name"] not in prepared["redo"]:
        text = (text + " " if text else "") + "reuse is off for a long step"
    return text.strip()


def _ranges(indexes):
    if not indexes:
        return ""
    groups = []
    start = previous = indexes[0]
    for index in indexes[1:]:
        if index == previous + 1:
            previous = index
            continue
        groups.append((start, previous))
        start = previous = index
    groups.append((start, previous))
    parts = []
    for start, end in groups:
        parts.append(f"r{start:03d}" if start == end else f"r{start:03d}-r{end:03d}")
    return ", ".join(parts)


def _repeat_count(card, case, step, software):
    bindings = bench_cards.bindings_for(card, case, step, index=0, count=1, task=None, upstream={}, software=software)
    return bench_cards.step_repeat(step, bindings)


def _inputs(prepared, case, step, index, count, produced, task=None):
    bindings = _task_bindings(prepared, case, step, index, count, task=task, produced=produced)
    files = _stage(step, bindings, dest=None, write=False)
    upstream = {}
    for name in step["after"]:
        previous = produced.get(name)
        if not previous:
            continue
        for repeat in previous["repeats"]:
            done = repeat.get("done")
            if not done:
                slot = Path(repeat["directory"]) if repeat.get("directory") else None
                done = _read_done(slot) if slot else None
            if not done:
                continue
            for output, records in (done.get("outputs") or {}).items():
                upstream.setdefault(f"{name}.{output}", []).extend(item["sha256"] for item in records)
    available = prepared.get("fetch") or {}
    fetch = {name: available[name]["sha256"] for name in bench_cards.referenced_fetch(step, bindings) if name in available}
    return {"definition": bench_cards.definition(step),
            "variables": bench_cards.recorded_values(step, bindings),
            "files": files, "software": {name: prepared["software"][name]["id"] for name in step["software"]},
            "upstream": upstream, "fetch": fetch,
            "environment": {key: prepared["environment"][key]
                            for key in ("name", "revision", "files") if key in prepared["environment"]}}


def _task_bindings(prepared, case, step, index, count, task, produced=None):
    upstream = {}
    if produced:
        for name, unit in produced.items():
            paths = {}
            dirs = []
            for repeat in unit["repeats"]:
                directory = repeat.get("directory")
                if directory:
                    dirs.append(directory)
                done = repeat.get("done") or (_read_done(Path(directory)) if directory else None)
                if not done:
                    continue
                for output, records in (done.get("outputs") or {}).items():
                    paths.setdefault(output, []).extend(str(Path(directory) / item["path"]) for item in records)
            upstream[name] = {"outputs": paths, "dirs": dirs}
    return bench_cards.bindings_for(prepared["card"], case, step, index=index, count=count, task=task,
                                    upstream=upstream, software=prepared["software"], fetch=prepared.get("fetch"))


# Execution

def _execute(home, directory, prepared, *, jobs, progress, retry_failed=False):
    env = _base_environment(prepared)
    chains = []
    for case in prepared["cases"]:
        if case.get("skip"):
            continue
        units = [unit for unit in prepared["units"] if unit["case"] == case["name"]]
        chains.append(units)
    if jobs <= 1 or len(chains) <= 1:
        for chain in chains:
            _run_chain(home, directory, prepared, chain, env, progress, retry_failed)
        return
    from concurrent.futures import ThreadPoolExecutor
    workers = min(jobs, len(chains))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_chain, home, directory, prepared, chain, env, progress, retry_failed)
                   for chain in chains]
        for future in futures:
            future.result()


def _run_chain(home, directory, prepared, units, env, progress, retry_failed):
    failed = False
    produced = {}
    for unit in units:
        if failed:
            unit["status"] = "skipped"
            continue
        _run_unit(home, directory, prepared, unit, produced, env, progress, retry_failed)
        produced[unit["step"]] = unit
        if unit.get("status") in {"failed", "error"}:
            failed = True


def _run_unit(home, directory, prepared, unit, produced, env, progress, retry_failed):
    step = next(item for item in prepared["card"]["steps"] if item["name"] == unit["step"])
    case = next(item for item in prepared["cases"] if item["name"] == unit["case"])
    if unit["compare"]:
        _run_compare(directory, prepared, case, step, unit, produced)
        return
    root = _step_dir(prepared, case["name"], step["name"])
    lock = root.with_name(root.name + ".lock")
    with builds.file_lock(lock, blocking=True, message=f"Bench step is busy: {case['name']} {step['name']}"):
        fresh = _decide_step(prepared, case, step, produced, _ran(produced))
        if fresh["action"] == "blocked" and not retry_failed:
            unit.update(fresh)
            unit["status"] = "blocked"
            return
        work_root = root.with_name(root.name + ".new") if fresh["replace"] else root
        if fresh["action"] == "keep":
            _apply_keep(prepared, work_root, fresh, directory)
            unit.update(fresh, status="reused")
            return
        statuses = []
        for repeat in fresh["repeats"]:
            if repeat["action"] == "reuse" and not (retry_failed and False):
                statuses.append("reused")
                _record_task(directory, case["name"], step["name"], repeat["index"], "reused", repeat.get("directory"))
                continue
            if repeat["action"] == "keep":
                statuses.append("reused")
                continue
            status = _run_repeat(home, directory, prepared, case, step, fresh, repeat, work_root, env, progress)
            statuses.append(status)
            if status == "failed":
                break
        if fresh["replace"] and statuses and all(item == "passed" or item == "reused" for item in statuses) and len(statuses) == fresh["count"]:
            _commit_new(root)
            for repeat in fresh["repeats"]:
                repeat["directory"] = str(root / f"r{repeat['index']:03d}")
                repeat["done"] = _read_done(Path(repeat["directory"]))
        elif not fresh["replace"]:
            for repeat in fresh["repeats"]:
                slot = root / f"r{repeat['index']:03d}"
                if slot.is_dir():
                    repeat["directory"] = str(slot)
                    repeat["done"] = _read_done(slot)
        unit.update(fresh)
        unit["status"] = "failed" if "failed" in statuses else "passed"


def _ran(produced):
    return {name for name, unit in produced.items()
            if any(item["action"] == "run" for item in unit["repeats"]) or unit.get("status") == "failed"}


def _run_repeat(home, directory, prepared, case, step, unit, repeat, work_root, env, progress):
    index = repeat["index"]
    final = work_root / f"r{index:03d}"
    partial = work_root / f"r{index:03d}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    count = unit["count"]
    # Expand with the final path so the saved file hashes match the next decision.
    # Commands run in the partial directory and move onto that path after success.
    bindings = _task_bindings(prepared, case, step, index, count, final, produced=_previous(prepared, case["name"], step["name"]))
    files = _stage(step, bindings, partial, write=True)
    started = time.monotonic()
    log_dir = partial / ".jforge"
    log_dir.mkdir(parents=True, exist_ok=True)
    command_env = _command_env(env, prepared, step, case, partial, bindings)
    failed = None
    for number, command in enumerate(step["commands"], start=1):
        expanded = bench_cards.expand(command, bindings, where=f"{step['name']} command")
        code, output = _invoke(expanded, bindings, partial, prepared, log=log_dir / f"command-{number}.log",
                               env=command_env, already_expanded=True)
        if progress:
            progress(f"{case['name']} {step['name']} r{index:03d} command {number} exit {code}")
        if code != 0:
            failed = f"command {number} failed (exit {code})"
            break
    verify_summary = None
    if failed is None and step.get("verify"):
        code, output = _invoke(step["verify"], bindings, partial, prepared, log=log_dir / "verify.log", env=command_env)
        verify_summary = _summary_line(output)
        if code != 0:
            failed = f"verify failed (exit {code})"
    task_dir = directory / "tasks" / case["name"] / f"{step['name']}.{index}"
    task_dir.mkdir(parents=True, exist_ok=True)
    if failed:
        write_json(task_dir / "state.json", {"schema": TASK_SCHEMA, "status": "failed", "error": failed})
        note = task_dir / "partial"
        if note.exists() or note.is_symlink():
            note.unlink()
        note.symlink_to(partial, target_is_directory=True)
        return "failed"
    try:
        recorded = _collect_outputs(partial, step["outputs"])
    except ForgeError as exc:
        write_json(task_dir / "state.json", {"schema": TASK_SCHEMA, "status": "failed", "error": str(exc)})
        return "failed"
    if step["clean"]:
        _clean(partial, recorded)
    done = {"schema": DONE_SCHEMA, "bench": prepared["card"]["name"], "profile": prepared["data_profile"],
            "case": case["name"], "step": step["name"], "repeat": index,
            "seed": None if step.get("seed") is None else step["seed"] + index,
            "inputs": {**repeat["inputs"], "files": files}, "outputs": recorded,
            "verify": None if verify_summary is None else {"exit": 0, "summary": verify_summary},
            "run": directory.name, "host": socket.gethostname(), "seconds": round(time.monotonic() - started, 3),
            "finished_at": now(), "kept": [], "forge": __version__}
    write_json(log_dir / "done.json", done)
    if unit["replace"]:
        if final.exists():
            shutil.rmtree(final)
        partial.rename(final)
    else:
        _publish_partial(partial, final)
    repeat["directory"] = str(final)
    repeat["done"] = done
    repeat["action"] = "run"
    write_json(task_dir / "state.json", {"schema": TASK_SCHEMA, "status": "passed", "seconds": done["seconds"]})
    link = task_dir / "data"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(final, target_is_directory=True)
    return "passed"


def _previous(prepared, case_name, step_name):
    """Units for this case that were updated in place before this step."""
    produced = {}
    for unit in prepared["units"]:
        if unit["case"] != case_name:
            continue
        if unit["step"] == step_name:
            break
        produced[unit["step"]] = unit
    return produced


def _apply_keep(prepared, root, unit, directory):
    for repeat in unit["repeats"]:
        slot = root / f"r{repeat['index']:03d}"
        done = _read_done(slot)
        if not done:
            raise ForgeError(f"Cannot keep a missing result: {slot}")
        changes = _own_changes(done.get("inputs", {}), repeat["inputs"])
        done["inputs"] = repeat["inputs"]
        done.setdefault("kept", []).append({"run": directory.name, "changes": changes})
        write_json(slot / ".jforge" / "done.json", done)
        repeat["done"] = done
        repeat["directory"] = str(slot)
        repeat["action"] = "keep"


def _run_compare(directory, prepared, case, step, unit, produced):
    compare = step["compare"]
    try:
        _compare_case(directory, prepared, case, step, unit, produced, compare)
    except ForgeError as exc:
        unit["status"] = "failed"
        unit["compare_status"] = "error"
        unit["items"] = [{"name": step["name"], "status": "failed", "reason": str(exc)}]


def _compare_case(directory, prepared, case, step, unit, produced, compare):
    bindings = _task_bindings(prepared, case, step, 0, 1, directory, produced=produced)
    actual = bench_cards.expand(compare["actual"], bindings, where=f"{step['name']} actual")
    paths = actual if isinstance(actual, list) else [actual]
    if compare["expected"] == "baseline":
        items = _baseline_items(prepared, case, step, compare, bindings, paths)
    else:
        expected_path = bench_cards.expand(compare["expected"], bindings, where=f"{step['name']} expected")
        reference = _load_document(Path(expected_path))
        items = []
        for number, path in enumerate(paths):
            document = _load_document(Path(path))
            found = select_items(document, bench_cards.expand(compare["select"], bindings, where=f"{step['name']} select"))
            prefix = f"r{number:03d}:" if len(paths) > 1 else ""
            items.extend(_compare_document(found, reference, compare, prefix))
    statuses = {item["status"] for item in items}
    if "failed" in statuses:
        status = "failed"
    elif "passed" in statuses:
        status = "passed"
    else:
        status = "reported"
    target = directory / "cases" / case["name"]
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / f"{step['name']}.json", {"step": step["name"], "status": status, "items": items})
    unit["status"] = "passed"
    unit["compare_status"] = status
    unit["items"] = items
    for repeat in unit["repeats"]:
        repeat["action"] = "run"
        repeat["directory"] = str(target)


def _compare_document(found, reference, compare, prefix):
    default = reference.get("tolerance") or compare.get("tolerance")
    items = []
    expected = reference.get("values") or {}
    for name, spec in expected.items():
        label = prefix + str(name)
        if name not in found and str(name) not in found:
            items.append({"name": label, "status": "failed", "reason": "missing item"})
            continue
        node = found.get(name, found.get(str(name)))
        actual, actual_error = _metric(node, compare.get("value", "value"), compare.get("error", "error"))
        if isinstance(spec, dict):
            target, target_error = _metric(spec, "value", "error")
            tolerance = spec.get("tolerance") or default
        else:
            target, target_error = float(spec), None
            tolerance = default
        result = judge(actual, target, actual_error, target_error, tolerance)
        items.append({"name": label, "status": result["status"], "actual": actual, "expected": target,
                      "actual_error": actual_error, "expected_error": target_error, "delta": result["delta"],
                      "sigma": result["sigma"], "reason": result.get("reason"),
                      "source": reference.get("source"), "tolerance": tolerance})
    for name in found:
        if name in expected or str(name) in expected:
            continue
        node = found[name]
        actual, actual_error = _metric(node, compare.get("value", "value"), compare.get("error", "error"))
        items.append({"name": prefix + str(name), "status": "no reference", "actual": actual,
                      "actual_error": actual_error, "tolerance": compare.get("tolerance")})
    return items


def _metric(node, value_key, error_key):
    if isinstance(node, dict):
        if value_key not in node:
            raise ForgeError(f"Compared item has no {value_key}.")
        value = node[value_key]
        error = node.get(error_key)
    elif isinstance(node, bool) or not isinstance(node, (int, float)):
        raise ForgeError(f"Compared item is not a number: {node!r}")
    else:
        value, error = node, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ForgeError(f"Compared {value_key} is not a number.")
    if error is not None and (isinstance(error, bool) or not isinstance(error, (int, float))):
        raise ForgeError(f"Compared {error_key} is not a number.")
    return float(value), None if error is None else float(error)


def _load_document(path):
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            return json.loads(text)
        if path.suffix in {".yaml", ".yml"}:
            return yaml.load(text, Loader=UniqueLoader)
        try:
            return json.loads(text)
        except ValueError:
            return yaml.load(text, Loader=UniqueLoader)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read {path}: {exc}") from exc


# Files, processes, outputs

def _stage(step, bindings, dest, *, write):
    records = {}

    def add_file(name, data):
        if name in records:
            raise ForgeError(f"Duplicated task file: {name}")
        if name == ".jforge" or name.startswith(".jforge/"):
            raise ForgeError("A staged file cannot replace the task metadata.")
        records[name] = "sha256:" + hashlib.sha256(data).hexdigest()
        if write:
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def add_tree(name, source, render):
        key = name.rstrip("/") + "/"
        if key in records:
            raise ForgeError(f"Duplicated task file: {key}")
        rows = []
        files = [path for path in sorted(source.rglob("*")) if path.is_file()]
        rendered = []
        for path in files:
            relative = path.relative_to(source).as_posix()
            if render:
                data = bench_cards.expand(path.read_text(encoding="utf-8"), bindings, where=step["name"]).encode()
            else:
                data = path.read_bytes()
            rows.append([relative, hashlib.sha256(data).hexdigest()])
            rendered.append((relative, data))
        records[key] = "sha256:" + hashlib.sha256(json.dumps(rows).encode()).hexdigest()
        if write:
            for relative, data in rendered:
                target = dest / name / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

    for field, render in (("files", False), ("templates", True)):
        for raw in step.get(field) or []:
            expanded = bench_cards.expand(raw, bindings, where=f"{step['name']} {field}")
            if not isinstance(expanded, str):
                raise ForgeError(f"{step['name']} {field} must expand to a path.")
            contents = expanded.endswith("/")
            path = Path(expanded)
            if not path.exists():
                raise ForgeError(f"Missing {field} path: {path}")
            if contents:
                if not path.is_dir():
                    raise ForgeError(f"{field} path is not a directory: {path}")
                for child in sorted(path.iterdir()):
                    if child.is_dir():
                        add_tree(child.name, child, render)
                    elif child.is_file():
                        data = child.read_bytes()
                        if render:
                            data = bench_cards.expand(data.decode(), bindings, where=step["name"]).encode()
                        add_file(child.name, data)
                    else:
                        raise ForgeError(f"Unsupported task file: {child}")
            elif path.is_dir():
                add_tree(path.name, path, render)
            elif path.is_file():
                data = path.read_bytes()
                if render:
                    data = bench_cards.expand(data.decode(), bindings, where=step["name"]).encode()
                add_file(path.name, data)
            else:
                raise ForgeError(f"Unsupported task file: {path}")
    return records


def _base_environment(prepared):
    script = prepared["environment"].get("activation")
    if script:
        return cmake.capture(script)
    return os.environ.copy()


def _command_env(base, prepared, step, case, task, bindings):
    scripts = []
    for name in step["software"]:
        scripts.extend(prepared["software"][name].get("activation") or [])
    env = dict(base)
    for script in scripts:
        env = cmake.capture(script, environment=env)
    seed = bindings.get("seed")
    env.update({
        "BENCH_DIR": prepared["card"]["dir"],
        "CASE_DIR": case.get("dir") or "",
        "CASE_NAME": case["name"],
        "TASK_DIR": str(task),
        "SEED": "" if seed is None else str(seed),
        "REPEAT_INDEX": str(bindings["repeat"]["index"]),
    })
    extra = bench_cards.expand(step.get("env") or {}, bindings, where=f"{step['name']} env")
    for key, value in extra.items():
        env[key] = str(value)
    return env


def _invoke(command, bindings, cwd, prepared, *, log, env=None, already_expanded=False):
    if not already_expanded:
        command = bench_cards.expand(command, bindings, where="verify")
    if isinstance(command, str):
        argv = ["bash", "--noprofile", "--norc", "-c", command]
    else:
        argv = [str(part) for part in command]
    result = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout or "")
    return result.returncode, result.stdout or ""


def _summary_line(output):
    lines = [line for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _collect_outputs(task, outputs):
    recorded = {}
    for name, pattern in outputs.items():
        if any(char in pattern for char in "*?["):
            matches = sorted(path for path in task.glob(pattern) if path.exists())
        else:
            candidate = task / pattern
            matches = [candidate] if candidate.exists() else []
        if not matches:
            raise ForgeError(f"Output {name} matched nothing ({pattern}).")
        rows = []
        for match in matches:
            relative = match.relative_to(task).as_posix()
            if match.is_dir():
                rows.append({"path": relative + "/", "bytes": _tree_bytes(match), "mtime": _tree_mtime(match),
                             "sha256": _tree_sha(match)})
            elif match.is_file():
                stat = match.stat()
                rows.append({"path": relative, "bytes": stat.st_size, "mtime": stat.st_mtime,
                             "sha256": "sha256:" + digest(match)})
            else:
                raise ForgeError(f"Output {name} is not a file: {match}")
        recorded[name] = rows
    return recorded


def _outputs_present(done, task, full):
    if done.get("verify_status") == "failed":
        return False
    changed = False
    for records in (done.get("outputs") or {}).values():
        for item in records:
            path = task / item["path"]
            if not path.exists():
                return False
            if path.is_dir():
                size, mtime = _tree_bytes(path), _tree_mtime(path)
                digest_now = None
                if size != item.get("bytes"):
                    return False
                if full or mtime != item.get("mtime"):
                    digest_now = _tree_sha(path)
            else:
                stat = path.stat()
                size, mtime = stat.st_size, stat.st_mtime
                if size != item.get("bytes"):
                    return False
                digest_now = "sha256:" + digest(path) if full or mtime != item.get("mtime") else None
            if digest_now is not None and digest_now != item.get("sha256"):
                return False
            if digest_now is not None and mtime != item.get("mtime"):
                item["mtime"] = mtime
                changed = True
    if changed:
        write_json(task / ".jforge" / "done.json", done)
    return True


def _tree_bytes(path):
    return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())


def _tree_mtime(path):
    times = [path.stat().st_mtime]
    times.extend(file.stat().st_mtime for file in path.rglob("*") if file.is_file())
    return max(times) if times else path.stat().st_mtime


def _tree_sha(path):
    rows = []
    for file in sorted(path.rglob("*")):
        if file.is_file():
            rows.append([file.relative_to(path).as_posix(), digest(file)])
    return "sha256:" + hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _clean(task, outputs):
    files = set()
    directories = set()
    for records in outputs.values():
        for item in records:
            relative = item["path"]
            if relative.endswith("/"):
                directories.add(relative.rstrip("/"))
            else:
                files.add(relative)
    for path in sorted(task.rglob("*"), reverse=True):
        relative = path.relative_to(task).as_posix()
        if relative == ".jforge" or relative.startswith(".jforge/"):
            continue
        if relative in files or any(relative == directory or relative.startswith(directory + "/") for directory in directories):
            continue
        if path.is_dir():
            if any(name.startswith(relative + "/") for name in files):
                continue
            if not any(path.iterdir()):
                path.rmdir()
            continue
        path.unlink()


def _publish_partial(partial, final):
    if final.exists():
        displaced = final.with_name(final.name + ".displaced")
        if displaced.exists():
            shutil.rmtree(displaced)
        final.rename(displaced)
        partial.rename(final)
        shutil.rmtree(displaced)
    else:
        partial.rename(final)


def _commit_new(step_dir):
    new = step_dir.with_name(step_dir.name + ".new")
    if not new.is_dir():
        return
    old = step_dir.with_name(step_dir.name + ".old")
    if old.exists():
        shutil.rmtree(old)
    if step_dir.exists():
        step_dir.rename(old)
    new.rename(step_dir)
    if old.exists():
        shutil.rmtree(old)


def _read_done(directory):
    if directory is None:
        return None
    path = Path(directory) / ".jforge" / "done.json"
    if not path.is_file():
        return None
    try:
        done = read_json(path)
    except ForgeError:
        return None
    if done.get("schema") != DONE_SCHEMA:
        return None
    return done


def _step_dir(prepared, case, step):
    root = Path(prepared.get("data_root") or prepared["home"].root / "bench" / "data")
    return root / prepared["card"]["name"] / prepared["data_profile"] / case / step


def _record_task(directory, case, step, index, status, data):
    task_dir = directory / "tasks" / case / f"{step}.{index}"
    task_dir.mkdir(parents=True, exist_ok=True)
    write_json(task_dir / "state.json", {"schema": TASK_SCHEMA, "status": status})
    if data:
        link = task_dir / "data"
        if not link.exists() and not link.is_symlink():
            link.symlink_to(data, target_is_directory=True)


# Reports

def _finish(home, directory, prepared, forced):
    write_json(directory / "inputs.json", _input_snapshot(prepared))
    if forced:
        status = forced
    else:
        payload = _write_reports(directory, prepared)
        status = payload["status"]
    result_path = directory / "result.json"
    current = read_json(result_path) if result_path.is_file() else {}
    current.update(status=status, finished_at=now())
    write_json(result_path, current)
    if status != "interrupted":
        _append_index(home, directory, prepared, status)
        _update_gate_state(home, prepared, directory.name, status)
    return status


def _write_reports(directory, prepared):
    cases = []
    for case in prepared["cases"]:
        if case.get("skip"):
            cases.append({"name": case["name"], "status": "skipped", "reason": case.get("skip")})
            continue
        units = [unit for unit in prepared["units"] if unit["case"] == case["name"]]
        status = _case_status(units)
        record = {"name": case["name"], "status": status, "settings": case["settings"],
                  "steps": [{"name": unit["step"], "action": unit["action"], "status": unit.get("status"),
                             "detail": unit.get("detail"), "compare": unit.get("compare_status"),
                             "items": unit.get("items")} for unit in units]}
        case_dir = directory / "cases" / case["name"]
        case_dir.mkdir(parents=True, exist_ok=True)
        write_json(case_dir / "case.json", record)
        if prepared["card"].get("report"):
            _custom_report(prepared, case_dir)
        else:
            write_text(case_dir / "report.md", _case_markdown(prepared["card"]["name"], record))
        cases.append(record)
    statuses = [item["status"] for item in cases if item["status"] != "skipped"]
    if any(item == "error" for item in statuses):
        status = "error"
    elif any(item == "failed" for item in statuses):
        status = "failed"
    elif any(item == "incomplete" for item in statuses):
        status = "incomplete"
    elif statuses and all(item == "passed" for item in statuses):
        status = "passed"
    else:
        status = "reported"
    payload = {"schema": "jforge.bench.report/1", "name": prepared["card"]["name"],
               "profile": prepared["profile"]["name"], "status": status, "cases": cases,
               "subject": prepared["card"].get("subject"), "machine_class": prepared.get("machine_class"),
               "partial": bool(prepared.get("partial")), "rerun_of": prepared.get("rerun_of"),
               "software": [{"name": item["name"], "id": item["id"], "action": item["action"],
                             "path": item["path"]} for item in prepared["software"].values()]}
    write_json(directory / "report.json", payload)
    write_text(directory / "report.md", _summary_markdown(payload))
    return payload


def _case_status(units):
    if any(unit.get("status") in {"failed", "error", "blocked"} for unit in units):
        return "error"
    if any(unit.get("status") in {None, "skipped"} for unit in units):
        return "incomplete"
    items = [item for unit in units for item in (unit.get("items") or [])]
    judged = [item for item in items if item["status"] in {"passed", "failed", "reported"}]
    if any(item["status"] == "failed" for item in judged):
        return "failed"
    if any(item["status"] == "passed" for item in judged):
        return "passed"
    return "reported"


def _custom_report(prepared, case_dir):
    argv = []
    for part in prepared["card"]["report"]:
        path = Path(part)
        if path.is_absolute() or "/" in part:
            path = path if path.is_absolute() else Path(prepared["card"]["dir"]) / path
            argv.append(str(path))
        else:
            argv.append(part)
    result = subprocess.run(argv, cwd=case_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        raise ForgeError(f"Report command failed (exit {result.returncode}).\n{result.stdout}")
    if not (case_dir / "report.md").is_file():
        raise ForgeError("Report command did not write report.md.")


def _case_markdown(name, record):
    lines = [f"# {record['name']}", "", f"status: {record['status']}", ""]
    items = [item for step in record["steps"] for item in (step.get("items") or [])]
    if items:
        lines.extend(["| item | actual | expected | delta | sigma | status |",
                      "| --- | --- | --- | --- | --- | --- |"])
        for item in items:
            lines.append("| {name} | {actual} | {expected} | {delta} | {sigma} | {status} |".format(
                name=item["name"], actual=_plain(item.get("actual")), expected=_plain(item.get("expected")),
                delta=_plain(item.get("delta")), sigma=_plain(item.get("sigma")),
                status=item["status"] if not item.get("reason") else f"{item['status']}: {item['reason']}"))
        lines.append("")
    return "\n".join(lines)


def _summary_markdown(payload):
    lines = [f"# {payload['name']} ({payload['profile']})", "", f"status: {payload['status']}", "",
             "| case | status |", "| --- | --- |"]
    for case in payload["cases"]:
        lines.append(f"| {case['name']} | {case['status']} |")
    lines.append("")
    return "\n".join(lines)


def _plain(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _append_index(home, directory, prepared, status):
    row = {"run_id": directory.name, "name": prepared["card"]["name"], "profile": prepared["profile"]["name"],
           "environment": prepared["environment"].get("name"), "status": status, "at": now(),
           "cases": [case["name"] for case in prepared["cases"]], "partial": bool(prepared.get("partial")),
           "machine_class": prepared.get("machine_class"), "rerun_of": prepared.get("rerun_of")}
    path = home.root / "bench" / "index.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with builds.file_lock(path.parent / "index.lock", blocking=True):
        rows = []
        if path.is_file():
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows = [item for item in rows if item.get("run_id") != directory.name]
        rows.append(row)
        write_text(path, "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows))


def _state_key(prepared):
    return builds.fingerprint({"id": prepared["card"]["name"], "project": prepared["card"]["dir"],
                               **({"environment": prepared["environment"]["name"]}
                                  if prepared["environment"].get("name") else {})})


def _update_gate_state(home, prepared, run_id, status):
    if prepared.get("partial"):
        return
    path = home.root / "bench" / "state" / f"{_state_key(prepared)}.json"
    report_path = home.root / "runs" / run_id / "report.json"
    report = read_json(report_path) if report_path.is_file() else None
    with builds.file_lock(home.root / "bench" / "state.lock", blocking=True):
        state = read_json(path) if path.is_file() else {"schema": "jforge.bench.state/1", "profiles": {}}
        state["profiles"][prepared["profile"]["name"]] = {"run_id": run_id, "status": status}
        if report and _gate(report, prepared["profile"].get("require")):
            accepted = state.setdefault("accepted", {})
            for case in report["cases"]:
                if case["status"] not in {"passed", "reported"}:
                    continue
                rows = [item for item in accepted.get(case["name"], []) if item.get("run_id") != run_id]
                rows.append({"run_id": run_id, "machine_class": prepared.get("machine_class"),
                             "profile": prepared["profile"]["name"]})
                accepted[case["name"]] = rows
        write_json(path, state)


def _public_result(directory, status):
    payload = read_json(directory / "report.json") if (directory / "report.json").is_file() else {"cases": []}
    text = _summary_markdown(payload) if payload.get("cases") else f"{status}\n"
    return {"run_id": directory.name, "status": status, "path": str(directory), "cases": payload.get("cases", []),
            "text": text}


def _request(prepared, run_id, *, jobs, executor, cases, tags):
    return {"kind": "bench", "schema": "jforge.bench.run/1", "run_id": run_id,
            "suite_id": prepared["card"]["name"], "name": prepared["card"]["name"],
            "card": prepared["card"]["path"], "card_sha256": prepared["card"]["sha256"],
            "profile": prepared["profile"]["name"], "environment": prepared["environment"].get("name"),
            "redo": prepared["redo"], "keep": prepared["keep"], "verify_full": prepared["verify_full"],
            "case_filter": list(cases), "tag_filter": list(tags), "jobs": jobs, "executor": executor,
            "executor_kind": prepared["executor_spec"]["kind"], "data_root": prepared.get("data_root"),
            "machine_class": prepared.get("machine_class"), "partial": bool(prepared.get("partial")),
            "rerun_of": prepared.get("rerun_of"), "baseline": prepared.get("baseline_run"),
            "subject": prepared["card"].get("subject"), "started_at": now(), "status": "running"}


def _plan_view(prepared):
    software = [{"name": item["name"], "label": item["label"], "action": item["action"],
                 "detail": item["detail"], "id": item["id"]} for item in prepared["software"].values()]
    cases = []
    for case in prepared["cases"]:
        if case.get("skip"):
            cases.append({"name": case["name"], "skipped": case.get("skip"), "steps": []})
            continue
        steps = [{"name": unit["step"], "action": unit["action"], "detail": unit["detail"]}
                 for unit in prepared["units"] if unit["case"] == case["name"]]
        cases.append({"name": case["name"], "steps": steps})
    run_tasks = sum(1 for unit in prepared["units"] for repeat in unit["repeats"] if repeat["action"] == "run")
    reuse_tasks = sum(1 for unit in prepared["units"] for repeat in unit["repeats"] if repeat["action"] in {"reuse", "keep"})
    builds_needed = sum(1 for item in software if item["action"] == "build")
    reused_bytes = 0
    for unit in prepared["units"]:
        for repeat in unit["repeats"]:
            if repeat["action"] not in {"reuse", "keep"}:
                continue
            done = repeat.get("done") or {}
            for records in (done.get("outputs") or {}).values():
                reused_bytes += sum(item.get("bytes") or 0 for item in records)
    summary = f"summary: build {builds_needed} · run {run_tasks} tasks · reuse {reuse_tasks} tasks · data {_size(reused_bytes)} reused"
    text = _plan_text(software, cases, summary)
    return {"name": prepared["card"]["name"], "profile": prepared["profile"]["name"], "software": software,
            "cases": cases, "summary": summary, "text": text, "blocked": prepared["blocked"]}


def _plan_text(software, cases, summary):
    lines = ["software"]
    for item in software:
        detail = item["detail"] or ""
        lines.append(f"  {item['name']:<12} {item['label']:<14} {item['action']:<8} {detail}".rstrip())
    if not software:
        lines.append("  (none)")
    for case in cases:
        if case.get("skipped"):
            lines.append(f"case {case['name']}  skipped  {case['skipped']}")
            continue
        lines.append(f"case {case['name']}")
        for step in case["steps"]:
            lines.append(f"  {step['name']:<12} {step['action']:<8} {step['detail']}".rstrip())
    lines.append(summary)
    return "\n".join(lines) + "\n"


def _size(number):
    if number >= 10 ** 9:
        return f"{number / 10 ** 9:.1f} GB"
    if number >= 10 ** 6:
        return f"{number / 10 ** 6:.1f} MB"
    return f"{number} B"


def _block_message(prepared):
    lines = []
    card = prepared["card"]["path"]
    for unit in prepared["units"]:
        if unit["action"] != "blocked":
            continue
        lines.append(f"{unit['case']}  {unit['step']}: inputs differ from the existing results")
        seen_files = set()
        for change in unit["changes"]:
            if change["kind"] == "files":
                if change["name"] in seen_files:
                    continue
                seen_files.add(change["name"])
            lines.append(_format_change(change))
        lines.append(f"  regenerate:           jforge bench run {card} --redo {unit['step']}")
        lines.append(f"  keep the old results: jforge bench run {card} --keep {unit['step']}")
    return "\n".join(lines)


def _format_change(change):
    kind = change["kind"]
    if kind == "files":
        return f"  files     {change['name']} changed"
    if kind == "variables":
        group, _, leaf = change["name"].partition(".")
        return f"  {group:<10}{leaf} {_plain(change['old'])} -> {_plain(change['new'])}"
    if kind == "software":
        return f"  software  {change['name']} {change['old']} -> {change['new']}"
    if kind == "fetch":
        return f"  fetch     {change['name']} {change['old']} -> {change['new']}"
    if kind == "definition":
        return "  definition changed"
    if kind == "environment":
        return "  environment changed"
    return f"  {kind:<10}{change['name']} changed"


def _gate(report, require):
    statuses = [item["status"] for item in report.get("cases", []) if item["status"] != "skipped"]
    if not statuses:
        return False
    if require == "passed":
        return all(item == "passed" for item in statuses)
    return not any(item in {"error", "incomplete", "failed"} for item in statuses)


def _gate_reason(report, require):
    if require == "passed":
        return "require passed: every case must pass"
    bad = [item["name"] for item in report.get("cases", []) if item["status"] in {"error", "incomplete", "failed"}]
    if bad:
        return "cases not accepted: " + ", ".join(bad)
    return "No cases ran."


def _gate_text(name, profile, report, accepted, reason):
    lines = [f"{name} {profile}: {'accepted' if accepted else 'not accepted'}"]
    if reason and not accepted:
        lines.append(reason)
    for case in report.get("cases", []):
        lines.append(f"  {case['name']:<12} {case['status']}")
    return "\n".join(lines) + "\n"


def _run_dir(home, run_id):
    identifier(run_id)
    directory = home.root / "runs" / run_id
    if not directory.is_dir() or not (directory / "bench.json").is_file():
        raise ForgeError(f"Bench run not found: {run_id}", hint="jforge bench history")
    return directory


def _task_rows(directory):
    rows = []
    tasks = directory / "tasks"
    if not tasks.is_dir():
        return rows
    for case_dir in sorted(path for path in tasks.iterdir() if path.is_dir()):
        for task in sorted(path for path in case_dir.iterdir() if path.is_dir()):
            state = read_json(task / "state.json") if (task / "state.json").is_file() else {"status": "missing"}
            rows.append({"case": case_dir.name, "task": task.name, "status": state.get("status")})
    return rows


def _status_text(request, result, tasks):
    lines = [f"{request['name']} {request['profile']} {result.get('status', 'running')}"]
    for task in tasks:
        lines.append(f"  {task['case']} {task['task']} {task['status']}")
    return "\n".join(lines) + "\n"


_MISSING = object()


def _dig(node, parts):
    current = node
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _walk(node, parts):
    if not parts:
        return {}
    head, *rest = parts
    if head == "*":
        if not isinstance(node, dict):
            return {}
        found = {}
        for key, child in node.items():
            if not rest:
                found[str(key)] = child
            else:
                for name, value in _walk(child, rest).items():
                    found[str(key) if not name else f"{key}.{name}"] = value
        return found
    if not isinstance(node, dict) or head not in node:
        return {}
    if not rest:
        return {head: node[head]}
    return _walk(node[head], rest)


# Baselines, comparison and data directories


def _machine_class(home):
    path = home.root / "executors" / "local.yaml"
    if path.is_file():
        try:
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
        except yaml.YAMLError as exc:
            raise ForgeError(f"Cannot read {path}: {exc}") from exc
        if isinstance(raw, dict) and isinstance(raw.get("machine_class"), str) and raw["machine_class"].strip():
            return raw["machine_class"].strip()
    return "local-" + platform.machine()


def _finished_bench(home, run_id):
    directory = _run_dir(home, run_id)
    request = read_json(directory / "bench.json")
    if request.get("kind") != "bench":
        raise ForgeError(f"Run {run_id} is not a bench run.")
    result = read_json(directory / "result.json") if (directory / "result.json").is_file() else {}
    if not result.get("finished_at") or result.get("status") in {None, "running"}:
        raise ForgeError(f"Run {run_id} has not finished.", code=1)
    report_path = directory / "report.json"
    if not report_path.is_file():
        raise ForgeError(f"Run {run_id} has no report.")
    inputs_path = directory / "inputs.json"
    inputs = read_json(inputs_path) if inputs_path.is_file() else {"cases": {}}
    return {"run_id": run_id, "directory": directory, "name": request["name"], "profile": request.get("profile"),
            "request": request, "report": read_json(report_path), "inputs": inputs}


def _baseline_items(prepared, case, step, compare, bindings, paths):
    reference, reason, source = _find_baseline(prepared, case["name"], step["name"])
    tolerance = compare.get("tolerance")
    items = []
    for number, path in enumerate(paths):
        document = _load_document(Path(path))
        found = select_items(document, bench_cards.expand(compare["select"], bindings, where=f"{step['name']} select"))
        prefix = f"r{number:03d}:" if len(paths) > 1 else ""
        if reference is None:
            if not found:
                items.append({"name": prefix + "(baseline)", "status": "reported", "reason": reason,
                              "tolerance": tolerance})
            for name, node in found.items():
                actual, error = _metric(node, compare.get("value", "value"), compare.get("error", "error"))
                items.append({"name": prefix + str(name), "status": "reported", "reason": reason,
                              "actual": actual, "actual_error": error, "tolerance": tolerance})
            continue
        synthetic = {"source": source, "values": {
            name: {"value": item["actual"], "error": item.get("actual_error")} for name, item in reference.items()}}
        items.extend(_compare_document(found, synthetic, compare, prefix))
    return items


def _find_baseline(prepared, case_name, step_name):
    home = prepared["home"]
    subject = prepared["card"].get("subject")
    current = _input_snapshot(prepared)["cases"].get(case_name, {})
    if prepared.get("baseline_run"):
        items, reason = _baseline_from_run(home, prepared["baseline_run"], prepared, case_name, step_name,
                                           current, subject)
        source = None if items is None else f"baseline {prepared['baseline_run']}"
        return items, reason, source
    path = home.root / "bench" / "state" / f"{_state_key(prepared)}.json"
    if not path.is_file():
        return None, "no baseline", None
    state = read_json(path)
    rows = [item for item in state.get("accepted", {}).get(case_name, [])
            if item.get("run_id") != prepared.get("run_id")]
    machine = prepared.get("machine_class")
    same = [item for item in rows if item.get("machine_class") == machine]
    if rows and not same:
        others = ", ".join(sorted({item.get("machine_class") or "" for item in rows}))
        return None, f"different machine class {machine} (accepted runs are {others})", None
    fallback = "no baseline"
    for row in reversed(same):
        items, reason = _baseline_from_run(home, row["run_id"], prepared, case_name, step_name, current, subject)
        if items is not None:
            return items, None, f"baseline {row['run_id']}"
        fallback = reason
    return None, fallback, None


def _baseline_from_run(home, run_id, prepared, case_name, step_name, current, subject):
    try:
        previous = _finished_bench(home, run_id)
    except ForgeError as exc:
        return None, str(exc)
    other_machine = previous["report"].get("machine_class")
    if other_machine != prepared.get("machine_class"):
        return None, f"different machine class {prepared.get('machine_class')} and {other_machine}"
    old = (previous["inputs"].get("cases") or {}).get(case_name)
    if old is None:
        return None, "no baseline"
    changes = _case_input_changes(old, current, subject)
    if changes:
        return None, "not comparable: " + _format_change(changes[0]).strip()
    items = _step_items(next((item for item in previous["report"]["cases"] if item["name"] == case_name), None),
                        step_name)
    usable = {name: item for name, item in items.items()
              if isinstance(item.get("actual"), (int, float)) and not isinstance(item.get("actual"), bool)}
    if not usable:
        return None, "no baseline"
    return usable, None


def _input_snapshot(prepared):
    cases = {}
    for unit in prepared["units"]:
        if unit.get("compare"):
            continue
        repeats = []
        for repeat in unit["repeats"]:
            inputs = repeat.get("inputs") or {}
            if not inputs and repeat.get("done"):
                inputs = (repeat["done"] or {}).get("inputs") or {}
            repeats.append({"index": repeat["index"], "inputs": inputs})
        cases.setdefault(unit["case"], {})[unit["step"]] = repeats
    return {"subject": prepared["card"].get("subject"), "machine_class": prepared.get("machine_class"),
            "cases": cases}


def _without_subject(inputs, subject):
    if not inputs:
        return {}
    clone = json.loads(json.dumps(inputs))
    software = clone.get("software")
    if subject and isinstance(software, dict):
        software.pop(subject, None)
    return clone


def _case_input_changes(old, new, subject):
    changes = []
    for step in sorted(set(old) | set(new)):
        old_reps = {item["index"]: item.get("inputs") or {} for item in old.get(step) or []}
        new_reps = {item["index"]: item.get("inputs") or {} for item in new.get(step) or []}
        if set(old_reps) != set(new_reps):
            changes.append({"kind": "definition", "name": f"{step} repeats",
                            "old": sorted(old_reps), "new": sorted(new_reps)})
        for index in sorted(set(old_reps) & set(new_reps)):
            for change in _own_changes(_without_subject(old_reps[index], subject),
                                       _without_subject(new_reps[index], subject)):
                copied = dict(change)
                if copied["kind"] == "files":
                    copied["name"] = f"{step}/{copied['name']}"
                else:
                    copied["name"] = f"{step} {copied['name']}"
                changes.append(copied)
    return changes


def _runs_input_changes(left, right, subject):
    changes = []
    cases = set((left["inputs"].get("cases") or {})) | set((right["inputs"].get("cases") or {}))
    for case in sorted(cases):
        for change in _case_input_changes((left["inputs"].get("cases") or {}).get(case, {}),
                                          (right["inputs"].get("cases") or {}).get(case, {}), subject):
            copied = dict(change)
            copied["name"] = f"{case} {copied['name']}"
            changes.append(copied)
    return changes


def _step_items(case, step):
    if not case:
        return {}
    for spec in case.get("steps") or []:
        if spec["name"] == step:
            return {item["name"]: item for item in spec.get("items") or []}
    return {}


def _subject_id(report, subject):
    software = report.get("software") or []
    if subject:
        for item in software:
            if item.get("name") == subject:
                return item.get("id")
    return ",".join(item.get("id") or "" for item in software)


def _compare_text(left, right, comparable, changes, rows, only_a, only_b, same_machine):
    lines = [f"{left['name']}  {left['run_id']} -> {right['run_id']}  "
             + ("comparable" if comparable else "not comparable")]
    if not same_machine:
        lines.append(f"  machine class {left['report'].get('machine_class')} -> {right['report'].get('machine_class')}")
    for change in changes:
        lines.append(_format_change(change))
    for name in only_a:
        lines.append(f"  only in {left['run_id']}: {name}")
    for name in only_b:
        lines.append(f"  only in {right['run_id']}: {name}")
    current = None
    for row in rows:
        if row["case"] != current:
            current = row["case"]
            lines.append(f"case {current}")
        detail = row["reason"] or ""
        if row["delta"] is not None:
            ratio = "" if row["ratio"] is None else f"  ratio {_plain(row['ratio'])}"
            detail = f"{_plain(row['a'])} -> {_plain(row['b'])}  delta {_plain(row['delta'])}{ratio}"
        lines.append(f"  {row['step']} {row['item']}  {detail}".rstrip())
    return "\n".join(lines) + "\n"


def _data_card(card):
    if card is None:
        return None, None
    path = Path(card).expanduser()
    if path.is_file():
        loaded = bench_cards.load(path)
        return loaded, loaded["name"]
    return None, str(card)


def _data_keep(card):
    profiles = set()
    if card["profiles"]:
        for spec in card["profiles"].values():
            profiles.add(spec.get("data") or "")
        profiles.update(card["profiles"])
        profiles.discard("")
    else:
        profiles.add("default")
    cases = {item["name"] for item in bench_cards._discover(card)}
    steps = {step["name"] for step in card["steps"]}
    return {"profiles": profiles, "cases": cases, "steps": steps}


def _prune_dirs(card_dir, keep):
    doomed = []
    for path in card_dir.rglob("*"):
        if not path.is_dir():
            continue
        if path.name.endswith(".partial") or path.name.endswith(".new"):
            doomed.append(path)
            continue
        if keep is None:
            continue
        parts = path.relative_to(card_dir).parts
        if len(parts) == 1 and parts[0] not in keep["profiles"]:
            doomed.append(path)
        elif len(parts) == 2 and parts[1] not in keep["cases"]:
            doomed.append(path)
        elif len(parts) == 3 and parts[2] not in keep["steps"]:
            doomed.append(path)
    doomed.sort(key=lambda item: len(item.parts))
    chosen = []
    for path in doomed:
        if any(parent == path or parent in path.parents for parent in chosen):
            continue
        chosen.append(path)
    return chosen
