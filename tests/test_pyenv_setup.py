"""Installer checks with disposable interpreters and offline fixture wheels."""

import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools/jforge-pyenv-setup.sh"


def fixture_wheel(directory, version):
    wheel = directory / f"jarvis_forge-{version}-py3-none-any.whl"
    metadata = f"jarvis_forge-{version}.dist-info"
    files = {
        "jarvis_forge/__init__.py": f"__version__ = {version!r}\n",
        "jarvis_forge/__main__.py": '''import os, sys
from pathlib import Path
from . import __version__
def main():
    if sys.argv[1:] == ["--version"]:
        print("Jarvis-Forge " + __version__)
    elif sys.argv[1:2] == ["--home"] and sys.argv[3:] == ["init"]:
        from .storage import ForgeHome
        home = ForgeHome.resolve(sys.argv[2]); home.initialize(); home.remember_default()
    elif sys.argv[1:] == ["shell-init"]:
        print("# fixture shell hook")
    else:
        raise SystemExit("Unexpected fixture arguments: " + repr(sys.argv))
if __name__ == "__main__":
    main()
''',
        f"{metadata}/METADATA": f"Metadata-Version: 2.1\nName: Jarvis-Forge\nVersion: {version}\n",
        f"{metadata}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{metadata}/entry_points.txt": "[console_scripts]\njforge = jarvis_forge.__main__:main\n",
    }
    files[f"{metadata}/RECORD"] = "".join(f"{name},,\n" for name in [*files, f"{metadata}/RECORD"])
    package = SCRIPT.parents[1] / "src/jarvis_forge"
    for name in ("storage.py",):
        files["jarvis_forge/" + name] = (package / name).read_text()
    with zipfile.ZipFile(wheel, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)


@pytest.mark.parametrize("separate", [False, True])
def test_setup_installs_in_the_selected_prefix_and_pip_updates_the_running_cli(tmp_path, separate):
    root = tmp_path / "installation $ with spaces"
    root.mkdir()
    pyenv_root = tmp_path / "independent-pyenv" if separate else root
    version = ".".join(map(str, sys.version_info[:3]))
    prefix = pyenv_root / "versions" / version
    # A disposable interpreter keeps the real developer's base site-packages intact.
    subprocess.run([sys.executable, "-m", "venv", str(prefix)], check=True, capture_output=True)
    python = prefix / "bin/python"
    pyenv = pyenv_root / "pyenv/bin/pyenv"
    pyenv.parent.mkdir(parents=True)
    pyenv.write_text('''#!/bin/bash
case "$1" in
    global) printf '%s\\n' "$2" > "$PYENV_ROOT/version" ;;
    rehash) touch "$PYENV_ROOT/rehash-called" ;;
    init) printf 'export PATH=%q:"$PATH"\\n' "$PYENV_ROOT/versions/''' + version + '''/bin" ;;
    *) exit 2 ;;
esac
''')
    pyenv.chmod(0o755)
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    fixture_wheel(wheels, "1.2.1")
    login = tmp_path / "login"
    login.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYENV_", "PIP_", "JFORGE_", "_JFORGE_"))
           and key not in {"VIRTUAL_ENV", "BASH_ENV", "ENV"}}
    env.update(HOME=str(login), SHELL="/bin/bash", PIP_NO_INDEX="1", PIP_FIND_LINKS=str(wheels))
    launcher = root / "jforge-bootstrap/bin/jforge"

    def run(argv):
        return subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, check=True)

    args = ["bash", str(SCRIPT), "--python", version]
    scratch = tmp_path / "node-scratch"
    env["SLURM_TMPDIR"] = str(scratch)
    retained = []
    if separate:
        args += ["--pyenv-root", str(pyenv_root), "--stage", str(scratch)]
        old = root / "jforge"
        old.mkdir()
        (old / "config.json").write_text(json.dumps({"schema_version": 1, "layout_version": 2}))
        failed = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True)
        assert failed.returncode != 0 and "Repeat setup with --home" in failed.stderr
        retained = sorted(scratch.iterdir())
        assert run([str(python), "-I", "-c", "import importlib.util; assert importlib.util.find_spec('jarvis_forge') is None"]).returncode == 0
        run([*args, "--home", str(tmp_path / "forge-data")])
    else:
        run(args)
    assert not launcher.is_symlink()
    assert (pyenv_root / "rehash-called").is_file()
    manifest = json.loads((root / "jforge-bootstrap/metadata.json").read_text())
    assert manifest["pyenv_root"] == str(pyenv_root)
    assert manifest["build"]["stage"] == str(scratch)
    assert sorted(scratch.iterdir()) == retained
    assert not (root / "tmp").exists()
    assert run([str(launcher), "--version"]).stdout.strip() == "Jarvis-Forge 1.2.1"
    assert not (root / "jforge-bootstrap/venvs").exists()

    fixture_wheel(wheels, "1.2.2")
    run(args)  # Rerunning setup must upgrade without a flag or version constraint.
    assert run([str(launcher), "--version"]).stdout.strip() == "Jarvis-Forge 1.2.2"
    fixture_wheel(wheels, "1.2.3")
    run([str(python), "-m", "pip", "install", "--upgrade", "Jarvis-Forge"])
    assert run([str(launcher), "--version"]).stdout.strip() == "Jarvis-Forge 1.2.3"
    installed = json.loads(run([str(python), "-I", "-c",
                               "import json,jarvis_forge; print(json.dumps(jarvis_forge.__file__))"]).stdout)
    assert Path(installed).is_relative_to(prefix)
    assert "jforge-bootstrap" not in installed
    run(args)  # Reuse the separately remembered data home and existing Python.
    for shell, options in (("bash", ["--noprofile", "--norc"]), ("zsh", ["-f"])):
        if executable := shutil.which(shell):
            probe = 'source "$1/activate.sh"; source "$1/activate.sh"; jforge --version; command python -m jarvis_forge --version'
            assert run([executable, *options, "-c", probe, "probe", str(root)]).stdout.splitlines() == [
                "Jarvis-Forge 1.2.3", "Jarvis-Forge 1.2.3"]
    assert list(login.iterdir()) == []


