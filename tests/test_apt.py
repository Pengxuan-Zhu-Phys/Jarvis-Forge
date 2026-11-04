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


@pytest.mark.parametrize(
    "args",
    [
        ["list"],
        ["search", "linear"],
        ["show", "eigen"],
        ["edit-sources"],
        ["install", "eigen", "--dry-run"],
        ["satisfy", "eigen>=3.4", "--dry-run"],
        ["switch", "--archived"],
    ],
)
def test_queries_and_dry_runs_are_offline(tmp_path, capsys, args):
    home = tmp_path / "absent"
    assert main(["--home", str(home), "apt", *args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert not home.exists()


@pytest.mark.parametrize("action", ["import", "switch"])
@pytest.mark.parametrize("old_source", ["ubuntu", "debian", "legacy"])
def test_removed_sources_are_rejected_before_mutation(
    tmp_path, capsys, action, old_source
):
    home = tmp_path / "absent"
    assert main(["--home", str(home), "apt", action, old_source, "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert "removed" in result["error"]["message"]
    assert "apt switch spack" in result["error"]["hint"]
    assert not home.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["install", "gsl", "--no-official"],
        ["import", "spack", "--suite", "noble"],
        ["update", "--recipes-only"],
        ["sources", "--add", "bad", "--url", "http://example.org"],
        ["install", "gsl", "--cache", "bad"],
    ],
)
def test_invalid_and_obsolete_options_do_not_initialize(tmp_path, capsys, args):
    home = tmp_path / "absent"
    assert main(["--home", str(home), "apt", *args, "--json"]) == 2
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert not home.exists()
