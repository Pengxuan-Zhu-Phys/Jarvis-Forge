"""jforge test run: execute test cards, reuse evidence and write acceptance records.

Design: Jarvis-Books Docs/Jarvis-Forge/TEST_CATALOG_DESIGN.md. Current behaviour:
Docs/Jarvis-Forge/CHECKER.md. Loading, selection and sharding live in testing.py.
"""

from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

from . import __version__, builds, checker, cmake, testing, testing_evidence
from .package_operations import run_command
from .storage import ForgeError, digest, identifier, new_id, now, read_json, write_json


EXECUTOR = 1  # Bump when argv assembly or evidence parsing changes what a passed slot means.
MESSAGE_LIMIT = 500
TREE_LIMIT = 20
PASSING = frozenset({"passed", "reused"})


# Source identity

def _git(args, cwd):
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=environment,
                                stdin=subprocess.DEVNULL, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def _static_version(project):
    path = Path(project) / "pyproject.toml"
    if not path.is_file():
        return None
    import tomllib
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    table = data.get("project", {})
    version = table.get("version")
    return version if isinstance(version, str) and "version" not in table.get("dynamic", []) else None


def _bytecode(path):
    """Python bytecode caches are never source, even without a .gitignore entry."""
    return "__pycache__/" in path or path.endswith((".pyc", ".pyo"))


class Source:
    """Git identity of a project directory; None-valued when the project is not in Git."""

    def __init__(self, project):
        self.project = Path(project).resolve()
        self.top = self.spec = None
        top = _git(["rev-parse", "--show-toplevel"], self.project)
        if top is not None:
            self.top = Path(top.decode().strip()).resolve()
            prefix = self.project.relative_to(self.top).as_posix()
            self.prefix = "" if prefix == "." else prefix
            self.spec = self.prefix or "."

    def status(self):
        if self.top is None:
            return None
        output = _git(["status", "--porcelain=v1", "--untracked-files=all", "-z", "--", self.spec], self.top)
        if output is None:
            return None
        return sorted(item for item in (raw.decode(errors="replace") for raw in output.split(b"\0") if raw)
                      if not _bytecode(item))

    def identity(self):
        if self.top is None:
            return None
        commit = _git(["rev-parse", "--verify", "HEAD^{commit}"], self.top)
        tree = _git(["rev-parse", f"HEAD:{self.prefix}" if self.prefix else "HEAD^{tree}"], self.top)
        status = self.status()
        if commit is None or tree is None or status is None:
            return None
        commit, tree = commit.decode().strip(), tree.decode().strip()
        dirty_digest = None
        if status:
            h = hashlib.sha256()
            h.update(_git(["diff", "HEAD", "--binary", "--", self.spec], self.top) or b"")
            untracked = _git(["ls-files", "--others", "--exclude-standard", "-z", "--", self.spec], self.top) or b""
            for name in sorted(item for item in untracked.split(b"\0") if item and not _bytecode(item.decode(errors="replace"))):
                h.update(name + b"\0")
                file = self.top / name.decode(errors="surrogateescape")
                if file.is_symlink():
                    h.update(os.readlink(file).encode(errors="surrogateescape"))
                elif file.is_file():
                    h.update(digest(file).encode())
            dirty_digest = h.hexdigest()
        key = hashlib.sha256(json.dumps({"tree": tree, "dirty_digest": dirty_digest}).encode()).hexdigest()
        return {"commit": commit, "tree": tree, "dirty": bool(status), "dirty_digest": dirty_digest,
                "source_key": key, "version": _static_version(self.project)}


# Package probe and identity plugin

_PROBE = r'''
import importlib.metadata as metadata, json, re, sys
rows = []
for dist in metadata.distributions():
    name = dist.metadata.get("Name") if dist.metadata is not None else None
    if not name:
        continue
    try:
        direct = dist.read_text("direct_url.json")
    except Exception:
        direct = None
    rows.append({"name": re.sub(r"[-_.]+", "-", name).lower(), "version": dist.version,
                 "direct_url": direct, "location": str(dist.locate_file(""))})
rows.sort(key=lambda row: (row["name"], row["version"] or "", row["direct_url"] or "", row["location"]))
sys.stdout.write(json.dumps(rows))
'''

_PLUGIN = '''"""Written by Jarvis-Forge for one jforge test step; records interpreter state only."""

import json
import os
from pathlib import Path
import sys

import pytest

_CONFIG = Path(__file__).with_name("jforge_identity.json")


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    # Files outside --rootdir get node IDs without a path; give failed and
    # skipped collection reports one, so junit names the file.
    outcome = yield
    report = outcome.get_result()
    path = getattr(collector, "path", None)
    if report.passed or path is None or report.nodeid.split("::")[0]:
        return
    rel = os.path.relpath(path, collector.config.rootpath).replace(os.sep, "/")
    report.nodeid = rel + report.nodeid


def pytest_sessionfinish(session, exitstatus):
    step = os.environ.get("JFORGE_STEP_DIR")
    if not step or not _CONFIG.is_file():
        return
    project = Path(json.loads(_CONFIG.read_text())["project"])
    plugin = str(Path(__file__).resolve().parent)
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    conftests, modules = set(), {}
    for plugin_object in session.config.pluginmanager.get_plugins():
        file = getattr(plugin_object, "__file__", None)
        if file and Path(file).name == "conftest.py":
            conftests.add(str(Path(file).resolve()))
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if not file:
            continue
        try:
            path = Path(file).resolve()
        except (OSError, ValueError):
            continue
        if path.is_relative_to(project):
            modules[name] = str(path)
    record = {"executable": sys.executable, "version": sys.version,
              "sys_path": [entry for entry in sys.path if entry != plugin],
              "conftest": sorted(conftests), "project_modules": dict(sorted(modules.items())),
              "worker": worker, "exitstatus": int(exitstatus)}
    name = f"identity-{worker}.json" if worker else "identity.json"
    Path(step, name).write_text(json.dumps(record, indent=2) + "\\n")
'''


def write_plugin(step_dir, project):
    directory = step_dir / "plugin"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "jforge_identity.py").write_text(_PLUGIN)
    (directory / "jforge_identity.json").write_text(json.dumps({"project": str(project)}) + "\n")
    return directory


