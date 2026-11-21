import json
from pathlib import Path

import pytest
import typer

from jarvis_forge import __version__, cli
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeHome, write_json


def test_root_help_lists_commands_by_group(capsys):
    assert main(["-h"]) == 0
    output = capsys.readouterr().out
    assert "Environment" in output and "Acceptance" in output
    assert "Runs" in output and "Setup" in output
    # Rich boxes each command, so the row is "│ apt " rather than a line that starts with "apt".
    for earlier, later in (("│ apt ", "│ test "), ("│ test ", "│ suite "),
                           ("│ suite ", "│ playlist "), ("│ playlist ", "│ history "),
                           ("│ history ", "│ init ")):
        assert output.index(earlier) < output.index(later)
    for removed in ("│ paths ", "│ example ", "│ checker ", "│ toolchain ", "│ executor ",
                    "│ list ", "│ info "):
        assert removed not in output
    assert "\x1b[" not in output


@pytest.mark.parametrize("args,candidate", [(["list"], "jforge apt list / jforge build list"),
                                           (["buld"], "jforge build"),
                                           (["build", "fetc"], "jforge build fetch")])
@pytest.mark.parametrize("as_json", [False, True])
def test_unknown_command_has_one_error_and_one_suggestion(tmp_path, capsys, args, candidate, as_json):
    path = tmp_path / "absent"
    assert main(["--home", str(path), *args, *(["--json"] if as_json else [])]) == 2
    captured = capsys.readouterr()
    if as_json:
        result = json.loads(captured.out)
        assert not result["ok"] and not captured.err
        text = result["error"]["message"] + "\n" + result["error"]["hint"]
    else:
        assert not captured.out
        text = captured.err
    assert text.count("Unknown command:") == 1
    assert text.count(candidate) == 1
    assert text.count("Did you mean") <= 1
    assert not path.exists()


@pytest.mark.parametrize("command", [[], ["init"], ["doctor"], ["history"], ["show"], ["logs"],
    ["apt"], ["env"], ["build"], ["test"], ["bench"], ["suite"],
    ["suite", "example"], ["suite", "validate"], ["suite", "plan"], ["suite", "run"], ["suite", "compare"],
    ["profile"], ["profile", "show"], ["shell-init"], ["bench", "executor"]])
def test_help(command, capsys):
    assert main([*command, "-h"]) == 0
    out = capsys.readouterr()
    assert "Usage:" in out.out and "Show this message and exit." in out.out
    assert "\x1b[" not in out.out


@pytest.mark.parametrize('command', [[], ['list'], ['update'], ['show'], ['fetch'], ['install'],
                                      ['remove'], ['autoremove'], ['upgrade'], ['search']])
def test_build_help_and_reserved_workflow_namespace(command, capsys):
    assert main(['build', *command, '--help']) == 0
    output = capsys.readouterr().out
    assert 'Usage: jforge build' in output
    assert 'jforge workflow' not in output


