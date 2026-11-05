import json
from pathlib import Path

import pytest
import typer

from jarvis_forge import cli
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeHome, write_json


@pytest.mark.parametrize("command", [[], ["init"], ["paths"], ["doctor"], ["list"], ["info"],
    ["example"], ["validate"], ["plan"], ["run"], ["history"], ["show"], ["logs"], ["compare"]])
def test_help(command, capsys):
    assert main([*command, "-h"]) == 0
    out = capsys.readouterr()
    assert "Usage:" in out.out and "Show this message and exit." in out.out
    assert "\x1b[" not in out.out


@pytest.mark.parametrize('command', [[], ['list'], ['update'], ['show'], ['fetch'], ['validate'],
                                      ['plan'], ['run'], ['resume'], ['artifacts'], ['env']])
def test_build_help_and_reserved_workflow_namespace(command, capsys):
    assert main(['build', *command, '--help']) == 0
    output = capsys.readouterr().out
    assert 'Usage: jforge build' in output
    assert 'jforge workflow' not in output


def test_build_json_uses_build_command_and_workflow_is_reserved(capsys):
    assert main(['build', 'list', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['command'] == 'build list'
    assert 'builds' in result
    assert main(['workflow', '--json']) == 2
    assert json.loads(capsys.readouterr().out)['ok'] is False


@pytest.mark.parametrize("args", [["--version"], ["-v"], ["-V"]])
def test_version(args, capsys):
    assert main(args) == 0
    assert capsys.readouterr().out == "Jarvis-Forge 1.1.2\n"


@pytest.mark.parametrize("args", [["--json", "--version"], ["--version", "--json"]])
def test_json_version(args, capsys):
    assert main(args) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["name"] == "Jarvis-Forge" and value["command"] == "version"
    assert value["version"] == "1.1.2"


@pytest.mark.parametrize("args", [["unknown"], ["--unknown"], ["run"], ["--home"]])
def test_json_errors(args, capsys):
    assert main([*args, "--json"]) == 2
    result = capsys.readouterr()
    value = json.loads(result.out)
    assert value["ok"] is False and value["error"]["message"]
    assert "Traceback" not in result.err


def test_cli_catches_the_click_exceptions_owned_by_typer():
    assert issubclass(typer.BadParameter, cli.ClickException)


@pytest.mark.parametrize("exception,code", [(typer.Abort(), 130), (typer.Exit(17), 17)])
def test_typer_abort_and_exit_preserve_exit_codes(monkeypatch, capsys, exception, code):
    def invoke(**kwargs):
        raise exception

    monkeypatch.setattr(cli, "app", invoke)
    assert main(["--json"]) == code
    output = capsys.readouterr()
    assert "Unexpected error" not in output.out + output.err
    if code == 130:
        assert json.loads(output.out)["error"]["message"] == "Interrupted."


def test_global_options_after_commands_do_not_create_home(tmp_path, capsys):
    home = tmp_path / "absent"
    assert main(["paths", "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["home"] == str(home)
    assert not home.exists()


def test_run_failure_exit_code_and_json_are_preserved(tmp_path, capsys):
    home = tmp_path / "home"
    ForgeHome(home).initialize()
    suite = tmp_path / "failed.yaml"
    suite.write_text("schema_version: 1\nid: failed\nsteps:\n  - id: fail\n    script: exit 7\n")
    assert main(["run", str(suite), "--home", str(home), "--json"]) == 1
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert not value["ok"] and value["steps"][0]["exit_code"] == 7
    assert isinstance(value["error"], dict) and value["error"]["message"]


def test_multiple_package_instances_require_explicit_path_selection(tmp_path, capsys):
    home = tmp_path / "home"
    for name in ["a", "b"]:
        write_json(home / "packages/eigen/3.4.0" / name / "manifest.json",
                   {"name": "eigen", "version": "3.4.0", "install_id": name,
                    "prefix": str(tmp_path / name)})
    assert main(["info", "eigen", "--archived", "--path", "--home", str(home), "--json"]) == 2
    capsys.readouterr()
    assert main(["info", "eigen", "--archived", "--path", "--home", str(home)]) == 2
    capsys.readouterr()
    assert main(["info", "eigen", "--archived", "--instance", "a", "--path", "--home", str(home)]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "a")


def test_host_entry_point_uses_host_program_name(capsys):
    assert main(["run", "--help"], prog="Jarvis forge") == 0
    assert "Usage: Jarvis forge run" in capsys.readouterr().out
    assert main(["unknown"], prog="Jarvis forge") == 2
    output = capsys.readouterr()
    assert "Jarvis forge" in output.err and "Traceback" not in output.err


def test_corrupt_package_manifest_returns_error_not_empty_list(tmp_path, capsys):
    home = tmp_path / "home"
    path = home / "packages/eigen/3.4.0/bad/manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    assert main(["list", "--archived", "--home", str(home), "--json"]) == 2
    assert not json.loads(capsys.readouterr().out)["ok"]