def probe_packages(python, environment, cwd):
    try:
        result = subprocess.run([python, "-c", _PROBE], cwd=cwd, env=environment, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForgeError(f"Cannot list installed distributions with {python}: {exc}", code=1) from exc
    if result.returncode:
        raise ForgeError(f"Cannot list installed distributions with {python}: "
                         + result.stderr.decode(errors="replace")[-MESSAGE_LIMIT:], code=1)
    rows = json.loads(result.stdout)
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(), rows


# junit evidence

def _outcome(case):
    for tag, outcome in (("error", "error"), ("failure", "failed")):
        element = case.find(tag)
        if element is not None:
            return outcome, element
    element = case.find("skipped")
    if element is not None:
        return ("xfailed" if element.get("type") == "pytest.xfail" else "skipped"), element
    return "passed", None


COLLECTION = {"collection failure", "collection skipped"}


def _mangle(path):
    return re.sub(r"\.py$", "", path).replace("/", ".")


def _chain(rel, classname, file):
    """Class chain of a testcase; pytest writes "" or ".Class" for files outside --rootdir."""
    if not classname:
        return []
    if classname.startswith("."):
        return classname[1:].split(".")
    prefix = _mangle(file or "")
    if prefix and classname == prefix:
        return []
    if prefix and classname.startswith(prefix + "."):
        return classname[len(prefix) + 1:].split(".")
    stem = PurePosixPath(rel).stem
    parts = classname.split(".")
    return parts[len(parts) - parts[::-1].index(stem):] if stem in parts else []


def _skip_message(element):
    """The reason of a skip; a module-level skip carries it in the (path, line, reason) text."""
    message = element.get("message") or ""
    if message == "collection skipped" and element.text:
        try:
            value = ast.literal_eval(element.text.strip())
        except (SyntaxError, ValueError):
            return element.text
        if isinstance(value, tuple) and len(value) == 3 and isinstance(value[2], str):
            return value[2].removeprefix("Skipped: ")
    return message or element.text or ""


def parse_junit(path, project, rootdir, *, step=None, rewrite=False):
    """Read pytest junit (xunit1) and return counts, cases and problems.

    Counts come from the testsuite attributes. A testcase must carry a file
    inside the project; an empty classname is rebuilt from that file.
    """
    project, rootdir = Path(project).resolve(), Path(rootdir)
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        return None, [], [f"junit unreadable: {exc}"]
    root = tree.getroot()
    if root.tag not in {"testsuite", "testsuites"}:
        return None, [], ["junit has no testsuite"]
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    raw = {}
    try:
        for key in ("tests", "failures", "errors", "skipped"):
            raw[key] = sum(int(suite.get(key, 0)) for suite in suites)
    except ValueError:
        return None, [], ["junit counts are not integers"]
    cases, problems, changed, outside = [], [], False, []
    for case in root.iter("testcase"):
        classname, name, file = case.get("classname") or "", case.get("name") or "", case.get("file")
        rel = None
        if file:
            resolved = (rootdir / file).resolve()
            if resolved.is_relative_to(project):
                rel = resolved.relative_to(project).as_posix()
        if rel is None:
            outside.append(name or classname or "?")
            continue
        outcome, element = _outcome(case)
        collected = element is not None and element.get("message") in COLLECTION
        chain = [] if collected else _chain(rel, classname, file)
        canonical = ".".join([_mangle(rel), *chain])
        if not classname or classname.startswith("."):
            case.set("classname", canonical)
            changed = True
        node = rel if collected else "::".join([rel, *chain, name])
        message = None
        if element is not None:
            message = (_skip_message(element) if outcome == "skipped" else
                       element.get("message") or element.text or "")[:MESSAGE_LIMIT]
        try:
            duration = float(case.get("time") or 0)
        except ValueError:
            duration = 0.0
        cases.append({"step": step, "node": node, "outcome": outcome,
                      "duration": duration, "message": message})
    if outside:
        problems.append("junit testcase without a file inside the project: " + ", ".join(outside[:5]))
    if changed and rewrite:
        tree.write(path, encoding="utf-8", xml_declaration=True)
    xfailed = sum(case["outcome"] == "xfailed" for case in cases)
    passed = raw["tests"] - raw["failures"] - raw["errors"] - raw["skipped"]
    consistent = passed >= 0 and xfailed <= raw["skipped"]
    counts = {"tests": raw["tests"], "passed": passed, "failed": raw["failures"], "errors": raw["errors"],
              "skipped": raw["skipped"] - xfailed, "xfailed": xfailed, "consistent": consistent}
    if not consistent:
        counts["raw"] = raw
        problems.append("junit counts are inconsistent")
    return counts, cases, problems


def skip_summary(cases):
    reasons = Counter(case["message"] or "" for case in cases if case["outcome"] == "skipped")
    return [{"message": message, "count": count} for message, count in reasons.most_common()]


def unexpected_skips(cases, allow):
    if allow is None:
        return []
    return [case for case in cases if case["outcome"] == "skipped"
            and not any(re.search(pattern, case["message"] or "") for pattern in allow)]


def pytest_status(code, counts, problems):
    """Map the pytest exit code and evidence to (status, reason)."""
    if code is None:
        return "failed", "pytest did not exit normally"
    if code < 0:
        return "failed", f"pytest ended by signal {-code}"
    if code == 0:
        return ("failed", problems[0]) if problems else ("passed", None)
    if code == 5 and counts is not None and counts["tests"] == 0 and not problems:
        return "passed", "no tests selected"
    return "failed", {1: "test failures", 2: "collection error or pytest interruption",
                      3: "pytest internal error", 4: "pytest usage error"}.get(code, f"pytest exit {code}")


def pytest_argv(python, group, step_dir, paths, project, nodes=None):
    """Assemble the pytest command; *nodes* (test IDs) replace *paths* in a --rerun-failed step."""
    installed = group["isolate"] == "installed"
    args = group["pytest_args"]
    argv = [python, "-m", "pytest", "-vv" if "-vv" in args else "-q",
            next((arg for arg in args if arg.startswith("--tb=")), "--tb=short"),
            "-p", "jforge_identity", f"--import-mode={group['import_mode']}",
            f"--junitxml={step_dir / 'junit.xml'}",
            f"--override-ini=cache_dir={step_dir / 'pytest-cache'}",
            "--override-ini=junit_family=xunit1"]
    if installed:
        argv += [f"--rootdir={step_dir / 'work'}", "--override-ini=addopts=", "--override-ini=pythonpath="]
    if group["markers"]:
        argv += ["-m", group["markers"]]
    if group["workers"] is not None:
        argv += ["-n", str(group["workers"])]
    argv += [arg for arg in args if arg not in {"-q", "-vv"} and not arg.startswith("--tb=")]
    targets = list(nodes) if nodes else sorted(paths)
    argv += [str(Path(project) / target) if installed else target for target in targets]
    return argv


def _effective(environment):
    """PYTHONPATH without the leading plugin directory."""
    return os.pathsep.join(environment.get("PYTHONPATH", "").split(os.pathsep)[1:])


# Locks and state

class Locks:
    """Hold several file locks and release each one when its step ends."""

    def __init__(self):
        self.held = {}

    def acquire(self, path, *, blocking=False, message=None):
        if path in self.held:
            return
        lock = builds.file_lock(path, blocking=blocking, message=message)
        lock.__enter__()
        self.held[path] = lock

    def release(self, path):
        lock = self.held.pop(path, None)
        if lock is not None:
            lock.__exit__(None, None, None)

    def release_all(self):
        for path in list(self.held):
            self.release(path)


def card_key(card, environment_name):
    value = {"id": card["id"], "project": card["project"]}
    if environment_name:
        value["environment"] = environment_name
    return builds.fingerprint(value)


def _locks_dir(home):
    return home.root / "checker" / "locks"


def read_state(home, key):
    path = home.root / "checker" / "state" / f"{key}.json"
    state = read_json(path) if path.exists() else {}
    state.setdefault("checks", {})
    state.setdefault("groups", {})
    return state


def update_state(home, key, change):
    path = home.root / "checker" / "state" / f"{key}.json"
    with builds.file_lock(_locks_dir(home) / f"{key}.state.lock", blocking=True):
        state = read_state(home, key)
        state["schema_version"] = 2
        change(state)
        write_json(path, state)


def append_index(home, row):
    path = home.root / "checker" / "index.jsonl"
    with builds.file_lock(_locks_dir(home) / "index.lock", blocking=True):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")


# Preflight

def _gate_checks(card, profile, ids):
    if profile not in testing.GATE_PROFILES:
        return
    for item_id in ids:
        group = card["groups"].get(item_id)
        if group is not None and group["skip_allow"] is None:
            raise ForgeError(f"Group {item_id} is in gate profile {profile} and must declare skip_allow.",
                             hint="Use skip_allow: [] to allow no skips.")
    if card["groups"]:
        if card["coverage"] is None:
            raise ForgeError(f"Card {card['id']} has a gate profile and pytest groups, so it must declare coverage.")
        testing.check_coverage(card)


def load_rerun(home, run_id):
    """The finished run that --rerun-failed RUN_ID starts from: its request, steps and cases."""
    identifier(run_id)
    directory = home.root / "runs" / run_id
    if not (directory / "request.json").is_file():
        raise ForgeError(f"No test run {run_id}.", hint="List runs with jforge test history.")
    request = read_json(directory / "request.json")
    if request.get("catalog") != "test":
        raise ForgeError(f"Run {run_id} is not a jforge test run.")
    result = read_json(directory / "result.json") if (directory / "result.json").is_file() else None
    if result is None or result.get("status") == "running" or not result.get("finished_at"):
        raise ForgeError(f"Run {run_id} has not finished.", code=1)
    cases = []
    if (directory / "cases.jsonl").is_file():
        cases = [json.loads(row) for row in (directory / "cases.jsonl").read_text(encoding="utf-8").splitlines()
                 if row.strip()]
    return {"run_id": run_id, "request": request, "steps": {step["id"]: step for step in result.get("steps", [])},
            "cases": cases}


def preflight(home, path, *, profile=None, groups=(), batch=None, acceptance_id=None,
              environment_name=None, root=None, project=None, rerun_failed=None, full=False, strict=False):
    if profile and groups:
        raise ForgeError("--profile and --group are mutually exclusive.")
    previous = None
    if rerun_failed is not None:
        if profile or groups or batch or full or acceptance_id is not None:
            raise ForgeError("--rerun-failed takes its selection and shard from the run; it does not combine with "
                             "--profile, --group, --batch, --full or --acceptance-id.",
                             hint="A rerun never joins an acceptance. Run the profile again for acceptance evidence.")
        home.require_initialized()
        previous = load_rerun(home, rerun_failed)
        batch = "{index}/{count}".format(**previous["request"]["batch"])
    index, count = testing.parse_batch(batch)
    if acceptance_id is not None:
        identifier(acceptance_id)
    if count > 1 and acceptance_id is None and previous is None:
        raise ForgeError("Sharded runs need --acceptance-id.",
                         hint="Give every shard the same --acceptance-id, then run jforge test summarize.")
    target = testing.load_target(path, root=root, project=project)
    config = home.require_initialized()
    catalog = target["kind"] == "catalog"
    entries = target["entries"]
    if previous is not None:
        request = previous["request"]
        entries = [entry for entry in entries
                   if entry["card"]["id"] == request["suite_id"]
                   and (not catalog or entry["id"] == request.get("project_id"))]
        if not entries:
            raise ForgeError(f"Run {rerun_failed} belongs to card {request['suite_id']}"
                             + (f" (catalog card {request['project_id']})" if request.get("project_id") else "")
                             + f", which {path} does not hold.")
        environment_name = environment_name or (request.get("environment") or {}).get("name")
    plans = []
    for entry in entries:
        card = entry["card"]
        try:
            if previous is not None:
                request = previous["request"]
                selection = testing.select(card, request.get("profile"), () if request.get("profile")
                                           else request.get("seeds") or request.get("groups") or ())
            else:
                selection = testing.select(card, profile, groups)
            testing.check_intersections(card, selection["ids"], selection["profile"])
            for item_id in selection["ids"]:
                group = card["groups"].get(item_id)
                if group is not None and not testing.group_files(card, group):
                    raise ForgeError(f"Selected group {item_id} matches no files after omit.")
            _gate_checks(card, selection["profile"], selection["ids"])
            if strict:
                testing.validate_card(card)
            name = environment_name or entry["env"] or card["environment"].get("name")
            if name:
                from .environment_registry import show
                show(home, name, card["environment"].get("revision") if not (environment_name or entry["env"]) else None)
        except ForgeError as exc:
            raise ForgeError(testing._context(entry, catalog) + str(exc), hint=exc.hint) from exc
        plans.append({"entry": entry, "card": card, "selection": selection, "environment_name": name,
                      "key": card_key(card, name), "previous": previous})
    return {"target": target, "config": config, "index": index, "count": count, "plans": plans}


# Execution

class _Interrupted(Exception):
    pass


def _sigterm(signum, frame):
    raise KeyboardInterrupt


def run(home, path, *, profile=None, groups=(), batch=None, acceptance_id=None, full=False,
        environment_name=None, root=None, project=None, jobs=1, progress=None, rerun_failed=None, strict=False):
    prepared = preflight(home, path, profile=profile, groups=groups, batch=batch, acceptance_id=acceptance_id,
                         environment_name=environment_name, root=root, project=project,
                         rerun_failed=rerun_failed, full=full, strict=strict)
    target, index, count = prepared["target"], prepared["index"], prepared["count"]
    generated = acceptance_id is None and rerun_failed is None
    if generated and target["kind"] == "catalog":
        acceptance_id = new_id("acceptance")
    locks = Locks()
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, _sigterm)
    runs, interrupted = [], False
    try:
        for plan in prepared["plans"]:
            for item_id in plan["selection"]["seeds"]:
                step = testing.step_id(item_id, index, count) if item_id in plan["card"]["groups"] else item_id
                locks.acquire(_locks_dir(home) / f"{plan['key']}.{step}.lock",
                              message=f"Test step is busy: {step}")
        for plan in prepared["plans"]:
            run_record = _CardRun(home, prepared, plan, acceptance_id, full, jobs, progress, locks, strict=strict)
            try:
                run_record.execute()
            except _Interrupted:
                interrupted = True
            runs.append(run_record.result)
            if interrupted:
                break
    except KeyboardInterrupt:
        interrupted = True
    finally:
        locks.release_all()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    summary = None
    if generated and runs and not interrupted:
        if target["kind"] == "card":
            acceptance_id = runs[0]["acceptance_id"]
        summary = summarize(home, acceptance_id, missing_ok=True)
    statuses = [item["status"] for item in runs]
    status = "interrupted" if interrupted else "passed" if statuses and all(s == "passed" for s in statuses) else "failed"
    return {"target": target["kind"], "path": target["path"], "catalog_id": target["id"],
            "acceptance_id": acceptance_id if runs else None, "status": status, "runs": runs,
            "summary": summary}


