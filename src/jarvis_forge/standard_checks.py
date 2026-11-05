"""Standard dependency probes; the worker runs under the selected Python using stdlib only."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig


def validate(spec):
    from .storage import ForgeError
    from packaging.specifiers import SpecifierSet, InvalidSpecifier
    if not isinstance(spec, dict) or not spec or set(spec) - {"python", "tools"}:
        raise ForgeError("Standard checks accept python and tools sections.")
    python = spec.get("python", {})
    if not isinstance(python, dict) or set(python) - {"version", "development", "modules"}:
        raise ForgeError("Python checks accept version, development and modules.")
    if "development" in python and not isinstance(python["development"], bool):
        raise ForgeError("python.development must be boolean.")
    modules = python.get("modules", [])
    if isinstance(modules, list):
        modules = {name: None for name in modules if isinstance(name, str)}
        if len(modules) != len(python.get("modules", [])):
            raise ForgeError("python.modules must contain distinct module names.")
    if not isinstance(modules, dict):
        raise ForgeError("python.modules must be a list or name/version mapping.")
    constraints = [python.get("version")]
    for name, value in modules.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name):
            raise ForgeError(f"Invalid Python module name: {name}")
        if isinstance(value, dict):
            if set(value) - {"version", "version_attribute", "distribution"} or not all(isinstance(v, str) for v in value.values()):
                raise ForgeError(f"Invalid module check: {name}")
            constraints.append(value.get("version"))
        elif value is not None and not isinstance(value, str):
            raise ForgeError(f"Module version constraint must be a string: {name}")
        else:
            constraints.append(value)
    tools = spec.get("tools", [])
    if not (isinstance(tools, list) and all(isinstance(v, str) and v for v in tools)) and not (
            isinstance(tools, dict) and all(isinstance(k, str) and k and isinstance(v, str) for k, v in tools.items())):
        raise ForgeError("tools must be executable names or a name/version mapping.")
    if isinstance(tools, dict):
        constraints.extend(tools.values())
    for value in constraints:
        if value is not None:
            if not isinstance(value, str):
                raise ForgeError("Version constraints must be strings.")
            try:
                SpecifierSet(value)
            except InvalidSpecifier as exc:
                raise ForgeError(f"Invalid dependency version constraint: {value}") from exc


def probe(spec):
    """Import modules and report actual versions/paths without importing Forge in the worker."""
    import os
    report = {"python": {"path": sys.executable, "version": sys.version.split()[0]}, "modules": {}, "tools": {}}
    python = spec.get("python", {})
    if python.get("development"):
        header = Path(sysconfig.get_path("include")) / "Python.h"
        library = next((Path(sysconfig.get_config_var("LIBDIR") or sys.prefix) / value
                        for value in (sysconfig.get_config_var("LDLIBRARY"), sysconfig.get_config_var("LIBRARY"))
                        if value and (Path(sysconfig.get_config_var("LIBDIR") or sys.prefix) / value).is_file()), None)
        report["python"].update(header=str(header), library=str(library) if library else None,
                                development=header.is_file() and library is not None)
    modules = python.get("modules", [])
    modules = {name: {} for name in modules} if isinstance(modules, list) else modules
    for name, settings in modules.items():
        settings = settings if isinstance(settings, dict) else {}
        try:
            module = importlib.import_module(name)
            attribute = settings.get("version_attribute", "__version__")
            version = module
            for part in attribute.split("."):
                version = getattr(version, part, None)
            if callable(version):
                version = version()
            if version is None:
                try:
                    version = importlib.metadata.version(settings.get("distribution", name))
                except importlib.metadata.PackageNotFoundError:
                    pass
            report["modules"][name] = {"status": "found", "path": str(getattr(module, "__file__", None)),
                                        "version": str(version) if version is not None else None}
        except Exception as exc:
            report["modules"][name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    tools = spec.get("tools", [])
    for name in tools:
        selection = os.environ.get({"cc": "CC", "c++": "CXX", "gfortran": "FC"}.get(name, ""), name)
        try:
            parts = shlex.split(selection)
            if len(parts) != 1:
                raise ValueError("Select a single executable path")
            executable = shutil.which(parts[0])
            if not executable:
                raise FileNotFoundError(selection)
            result = subprocess.run([executable, "--version"], capture_output=True, text=True, errors="replace", timeout=30)
            if result.returncode:
                raise RuntimeError(f"--version exited {result.returncode}")
            line = (result.stdout or result.stderr).splitlines()[0]
            version = re.search(r"\d+\.\d+(?:\.\d+)*", line)
            report["tools"][name] = {"status": "found", "path": executable, "description": line,
                                      "version": version.group(0) if version else None}
        except Exception as exc:
            report["tools"][name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    return report


def evaluate(report, spec):
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version, InvalidVersion
    failures = []

    def version(label, observed, constraint):
        if constraint:
            try:
                good = observed is not None and Version(observed) in SpecifierSet(constraint)
            except InvalidVersion:
                good = False
            if not good:
                failures.append(f"{label}: version {observed or 'unknown'} does not satisfy {constraint}")

    version("Python", report["python"]["version"], spec.get("python", {}).get("version"))
    if spec.get("python", {}).get("development") and not report["python"].get("development"):
        failures.append("Python development headers/library are missing")
    modules = spec.get("python", {}).get("modules", [])
    modules = {name: None for name in modules} if isinstance(modules, list) else modules
    for name, expected in modules.items():
        observed = report["modules"][name]
        if observed["status"] != "found":
            failures.append(f"Python module {name}: {observed['error']}")
        else:
            version(name, observed.get("version"), expected.get("version") if isinstance(expected, dict) else expected)
    tools = spec.get("tools", [])
    for name, observed in report["tools"].items():
        if observed["status"] != "found":
            failures.append(f"Tool {name}: {observed['error']}")
        elif isinstance(tools, dict):
            version(name, observed.get("version"), tools[name])
    return failures


def execute(spec, python, invoke, directory):
    from .storage import ForgeError, read_json, write_json
    validate(spec)
    request, output = directory / "check-request.json", directory / "checks.json"
    write_json(request, spec)
    invoke([python, str(Path(__file__).resolve()), "--request", str(request), "--report", str(output)])
    report = read_json(output)
    failures = evaluate(report, spec)
    report.update(status="failed" if failures else "passed", failures=failures, requirements=spec)
    write_json(output, report)
    if failures:
        raise ForgeError("Dependency checks failed: " + "; ".join(failures), code=1)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    observed = probe(json.loads(args.request.read_text()))
    args.report.write_text(json.dumps(observed, indent=2) + "\n")
    print("Python", observed["python"]["version"], observed["python"]["path"])
    if "development" in observed["python"]:
        print("Python development:", "found" if observed["python"]["development"] else "missing")
    for category in ("modules", "tools"):
        for name, row in observed[category].items():
            print(name, row.get("version") or "version unavailable", row.get("path") or row.get("error"))
