"""Exercise the Homebrew CLI with a local executable and a disposable Cellar."""

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis_forge import apt_profiles, build_catalog, homebrew_backend as brew
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeError, ForgeHome, read_json, write_json


@pytest.fixture
def setup_brew(tmp_path, monkeypatch, capsys):
    prefix = tmp_path / "brew prefix"
    (prefix / "Cellar").mkdir(parents=True)
    (prefix / "opt").mkdir()
    (prefix / "bin").mkdir()
    executable = prefix / "bin/brew"
    script = tmp_path / "brew.py"
    script.write_text("""import json, os, sys
from pathlib import Path

prefix = Path(os.environ["FAKE_BREW_PREFIX"])
installed = prefix / "installed.json"
rows = json.loads(installed.read_text())
args = sys.argv[1:]
with (prefix / "calls.jsonl").open("a") as stream:
    keys = os.environ["FAKE_BREW_CAPTURE"].split()
    print(json.dumps({"args": args, "env": {k: os.environ.get(k) for k in keys}}), file=stream)
verb, *options = args
target = options[-1].removeprefix("homebrew/core/") if options else None
if verb == "--prefix":
    print(prefix)
elif verb == "--cellar":
    print(prefix / "Cellar")
elif verb == "info":
    print(json.dumps({"formulae": list(rows.values()) if options[-1] == "--installed"
                      else [{"name": target, "tap": "homebrew/core"}]}))
elif verb == "formulae":
    print("eigen\\ngsl\\nneeds-python\\nroot")
elif verb == "deps":
    if target == "needs-python":
        print("native-library\\npython@3.14")
elif verb == "install":
    for name in (option.removeprefix("homebrew/core/") for option in options[1:]):
        assert name not in rows, name
        keg = prefix / "Cellar" / name / "1.0"
        (keg / "lib/pkgconfig").mkdir(parents=True)
        (keg / "INSTALL_RECEIPT.json").write_text("{}")
        (prefix / "opt" / name).symlink_to(keg)
        rows[name] = {"name": name, "tap": "homebrew/core",
                      "installed": [{"version": "1.0", "runtime_dependencies": []}]}
        installed.write_text(json.dumps(rows))
        if os.getenv("FAKE_BREW_FAIL"):
            sys.stderr.write("simulated install failure after writing a keg\\n")
            sys.exit(7)
elif verb != "update":
    sys.exit(f"unexpected Homebrew action: {verb}")
""")
    executable.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n")
    executable.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "login"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("ZDOTDIR", raising=False)
    monkeypatch.setenv("PATH", str(prefix / "bin") + ":" + str(Path(sys.executable).parent) + ":/usr/bin:/bin")
    monkeypatch.setenv("FAKE_BREW_PREFIX", str(prefix))
    monkeypatch.setenv("FAKE_BREW_CAPTURE", " ".join([*brew.BREW_ENV, "HOMEBREW_MAKE_JOBS"]))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(brew, "_darwin", lambda: None)
    if brew.os.geteuid() == 0:
        monkeypatch.setattr(brew.os, "geteuid", lambda: 1000)

    def add(name, dependencies=()):
        keg = prefix / "Cellar" / name / "1.0"
        (keg / "lib/pkgconfig").mkdir(parents=True)
        (keg / "INSTALL_RECEIPT.json").write_text("{}")
        (prefix / "opt" / name).symlink_to(keg)
        return {"name": name, "tap": "homebrew/core", "installed": [{
            "version": "1.0", "runtime_dependencies": [
                {"full_name": dep, "pkg_version": "1.0"} for dep in dependencies
            ],
        }]}

    (prefix / "installed.json").write_text(json.dumps({
        row["name"]: row for row in (add("eigen"), add("root", ["stale", "python@3.14"]))
    }))
    home = tmp_path / "forge"

    def cli(*args, as_json=True, status=0):
        result = main(["--home", str(home), *(["--json"] if as_json else []), *args])
        captured = capsys.readouterr()
        assert result == status, captured
        return json.loads(captured.out) if as_json else captured.out

    def writes():
        calls = map(json.loads, (prefix / "calls.jsonl").read_text().splitlines())
        return [call for call in calls if call["args"][0] in {"install", "uninstall", "update", "upgrade"}]

    created = cli("apt", "profile", "mac", "--create", "--backend", "homebrew")
    assert created["profile"] == "homebrew-mac"
    return cli, prefix, ForgeHome(Path(created["apt_home"])), writes