class _CardRun:
    def __init__(self, home, prepared, plan, acceptance_id, full, jobs, progress, locks, *, strict=False):
        self.home, self.plan, self.full, self.jobs, self.progress, self.locks = home, plan, full, jobs, progress, locks
        self.card, self.selection, self.key = plan["card"], plan["selection"], plan["key"]
        self.index, self.count = prepared["index"], prepared["count"]
        self.project = Path(self.card["project"])
        self.run_id = new_id("checker")
        # A --rerun-failed run is partial: it never joins an acceptance and never touches reuse state.
        self.previous = plan.get("previous")
        self.acceptance_id = None if self.previous else acceptance_id or self.run_id
        self.directory = home.root / "runs" / self.run_id
        self.config = prepared["config"]
        self.gate = self.selection["profile"] in testing.GATE_PROFILES and self.previous is None
        self.strict = strict
        self.packages = {}
        self.cases = []

    def event(self, message, level="INFO"):
        self.log(message, level=level)

    def execute(self):
        card, directory = self.card, self.directory
        directory.mkdir(parents=True)
        self.source = Source(self.project)
        identity = self.source.identity()
        self.request = {
            "schema_version": 1, "kind": "checker", "catalog": "test", "forge_version": __version__,
            "run_id": self.run_id, "suite_id": card["id"], "project_id": self.plan["entry"]["id"],
            "card": card["path"], "project": str(self.project), "profile": self.selection["profile"],
            "groups": self.selection["ids"], "seeds": self.selection["seeds"],
            "batch": {"index": self.index, "count": self.count}, "acceptance_id": self.acceptance_id,
            "full": self.full, "partial": self.previous is not None,
            "rerun_of": self.previous["run_id"] if self.previous else None,
            "host": {"host_id": self.config["host_id"], "platform": f"{platform.system().lower()}-{platform.machine()}"},
            "environment": None, "source": identity, "packages_digest": None, "started_at": now()}
        write_json(directory / "request.json", self.request)
        (directory / "checker.yaml").write_text(card["text"])
        self.result = {**self.request, "path": str(directory), "status": "running", "steps": [], "warnings": []}
        write_json(directory / "result.json", self.result)
        from .build_logging import ProcessLog
        self.log = ProcessLog(directory / "checker.log", "Jarvis-Forge.Test", self.progress)
        interrupted = False
        try:
            self._prepare()
            self._steps()
        except KeyboardInterrupt:
            interrupted = True
            self.result.update(status="interrupted", error="Interrupted.")
        except (ForgeError, OSError, ValueError) as exc:
            self.result.update(status="failed", error=str(exc))
        finally:
            self._finish()
        if interrupted:
            raise _Interrupted

    def _prepare(self):
        card = self.card
        environment, observed, origin, script, name = checker.prepare_environment(
            self.home, self.directory, card["environment"], Path(card["path"]).parent, self.plan["environment_name"])
        self.environment, self.observed, self.origin, self.script = environment, observed, origin, script
        self.python = observed.get("python", {})
        if not self.python.get("path"):
            raise ForgeError("The selected environment has no python3 on PATH.", code=1)
        self.request["environment"] = {key: value for key, value in origin.items() if key != "files"}
        packages = self._packages(self._step_environment(None, None, None), self.project)
        self.request["packages_digest"] = packages
        if self.gate:
            subject, evidence, problems = self._evidence((environment, observed, origin), self.request["source"])
            self.request.update(source=subject, evidence=evidence, evidence_problems=problems)
        write_json(self.directory / "request.json", self.request)
        self.result.update(self.request)
        if self.gate and self.request["evidence_problems"] and (self.strict or card.get("subject")):
            self.result.update(status="blocked", error=" ".join(self.request["evidence_problems"]))
            return
        self.context = {"project": self.project, "card_dir": Path(card["path"]).parent, "environment": environment,
                        "environment_origin": origin, "observed": observed, "env_script": script,
                        "environment_name": name, "jobs": self.jobs, "run_dir": self.directory,
                        "acceptance": self.request.get("evidence")}

    def _evidence(self, prepared=None, source=None):
        return testing_evidence.snapshot(self.home, self.card, self.selection, self.plan["environment_name"],
                                         prepared=prepared, source=source)

    def _packages(self, environment, cwd):
        key = (_effective(environment), str(cwd))
        if key not in self.packages:
            value, rows = probe_packages(self.python["path"], environment, cwd)
            file = self.directory / f"packages-{value[:12]}.json"
            if not file.exists():
                write_json(file, {"packages_digest": value, "distributions": rows})
            self.packages[key] = value
        return self.packages[key]

    def _step_environment(self, group, step_dir, plugin):
        environment = testing.python_environment(self.environment, self.project, group)
        if group is not None:
            environment["JFORGE_RUN_DIR"] = str(self.directory)
            environment["JFORGE_STEP_DIR"] = str(step_dir)
        effective = environment.get("PYTHONPATH")
        # The plugin directory always leads so the probe key and the fingerprint can drop it.
        environment["PYTHONPATH"] = os.pathsep.join([str(plugin or self.directory / "no-plugin"),
                                                     *([effective] if effective else [])])
        return environment

    def _steps(self):
        if self.result["status"] == "blocked":
            return
        statuses, tokens = {}, {}
        seeds = set(self.selection["seeds"])
        for item_id in self.selection["ids"]:
            group = self.card["groups"].get(item_id)
            step = testing.step_id(item_id, self.index, self.count) if group else item_id
            lock = _locks_dir(self.home) / f"{self.key}.{step}.lock"
            if item_id not in seeds:
                self.locks.acquire(lock, blocking=True)
            step_dir = self.directory / "steps" / step
            step_dir.mkdir(parents=True)
            prerequisites = {need: (statuses[need], tokens[need]) for need in (group or self.card["checks"][item_id]).get("needs", [])}
            record = {"id": step, "status": "running", "started_at": now()}
            carried = self._carried(group, step)
            try:
                if carried is not None:
                    record, token = carried
                elif group is not None:
                    record, token = self._group(group, step, step_dir, prerequisites)
                else:
                    record, token = self._check(self.card["checks"][item_id], step_dir, prerequisites)
            except KeyboardInterrupt:
                record.update(status="interrupted", finished_at=now())
                self._record(step_dir, record)
                raise
            except (ForgeError, OSError, ValueError) as exc:
                record.update(status="failed", reason=str(exc))
                token = builds.fingerprint({"failed": step, "run": self.run_id})
            finally:
                self.locks.release(lock)
            statuses[item_id], tokens[item_id] = record["status"], token
            record["finished_at"] = now()
            self._record(step_dir, record)
            level = "ERROR" if record["status"] == "failed" else "WARNING" if record["status"] == "blocked" else "INFO"
            self.event(f"Test {step}: {record['status']}" + (f" ({record['reason']})" if record.get("reason") else ""), level)
        self.result["status"] = "passed" if all(s in PASSING for s in statuses.values()) else "failed"

    def _shard_digest(self, group):
        return testing.shard_set_digest(testing.group_files(self.card, group), self.count, group["markers"])

    def _carried(self, group, step):
        """In a --rerun-failed run, keep a step that passed before on the same shard set."""
        before = self.previous["steps"].get(step) if self.previous else None
        if before is None or before.get("status") not in PASSING:
            return None
        if group is not None and (before.get("batch") or {}).get("shard_set_digest") != self._shard_digest(group):
            return None
        origin = before.get("reused_from") or self.previous["run_id"]
        record = {key: value for key, value in before.items()
                  if key not in {"started_at", "finished_at", "duration_seconds", "reason", "reused_from", "rerun"}}
        record.update(status="reused", reused_from=origin, started_at=now(),
                      reason=f"Passed in run {origin}; --rerun-failed does not run it again.")
        self.cases.extend(case for case in self.previous["cases"] if case.get("step") == step)
        return record, builds.fingerprint({"carried": step, "from": origin})

    def _rerun_nodes(self, group, step):
        """Test IDs that failed in the previous run of this step, or None to run the whole shard.

        Only a plain test failure (pytest exit 1, no unexpected skip) narrows the step;
        collection errors, timeouts and evidence problems rerun the shard.
        """
        before = self.previous["steps"].get(step) if self.previous else None
        if (before is None or before.get("status") != "failed" or before.get("pytest_exit_code") != 1
                or before.get("unexpected_skips")
                or (before.get("batch") or {}).get("shard_set_digest") != self._shard_digest(group)):
            return None
        nodes = sorted({case["node"] for case in self.previous["cases"]
                        if case.get("step") == step and case.get("outcome") in {"failed", "error"}})
        return nodes or None

    def _record(self, step_dir, record):
        write_json(step_dir / "result.json", record)
        self.result["steps"].append(record)
        write_json(self.directory / "result.json", self.result)

    def _check(self, check, step_dir, prerequisites):
        from . import testing_package
        partial = self.previous is not None
        cached = None if partial else read_state(self.home, self.key)["checks"].get(check["id"])
        context = {**self.context, "packages": self._packages, "step_environment": self._step_environment}
        exclusive = check.get("exclusive", [])
        with self._resources(exclusive):
            record, token, slot = checker.run_check(
                self.home, check, context, step_dir, cached=cached, prerequisites=prerequisites, full=self.full,
                run_id=self.run_id, progress=self.progress, event=self.event,
                executors={"package": testing_package.executor()})
        record["kind"] = testing.check_kind(check)

        def change(state):
            if slot is not None:
                state["checks"][check["id"]] = slot
            elif record["status"] != "reused":
                state["checks"].pop(check["id"], None)
        if not partial:
            update_state(self.home, self.key, change)
        return record, token

    def _resources(self, names):
        from contextlib import ExitStack
        stack = ExitStack()
        for name in sorted(set(names)):
            stack.enter_context(builds.file_lock(_locks_dir(self.home) / f"resource.{name}.lock", blocking=True))
        return stack

    def _group(self, group, step, step_dir, prerequisites):
        card, project = self.card, self.project
        files = testing.group_files(card, group)
        paths = [file for file in files if testing.bucket(file, self.count) == self.index - 1]
        slot_key = f"{self.index}/{self.count}"
        record = {"id": step, "group_id": group["id"], "kind": "pytest", "status": "running",
                  "started_at": now(), "reason": None,
                  "batch": {"index": self.index, "count": self.count, "paths": paths,
                            "shard_set_digest": testing.shard_set_digest(files, self.count, group["markers"])},
                  "isolate": group["isolate"], "import_mode": group["import_mode"], "markers": group["markers"]}
        if any(status not in PASSING for status, _ in prerequisites.values()):
            record.update(status="blocked", reason="A prerequisite did not pass.")
            return record, builds.fingerprint({"blocked": step, "run": self.run_id})
        if not paths:
            record.update(status="passed", reason="empty batch",
                          counts={"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0,
                                  "xfailed": 0, "consistent": True}, skips=[])
            return record, builds.fingerprint({"empty": record["batch"]["shard_set_digest"], "index": self.index})

        partial = self.previous is not None
        nodes = self._rerun_nodes(group, step) if partial else None
        if partial:
            record["rerun"] = {"from": self.previous["run_id"], "nodes": nodes}
        installed = group["isolate"] == "installed"
        plugin = write_plugin(step_dir, project)
        environment = self._step_environment(group, step_dir, plugin)
        work = step_dir / "work"
        if installed:
            work.mkdir()
        cwd = work if installed else project
        packages = self._packages(environment, cwd)
        effective = _effective(environment)
        inputs = checker.input_manifest(project, group["inputs"])
        hashes = [(path, digest(project / path)) for path in paths]
        fingerprint = builds.fingerprint({
            "executor": EXECUTOR,
            "group": {key: group[key] for key in ("markers", "isolate", "import_mode", "pythonpath", "env",
                                                  "pytest_args", "workers", "skip_allow", "timeout_seconds",
                                                  "distributions", "needs")},
            "batch": slot_key, "paths": hashes, "inputs": inputs,
            "python": {"path": self.python.get("path"), "version": self.python.get("version")},
            "pythonpath": effective, "packages_digest": packages, "environment_origin": self.origin,
            "prerequisites": {need: token for need, (_, token) in prerequisites.items()}})
        identity = self.request["source"]
        source_key = identity["source_key"] if identity else None
        record.update(python={"path": self.python.get("path"), "version": self.python.get("version")},
                      pythonpath=effective, pythonpath_cleared=installed, packages_digest=packages,
                      source_key=source_key, fingerprint=fingerprint, tree_modified=[])
        gate = self.selection["profile"] in testing.GATE_PROFILES
        slot = read_state(self.home, self.key)["groups"].get(group["id"], {}).get("batches", {}).get(slot_key)
        reused = None if partial else self._reusable(group, slot, fingerprint, source_key, gate, step)
        if reused is not None:
            counts, cases = reused
            record.update(status="reused", reused_from=slot["run_id"], counts=counts,
                          skips=skip_summary(cases), junit=slot["junit"],
                          reason="Same fingerprint" + (" and source tree." if gate else "."))
            self.cases.extend(cases)
            return record, slot["dependency_token"]

        if installed:
            from . import testing_package
            probe = testing_package.probe(self.python["path"], environment, cwd, project, group["distributions"])
            write_json(step_dir / "distributions.json", probe)
            problems = testing_package.distribution_problems(probe)
            if problems:
                record.update(status="failed", reason=problems[0], isolation_problems=problems)
                if not partial:
                    self._drop_slot(group["id"], slot_key)
                return record, fingerprint

        with self._resources(group["exclusive"]):
            before = self.source.status()
            started = time.monotonic()
            self.event(f"Test {step}: running " + (f"{len(nodes)} failed test(s) again" if nodes
                                                   else f"{len(paths)} file(s)"))
            script = cmake.write_environment(step_dir / "environment.sh", base=environment)
            argv = pytest_argv(self.python["path"], group, step_dir, paths, project, nodes=nodes)
            try:
                run_command(argv, cwd, script, step_dir, "1", group["timeout_seconds"],
                            progress=self.progress, process_env=environment)
            except ForgeError:
                pass  # The command record below holds the exit code, timeout or start failure.
            finally:
                for stream in ("stdout", "stderr"):
                    source = step_dir / f"command-1.{stream}.log"
                    if source.is_file():
                        shutil.copyfile(source, step_dir / f"{stream}.log")
            command = read_json(step_dir / "command-1.json")
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            after = self.source.status()
        code = command.get("exit_code")
        record["pytest_exit_code"] = code
        rootdir = work if installed else project
        counts, cases, problems = (None, [], [])
        if (step_dir / "junit.xml").is_file():
            counts, cases, problems = parse_junit(step_dir / "junit.xml", project, rootdir, step=step, rewrite=True)
        elif code in (0, 5):
            problems = ["junit missing"]
        ran = sorted({node.split("::")[0] for node in nodes}) if nodes else paths
        identity_problems = self._identity(group, step_dir, ran) if code is not None and code >= 0 else []
        if command.get("status") == "timeout":
            status, reason = "failed", f"timeout after {group['timeout_seconds']} s"
        else:
            unexpected = unexpected_skips(cases, group["skip_allow"])
            if unexpected:
                record["unexpected_skips"] = [{"node": case["node"], "message": case["message"]} for case in unexpected[:50]]
            status, reason = pytest_status(code, counts, problems + identity_problems + (["unexpected skip"] if unexpected else []))
            if command.get("error"):
                reason = command["error"]
        record.update(status=status, reason=reason, counts=counts, skips=skip_summary(cases),
                      junit=str(step_dir / "junit.xml") if (step_dir / "junit.xml").is_file() else None)
        if before is not None and after is not None and before != after:
            record["tree_modified"] = sorted(set(before) ^ set(after))[:TREE_LIMIT]
            self.result["warnings"].append(f"Step {step} changed the source tree.")
        self.cases.extend(cases)
        unchanged = (inputs == checker.input_manifest(project, group["inputs"])
                     and all((project / path).is_file() and digest(project / path) == sha for path, sha in hashes))
        token = fingerprint
        new_slot = None
        if partial:
            # Evidence from a partial run never feeds reuse: a passed slot needs the whole shard.
            if status == "passed":
                token = builds.fingerprint({"signature": fingerprint, "execution": self.run_id})
            return record, token
        if status == "passed" and unchanged:
            token = builds.fingerprint({"signature": fingerprint, "execution": None if group["cache"] else self.run_id})
            if group["cache"]:
                evidence = {}
                for name in ("junit.xml", "stdout.log", "stderr.log", "identity.json"):
                    file = step_dir / name
                    if file.is_file():
                        evidence[str(file)] = digest(file)
                new_slot = {"fingerprint": fingerprint, "packages_digest": packages,
                            "acceptance": self.request.get("evidence"),
                            "source_key": None if record["tree_modified"] else source_key,
                            "run_id": self.run_id, "step_id": step, "junit": str(step_dir / "junit.xml"),
                            "rootdir": str(rootdir), "evidence": evidence, "counts": counts,
                            "dependency_token": token}
        elif status == "passed":
            token = builds.fingerprint({"signature": fingerprint, "execution": self.run_id})

        def change(state):
            batches = state["groups"].setdefault(group["id"], {}).setdefault("batches", {})
            if new_slot is not None:
                batches[slot_key] = new_slot
            elif status != "passed" or not unchanged:
                batches.pop(slot_key, None)
        update_state(self.home, self.key, change)
        return record, token

    def _drop_slot(self, group_id, slot_key):
        update_state(self.home, self.key,
                     lambda state: state["groups"].setdefault(group_id, {}).setdefault("batches", {}).pop(slot_key, None))

    def _reusable(self, group, slot, fingerprint, source_key, gate, step):
        if self.full or not group["cache"] or not slot or slot.get("fingerprint") != fingerprint:
            return None
        if gate and (not slot.get("source_key") or slot["source_key"] != source_key
                     or slot.get("acceptance") != self.request.get("evidence")):
            return None
        for filename, sha in slot.get("evidence", {}).items():
            if not Path(filename).is_file() or digest(Path(filename)) != sha:
                return None
        counts, cases, problems = parse_junit(Path(slot["junit"]), self.project, Path(slot["rootdir"]), step=step)
        if counts is None or problems or unexpected_skips(cases, group["skip_allow"]):
            return None
        return counts, cases

    def _identity(self, group, step_dir, paths):
        if group["isolate"] != "installed":
            return []
        from . import testing_package
        return testing_package.installed_problems(self.project, step_dir, paths)

    def _finish(self):
        start = self.request.get("source")
        end = self.source.identity()
        evidence = self.request.get("evidence")
        if evidence and self.result["status"] == "passed":
            end, current, problems = self._evidence()
            changes = testing_evidence.changes(evidence, current)
            self.result["evidence_end"] = current
            self.result["evidence_changes"] = changes or (problems if not self.request["evidence_problems"] else [])
        elif self.card.get("subject"):
            end = start
        self.result["source_end"] = end
        self.result["source_changed"] = bool(start and (end is None or end["source_key"] != start["source_key"]))
        if self.result["source_changed"]:
            self.result["warnings"].append("The source tree changed during the run.")
        self.result["finished_at"] = now()
        self.result["executed"] = sum(s["status"] in {"passed", "failed"} for s in self.result["steps"])
        self.result["reused"] = sum(s["status"] == "reused" for s in self.result["steps"])
        if self.result["status"] == "running":
            self.result["status"] = "failed"
        with (self.directory / "cases.jsonl").open("w", encoding="utf-8") as stream:
            for case in self.cases:
                stream.write(json.dumps(case, sort_keys=True) + "\n")
        report = run_report(self.result)
        write_json(self.directory / "report.json", report)
        (self.directory / "report.md").write_text(report_markdown(report))
        write_json(self.directory / "result.json", self.result)
        steps = self.result["steps"]
        append_index(self.home, {
            "run_id": self.run_id, "suite_id": self.card["id"], "project_id": self.request["project_id"],
            "profile": self.request["profile"], "acceptance_id": self.acceptance_id,
            "status": self.result["status"], "started_at": self.request["started_at"],
            "finished_at": self.result["finished_at"], "groups": self.request["groups"],
            "batch": self.request["batch"], "partial": self.request["partial"],
            "rerun_of": self.request["rerun_of"],
            "source_key": (start or {}).get("source_key"), "packages_digest": self.request["packages_digest"],
            "environment": self.request["environment"], "host": self.request["host"],
            "reused": [s["id"] for s in steps if s["status"] == "reused"],
            "executed": [s["id"] for s in steps if s["status"] in {"passed", "failed"}]})


