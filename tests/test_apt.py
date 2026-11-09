"""Public package CLI contracts for the single Spack backend."""

import json
import pytest
from jarvis_forge.cli import main


@pytest.mark.parametrize(
    "command",
    [
        "",
        "list",
        "search",
        "show",
        "install",
        "reinstall",
        "remove",
        "autoremove",
        "update",
        "upgrade",
        "full-upgrade",
        "edit-sources",
        "satisfy",
        "env",
        "import",
        "switch",
        "profile",
        "sources",
        "check",
        "compilers",
        "shell-init",
    ],
)
def test_apt_help(command, capsys):
    assert main(["apt", *([command] if command else []), "-h"]) == 0
    out = capsys.readouterr().out
    assert "Usage: jforge apt" in out and "Show this message and exit." in out
    assert "\x1b[" not in out


def test_apt_help_displays_current_profile_commands(tmp_path, capsys):
    args = ["--home", str(tmp_path / "forge"), "apt"]
    assert main([*args, "-h"]) == 0
    output = capsys.readouterr().out
    assert "switch" not in output
    assert main([*args, "--json"]) == 0
    commands = json.loads(capsys.readouterr().out)["commands"]
    assert "profile" in commands
    assert not {"switch", "profiles", "use", "create"}.intersection(commands)
    assert main([*args, "switch", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "apt switch"


@pytest.mark.parametrize(
    "args",
    [
        ["list"],
        ["list", "--available"],
        ["search", "linear"],
        ["show", "eigen"],
        ["edit-sources"],
        ["install", "eigen", "--dry-run"],
        ["satisfy", "eigen>=3.4", "--dry-run"],
        ["switch", "--archived"],
        ["profile"],
    ],
)
def test_queries_and_dry_runs_are_offline(tmp_path, capsys, args):
    home = tmp_path / "absent"
    assert main(["--home", str(home), "apt", *args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert not home.exists()


def test_apt_list_defaults_to_installed_packages(tmp_path, capsys, monkeypatch):
    from jarvis_forge import spack_backend

    installed = [{"name": "eigen", "version": "5.0.1"}, {"name": "gsl", "version": "2.8"}]
    available = [{"name": "eigen", "status": "available"}, {"name": "root", "status": "available"}]
    monkeypatch.setattr(spack_backend, "current", lambda home: {"packages": installed})
    monkeypatch.setattr(spack_backend, "catalog", lambda home, pattern: available)
    args = ["--home", str(tmp_path / "forge"), "apt", "list", "e*", "--json"]
    for flags in ([], ["--installed"]):
        assert main([*args, *flags]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["view"] == "installed" and result["packages"] == installed[:1]
    assert main([*args, "--available"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["view"] == "available" and result["packages"] == available[:1]
    assert not (tmp_path / "forge").exists()


@pytest.mark.parametrize("action", ["import", "switch"])
@pytest.mark.parametrize("old_source", ["ubuntu", "debian", "legacy"])
def test_removed_sources_are_rejected_before_mutation(
    tmp_path, capsys, action, old_source
):
    home = tmp_path / "absent"
    assert main(["--home", str(home), "apt", action, old_source, "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert "removed" in result["error"]["message"]
    assert "apt import spack" in result["error"]["hint"]
    assert not home.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["install", "gsl", "--no-official"],
        ["import", "spack", "--suite", "noble"],
        ["update", "--recipes-only"],
        ["sources", "--add", "bad", "--url", "http://example.org"],
        ["install", "gsl", "--cache", "bad"],
        ["profile", "--create"],
        ["profile", "gambit", "--archived"],
    ],
)
def test_invalid_and_obsolete_options_do_not_initialize(tmp_path, capsys, args):
    home = tmp_path / "absent"
    assert main(["--home", str(home), "apt", *args, "--json"]) == 2
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert not home.exists()