def test_profile_inventory_selection_and_reversible_shell(setup_brew):
    cli, prefix, home, writes = setup_brew
    assert cli("apt", "profile")["environments"][0]["backend"] == "homebrew"
    assert cli("env", "list")["profiles"][0]["id"] == "homebrew-mac"
    assert cli("apt", "search", "eigen")["packages"][0]["selected"] is False
    assert cli("apt", "show", "eigen")["installed_instances"][0]["prefix"] == str(prefix / "Cellar/eigen/1.0")
    assert cli("apt", "prefix", "eigen", "--path", as_json=False).strip() == str(prefix / "Cellar/eigen/1.0")
    installed = cli("apt", "install", "libeigen3-dev")
    assert installed["packages"][0]["name"] == "eigen"
    assert installed["environment_activation"] == str(home.root.parents[2] / "env/apt/activate.sh")
    assert Path(installed["environment_activation"]).is_file()
    assert installed["activation"] in Path(installed["environment_activation"]).read_text()
    assert cli("apt", "list", "eigen")["packages"][0]["selected"] is True
    active = cli("apt", "env", "--path", as_json=False).strip()
    # The alternate profile shares the same physical installation.
    empty = cli("apt", "profile", "alternate", "--create", "--backend", "homebrew")["activation"]
    assert cli("apt", "list", "eigen")["packages"][0]["selected"] is False
    selected = cli("apt", "profile", "mac")
    assert cli("apt", "env", "--path", as_json=False).strip() == active
    assert selected["activation"] in Path(active).read_text()
    assert not writes()
    for shell in ("bash", "zsh"):
        executable = shutil.which(shell)
        if not executable:
            continue
        subprocess.run([executable, "-c", """
            set -e
            probe='import sys; print(sys.executable, sys.prefix, sys.base_prefix)'
            before=$(python -c "$probe")
            before_path=$PATH
            before_flags=$-
            source "$1"
            test "$before" = "$(python -c "$probe")"
            test "$before_path" = "$PATH"
            test "$before_flags" = "$-"
            test "$CMAKE_PREFIX_PATH" = "$3"
            test "$PKG_CONFIG_PATH" = "$3/lib/pkgconfig"
            source "$2"
            test -z "${CMAKE_PREFIX_PATH:-}"
            test -z "${PKG_CONFIG_PATH:-}"
        """, "test", active, empty, str(prefix / "Cellar/eigen/1.0")], check=True)
    cli("apt", "remove", "eigen", "--yes")
    assert cli("apt", "list", "eigen")["packages"][0]["selected"] is False
    assert not brew.current(home)["roots"]
    assert not writes()


@pytest.mark.parametrize(("formula", "dependency"), [
    ("python@3.14", "python@3.14"), ("needs-python", "python@3.14"), ("root", "python@3.14"),
])
def test_python_policy_precedes_install(setup_brew, formula, dependency):
    cli, _, home, writes = setup_brew
    before = brew.current(home)
    result = cli("apt", "install", formula, status=2)
    assert dependency in result["error"]["message"]
    assert "Python belongs to pyenv" in result["error"]["message"]
    assert brew.current(home) == before
    assert not writes()


