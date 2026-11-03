from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest
import yaml
from packaging.version import Version

from jarvis_forge.catalog import Catalog, requirement, source_config
from jarvis_forge.cli import main
from jarvis_forge.downloads import unpack
from jarvis_forge.packages import (edit_sources, environment_path, install, installed, locked,
                                   plan_install, plan_remove, remove, state, update)
from jarvis_forge.storage import ForgeError, ForgeHome, read_json, write_json


@pytest.fixture
def home(tmp_path):
    home = ForgeHome(tmp_path / "forge home")
    home.initialize()
    return home


def recipe(home, tmp_path, name="toy", version="1.0", depends=(), fail=False):
    archive = tmp_path / f"{name}-{version}.tar.gz"
    content = f"#!/usr/bin/env bash\nprintf '%s\\n' '{name} {version}'\n".encode()
    with tarfile.open(archive, "w:gz") as package:
        info = tarfile.TarInfo(f"{name}-{version}/tool")
        info.size, info.mode = len(content), 0o755
        package.addfile(info, io.BytesIO(content))
    data = {
        "schema_version": 1, "name": name, "version": version,
        "description": f"A small {name} package for real local archive tests",
        "source": {"url": archive.as_uri(), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                   "subdir": f"{name}-{version}"},
        "depends": list(depends),
        "install": [["${python}", "-c",
                     "from pathlib import Path; import shutil,sys; p=Path(sys.argv[1]); "
                     "(p/'bin').mkdir(exist_ok=True); shutil.copy2('tool',p/'bin'/sys.argv[2])",
                     "${prefix}", name]],
        "check": [["${prefix}/bin/" + name, "--version"]],
        "environment": {"prepend": {"PATH": ["${prefix}/bin"], "CMAKE_PREFIX_PATH": ["${prefix}"]},
                        "set": {name.upper() + "_ROOT": "${prefix}"}},
    }
    if fail:
        data["check"] = [["${python}", "-c", "raise SystemExit(9)"]]
    path = home.root / "recipes" / f"{name}-{version}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))
    return path, data


def test_real_local_install_activation_and_receipts(home, tmp_path):
    recipe(home, tmp_path)
    result = install(home, ["toy=1.0"])
    assert result["status"] == "passed"
    records = installed(home)
    assert len(records) == 1 and records[0]["active"] and not records[0]["automatic"]
    prefix = Path(records[0]["prefix"])
    assert prefix.is_relative_to(home.root)
    activate = environment_path(home)
    probe = subprocess.run(["bash", "-c", 'source "$1"; toy --version; printf "%s" "$TOY_ROOT"',
                            "bash", activate], capture_output=True, text=True, check=True)
    assert probe.stdout == "toy 1.0\n" + str(prefix)
    assert read_json(Path(result["path"]) / "result.json")["status"] == "passed"
    assert (Path(result["path"]) / "toy/command-002-check.stdout.log").read_text() == "toy 1.0\n"


def test_repeated_install_reuses_receipt_without_rebuilding(home, tmp_path):
    recipe(home, tmp_path)
    first = install(home, ["toy"])
    second = install(home, ["toy"])
    assert second["packages"][0]["action"] == "reuse"
    assert first["packages"][0]["prefix"] == second["packages"][0]["prefix"]
    assert len(home.packages()) == 1


def test_reinstall_keeps_old_instance_and_old_activation(home, tmp_path):
    recipe(home, tmp_path)
    first = install(home, ["toy"])
    old_activation = Path(first["activation"]).read_text()
    second = install(home, ["toy"], mode="reinstall")
    assert first["packages"][0]["install_id"] != second["packages"][0]["install_id"]
    assert Path(first["packages"][0]["prefix"]).is_dir()
    assert Path(first["activation"]).read_text() == old_activation
    assert len(installed(home)) == 2


def test_failed_upgrade_preserves_working_active_environment(home, tmp_path):
    recipe(home, tmp_path, version="1.0")
    first = install(home, ["toy"])
    before = state(home)
    recipe(home, tmp_path, version="2.0", fail=True)
    with pytest.raises(ForgeError, match="exit 9"):
        install(home, [], mode="upgrade")
    assert state(home) == before
    assert Path(first["packages"][0]["prefix"]).is_dir()
    assert len(installed(home)) == 1
    results = [read_json(p) for p in (home.root / "operations").glob("*/result.json")]
    assert any(r["status"] == "failed" for r in results)


def test_upgrade_installs_new_version_and_retains_old(home, tmp_path):
    recipe(home, tmp_path, version="1.0")
    install(home, ["toy"])
    recipe(home, tmp_path, version="2.0")
    result = install(home, [], mode="full-upgrade")
    assert result["packages"][0]["version"] == "2.0"
    assert {r["version"] for r in installed(home)} == {"1.0", "2.0"}
    assert [r["version"] for r in installed(home) if r["active"]] == ["2.0"]


