"""Observe the user's activated environment without choosing or changing it.

This file also runs directly under Forge's controller Python with -I. Its
collector uses only the standard library, independently of the target Python.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile


VARIABLES = ("PATH", "CC", "CXX", "FC", "CFLAGS", "CXXFLAGS", "FFLAGS", "FCFLAGS",
             "CPPFLAGS", "LDFLAGS", "CMAKE_PREFIX_PATH", "PKG_CONFIG_PATH",
             "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "LOADEDMODULES",
             "PYENV_ROOT", "PYENV_VERSION", "VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH",
             "JFORGE_ENV_NAME", "JFORGE_ENV_REVISION")
TOOLS = ("bash", "cc", "c++", "cpp", "gfortran", "cmake", "make", "git", "pkg-config",
         "curl", "wget", "tar", "redis-server", "sbatch", "python", "python3", "pyenv")
PYTHON_PROBE = """
import importlib.metadata, json, sys, sysconfig
packages = sorted(({'name': d.metadata.get('Name', ''), 'version': d.version}
                   for d in importlib.metadata.distributions()), key=lambda d: (d['name'].lower(), d['version']))
print(json.dumps({'executable': sys.executable, 'version': sys.version.split()[0], 'full_version': sys.version,
                  'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
                  'include': sysconfig.get_path('include'),
                  'library_dir': sysconfig.get_config_var('LIBDIR'),
                  'library': sysconfig.get_config_var('LDLIBRARY'), 'packages': packages}))
"""


def _query(argv):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, errors="replace", timeout=3,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return {"exit_code": result.returncode, "stdout": result.stdout.strip(),
                "stderr": result.stderr[:8192].strip()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exit_code": None, "stdout": "", "stderr": str(exc)}


def _tool(command):
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return {"command": command, "path": None, "version": None,
                "status": "invalid", "error": str(exc)}
    path = shutil.which(argv[0]) if argv else None
    if not path:
        return {"command": command, "path": None, "version": None, "status": "missing"}
    query = _query([path, *argv[1:], "--version"])
    text = query["stdout"] or query["stderr"]
    return {"command": command, "path": path, "realpath": os.path.realpath(path),
            "version": text.splitlines()[0] if text else None,
            "status": "found" if query["exit_code"] == 0 else "probe_failed",
            "exit_code": query["exit_code"], "error": query["stderr"] or None}


def collect():
    tools = {name: _tool(name) for name in TOOLS}
    compilers = {}
    for variable, fallback in (("CC", "cc"), ("CXX", "c++"), ("FC", "gfortran")):
        configured = os.environ.get(variable)
        compilers[variable] = {**_tool(configured or fallback), "configured": configured}
    pythons = {}
    for name in ("python", "python3"):
        tool = tools[name]
        entry = {"command": name, "path": tool["path"], "status": "missing"}
        if tool["path"]:
            # Target environment variables are intentional; do not use -I here:
            # PYTHONHOME/PYTHONPATH can be meaningful in the user's environment.
            result = _query([tool["path"], "-c", PYTHON_PROBE])
            try:
                value = json.loads(result["stdout"])
                if result["exit_code"] != 0 or not isinstance(value, dict):
                    raise ValueError("Python environment probe failed")
                entry.update(value, status="found")
            except (ValueError, TypeError):
                entry.update(status="probe_failed", error=result["stderr"] or result["stdout"],
                             exit_code=result["exit_code"])
        pythons[name] = entry
    pyenv = {"path": tools["pyenv"]["path"]}
    if pyenv["path"]:
        for key, arguments in (("selection", ["version"]), ("python", ["which", "python"])):
            result = _query([pyenv["path"], *arguments])
            pyenv[key] = result["stdout"] if result["exit_code"] == 0 else None
            if result["exit_code"] != 0:
                pyenv[key + "_error"] = result["stderr"] or result["stdout"]
    return {"schema_version": 2,
            "variables": {key: os.environ[key] for key in VARIABLES if key in os.environ},
            "tools": {key: value["path"] for key, value in tools.items()},
            "tool_details": tools, "compilers": compilers, "pythons": pythons, "pyenv": pyenv}


def probe_command(output: Path) -> str:
    return shlex.join([sys.executable, "-I", str(Path(__file__).resolve()), str(output)])


def inspect_environment(shell: Path | None = None, timeout: float = 60) -> dict:
    """Source an explicit script in Bash and collect a report without Forge state."""
    from .storage import ForgeError, digest, read_json

    if os.name != "posix":
        raise ForgeError("Environment inspection requires a POSIX system with Bash.")
    bash = shutil.which("bash")
    if not bash:
        raise ForgeError("Bash is required for environment inspection.")
    receipt = None
    if shell is not None:
        shell = shell.expanduser().resolve()
        if not shell.is_file():
            raise ForgeError(f"Environment script is not a file: {shell}")
        receipt = {"path": str(shell), "sha256": digest(shell)}
    with tempfile.TemporaryDirectory(prefix="jforge-doctor-") as directory:
        root = Path(directory)
        output = root / "activation.json"
        lines = ["set -e", "set -o pipefail"]
        if shell is not None:
            lines.append("source " + shlex.quote(str(shell)))
        lines.append(probe_command(output))
        script = root / "inspect.sh"
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with (root / "stdout").open("wb") as stdout, (root / "stderr").open("wb") as stderr:
            process = subprocess.Popen([bash, "--noprofile", "--norc", str(script)],
                                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            try:
                process.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                if isinstance(exc, KeyboardInterrupt):
                    raise
                raise ForgeError(f"Environment inspection timed out after {timeout:g} seconds.", code=1) from exc
        diagnostics = {key: (root / key).read_text(errors="replace")[-8192:]
                       for key in ("stdout", "stderr")}
        if process.returncode != 0 or not output.is_file():
            reason = diagnostics["stderr"].strip() or diagnostics["stdout"].strip()
            raise ForgeError(f"Environment activation or inspection failed (exit {process.returncode})."
                             + (f"\n{reason}" if reason else ""), code=1)
        if shell is not None and digest(shell) != receipt["sha256"]:
            raise ForgeError("Environment script changed during inspection.", code=1)
        return {**read_json(output), "environment_shell": receipt, "activation_output": diagnostics}


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(json.dumps(collect(), indent=2) + "\n", encoding="utf-8")
