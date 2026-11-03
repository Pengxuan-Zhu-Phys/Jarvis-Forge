"""Acceptance of user-selected environments and recorded tool changes."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
import yaml

from jarvis_forge.cli import main
from jarvis_forge.comparison import compare
from jarvis_forge.environment import inspect_environment
from jarvis_forge.runner import execute
from jarvis_forge.storage import ForgeError, ForgeHome, read_json


@pytest.fixture
def selected(tmp_path):
    target = tmp_path / "target python"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(target)], check=True)
    tools = tmp_path / "custom tools"
    tools.mkdir()
    compiler = tools / "site-fortran"
    compiler.write_text("#!/bin/sh\nprintf 'Site Fortran 13.2\\n'\n")
    compiler.chmod(0o755)
    pyenv = tools / "pyenv"
    pyenv.write_text("#!/bin/sh\ncase \"$1\" in\n"
                     "--version) echo 'pyenv 2.6';;\n"
                     "version) echo 'user-chosen-cbs (set by user script)';;\n"
                     "which) echo \"$VIRTUAL_ENV/bin/python\";;\nesac\n")
    pyenv.chmod(0o755)
    shell = tmp_path / "selected environment.sh"
    shell.write_text(f"export PATH={shlex.quote(str(target / 'bin'))}:{shlex.quote(str(tools))}:\"$PATH\"\n"
                     f"export FC={shlex.quote(shlex.quote(str(compiler)))}\n"
                     "export PYENV_VERSION=user-chosen-cbs\n"
                     f"export VIRTUAL_ENV={shlex.quote(str(target))}\n"
                     "printf 'user activation output\\n'\n")
    return shell, target, compiler


def test_doctor_observes_target_python_and_compiler_without_changing_controller(selected, tmp_path, capsys):
    shell, target, compiler = selected
    previous = dict(os.environ)
    absent = tmp_path / "no-forge-home"
    assert main(["doctor", "--shell", str(shell), "--home", str(absent), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    environment = report["environment"]
    assert report["python"] == sys.executable
    assert environment["pythons"]["python"]["prefix"] == str(target)
    assert environment["pythons"]["python"]["executable"] != sys.executable
    assert environment["compilers"]["FC"]["path"] == str(compiler)
    assert environment["compilers"]["FC"]["version"] == "Site Fortran 13.2"
    assert environment["variables"]["PYENV_VERSION"] == "user-chosen-cbs"
    assert environment["pyenv"]["selection"] == "user-chosen-cbs (set by user script)"
    assert environment["pyenv"]["python"] == str(target / "bin/python")
    assert environment["activation_output"]["stdout"] == "user activation output\n"
    assert environment["pythons"]["python"]["packages"] == []
    assert os.environ == previous and not absent.exists()


def test_doctor_text_and_missing_selected_tools(selected, capsys):
    shell, _, _ = selected
    shell.write_text(shell.read_text() + "export CC=missing-site-compiler\n")
    assert main(["doctor", "--shell", str(shell)]) == 0
    output = capsys.readouterr()
    assert "missing" in output.out and "Site Fortran" in output.out
    assert "user activation output" in output.err
    assert "\x1b[" not in output.out


def test_failed_activation_reports_failure_without_selecting_another_environment(tmp_path, capsys):
    shell = tmp_path / "failed.sh"
    shell.write_text("echo 'activation failed' >&2\nreturn 9\n")
    assert main(["doctor", "--shell", str(shell), "--json"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert not output["ok"] and "activation failed" in output["error"]["message"]
    assert main(["doctor", "--shell", str(tmp_path / "absent"), "--json"]) == 2
    capsys.readouterr()


def test_inspection_timeout_stops_activation_children(tmp_path):
    shell = tmp_path / "slow.sh"
    shell.write_text("sleep 30\n")
    with pytest.raises(ForgeError, match="timed out"):
        inspect_environment(shell, timeout=0.1)


def test_declared_fortran_recipe_records_users_fc_and_flags(tmp_path, monkeypatch):
    from jarvis_forge.packages import _context
    compiler = tmp_path / "custom-fc"
    compiler.write_text("#!/bin/sh\necho 'Site Fortran 13.2'\n")
    compiler.chmod(0o755)
    monkeypatch.setenv("FC", str(compiler))
    monkeypatch.setenv("FFLAGS", "-O2")
    monkeypatch.setenv("FCFLAGS", "-fPIC")
    context = _context([{"requires_tools": ["gfortran"]}])
    assert context["tools"]["gfortran"]["path"] == str(compiler)
    assert context["flags"]["FC"] == str(compiler)
    assert context["flags"]["FFLAGS"] == "-O2" and context["flags"]["FCFLAGS"] == "-fPIC"


def test_runs_record_target_environment_and_compare_actual_tool_versions(selected, tmp_path):
    shell, target, compiler = selected
    home = ForgeHome(tmp_path / "home")
    home.initialize()
    suite = tmp_path / "suite.yaml"
    suite.write_text(yaml.safe_dump({"schema_version": 1, "id": "selected-env",
                                    "environment": {"shell": shell.name},
                                    "steps": [{"id": "python", "argv": ["python", "-c", "import sys; print(sys.prefix)"]}]}))
    first = execute(home, suite)
    compiler.write_text("#!/bin/sh\nprintf 'Site Fortran 14.1\\n'\n")
    second = execute(home, suite)
    assert first["status"] == second["status"] == "passed"
    environment = first["steps"][0]["environment"]
    assert environment["pythons"]["python"]["prefix"] == str(target)
    assert environment["compilers"]["FC"]["version"] == "Site Fortran 13.2"
    assert read_json(Path(first["path"]) / "steps/python/activation.json") == environment
    result = compare(home, first["run_id"], second["run_id"])
    assert result["environment_changed"] and not result["suite_changed"]
    assert result["steps"][0]["activation_changed"] and not result["steps"][0]["command_changed"]
    assert result["status"] == "different" and not result["check_passed"]
    (Path(first["path"]) / "steps/python/activation.json").unlink()
    assert compare(home, first["run_id"], second["run_id"])["status"] == "not_comparable"
