"""Noninteractive profile operations and NFS-like interrupted deletion."""

import errno
import fcntl
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis_forge import apt_cli, scope_cleanup, unified_profiles as profiles
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeError, ForgeHome, read_json
from profile_fixtures import project_profile


def test_profile_remove_closes_inner_locks_before_deleting_them(tmp_path, monkeypatch, capsys):
    base = ForgeHome(tmp_path / "forge")
    scope = project_profile(base, "ubuntu24")
    real = scope_cleanup.shutil.rmtree
    observed = []

    def nfs_remove(path, *args, **kwargs):
        path = Path(path)
        if path.name == "locks":
            for lock in path.glob("*.lock"):
                with lock.open("r+") as stream:
                    # An open, held inner lock would make NFS leave a .nfs file.
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    observed.append(lock.name)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(scope_cleanup.shutil, "rmtree", nfs_remove)
    assert main(["--home", str(base.root), "profile", "remove", "ubuntu24", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "ubuntu24"
    assert set(observed) == {"apps.lock", "apt.lock"}
    assert not scope.root.exists() and profiles.selection(base) is None


def test_interrupted_remove_keeps_ownership_and_same_command_finishes(tmp_path, monkeypatch):
    base = ForgeHome(tmp_path / "forge")
    scope = project_profile(base, "ubuntu24")
    real = scope_cleanup.shutil.rmtree

    def fail(path, *args, **kwargs):
        if Path(path).name == "cache":
            raise PermissionError(errno.EACCES, "simulated cleanup failure", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(scope_cleanup.shutil, "rmtree", fail)
    with pytest.raises(ForgeError, match="Cleanup.*incomplete"):
        profiles.remove(base, "ubuntu24")
    assert read_json(scope.root / "metadata.json")["status"] == "removing"
    assert scope_cleanup.registered_removal(base, scope)
    monkeypatch.setattr(scope_cleanup.shutil, "rmtree", real)
    profiles.remove(base, "ubuntu24")
    assert not scope.root.exists()
    assert not scope_cleanup.receipt(base, scope).exists()


def test_profile_remove_refuses_an_active_operation_without_changing_files(tmp_path):
    from jarvis_forge.package_operations import locked

    base = ForgeHome(tmp_path / "forge")
    scope = project_profile(base, "ubuntu24")
    before = (scope.root / "metadata.json").read_bytes()
    with locked(scope):
        with pytest.raises(ForgeError, match="current operation|busy"):
            profiles.remove(base, "ubuntu24")
    assert (scope.root / "metadata.json").read_bytes() == before
    assert not scope_cleanup.receipt(base, scope).exists()


def test_profile_remove_recovers_lock_only_residue_and_protects_unknown_files(tmp_path):
    base = ForgeHome(tmp_path / "forge")
    base.initialize()
    scope = ForgeHome(base.root / "profiles/ubuntu24")
    locks = scope.root / "locks"
    locks.mkdir(parents=True)
    (locks / ".nfs001abc").write_text("")
    profiles.remove(base, "ubuntu24")
    assert not scope.root.exists()
    locks.mkdir(parents=True)
    user = locks / "user-file"
    user.write_text("keep")
    with pytest.raises(ForgeError, match="Unregistered"):
        profiles.remove(base, "ubuntu24")
    assert user.read_text() == "keep"


def test_native_plans_display_and_execute_without_prompt(monkeypatch, capsys):
    import typer

    monkeypatch.setattr(typer, "confirm", lambda *a, **kw: pytest.fail("No native operation should prompt"))
    context = SimpleNamespace(obj=SimpleNamespace(as_json=False))
    apt_cli._changes_confirmation(context, False, {"install": [{"name": "gcc", "version": "13.3.0"}]})
    apt_cli._confirm(context, False, "Remove ubuntu24?")
    output = capsys.readouterr().err
    assert "gcc@13.3.0" in output and "Remove ubuntu24" in output
    assert "[Y/n]" not in output and "--yes" not in output


def test_profile_cli_has_only_the_five_builtin_operations(tmp_path, capsys):
    from jarvis_forge.profile_cli import app

    assert {command.name for command in app.registered_commands} == {"check", "create", "use", "show", "remove"}
    home = tmp_path / "absent"
    args = ["--home", str(home), "--json", "profile"]
    assert main([*args, "show"]) == 0
    assert {row["id"] for row in json.loads(capsys.readouterr().out)["instances"]} == {"ubuntu22", "ubuntu24", "ubuntu26", "macos"}
    for removed in (["export", "ubuntu24"], ["create", "ubuntu24", "--platform", "ubuntu24"],
                    ["create", "custom-name"], ["check"]):
        assert main([*args, *removed]) == 2
        capsys.readouterr()
    assert not home.exists()