# Reports

def run_report(result):
    steps = []
    for step in result["steps"]:
        steps.append({key: step.get(key) for key in ("id", "kind", "group_id", "status", "reason", "counts", "skips",
                                                     "reused_from", "duration_seconds", "junit", "pytest_exit_code", "rerun")}
                     | {"batch": {k: v for k, v in (step.get("batch") or {}).items() if k != "paths"} or None,
                        "fingerprint": (step.get("fingerprint") or "")[:12] or None})
    source = result.get("source") or {}
    return {"schema_version": 1, "run_id": result["run_id"], "suite_id": result["suite_id"],
            "project_id": result.get("project_id"), "profile": result.get("profile"),
            "acceptance_id": result.get("acceptance_id"), "status": result["status"], "error": result.get("error"),
            "partial": bool(result.get("partial")), "rerun_of": result.get("rerun_of"),
            "source": result.get("source"), "source_changed": result.get("source_changed"),
            "evidence": result.get("evidence"), "evidence_problems": result.get("evidence_problems", []),
            "evidence_changes": result.get("evidence_changes", []),
            "environment": result.get("environment"), "packages_digest": result.get("packages_digest"),
            "started_at": result.get("started_at"), "finished_at": result.get("finished_at"),
            "commit": source.get("commit"), "warnings": result.get("warnings", []), "steps": steps}


