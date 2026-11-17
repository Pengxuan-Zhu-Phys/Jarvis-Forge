import json
import subprocess

import pytest
import yaml

from jarvis_forge.cli import main
from jarvis_forge.playlist_library import load
from jarvis_forge.storage import ForgeError, digest


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("JFORGE_LIBRARY", raising=False)
    directory = tmp_path / "workshop"
    directory.mkdir()
    checkout = tmp_path / "monitor"
    checkout.mkdir()
    (checkout / "test_one.py").write_text("raise RuntimeError('Do not import tests when listing cases')\n")
    card = checkout / "tests.yaml"
    card.write_text("""schema_version: 2
kind: checker
id: monitor
profiles:
  daily: [short]
  acceptance: [short]
default_profile: daily
coverage:
  paths: [test_one.py]
groups:
  - id: short
    match: [test_one.py]
    inputs: [test_one.py]
    needs: [preflight]
    skip_allow: []
    exclusive: [resource]
    timeout_seconds: 75
  - id: optional
    match: [test_one.py]
    cache: false
checks:
  - id: preflight
    argv: [unavailable-command]
    cache: false
""")
    manifest = {"schema_version": 1, "kind": "playlist-library", "id": "workshop", "title": "Workshop",
                "shelves": [{"id": "main", "title": "Main"}, {"id": "next", "title": "Next"}],
                "projects": [
                    {"id": "monitor", "title": "Monitor", "shelf": "main", "root": "../monitor",
                     "distributions": ["Jarvis-Monitor"], "environment": "default",
                     "sources": [{"id": "tests", "kind": "test", "path": "tests.yaml",
                                  "env": "local", "subjects": ["optional"]}]},
                    {"id": "optional", "title": "Optional", "shelf": "next",
                     "sources": [{"id": "tests", "kind": "test", "path": "tests.yaml"}]},
                ]}
    (directory / "library.yaml").write_text(yaml.safe_dump(manifest))
    return tmp_path / "forge", directory, manifest, card


