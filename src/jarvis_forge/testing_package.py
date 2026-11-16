"""Installed-isolation probes and the package standard check for jforge test.

Design: Jarvis-Books Docs/Jarvis-Forge/TEST_CATALOG_DESIGN.md (isolate: installed,
package 标准 check). Forge never installs or builds the package; the user prepares
the environment with pyenv and registers it with jforge env add.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

from . import cmake
from .package_operations import run_command
from .storage import ForgeError, digest, write_json


MESSAGE_LIMIT = 500
ISOLATION_DROPS = ("PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS")

_DISTRIBUTIONS = r'''
import importlib, importlib.metadata as metadata, json, sys
from pathlib import Path, PurePosixPath

names, modules_wanted, project = json.loads(sys.argv[1]), json.loads(sys.argv[2]), Path(sys.argv[3]).resolve()


def inside(path):
    try:
        return Path(path).resolve().is_relative_to(project)
    except (OSError, ValueError):
        return False


def top_level(dist, name):
    text = dist.read_text("top_level.txt")
    if text:
        return [line.strip() for line in text.splitlines() if line.strip()]
    found = []
    for file in dist.files or []:
        parts = PurePosixPath(str(file)).parts
        if not parts or parts[0].endswith((".dist-info", ".egg-info")) or parts[0] in ("..", "__pycache__"):
            continue
        if len(parts) >= 2 and parts[1] == "__init__.py":
            found.append(parts[0])
        elif len(parts) == 1 and parts[0].endswith(".py") and not parts[0].startswith("__editable__"):
            found.append(parts[0][:-3])
    return sorted(set(found)) or [name.replace("-", "_")]


def module_row(name):
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:500]}
    file = getattr(module, "__file__", None)
    paths = [str(Path(p).resolve()) for p in getattr(module, "__path__", []) or []]
    return {"file": str(Path(file).resolve()) if file else None, "path": paths,
            "inside_project": bool(file and inside(file)) or any(inside(p) for p in paths)}


rows = []
for name in names:
    row = {"name": name}
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        row["found"] = False
        rows.append(row)
        continue
    try:
        direct = dist.read_text("direct_url.json")
    except Exception:
        direct = None
    files = []
    for file in dist.files or []:
        try:
            located = dist.locate_file(file)
        except Exception:
            continue
        if inside(located):
            files.append(str(Path(located).resolve()))
    row.update(found=True, version=dist.version, direct_url=json.loads(direct) if direct else None,
               files_in_project=files[:20], modules={m: module_row(m) for m in top_level(dist, name)})
    rows.append(row)
extra = {m: module_row(m) for m in modules_wanted}
print(json.dumps({"executable": sys.executable, "sys_path": sys.path, "distributions": rows, "modules": extra}))
'''


def isolated_environment(environment):
    clean = {key: value for key, value in environment.items() if key not in ISOLATION_DROPS}
    return clean


def probe(python, environment, cwd, project, distributions, modules=()):
    try:
        result = subprocess.run([python, "-c", _DISTRIBUTIONS, json.dumps(list(distributions)),
                                 json.dumps(list(modules)), str(project)],
                                cwd=cwd, env=environment, capture_output=True, stdin=subprocess.DEVNULL, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForgeError(f"Cannot probe installed distributions with {python}: {exc}", code=1) from exc
    if result.returncode:
        raise ForgeError(f"Cannot probe installed distributions with {python}: "
                         + result.stderr.decode(errors="replace")[-MESSAGE_LIMIT:], code=1)
    return json.loads(result.stdout)


def distribution_problems(record):
    problems = []
    for row in record["distributions"]:
        name = row["name"]
        if not row.get("found"):
            problems.append(f"distribution {name} is not installed")
            continue
        direct = row.get("direct_url") or {}
        if (direct.get("dir_info") or {}).get("editable"):
            problems.append(f"distribution {name} is an editable install")
        if row.get("files_in_project"):
            problems.append(f"distribution {name} has files inside the project: {row['files_in_project'][0]}")
        for module, info in row.get("modules", {}).items():
            if info.get("error"):
                problems.append(f"cannot import {module} from {name}: {info['error']}")
            elif info.get("inside_project"):
                problems.append(f"module {module} of {name} imports from the project: {info.get('file')}")
    for module, info in record.get("modules", {}).items():
        if info.get("error"):
            problems.append(f"cannot import {module}: {info['error']}")
        elif info.get("inside_project"):
            problems.append(f"module {module} imports from the project: {info.get('file')}")
    return problems


def _allowed(project, directories, path):
    for directory in directories:
        if directory == project:
            if path.parent == project:
                return True
        elif path.is_relative_to(directory):
            return True
    return False


def installed_problems(project, step_dir, paths):
    """Judge an installed-mode pytest session from the identity plugin records."""
    project = Path(project).resolve()
    records = sorted(step_dir.glob("identity*.json"))
    if not records:
        return ["identity record missing"]
    directories = sorted({(project / path).resolve().parent for path in paths})
    problems = []
    for file in records:
        identity = json.loads(file.read_text())
        imported = [(name, Path(module)) for name, module in identity.get("project_modules", {}).items()
                    if not _allowed(project, directories, Path(module))]
        if imported:
            name, module = imported[0]
            problems.append(f"source tree imported: {name} from {module}")
        loaded = set(identity.get("conftest", []))
        for directory in directories:
            conftest = directory / "conftest.py"
            if conftest.is_file() and str(conftest) not in loaded:
                problems.append(f"conftest not loaded: {conftest}")
    return sorted(set(problems))


def executor():
    return {"digest": digest(Path(__file__)), "run": run_package_check}


def run_package_check(spec, context, step_dir):
    package = spec["package"]
    project = Path(context["project"])
    python = context["observed"].get("python", {}).get("path")
    if not python:
        raise ForgeError("The selected environment has no python3 on PATH.", code=1)
    work = step_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    environment = isolated_environment(context["environment"])
    record = probe(python, environment, work, project, [package["distribution"]], package.get("modules", []))
    write_json(step_dir / "distributions.json", record)
    problems = distribution_problems(record)
    from .testing_run import _static_version
    version = _static_version(project)
    row = record["distributions"][0]
    if version and row.get("found") and row.get("version") != version:
        problems.append(f"installed {package['distribution']} {row.get('version')} differs from source version {version}")
    commands = []
    if not problems:
        script = cmake.write_environment(step_dir / "environment.sh", base=environment)
        for number, argv in enumerate(package.get("commands", []), start=1):
            try:
                run_command(argv, work, script, step_dir, str(number), 600, process_env=environment)
            except ForgeError:
                pass
            entry = json.loads((step_dir / f"command-{number}.json").read_text())
            commands.append({"argv": argv, "exit_code": entry.get("exit_code"), "status": entry.get("status")})
            if entry.get("exit_code") != 0:
                problems.append(f"command {' '.join(argv)} exited {entry.get('exit_code')}")
        for stream in ("stdout", "stderr"):
            with (step_dir / f"{stream}.log").open("wb") as merged:
                for number in range(1, len(commands) + 1):
                    part = step_dir / f"command-{number}.{stream}.log"
                    if part.is_file():
                        with part.open("rb") as source:
                            shutil.copyfileobj(source, merged)
    write_json(step_dir / "checks.json", {"status": "failed" if problems else "passed", "problems": problems,
                                          "distribution": row, "source_version": version, "commands": commands,
                                          "pythonpath_cleared": True})
    if problems:
        raise ForgeError("; ".join(problems), code=1)