def _counts_cell(counts, key):
    return str(counts.get(key, 0)) if counts else "-"


def report_markdown(report):
    source = report.get("source") or {}
    lines = [f"# Test run {report['run_id']}", "",
             f"- Suite: {report['suite_id']}" + (f" (catalog card {report['project_id']})" if report.get("project_id") else ""),
             f"- Profile: {report.get('profile') or 'groups'}",
             (f"- Partial: rerun of the failures in {report['rerun_of']}; never accepted" if report.get("partial")
              else f"- Acceptance ID: {report.get('acceptance_id')}"),
             f"- Status: {report['status']}",
             f"- Source: {(source.get('commit') or 'not in Git')[:12]}" + (" (dirty)" if source.get("dirty") else "")
             + (" — changed during the run" if report.get("source_changed") else ""),
             f"- Environment: {json.dumps(report.get('environment'))}",
             f"- Packages: {(report.get('packages_digest') or '-')[:12]}", ""]
    if report.get("error"):
        lines += [f"Error: {report['error']}", ""]
    lines += ["| Step | Status | Passed | Failed | Errors | Skipped | xfailed | Duration | Reused from | Reason |",
              "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |"]
    for step in report["steps"]:
        counts = step.get("counts")
        duration = f"{step['duration_seconds']:.1f} s" if step.get("duration_seconds") is not None else "-"
        lines.append(f"| {step['id']} | {step['status']} | {_counts_cell(counts, 'passed')} | {_counts_cell(counts, 'failed')} | "
                     f"{_counts_cell(counts, 'errors')} | {_counts_cell(counts, 'skipped')} | {_counts_cell(counts, 'xfailed')} | "
                     f"{duration} | {step.get('reused_from') or ''} | {step.get('reason') or ''} |")
    skips = [(step["id"], skip) for step in report["steps"] for skip in (step.get("skips") or [])]
    if skips:
        lines += ["", "## Skips", ""]
        lines += [f"- {step}: {skip['count']} × {skip['message']}" for step, skip in skips]
    if report.get("warnings"):
        lines += ["", "## Warnings", ""] + [f"- {warning}" for warning in report["warnings"]]
    return "\n".join(lines) + "\n"