@pytest.mark.parametrize(("scope", "field", "value", "message"), [
    ("library", "unknown", True, "unknown fields"),
    ("library", "schema_version", True, "schema_version: 1"),
    ("library", "id", "../outside", "Invalid identifier"),
    ("library", "title", None, "nonempty string"),
    ("shelf", "root", "/mnt/private", "unknown fields"),
    ("project", "root", "/mnt/private", "relative path"),
    ("project", "shelf", "missing", "unknown shelf"),
    ("project", "sources", {}, "must be a list"),
    ("source", "path", "/mnt/private/tests.yaml", "relative path"),
    ("source", "path", "C:/checkout/tests.yaml", "relative path"),
    ("source", "path", "../tests.yaml", "without '..'"),
    ("source", "profile", "daily", "unknown fields"),
    ("source", "subjects", ["missing"], "unknown subject"),
])
def test_library_rejects_invalid_fields(workspace, scope, field, value, message):
    _, directory, data, _ = workspace
    positions = {"library": data, "shelf": data["shelves"][0], "project": data["projects"][0],
                 "source": data["projects"][0]["sources"][0]}
    positions[scope][field] = value
    (directory / "library.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ForgeError, match=message):
        load(directory)


@pytest.mark.parametrize("scope", ["shelves", "projects", "sources"])
def test_library_rejects_duplicate_ids(workspace, scope):
    _, directory, data, _ = workspace
    entries = data["projects"][0][scope] if scope == "sources" else data[scope]
    entries.append(dict(entries[0]))
    (directory / "library.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ForgeError, match="Duplicate ID"):
        load(directory)


def test_init_is_exclusive_and_selection_precedence(workspace, capsys, monkeypatch):
    forge, original, _, _ = workspace
    alternative = original.parent / "alternative"
    options = ["--home", str(forge), "playlist"]
    assert main([*options, "init", str(alternative), "--id", "alternate", "--title", "Alternate", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["library"] == "alternate"
    initializer = alternative / "library.yaml"
    before = initializer.read_bytes()
    assert main([*options, "init", str(alternative), "--json"]) == 2
    assert initializer.read_bytes() == before
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert not forge.exists()
    assert main([*options, "use", str(original), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["library"] == "workshop"
    entries = ["project", "list", "--json"]
    assert main([*options, *entries]) == 0
    assert json.loads(capsys.readouterr().out)["library"] == "workshop"
    monkeypatch.setenv("JFORGE_LIBRARY", str(alternative))
    assert main([*options, *entries]) == 0
    assert json.loads(capsys.readouterr().out)["library"] == "alternate"
    assert main([*options, "--library", str(original), *entries]) == 0
    assert json.loads(capsys.readouterr().out)["library"] == "workshop"


def test_checkout_resolution_and_strict_verification(workspace, capsys):
    forge, directory, _, card = workspace
    options = ["--home", str(forge), "playlist", "--library", str(directory)]
    assert main([*options, "project", "list", "--json"]) == 0
    relative, absent = json.loads(capsys.readouterr().out)["projects"]
    assert (relative["root_source"], relative["root"], relative["available"]) == ("relative", str(card.parent), True)
    assert not absent["available"] and absent["root"] is None
    assert main([*options, "verify", "--strict", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"] and "optional is unavailable" in result["error"]["message"]
    assert main([*options, "verify"]) == 0
    assert "Warning:" in capsys.readouterr().err
    assert not forge.exists()
    for name in ("monitor", "optional"):
        assert main([*options, "project", "path", name, str(card.parent)]) == 0
    assert main([*options, "verify", "--strict"]) == 0
    capsys.readouterr()
    assert main([*options, "project", "list", "--json"]) == 0
    assert all(row["root_source"] == "registered" for row in json.loads(capsys.readouterr().out)["projects"])
    state = forge / "playlists/workshop/roots.json"
    assert json.loads(state.read_text()) == {"monitor": str(card.parent), "optional": str(card.parent)}
    moved = directory.parent / "moved"
    moved.mkdir()
    assert main([*options, "project", "path", "monitor", str(moved)]) == 0
    moved.rmdir()
    capsys.readouterr()
    assert main([*options, "project", "list", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)["projects"][0]
    assert row["root"] == str(moved) and row["root_source"] == "registered" and not row["available"]


def test_inventory_lists_all_test_steps_without_running_them(workspace, capsys, monkeypatch):
    forge, directory, _, card = workspace
    options = ["--home", str(forge), "playlist", "--library", str(directory)]

    def unexpected(*args, **kwargs):
        pytest.fail("Generating a playlist inventory must not start processes")

    monkeypatch.setattr(subprocess, "run", unexpected)
    assert main([*options, "cases", "--shelf", "main", "--project", "monitor", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["warnings"] == [] and result["library"] == "workshop"
    cases = {case["id"]: case for case in result["cases"]}
    assert set(cases) == {"monitor/tests/preflight", "monitor/tests/short", "monitor/tests/optional"}
    assert cases["monitor/tests/preflight"]["kind"] == "check"
    assert cases["monitor/tests/preflight"]["profiles"] == ["daily", "acceptance"]
    assert cases["monitor/tests/optional"]["profiles"] == []
    short = cases["monitor/tests/short"]
    assert short["needs"] == ["monitor/tests/preflight"]
    assert short["exclusive"] == ["resource"] and short["timeout_seconds"] == 75
    assert short["sha256"] == digest(card) and short["subjects"] == ["monitor", "optional"]
    assert not forge.exists()
    assert main([*options, "sync", "--json"]) == 0
    capsys.readouterr()
    cache = forge / "playlists/workshop/inventory.json"
    before = cache.read_bytes()
    timestamp = cache.stat().st_mtime_ns
    assert main([*options, "sync"]) == 0
    assert cache.read_bytes() == before and cache.stat().st_mtime_ns == timestamp
    capsys.readouterr()
    card.write_text("bad: yaml")
    assert main([*options, "sync", "--json"]) == 2
    assert "monitor/tests" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert cache.read_bytes() == before


@pytest.mark.parametrize("command", [[], ["init"], ["use"], ["project"], ["project", "list"],
                                     ["project", "path"], ["sync"], ["verify"], ["cases"]])
def test_playlist_cli_help(command, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert main(["playlist", *command, "-h"]) == 0
    output = capsys.readouterr().out
    assert f"Usage: jforge playlist{' ' if command else ''}{' '.join(command)}" in output
    assert "Show this message and exit." in output and "\x1b[" not in output


def test_playlist_cli_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("JFORGE_LIBRARY", raising=False)
    assert main(["playlist", "unknown"]) == 2
    stderr = capsys.readouterr().err
    assert "unknown" in stderr and "Traceback" not in stderr and "\x1b[" not in stderr
    assert main(["playlist", "cases", "--home", str(tmp_path / "absent"), "--json"]) == 2
    output = capsys.readouterr()
    error = json.loads(output.out)
    assert not error["ok"] and error["error"]["type"] and "No playlist library selected" in error["error"]["message"]
    assert not output.err and not (tmp_path / "absent").exists()