def test_plan_write_failure_retry_and_remove(setup_brew, monkeypatch):
    cli, prefix, home, writes = setup_brew
    before = brew.current(home)
    assert cli("apt", "install", "gsl", "--dry-run")["would_install"] == ["gsl"]
    assert cli("apt", "check", "gsl")["audited_dependencies"] == {"gsl": []}
    assert brew.current(home) == before
    assert not writes()
    monkeypatch.setenv("FAKE_BREW_FAIL", "1")
    error = cli("apt", "install", "gsl", "cmake", "--jobs", "3", status=1)["error"]
    assert "simulated install failure" in error["message"]
    assert "Operation logs:" in error["hint"]
    assert brew.current(home) == before
    assert (prefix / "Cellar/gsl/1.0").is_dir()
    assert cli("apt", "list", "gsl")["packages"][0]["selected"] is False
    monkeypatch.delenv("FAKE_BREW_FAIL")
    result = cli("apt", "install", "gsl", "cmake")
    assert result["would_install"] == ["cmake"]
    first, retry = writes()
    assert first["args"] == ["install", "--formula", "homebrew/core/gsl", "homebrew/core/cmake"]
    assert first["env"]["HOMEBREW_MAKE_JOBS"] == "3"
    assert retry["args"] == ["install", "--formula", "homebrew/core/cmake"]
    assert all(first["env"][k] == v for k, v in brew.BREW_ENV.items())
    assert read_json(Path(result["path"]) / "result.json")["state_committed"] is True
    assert (Path(result["path"]) / "brew.stderr.log").is_file()


def test_repair_lost_dependency_and_activation(setup_brew):
    cli, prefix, home, writes = setup_brew
    cli("apt", "install", "eigen", "gsl")
    state = brew.current(home)
    adapter = Path(state["activation"]).with_name("homebrew-environment.sh")
    adapter.unlink()
    cli("apt", "env", "--path", status=2, as_json=False)
    assert cli("env", "repair", "apt", "--dry-run")["changed"]
    assert not adapter.exists()
    assert cli("env", "repair", "apt")["activation"] != state["activation"]
    inventory = prefix / "installed.json"
    rows = read_json(inventory)
    rows["gsl"]["installed"][0]["runtime_dependencies"] = [{"full_name": "stale", "pkg_version": "1.0"}]
    inventory.write_text(json.dumps(rows))
    repair = cli("env", "repair")
    profile = next(item for item in repair["targets"] if item["name"] == "homebrew-mac")
    assert "stale" in profile["deferred_roots"]["gsl"]
    assert profile["removed"] == 1
    assert brew.current(home)["roots"] == ["eigen", "gsl"]
    assert [row["name"] for row in brew.current(home)["packages"]] == ["eigen"]
    cli("apt", "remove", "gsl", "--yes")
    assert [call["args"][0] for call in writes()] == ["install"]


def test_permission_and_spack_only_options(setup_brew, monkeypatch):
    cli, _, _, writes = setup_brew
    monkeypatch.setattr(brew.os, "geteuid", lambda: 0)
    result = cli("apt", "install", "gsl", status=2)
    assert "ordinary Homebrew owner" in result["error"]["message"]
    assert "Spack" in cli("apt", "compilers", status=2)["error"]["message"]
    assert "Spack cache" in cli("apt", "install", "gsl", "--cache", "only", status=2)["error"]["message"]
    assert not writes()


def test_explicit_update_and_missing_binding(setup_brew, monkeypatch):
    cli, prefix, home, writes = setup_brew
    catalog = {"platform": "darwin", "builds": [], "excluded_builds": {"root-build": ["linux"]}}
    monkeypatch.setattr(build_catalog, "update", lambda target: catalog)
    updated = cli("apt", "update")
    assert updated["backend"] == "homebrew"
    assert updated["build_catalog"] == catalog
    assert [call["args"] for call in writes()] == [["update"]]
    (prefix / "bin/brew").unlink()
    # Login can still load the previously generated script from its evidence.
    printed = cli("apt", "env", "--path", as_json=False).strip()
    assert brew.current(home)["activation"] in Path(printed).read_text()
    result = cli("apt", "list", status=1)
    assert "Homebrew launcher" in result["error"]["message"]