def test_dependencies_remove_and_autoremove(home, tmp_path):
    recipe(home, tmp_path, "lib")
    recipe(home, tmp_path, "app", depends=["lib>=1"])
    install(home, ["app"])
    assert [r["name"] for r in installed(home) if r["automatic"]] == ["lib"]
    with pytest.raises(ForgeError, match="still needed"):
        plan_remove(home, ["lib"])
    assert plan_remove(home, [], automatic=True)["packages"] == []
    remove(home, ["app"])
    orphan = plan_remove(home, [], automatic=True)
    assert [p["name"] for p in orphan["packages"]] == ["lib"]
    remove(home, [], automatic=True)
    assert installed(home) == []
    assert state(home)["active"] == {}


def test_requested_dependency_becomes_manual_and_survives_autoremove(home, tmp_path):
    recipe(home, tmp_path, "lib")
    recipe(home, tmp_path, "app", depends=["lib"])
    install(home, ["app"])
    install(home, ["lib"])
    remove(home, ["app"])
    assert plan_remove(home, [], automatic=True)["packages"] == []


def test_resolver_backtracks_and_reports_conflicts(home, tmp_path):
    recipe(home, tmp_path, "lib", "1.0")
    recipe(home, tmp_path, "lib", "2.0")
    recipe(home, tmp_path, "app", "1.0", depends=["lib<2"])
    recipe(home, tmp_path, "app", "2.0", depends=["lib>=2"])
    choices = Catalog(home).resolve(["app", "lib<2"])
    assert [(r["name"], r["version"]) for r in choices] == [("lib", "1.0"), ("app", "1.0")]
    with pytest.raises(ForgeError, match="No compatible"):
        Catalog(home).resolve(["app=2.0", "lib<2"])


def test_dependency_cycles_are_rejected(home, tmp_path):
    recipe(home, tmp_path, "a", depends=["b"])
    recipe(home, tmp_path, "b", depends=["a"])
    with pytest.raises(ForgeError, match="cycle"):
        Catalog(home).resolve(["a"])


@pytest.mark.parametrize("spec", ["toy=1.0", "toy==1.0", "toy>=1.0", "toy (>= 1.0)"])
def test_apt_style_constraints(spec):
    req = requirement(spec)
    assert req.name == "toy" and Version("1.0") in req.specifier


