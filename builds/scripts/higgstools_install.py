"""Install the built HiggsTools 1.1.3 bridge into the active Python.

The native module is produced by the CMake step. This script copies that
module into the Python package, builds one wheel without compiling again,
and installs it with pip --no-index --no-deps. No PYTHONPATH entry is added.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


PROBE = r'''
import hashlib, importlib.metadata, json, pathlib, site, sys
import Higgs
import Higgs._Higgs as native
import Higgs.predictions as hp
import Higgs.signals
version = importlib.metadata.version("HiggsTools")
if version != "1.1.3":
    raise RuntimeError("Expected HiggsTools 1.1.3, got " + version)
if getattr(native.signals, "_htsAssignmentTraceVersion", None) != 2:
    raise RuntimeError("Missing assignment bridge v2")
if not callable(getattr(native.signals, "_htsAssignmentTrace", None)):
    raise RuntimeError("Missing assignment trace function")
if getattr(native.signals, "_htsMeasurementBreakdownVersion", None) != 1 or not callable(
        getattr(native.signals.Signals, "_htsChisqWithMeasurementBreakdown", None)):
    raise RuntimeError("Missing HS measurement breakdown bridge v1")
binary = pathlib.Path(native.__file__).resolve()
roots = [pathlib.Path(p).resolve() for p in [*site.getsitepackages(), site.getusersitepackages()]]
if not any(binary.is_relative_to(p) for p in roots):
    raise RuntimeError("Binding is not normally installed in site-packages")
reference = hp.SMHiggs(125.)
if not reference.totalWidth() > 0:
    raise RuntimeError("Native reference calculation failed")
print(json.dumps({"version": version, "bridge_version": 2, "measurement_breakdown_version": 1,
    "python": sys.executable, "prefix": sys.prefix,
    "binary": str(binary), "package": Higgs.__file__,
    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    "reference_width_GeV": reference.totalWidth()}))
'''


def clean_environment():
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def native_module(build):
    binaries = sorted(path for path in build.rglob("_Higgs*.so") if path.is_file())
    if len(binaries) != 1:
        found = ", ".join(str(path) for path in binaries) or "none"
        raise SystemExit(f"Expected one _Higgs module under {build}, found {found}")
    binary = binaries[0]
    if shutil.which("ldd"):
        linked = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, check=True)
        missing = [line.strip() for line in linked.stdout.splitlines() if "not found" in line]
        if missing:
            raise SystemExit("Native binding is missing libraries:\n" + "\n".join(missing))
    return binary


def build_wheel(source, binary, wheels):
    from setuptools import Distribution, Extension

    package = Path(tempfile.mkdtemp(prefix="higgs-package-"))
    try:
        staged = package / "Higgs"
        shutil.copytree(source / "python" / "Higgs", staged)
        shutil.copy2(binary, staged / binary.name)
        dist = Distribution({"ext_modules": [Extension("Higgs._Higgs", sources=[])]})
        command = dist.get_command_obj("build")
        command.ensure_finalized()
        destination = source / command.build_lib / "Higgs"
        shutil.copytree(staged, destination, dirs_exist_ok=True)
        wheels.mkdir(parents=True, exist_ok=True)
        for old in wheels.glob("*.whl"):
            old.unlink()
        stage = Path(tempfile.mkdtemp(prefix="higgs-wheel-"))
        try:
            subprocess.run(
                [sys.executable, "setup.py", "bdist_wheel", "--skip-build",
                 "--bdist-dir", str(stage), "--dist-dir", str(wheels)],
                cwd=source, env=clean_environment(), check=True,
            )
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    finally:
        shutil.rmtree(package, ignore_errors=True)
    built = list(wheels.glob("*.whl"))
    if len(built) != 1:
        raise SystemExit(f"Expected one wheel in {wheels}")
    return built[0]


def install_wheel(wheel):
    subprocess.run(
        [sys.executable, "-m", "pip", "--isolated", "install", "--no-user", "--no-index",
         "--no-deps", "--force-reinstall", "--disable-pip-version-check", str(wheel)],
        env=clean_environment(), check=True,
    )


def probe():
    result = subprocess.run(
        [sys.executable, "-c", PROBE], env=clean_environment(), capture_output=True, text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SystemExit("HiggsTools runtime verification failed:\n" + detail)
    line = result.stdout.strip().splitlines()[-1]
    print(line, flush=True)
    return json.loads(line)


def main(argv):
    if len(argv) != 4:
        raise SystemExit("usage: higgstools_install.py SOURCE_DIR BUILD_DIR WHEEL_DIR")
    source, build, wheels = (Path(argv[1]).resolve(), Path(argv[2]).resolve(), Path(argv[3]).resolve())
    if not (source / "setup.py").is_file():
        raise SystemExit(f"HiggsTools setup.py is missing: {source}")
    binary = native_module(build)
    wheel = build_wheel(source, binary, wheels)
    print(f"wheel {wheel} sha256 {hashlib.sha256(wheel.read_bytes()).hexdigest()}", flush=True)
    install_wheel(wheel)
    identity = probe()
    if identity["version"] != "1.1.3" or identity["bridge_version"] != 2:
        raise SystemExit("Installed HiggsTools does not match the bridge build")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"HiggsTools install command failed with exit {exc.returncode}") from exc