def test_build_list_summarizes_the_catalog_and_preserves_json(tmp_path, capsys):
    catalog = tmp_path / "builds/catalog/index.json"
    recipe = {"id": "root-build", "revision": "2026.11.09.1", "description": "",
              "url": "https://example.org/root-build.yaml", "sha256": "a" * 64,
              "files": [{"path": "scripts/root_acceptance.py", "url": "https://example.org/root_acceptance.py",
                         "sha256": "b" * 64}]}
    libraries = [{"id": "math-libraries", "revision": "2026.11.09.1", "description": "Shared math tools"}]
    write_json(catalog, {"builds": [recipe], "libraries": libraries})
    args = ["--home", str(tmp_path), "build", "list", "--available"]
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "root-build" not in output and "root" in output and "2026.11.09.1" in output and "Scripts" in output
    assert "math-libraries" in output and "Shared math tools" in output
    assert "https://" not in output and "root_acceptance.py" not in output
    assert "sha256" not in output.lower()
    assert main([*args, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "build list"
    assert result["builds"] == [{**recipe, "app": "root"}] and result["libraries"] == libraries


def test_build_list_defaults_to_profile_registry(tmp_path,capsys):
    from profile_fixtures import project_profile
    from jarvis_forge.storage import read_json
    home=ForgeHome(tmp_path/'forge');scope=project_profile(home)
    prefix=scope.root/'apps/root-6.40.04';prefix.mkdir(parents=True)
    activation=prefix/'activate.sh';activation.write_text('# shell')
    row={'name':'root','artifact_id':'0001-publish-root','build_id':'root','output':'installation','prefix':str(prefix),'version':'6.40.04','activation':str(activation),'enabled':True}
    metadata=read_json(scope.root/'metadata.json');metadata['apps']=[row];write_json(scope.root/'metadata.json',metadata)
    args=['--home',str(home.root),'build','list']
    assert main([*args,'--json'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['view']=='installed' and result['builds'][0]['name']=='root'
    assert main(args)==0
    output=capsys.readouterr().out
    assert 'Installed apps (1)' in output and 'ON' in output and str(prefix) in output


def test_workflow_is_reserved(capsys):
    assert main(['workflow', '--json']) == 2
    assert json.loads(capsys.readouterr().out)['ok'] is False


@pytest.mark.parametrize("args", [["--version"], ["-v"], ["-V"]])
def test_version(args, capsys):
    assert main(args) == 0
    assert capsys.readouterr().out == f"Jarvis-Forge {__version__}\n"


@pytest.mark.parametrize("args", [["--json", "--version"], ["--version", "--json"]])
def test_json_version(args, capsys):
    assert main(args) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["name"] == "Jarvis-Forge" and value["command"] == "version"
    assert value["version"] == __version__


@pytest.mark.parametrize("args", [["unknown"], ["--unknown"], ["run"], ["checker"], ["toolchain"],
                                   ["executor"], ["paths"], ["example"], ["--home"]])
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
    assert main(["doctor", "--paths", "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["home"] == str(home)
    assert not home.exists()


def test_run_failure_exit_code_and_json_are_preserved(tmp_path, capsys):
    home = tmp_path / "home"
    ForgeHome(home).initialize()
    suite = tmp_path / "failed.yaml"
    suite.write_text("schema_version: 1\nid: failed\nsteps:\n  - id: fail\n    script: exit 7\n")
    assert main(["suite", "run", str(suite), "--home", str(home), "--json"]) == 1
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert not value["ok"] and value["steps"][0]["exit_code"] == 7
    assert isinstance(value["error"], dict) and value["error"]["message"]


def test_archived_layout_queries_are_refused_without_modifying_user_files(tmp_path,capsys):
    home=tmp_path/'home';file=home/'packages/eigen/3.4.0/old/manifest.json'
    write_json(file,{'name':'eigen','version':'3.4.0','prefix':str(tmp_path/'old')})
    before=file.read_bytes()
    assert main(['--home',str(home),'--json','apt','prefix','eigen','--archived'])==2
    assert 'replaced' in json.loads(capsys.readouterr().out)['error']['message']
    assert file.read_bytes()==before and not (home/'profiles').exists()


def test_host_entry_point_uses_host_program_name(capsys):
    assert main(["suite", "run", "--help"], prog="Jarvis forge") == 0
    assert "Usage: Jarvis forge suite run" in capsys.readouterr().out
    assert main(["unknown"], prog="Jarvis forge") == 2
    output = capsys.readouterr()
    assert "Jarvis forge" in output.err and "Traceback" not in output.err


def test_corrupt_package_manifest_returns_error_not_empty_list(tmp_path, capsys):
    home = tmp_path / "home"
    path = home / "packages/eigen/3.4.0/bad/manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    assert main(["apt", "instances", "--archived", "--home", str(home), "--json"]) == 2
    assert not json.loads(capsys.readouterr().out)["ok"]

