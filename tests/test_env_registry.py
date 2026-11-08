"""Named environments remain shared and explicitly selected across interpreters."""

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
from jarvis_forge.environment_registry import add, list_environments, show
from jarvis_forge.runner import execute
from jarvis_forge.storage import ForgeError, ForgeHome, write_json
from jarvis_forge.suite import load_suite, make_plan


@pytest.fixture
def home(tmp_path):
    return ForgeHome(tmp_path / "shared forge home")


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "user environment.sh"
    path.write_text("export FORGE_USER_VALUE=chosen-by-user\n")
    return path


@pytest.fixture
def target(tmp_path):
    path = tmp_path / "target python"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)], check=True)
    return path


def test_registry_queries_do_not_initialize_or_execute_scripts(home, tmp_path, capsys):
    assert main(["env", "list", "--home", str(home.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["environments"] == []
    assert not home.root.exists()
    assert main(["env", "show", "missing", "--home", str(home.root), "--json"]) == 2
    capsys.readouterr()
    assert not home.root.exists()
    marker = tmp_path / "must-not-execute"
    shell = tmp_path / "env.sh"
    shell.write_text("touch " + shlex.quote(str(marker)) + "\n")
    assert main(["env", "add", "cbs", "--shell", str(shell), "--home", str(home.root), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)["environment"]
    assert result["status"] == "ready" and not marker.exists()
    assert main(["env", "show", "cbs", "--path", "--home", str(home.root)]) == 0
    assert capsys.readouterr().out.strip() == result["activation"]
    assert not marker.exists()


def test_managed_copy_survives_source_removal_and_revisions_are_retained(home, script):
    first = add(home, "cbs", script)
    old_source = Path(first["path"]) / "source.sh"
    initial = old_source.read_bytes()
    script.write_text("export FORGE_USER_VALUE=new-value\n")
    with pytest.raises(ForgeError, match="already exists"):
        add(home, "cbs", script)
    second = add(home, "cbs", script, replace=True)
    script.unlink()
    assert second["parent_revision"] == first["revision"]
    assert old_source.read_bytes() == initial
    assert show(home, "cbs", first["revision"], strict=True)["revision"] == first["revision"]
    assert show(home, "cbs", strict=True)["revision"] == second["revision"]


def test_registry_is_shared_and_target_needs_no_forge_install(home, script, target):
    selected = add(home, "cbs", script)
    env = {**os.environ, "JFORGE_HOME": str(home.root),
           "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    code = "import json,sys; from jarvis_forge.storage import ForgeHome; from jarvis_forge.environment_registry import list_environments; print(json.dumps({'prefix':sys.prefix,'home':str(ForgeHome.resolve().root),'envs':list_environments(ForgeHome.resolve())}))"
    output = subprocess.check_output([str(target / "bin/python"), "-c", code], env=env, text=True)
    result = json.loads(output)
    assert result["home"] == str(home.root) and result["prefix"] == str(target)
    assert result["envs"][0]["revision"] == selected["revision"]
    raw = subprocess.check_output([str(target / "bin/python"), "-c", "import importlib.util; print(importlib.util.find_spec('typer'))"], text=True)
    assert raw.strip() == "None"


def test_named_run_and_doctor_use_selected_target_and_keep_controller(home, script, target, tmp_path, capsys):
    inherited = script.read_text()
    script.write_text(inherited + "export PATH=" + shlex.quote(str(target / "bin")) + ':"$PATH"\n')
    selected = add(home, "cbs", script)
    assert main(["doctor", "--env", "cbs", "--home", str(home.root), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["python"] == sys.executable
    assert result["environment"]["pythons"]["python"]["prefix"] == str(target)
    assert result["environment"]["pythons"]["python3"]["prefix"] == str(target)
    suite = tmp_path / "suite.yaml"
    suite.write_text(yaml.safe_dump({"schema_version": 1, "id": "named",
                                    "environment": {"name": "cbs"},
                                    "steps": [{"id": "target", "argv": ["python", "-c", "import os,sys; assert os.environ['FORGE_USER_VALUE']=='chosen-by-user'; print(sys.prefix)"]},
                                              {"id": "controller", "argv": ["${python}", "-c", "import sys; print(sys.prefix)"]}]}))
    first = execute(home, suite)
    assert first["status"] == "passed"
    archive = Path(first["path"]) / "environment-profile"
    assert (archive / "source.sh").read_bytes() == script.read_bytes()
    assert not (archive / "bin").exists()
    assert (archive / "manifest.json").is_file()
    assert first["steps"][0]["environment"]["pythons"]["python"]["prefix"] == str(target)
    script.write_text(inherited)
    add(home, "cbs", script, replace=True)
    second = execute(home, suite)
    assert second["status"] == "passed"
    assert compare(home, first["run_id"], second["run_id"])["environment_changed"]
    config = yaml.safe_load(suite.read_text())
    config["environment"]["revision"] = selected["revision"]
    suite.write_text(yaml.safe_dump(config))
    pinned = make_plan(load_suite(suite), home.root / "runs/planned", home)
    assert pinned["environment_profile"]["revision"] == selected["revision"]


def test_command_line_env_selection_overrides_suite_shell(home, script, tmp_path, capsys):
    selected = add(home, "cbs", script)
    suite = tmp_path / "suite.yaml"
    suite.write_text(yaml.safe_dump({"schema_version": 1, "id": "override",
                                    "environment": {"shell": "not-used.sh"},
                                    "steps": [{"id": "test", "argv": ["python", "--version"]}]}))
    assert main(["plan", str(suite), "--env", "cbs", "--home", str(home.root), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["environment_profile"]["revision"] == selected["revision"]
    assert result["environment_selection_source"] == "cli"
    assert main(["run", str(suite), "--env", "cbs", "--home", str(home.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


def test_retired_python_override_requires_a_new_script_revision(home, script):
    first = add(home, "cbs", script)
    manifest = Path(first["path"]) / "manifest.json"
    old = json.loads(manifest.read_text())
    write_json(manifest, {**old, "python": {"mode": "pyenv", "selection": "cbs"}})
    assert show(home, "cbs")["status"] == "legacy_python"
    with pytest.raises(ForgeError, match="retired Python override"):
        show(home, "cbs", strict=True)
    second = add(home, "cbs", script, replace=True)
    assert second["parent_revision"] == first["revision"]
    assert second["status"] == "ready"
    assert "python" not in second


def test_invalid_syntax_preserves_working_revision(home, script):
    first = add(home, "cbs", script)
    script.write_text("if then\n")
    with pytest.raises(ForgeError, match="Invalid Bash"):
        add(home, "cbs", script, replace=True)
    assert show(home, "cbs")["revision"] == first["revision"]


def test_modified_managed_file_is_visible_and_cannot_be_activated(home, script):
    first = add(home, "cbs", script)
    source = Path(first["path"]) / "source.sh"
    source.chmod(0o600)
    source.write_text("echo modified\n")
    assert list_environments(home)[0]["status"] == "modified_or_missing"
    with pytest.raises(ForgeError, match="missing or modified"):
        show(home, "cbs", strict=True)


def test_apt_environment_can_be_listed_but_not_overwritten(home, script):
    home.initialize()
    from jarvis_forge.apt_profiles import active_home
    target = active_home(home)
    target.initialize()
    activation = target.root / "environments/apt/revisions/op/activate.sh"
    activation.parent.mkdir(parents=True)
    activation.write_text("export TEST_APT=1\n")
    write_json(target.root / "apt/state.json", {"backend":"spack", "spack":{"roots":[], "packages":[]}, "activation": str(activation), "last_operation_id": "op"})
    assert list_environments(home)[0]["name"] == "apt"
    assert show(home, "apt", strict=True)["activation"] == str(activation)
    with pytest.raises(ForgeError, match="managed by jforge apt"):
        add(home, "apt", script)


@pytest.mark.parametrize("name", ["../outside", "a/b", ".bad"])
def test_invalid_names_do_not_create_home(home, script, name):
    with pytest.raises(ForgeError):
        add(home, name, script)
    assert not home.root.exists()


def test_shell_and_named_environment_are_mutually_exclusive_in_yaml(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("schema_version: 1\nid: invalid\nenvironment:\n  shell: x.sh\n  name: cbs\nsteps:\n  - id: x\n    script: ':'\n")
    with pytest.raises(ForgeError, match="Invalid suite"):
        load_suite(path)


def test_registered_environment_does_not_become_a_default(home, script, tmp_path):
    add(home, "cbs", script)
    suite = tmp_path / "plain.yaml"
    suite.write_text("schema_version: 1\nid: plain\nsteps:\n  - id: x\n    script: ':'\n")
    plan = make_plan(load_suite(suite), home.root / "runs/planned", home)
    assert plan["environment_profile"] is None and plan["environment_shell"] is None


def test_registry_does_not_write_through_symlinked_environment_directory(home, script, tmp_path):
    home.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home.root / "environments").symlink_to(outside)
    with pytest.raises(ForgeError, match="escapes"):
        add(home, "cbs", script)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("command", [["env"], ["env", "add"], ["env", "list"], ["env", "show"]])
def test_environment_cli_help(command, capsys):
    assert main([*command, "-h"]) == 0
    assert "Usage:" in capsys.readouterr().out