def test_queries_and_dry_run_do_not_create_user_home(tmp_path, capsys):
    path = tmp_path / "absent"
    for args in [["list"], ["search", "linear"], ["show", "eigen"], ["edit-sources"],
                 ["install", "eigen", "--dry-run"], ["satisfy", "eigen>=3.4", "--dry-run"]]:
        assert main(["--home", str(path), "apt", *args, "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
        assert not path.exists()


@pytest.mark.parametrize("command", [[], ["list"], ["search"], ["show"], ["install"], ["reinstall"],
    ["remove"], ["autoremove"], ["update"], ["upgrade"], ["full-upgrade"], ["edit-sources"], ["satisfy"], ["env"]])
def test_apt_help(command, capsys):
    assert main(["apt", *command, "-h"]) == 0
    output = capsys.readouterr().out
    assert "Usage: jforge apt" in output and "Show this message and exit." in output
    assert "\x1b[" not in output


def test_cli_install_info_and_remove_confirmation(home, tmp_path, capsys):
    recipe(home, tmp_path)
    args = ["--home", str(home.root), "apt"]
    assert main([*args, "install", "toy", "--json"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["command"] == "apt install" and value["packages"][0]["name"] == "toy"
    assert main(["info", "toy", "--home", str(home.root), "--path"]) == 0
    assert Path(capsys.readouterr().out.strip()).is_dir()
    assert main([*args, "env", "--path"]) == 0
    assert Path(capsys.readouterr().out.strip()).is_file()
    assert main([*args, "remove", "toy", "--json"]) == 2
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert len(installed(home)) == 1
    assert main([*args, "remove", "toy", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "apt remove"
    assert installed(home) == []


def test_update_sources_is_explicit_and_preserves_good_catalog_on_failure(home, tmp_path):
    path, data = recipe(home, tmp_path, "external")
    source = tmp_path / "repository"
    source.mkdir()
    (source / "external.yaml").write_text(path.read_text())
    path.unlink()
    edit_sources(home, add=str(source))
    assert not any(p["name"] == "external" for p in Catalog(home).list())
    update(home)
    assert any(p["name"] == "external" for p in Catalog(home).list())
    before = (home.root / "cache/apt/catalog.json").read_bytes()
    (source / "external.yaml").write_text("bad: yaml\n")
    with pytest.raises(ForgeError):
        update(home)
    assert (home.root / "cache/apt/catalog.json").read_bytes() == before


def test_checksum_failure_does_not_create_installation(home, tmp_path):
    path, data = recipe(home, tmp_path)
    data["source"]["sha256"] = "0" * 64
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ForgeError, match="SHA-256 mismatch"):
        install(home, ["toy"])
    assert home.packages() == [] and state(home)["active"] == {}


@pytest.mark.parametrize("kind", ["traversal", "symlink", "hardlink", "fifo"])
def test_archive_escape_and_special_files_are_rejected(tmp_path, kind):
    path = tmp_path / "unsafe.tar"
    with tarfile.open(path, "w") as package:
        info = tarfile.TarInfo("../outside" if kind == "traversal" else "link")
        info.type = {"traversal": tarfile.REGTYPE, "symlink": tarfile.SYMTYPE,
                     "hardlink": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE}[kind]
        info.linkname = "../outside"
        package.addfile(info, io.BytesIO())
    with pytest.raises(ForgeError):
        unpack(path, tmp_path / "extracted")
    assert not (tmp_path / "outside").exists()


def test_package_operation_lock_is_not_stolen(home, tmp_path):
    recipe(home, tmp_path)
    with locked(home):
        with pytest.raises(ForgeError, match="Another package operation"):
            install(home, ["toy"])


def test_unmanaged_or_outside_prefix_is_never_removed(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    write_json(home.root / "packages/toy/1.0/invalid/manifest.json", {
        "name": "toy", "version": "1.0", "install_id": "invalid", "prefix": str(outside), "managed_by": "jforge-apt"})
    with pytest.raises(ForgeError, match="Refusing"):
        remove(home, ["toy"])
    assert outside.exists()


def test_managed_directory_symlink_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "home"
    root.mkdir()
    (root / "packages").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ForgeError, match="escapes"):
        update(ForgeHome(root))
    assert list(outside.iterdir()) == []


def test_remove_refuses_a_changed_confirmation_plan(home, tmp_path):
    recipe(home, tmp_path)
    install(home, ["toy"])
    with pytest.raises(ForgeError, match="changed after"):
        remove(home, ["toy"], expected_keys=[])
    assert len(installed(home)) == 1


def test_timeout_leaves_command_result_and_no_install_receipt(home, tmp_path):
    path, data = recipe(home, tmp_path)
    data["check"] = [["${python}", "-c", "import time; time.sleep(30)"]]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ForgeError, match="timed out"):
        install(home, ["toy"], timeout=0.15)
    assert home.packages() == []
    commands = [read_json(p) for p in (home.root / "operations").glob("*/toy/command-*.json")]
    timed_out = [c for c in commands if c["status"] == "timeout"]
    assert len(timed_out) == 1
    assert timed_out[0]["finished_at"] and timed_out[0]["elapsed_seconds"] > 0


def test_missing_tool_fails_dry_run_before_initialization(tmp_path):
    home = ForgeHome(tmp_path / "home")
    path, data = recipe(home, tmp_path)
    data["requires_tools"] = ["jforge-missing-tool-for-test"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ForgeError, match="Required build tools"):
        plan_install(home, ["toy"])
    assert not (home.root / "config.json").exists()
    assert not (home.root / "operations").exists()


def test_update_empty_sources_and_apt_list_json(tmp_path, capsys):
    home = tmp_path / "fresh"
    assert main(["apt", "update", "--home", str(home), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["available"] >= 2
    assert main(["apt", "list", "--home", str(home), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {p["name"] for p in result["packages"]} >= {"eigen", "redis"}


def test_source_removal_requires_confirmation(home, tmp_path, capsys):
    source = tmp_path / "repo"
    source.mkdir()
    edit_sources(home, add=str(source))
    args = ["apt", "edit-sources", "--home", str(home.root), "--remove", str(source), "--json"]
    assert main(args) == 2
    capsys.readouterr()
    assert source_config(home)["sources"] == [str(source)]
    assert main([*args, "--yes"]) == 0
    assert json.loads(capsys.readouterr().out)["sources"] == []


def test_explicit_compiler_version_changes_instance_identity(home, tmp_path, monkeypatch):
    path, data = recipe(home, tmp_path)
    data["requires_tools"] = ["cc"]
    path.write_text(yaml.safe_dump(data))
    compiler = tmp_path / "compiler"
    compiler.write_text("#!/usr/bin/env bash\nprintf 'compiler 1.0\\n'\n")
    compiler.chmod(0o755)
    monkeypatch.setenv("CC", str(compiler))
    first = plan_install(home, ["toy"])
    assert first["context"]["tools"]["cc"]["path"] == str(compiler)
    compiler.write_text("#!/usr/bin/env bash\nprintf 'compiler 2.0\\n'\n")
    second = plan_install(home, ["toy"])
    assert first["packages"][0]["install_id"] != second["packages"][0]["install_id"]


def test_explicit_upgrade_constraint_is_not_ignored(home, tmp_path):
    recipe(home, tmp_path, version="1.0")
    install(home, ["toy"])
    recipe(home, tmp_path, version="2.0")
    plan = plan_install(home, ["toy=1.0"], mode="upgrade")
    assert plan["packages"][0]["version"] == "1.0"