# Records, summaries and status

def test_runs(home):
    """Yield (request, result) for every jforge test run, newest first."""
    base = home.root / "runs"
    for path in sorted(base.glob("checker-*/request.json"), reverse=True):
        try:
            request = read_json(path)
        except ForgeError:
            continue
        if request.get("catalog") != "test":
            continue
        final = path.parent / "result.json"
        result = None
        if final.is_file():
            try:
                result = read_json(final)
            except ForgeError:
                result = None
        yield request, result


def summarize(home, acceptance_id, *, rebuild=False, missing_ok=False):
    identifier(acceptance_id)
    home.require_initialized()
    if rebuild:
        rebuild_index(home)
    entries = {}
    for request, result in test_runs(home):
        if request.get("acceptance_id") != acceptance_id or request.get("partial"):
            continue
        name = request.get("project_id") or request["suite_id"]
        entries.setdefault(name, []).append((request, result))
    if not entries:
        if missing_ok:
            return None
        raise ForgeError(f"No test runs recorded for acceptance {acceptance_id}.",
                         hint="Pass the --acceptance-id that the runs used.")
    summary = []
    for name, runs in sorted(entries.items()):
        summary.append(_entry(name, runs))
    status = "passed" if all(entry["status"] == "passed" for entry in summary) else "failed"
    report = {"schema_version": 1, "acceptance_id": acceptance_id, "created_at": now(),
              "forge_version": __version__, "status": status,
              "accepted": all(entry["accepted"] for entry in summary), "entries": summary}
    directory = home.root / "checker" / "reports"
    write_json(directory / f"{acceptance_id}.json", report)
    (directory / f"{acceptance_id}.md").write_text(summary_markdown(report))
    report["path"] = str(directory / f"{acceptance_id}.json")
    return report