def run_helpers(tmp_path, commands, **environment):
    helpers = SCRIPT.read_text().split("\npyenv_root=${pyenv_root_option", 1)[0]
    return subprocess.run(
        ["bash", "-s"],
        input=helpers + '\nmkdir -p tmp jforge-bootstrap/logs\nexport TMPDIR="$bootstrap_root/tmp"\nbuild_jobs=1\n' + commands,
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
    function = "validate_python() {" + SCRIPT.read_text().split("validate_python() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
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
    result = subprocess.run(["bash", "-s"], input=function + "validate_python\n", text=True,
                            capture_output=True, env={**os.environ, "base_python": str(interpreter),
                                                     "python_version": ".".join(map(str, sys.version_info[:3]))})
    assert result.returncode != 0
    for name in ("curses", "curses.panel", "readline", "sqlite3"):
        assert f"{name}: native extension missing" in result.stderr


def test_linux_preflight_lists_every_required_library_without_starting_a_build(tmp_path):
    result = run_helpers(tmp_path, '''
make() { :; }
has_library() { return 1; }
build_library() { touch unexpected-build; }
preflight_linux_libraries
''', CC="true")
    assert result.returncode != 0
    for name in ("zlib", "bzip2", "xz", "libffi", "OpenSSL"):
        assert name in result.stderr
    assert "Python was not rebuilt" in result.stderr
    assert not (tmp_path / "unexpected-build").exists()


@pytest.mark.parametrize("legacy", [False, True])
def test_bootstrap_budget_obeys_affinity_and_ancestor_cgroup_limits(tmp_path, legacy):
    proc = tmp_path / "proc/self"
    proc.mkdir(parents=True)
    mount = tmp_path / "cgroup"
    child = mount / "child"
    child.mkdir(parents=True)
    (proc / "status").write_text("Cpus_allowed_list:\t0-1,4-5\n")
    if legacy:
        (proc / "cgroup").write_text("2:cpu,cpuacct:/child\n")
        (proc / "mountinfo").write_text(f"1 2 0:1 / {mount} rw - cgroup cgroup rw,cpu,cpuacct\n")
        (mount / "cpu.cfs_quota_us").write_text("150000\n")
        (mount / "cpu.cfs_period_us").write_text("100000\n")
    else:
        (proc / "cgroup").write_text("0::/child\n")
        (proc / "mountinfo").write_text(f"1 2 0:1 / {mount} rw - cgroup2 cgroup rw\n")
        (mount / "cpu.max").write_text("150000 100000\n")
    result = run_helpers(tmp_path, f'getconf() {{ printf "64\\n"; }}\navailable_build_jobs {shlex.quote(str(proc.parent))}\n')
    assert result.returncode == 0 and result.stdout.strip() == "2", result.stderr


def test_bootstrap_budget_caps_unrestricted_compiles(tmp_path):
    result = run_helpers(tmp_path, 'getconf() { printf "64\\n"; }\navailable_build_jobs absent-proc\n')
    assert result.returncode == 0 and result.stdout.strip() == "16", result.stderr