def test_init_migrates_legacy_spack_default_to_homebrew(setup_brew, monkeypatch):
    cli, _, profile, writes = setup_brew
    monkeypatch.setattr(apt_profiles, "sys", SimpleNamespace(platform="darwin"))
    base = ForgeHome(profile.root.parents[2])
    shutil.rmtree(profile.root)
    spack = base.root / "apt/profiles/spack-default"
    write_json(spack / "config.json", {})
    write_json(spack / "apt/spack.json", {})
    write_json(base.root / "apt/selection.json", {"schema_version": 1, "id": "spack-default"})
    assert cli("init")["apt_profile"] == {"id": "homebrew-mac", "backend": "homebrew", "status": "selected"}
    rows = {row["id"]: row for row in cli("apt", "profile")["environments"]}
    assert rows["homebrew-mac"]["active"] and not rows["spack-default"]["active"]
    assert cli("init")["apt_profile"]["id"] == "homebrew-mac"
    assert not writes()


def test_macos_apt_preview_is_native_and_recovers_missing_profile(setup_brew, monkeypatch):
    cli, _, profile, writes = setup_brew
    monkeypatch.setattr(apt_profiles, "sys", SimpleNamespace(platform="darwin"))
    base = profile.root.parents[2]
    selection = base / "apt/selection.json"
    shutil.rmtree(profile.root)
    selection.unlink()
    before = {file: file.read_bytes() for file in base.rglob("*") if file.is_file()}
    plan = cli("apt", "install", "eigen", "--dry-run")
    assert plan["backend"] == "homebrew" and plan["would_install"] == []
    assert before == {file: file.read_bytes() for file in base.rglob("*") if file.is_file()}
    cli("apt", "profile", "other", "--create", "--backend", "homebrew")
    write_json(selection, {"schema_version": 1, "platform": "darwin", "id": "homebrew-deleted"})
    profiles = {row["id"]: row for row in cli("apt", "profile")["environments"]}
    assert profiles["homebrew-deleted"]["status"] == "missing"
    assert profiles["homebrew-mac"]["active"]
    assert "homebrew-other" in profiles
    assert cli("init")["apt_profile"]["id"] == "homebrew-mac"
    assert read_json(selection)["id"] == "homebrew-mac"
    assert not writes()


def test_homebrew_registration_requires_darwin(monkeypatch):
    monkeypatch.setattr(brew.sys, "platform", "linux")
    with pytest.raises(ForgeError, match="require macOS"):
        brew.discover()


def test_spack_build_step_reports_homebrew_boundary(setup_brew, tmp_path):
    from jarvis_forge.builds import _step

    _, _, profile, writes = setup_brew
    home = ForgeHome(profile.root.parents[2])
    with pytest.raises(ForgeError, match="This build requires a Spack apt profile"):
        _step(home, {"uses": "apt.install"}, {"specs": ["eigen"]}, tmp_path, tmp_path / "logs",
              {}, {}, {}, {}, {}, 1, None, None)
    assert not writes()


def test_reuse_preserves_recorded_dependency_version(setup_brew):
    cli, prefix, _, writes = setup_brew
    cli("apt", "install", "gsl")
    inventory = prefix / "installed.json"
    rows = read_json(inventory)
    rows["eigen"]["installed"][0]["runtime_dependencies"] = [{"full_name": "gsl", "pkg_version": "1.0"}]
    rows["gsl"]["installed"].append({"version": "2.0", "runtime_dependencies": []})
    inventory.write_text(json.dumps(rows))
    upgraded = prefix / "Cellar/gsl/2.0"
    upgraded.mkdir()
    (upgraded / "INSTALL_RECEIPT.json").write_text("{}")
    link = prefix / "opt/gsl"
    link.unlink()
    link.symlink_to(upgraded)
    cli("apt", "profile", "new", "--create", "--backend", "homebrew")
    selected = cli("apt", "install", "eigen")["packages"]
    dependency = next(row for row in selected if row["name"] == "gsl")
    assert dependency["version"] == "1.0"
    assert dependency["automatic"]
    assert cli("apt", "install", "gsl", status=2)["error"]["message"].endswith("keg of 'gsl'.")
    assert [call["args"][0] for call in writes()] == ["install"]