def _entry(name, runs):
    reasons = []
    requests = [request for request, _ in runs]
    first = requests[0]
    profiles = {request.get("profile") for request in requests}
    suites = {request["suite_id"] for request in requests}
    if len(profiles) > 1 or len(suites) > 1:
        reasons.append("mixed profile")
    keys = {(request.get("source") or {}).get("source_key") for request in requests}
    if len(keys) > 1:
        reasons.append("mixed source")
    environments = {json.dumps([request.get("environment"), request.get("packages_digest")], sort_keys=True)
                    for request in requests}
    if len(environments) > 1:
        reasons.append("mixed environment")
    evidence = first.get("evidence")
    if any(request.get("evidence") != evidence for request in requests):
        reasons.append("mixed acceptance evidence")
    evidence_problems = list(dict.fromkeys(problem for request in requests
                                           for problem in request.get("evidence_problems", [])))
    evidence_changes = list(dict.fromkeys(change for _, result in runs if result
                                          for change in result.get("evidence_changes", [])))
    wanted = []
    for request in requests:
        for item_id in request.get("groups", []):
            if item_id not in wanted:
                wanted.append(item_id)
    groups, checks, check_errors, source_changed = {}, {}, {}, False
    started, finished, step_seconds = [], [], 0.0
    for request, result in runs:
        started.append(request.get("started_at"))
        if result is None or result.get("status") == "running" or not result.get("finished_at"):
            reasons.append(f"incomplete run {request['run_id']}")
            continue
        finished.append(result["finished_at"])
        source_changed = source_changed or bool(result.get("source_changed"))
        if result["status"] != "passed":
            if result.get("error") and result["status"] != "blocked":
                reasons.append(result["error"])
            elif not result.get("steps") and result["status"] != "blocked":
                reasons.append(f"run {request['run_id']} {result['status']}")
        for step in result.get("steps", []):
            if step.get("kind") == "pytest":
                batch = step["batch"]
                slot = groups.setdefault(step["group_id"], {"count": batch["count"], "digest": batch["shard_set_digest"],
                                                            "shards": {}})
                if slot["count"] != batch["count"] or slot["digest"] != batch["shard_set_digest"]:
                    raise ForgeError(f"Acceptance {name}: group {step['group_id']} mixes shard sets.",
                                     hint="Run every shard with the same --batch n and the same files.")
                if batch["index"] in slot["shards"]:
                    raise ForgeError(f"Acceptance {name}: group {step['group_id']} has shard "
                                     f"{batch['index']}/{batch['count']} twice.",
                                     hint="Use a new --acceptance-id for a repeated shard.")
                slot["shards"][batch["index"]] = step
                step_seconds += step.get("duration_seconds") or 0.0
            else:
                checks.setdefault(step["id"], []).append(step["status"])
                if step.get("error") or step.get("reason"):
                    check_errors[step["id"]] = step.get("error") or step["reason"]
    group_rows = {}
    for item_id in wanted:
        if item_id in groups:
            slot = groups[item_id]
            missing = sorted(set(range(1, slot["count"] + 1)) - set(slot["shards"]))
            if missing:
                reasons.append(f"group {item_id} misses shard(s) " + ", ".join(f"{k}/{slot['count']}" for k in missing))
            steps = list(slot["shards"].values())
            bad = [step["id"] for step in steps if step["status"] not in PASSING]
            if bad:
                reasons.append("failed: " + ", ".join(bad))
            totals = Counter()
            skips = Counter()
            for step in steps:
                for key, value in (step.get("counts") or {}).items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        totals[key] += value
                for skip in step.get("skips") or []:
                    skips[skip["message"]] += skip["count"]
            group_rows[item_id] = {"count": slot["count"], "shards": len(steps), "counts": dict(totals),
                                   "skips": [{"message": m, "count": c} for m, c in skips.most_common()],
                                   "duration_seconds": round(sum(step.get("duration_seconds") or 0 for step in steps), 3),
                                   "reused": sum(step["status"] == "reused" for step in steps)}
        elif item_id in checks:
            if any(status not in PASSING for status in checks[item_id]):
                reasons.append(f"{item_id}: {check_errors.get(item_id) or 'check did not pass'}")
        else:
            reasons.append(f"{item_id} has no result")
    complete = (len(finished) == len(runs) and not any(result["status"] == "interrupted"
                                                       for _, result in runs if result)
                and all(row["count"] == row["shards"] for row in group_rows.values())
                and (all(item in group_rows or item in checks for item in wanted)
                     or (len(runs) == 1 and first["batch"]["count"] == 1)))
    blocked = any(result and result.get("status") == "blocked" for _, result in runs)
    status = "blocked" if blocked else "failed" if reasons or not complete else "passed"
    source = first.get("source")
    profile = first.get("profile")
    accepted = (status == "passed" and profile in testing.GATE_PROFILES and source is not None
                and bool(source.get("source_key")) and not source.get("dirty") and not source_changed
                and evidence is not None and not evidence_problems and not evidence_changes)
    if status == "passed" and not accepted:
        if profile not in testing.GATE_PROFILES:
            reasons.append(f"profile {profile or 'groups'} is not a gate profile")
        elif source is None or not source.get("source_key"):
            reasons.append("unbound: the project is not in Git")
        elif source.get("dirty"):
            reasons.append("uncommitted changes")
        elif source_changed:
            reasons.append("source changed during the run")
    reasons.extend(evidence_problems + evidence_changes)
    wall = None
    if started and finished:
        from datetime import datetime
        wall = round((datetime.fromisoformat(max(finished)) - datetime.fromisoformat(min(started))).total_seconds(), 3)
    return {"entry": name, "suite_id": first["suite_id"], "project_id": first.get("project_id"),
            "card": first.get("card"), "profile": profile, "status": status, "accepted": accepted,
            "reasons": reasons, "source": source, "source_changed": source_changed,
            "environment": first.get("environment"), "packages_digest": first.get("packages_digest"),
            "evidence": evidence, "complete": complete, "selected": wanted,
            "finished_at": max(finished, default=None),
            "runs": sorted(request["run_id"] for request in requests), "groups": group_rows,
            "checks": {item_id: values for item_id, values in checks.items()},
            "step_seconds": round(step_seconds, 3), "wall_seconds": wall}


