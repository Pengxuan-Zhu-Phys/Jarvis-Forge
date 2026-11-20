"""Focused checks for the standalone installer without installing Python or Forge."""

import hashlib
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools/jforge-pyenv-setup.sh"


def run_helpers(tmp_path, commands, **environment):
    helpers = SCRIPT.read_text().split("\npyenv_root=$bootstrap_root\n", 1)[0]
    return subprocess.run(
        ["bash", "-s"],
        input=helpers + '\nmkdir -p tmp jforge-bootstrap/logs\nexport TMPDIR="$bootstrap_root/tmp"\n' + commands,
        cwd=tmp_path,
        env={**os.environ, **environment},
        text=True,
        capture_output=True,
    )


@pytest.mark.skipif(shutil.which("cc") is None, reason="requires a host C compiler")
def test_library_probe_accepts_headers_using_stdio_types(tmp_path):
    # Readline's public header uses FILE without including stdio.h itself.
    (tmp_path / "readline.h").write_text("extern FILE *readline_input;\n")
    result = run_helpers(tmp_path, "has_library readline.h '(void)0'\n", CC=shutil.which("cc"),
                         CPPFLAGS=f"-I{tmp_path}", LDFLAGS="")
    assert result.returncode == 0, result.stderr


def test_failed_dependency_configure_never_runs_make_or_install(tmp_path):
    cache = tmp_path / "cache/python-deps"
    cache.mkdir(parents=True)
    archive = cache / "fixture.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        content = b"#!/bin/sh\necho configuration-failed\nexit 23\n"
        info = tarfile.TarInfo("fixture/configure")
        info.size = len(content)
        info.mode = 0o755
        bundle.addfile(info, io.BytesIO(content))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    tools = tmp_path / "tools"
    tools.mkdir()
    make = tools / "make"
    make.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(tmp_path / 'make-ran'))}\n")
    make.chmod(0o755)
    result = run_helpers(tmp_path, f"build_library fixture https://unused.invalid/fixture {checksum} fixture\n",
                         PATH=f"{tools}{os.pathsep}{os.environ['PATH']}")
    assert result.returncode != 0
    assert "Could not configure fixture" in result.stderr
    assert "configuration-failed" in result.stderr
    assert not (tmp_path / "make-ran").exists()
    assert not (tmp_path / "python-deps/fixture/.source-sha256").exists()


@pytest.mark.skipif(shutil.which("cc") is None, reason="requires a host C compiler")
def test_dynamic_loader_rejects_a_library_with_an_unresolved_dependency(tmp_path):
    broken = tmp_path / "broken.so"
    flags = ["-dynamiclib", "-Wl,-undefined,dynamic_lookup"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
    subprocess.run([shutil.which("cc"), *flags, "-x", "c", "-", "-o", str(broken)],
                   input="extern int missing_terminal_symbol; int broken(void) { return missing_terminal_symbol; }\n",
                   text=True, capture_output=True, check=True)
    result = run_helpers(tmp_path, f"library_loads {shlex.quote(str(broken))}\n", CC=shutil.which("cc"), LDFLAGS="")
    assert result.returncode != 0
    assert "missing_terminal_symbol" in (tmp_path / "jforge-bootstrap/logs/library-loader.log").read_text()


def test_failed_loader_compilation_does_not_run_a_stale_probe(tmp_path):
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    stale = temporary / "library-loader"
    stale.write_text("#!/bin/sh\necho stale-probe-ran\n")
    stale.chmod(0o755)
    # Call in a conditional as production does, so Bash disables implicit errexit.
    result = run_helpers(tmp_path, "if library_loads unused.so; then exit 0; else exit 7; fi\n", CC="false", LDFLAGS="")
    assert result.returncode == 7
    assert "stale-probe-ran" not in (tmp_path / "jforge-bootstrap/logs/library-loader.log").read_text()


def test_python_validation_reports_all_missing_modules(tmp_path):
    function = "python_identity() {" + SCRIPT.read_text().split("python_identity() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    interpreter = tmp_path / "python"
    interpreter.write_text(
        f"#!{sys.executable}\n"
        "import importlib, sys\n"
        "original = importlib.import_module\n"
        "def load(name):\n"
        "    if name in ('curses', 'curses.panel', 'readline', 'sqlite3'):\n"
        "        raise ImportError('native extension missing')\n"
        "    return original(name)\n"
        "importlib.import_module = load\n"
        # Match Python's `-I - VERSION` argv when executing the piped check.
        "sys.argv = ['-', sys.argv[-1]]\n"
        "exec(compile(sys.stdin.read(), '<validation>', 'exec'))\n"
    )
    interpreter.chmod(0o755)
    result = subprocess.run(["bash", "-s"], input=function + "python_identity\n", text=True,
                            capture_output=True, env={**os.environ, "base_python": str(interpreter),
                                                     "python_version": ".".join(map(str, sys.version_info[:3]))})
    assert result.returncode != 0
    for name in ("curses", "curses.panel", "readline", "sqlite3"):
        assert f"{name}: native extension missing" in result.stderr