def summary_markdown(report):
    lines = [f"# Acceptance {report['acceptance_id']}", "",
             f"- Status: {report['status']}", f"- Accepted: {'yes' if report['accepted'] else 'no'}",
             f"- Created: {report['created_at']} by Jarvis-Forge {report['forge_version']}", ""]
    for entry in report["entries"]:
        source = entry.get("source") or {}
        lines += [f"## {entry['entry']}", "",
                  f"- Suite: {entry['suite_id']}, profile {entry.get('profile') or 'groups'}",
                  f"- Status: {entry['status']}, accepted: {'yes' if entry['accepted'] else 'no'}",
                  f"- Source: {(source.get('commit') or 'not in Git')[:12]}"
                  + (f" (version {source['version']})" if source.get("version") else "")
                  + (" (dirty)" if source.get("dirty") else ""),
                  f"- Environment: {json.dumps(entry.get('environment'))}, packages {(entry.get('packages_digest') or '-')[:12]}",
                  f"- Runs: {', '.join(entry['runs'])}",
                  f"- Step time {entry['step_seconds']:.1f} s, wall time {entry['wall_seconds'] or 0:.1f} s", ""]
        if entry["reasons"]:
            lines += ["Reasons:", ""] + [f"- {reason}" for reason in entry["reasons"]] + [""]
        lines += ["| Group | Shards | Passed | Failed | Errors | Skipped | xfailed | Reused shards | Duration |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for group_id, row in entry["groups"].items():
            counts = row["counts"]
            lines.append(f"| {group_id} | {row['shards']}/{row['count']} | {counts.get('passed', 0)} | {counts.get('failed', 0)} | "
                         f"{counts.get('errors', 0)} | {counts.get('skipped', 0)} | {counts.get('xfailed', 0)} | "
                         f"{row['reused']} | {row['duration_seconds']:.1f} s |")
        for check_id, statuses in entry["checks"].items():
            lines.append(f"| {check_id} (check) | - | - | - | - | - | - | - | {', '.join(sorted(set(statuses)))} |")
        skips = [(group_id, skip) for group_id, row in entry["groups"].items() for skip in row["skips"]]
        if skips:
            lines += ["", "Skips:", ""] + [f"- {group}: {skip['count']} × {skip['message']}" for group, skip in skips]
        lines.append("")
    return "\n".join(lines)


def rebuild_index(home):
    rows = []
    for request, result in test_runs(home):
        result = result or {}
        steps = result.get("steps", [])
        rows.append({"run_id": request["run_id"], "suite_id": request["suite_id"],
                     "project_id": request.get("project_id"), "profile": request.get("profile"),
                     "acceptance_id": request.get("acceptance_id"), "status": result.get("status", "incomplete"),
                     "started_at": request.get("started_at"), "finished_at": result.get("finished_at"),
                     "groups": request.get("groups"), "batch": request.get("batch"),
                     "partial": bool(request.get("partial")), "rerun_of": request.get("rerun_of"),
                     "source_key": (request.get("source") or {}).get("source_key"),
                     "packages_digest": request.get("packages_digest"), "environment": request.get("environment"),
                     "host": request.get("host"),
                     "reused": [s["id"] for s in steps if s.get("status") == "reused"],
                     "executed": [s["id"] for s in steps if s.get("status") in {"passed", "failed"}]})
    path = home.root / "checker" / "index.jsonl"
    with builds.file_lock(_locks_dir(home) / "index.lock", blocking=True):
        temporary = path.with_name(".index.jsonl.tmp")
        temporary.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in reversed(rows)))
        os.replace(temporary, path)


def status(home, path, *, profile="acceptance", environment_name=None, root=None, project=None,
           acceptance_id=None):
    target = testing.load_target(path, root=root, project=project)
    home.require_initialized()
    reports = []
    for file in (home.root / "checker" / "reports").glob("*.json"):
        try:
            reports.append(read_json(file))
        except ForgeError:
            continue
    cards = []
    for entry in target["entries"]:
        card = entry["card"]
        env = environment_name or entry["env"]
        selection = testing.select(card, profile)
        identity, current, problems = testing_evidence.snapshot(home, card, selection, env)
        name = env or card["environment"].get("name")
        matches = [(report, item) for report in reports for item in report.get("entries", [])
                   if item.get("suite_id") == card["id"] and item.get("profile") == profile
                   and (acceptance_id is None or report["acceptance_id"] == acceptance_id)
                   and (entry["id"] is None or item.get("project_id") in (None, entry["id"]))
                   and (item.get("environment") or {}).get("name") == name
                   and item.get("complete", True)]
        matches.sort(key=lambda pair: (pair[1].get("finished_at") or pair[0]["created_at"],
                                       pair[0]["acceptance_id"]), reverse=True)
        report, previous = next(((report, item) for report, item in matches
                                 if item.get("evidence") == current), matches[0] if matches else (None, None))
        if problems:
            verdict = "stale" if previous and previous.get("accepted") else "blocked"
            reasons = problems
        elif previous is None:
            verdict, reasons = "not_run", [f"No {profile} report for this card."]
        elif differences := testing_evidence.changes(previous.get("evidence"), current):
            verdict, reasons = "stale", differences
        elif previous["accepted"]:
            verdict, reasons = "accepted", []
        else:
            verdict = "blocked" if previous["status"] == "blocked" else "failed"
            reasons = previous["reasons"] or ["The latest complete run did not pass."]
        argv = ["jforge", "test", "accept", target["path"]]
        for flag, value in (("--profile", profile if profile != "acceptance" else None), ("--env", environment_name),
                            ("--root", root), ("--project", project)):
            if value is not None:
                argv.extend((flag, str(value)))
        for external in (current.get("bench") or {}).values():
            if external["reason"]:
                argv = external["next_argv"]
                break
        row = {"project_id": entry["id"], "suite_id": card["id"], "profile": profile, "environment": name,
               "source": identity, "accepted": verdict == "accepted", "status": verdict,
               "acceptance_id": report["acceptance_id"] if report else None,
               "reason": " ".join(reasons) or None, "reasons": reasons,
               "next_argv": argv if verdict != "accepted" else None}
        cards.append(row)
    return {"target": target["kind"], "path": target["path"], "catalog_id": target["id"], "profile": profile,
            "accepted": all(row["accepted"] for row in cards), "cards": cards}


def accept(home, path, *, profile="acceptance", full=False, environment_name=None, root=None, project=None,
           jobs=1, progress=None):
    if profile not in testing.GATE_PROFILES:
        raise ForgeError("test accept requires the acceptance or release profile.")
    result = run(home, path, profile=profile, full=full, environment_name=environment_name, root=root,
                 project=project, jobs=jobs, progress=progress, strict=True)
    if result["status"] == "interrupted":
        return result
    verdict = status(home, path, profile=profile, environment_name=environment_name, root=root, project=project,
                     acceptance_id=result["acceptance_id"])
    return {**result, **verdict, "accepted": verdict["accepted"] and result["status"] == "passed"}


def history(home, *, suite=None, project=None, profile=None, group=None, case=None):
    rows = []
    for request, result in test_runs(home):
        if suite and request["suite_id"] != suite:
            continue
        if project and request.get("project_id") != project:
            continue
        if profile and request.get("profile") != profile:
            continue
        if group and group not in request.get("groups", []):
            continue
        result = result or {}
        row = {"run_id": request["run_id"], "suite_id": request["suite_id"], "project_id": request.get("project_id"),
               "profile": request.get("profile"), "acceptance_id": request.get("acceptance_id"),
               "batch": request.get("batch"), "partial": bool(request.get("partial")),
               "status": result.get("status", "incomplete"), "started_at": request.get("started_at"),
               "commit": (request.get("source") or {}).get("commit"),
               "dirty": (request.get("source") or {}).get("dirty"),
               "executed": result.get("executed"), "reused": result.get("reused")}
        if case is not None:
            matches = []
            file = home.root / "runs" / request["run_id"] / "cases.jsonl"
            if file.is_file():
                for line in file.read_text(encoding="utf-8").splitlines():
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if case in item.get("node", ""):
                        matches.append({key: item.get(key) for key in ("step", "node", "outcome", "duration", "message")})
            if not matches:
                continue
            row["cases"] = matches
        rows.append(row)
    return rows


def report(home, identifier_value):
    identifier(identifier_value)
    home.require_initialized()
    run_dir = home.root / "runs" / identifier_value
    if (run_dir / "request.json").is_file():
        request = read_json(run_dir / "request.json")
        if request.get("catalog") != "test":
            raise ForgeError(f"Run {identifier_value} is not a jforge test run.", hint=f"jforge show {identifier_value}")
        file = run_dir / "report.json"
        if file.is_file():
            value = read_json(file)
        else:
            value = run_report({**request, **read_json(run_dir / "result.json")} if (run_dir / "result.json").is_file()
                               else {**request, "status": "incomplete", "steps": []})
        return {"kind": "run", "report": value, "markdown": report_markdown(value)}
    file = home.root / "checker" / "reports" / f"{identifier_value}.json"
    if file.is_file():
        value = read_json(file)
        return {"kind": "acceptance", "report": value, "markdown": summary_markdown(value)}
    raise ForgeError(f"No test run or acceptance report named {identifier_value}.", hint="jforge test history")
