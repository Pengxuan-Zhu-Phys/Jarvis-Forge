"""Readable SSH results without losing detailed or machine-readable evidence."""

import io
import json

import pytest
from rich.console import Console

from jarvis_forge import spack_backend
from jarvis_forge.cli import main
from jarvis_forge.presentation import render


def displayed(command, values, width=80):
    stream = io.StringIO()
    render(Console(file=stream, width=width, color_system=None), command, values)
    return stream.getvalue()


def installed(name, *, automatic=False, version="1.0"):
    return dict(name=name, version=version, active=True, automatic=automatic,
                install_id="a" * 32, prefix="/store/__spack_path_placeholder__/" + name)


def test_installed_list_has_one_row_per_instance_and_no_metadata_dump():
    packages = [installed(f"package-{i:02}", automatic=i >= 8) for i in range(96)]
    output = displayed("apt list", dict(backend="spack", packages=packages))
    assert "96 packages · 8 requested · 88 dependencies" in output
    assert len(output.splitlines()) < 106
    assert output.index("package-07") < output.index("package-08")
    for item in packages:
        assert output.count(item["name"]) == 1
    for hidden in ("Instance:", "Prefix:", "Automatic:", "__spack_path_placeholder__", "a" * 32):
        assert hidden not in output
    assert "Details: jforge apt show <package>" in output
    assert "\x1b[" not in output


def test_narrow_list_preserves_long_names_versions_and_all_instances():
    name = "a-package-name-longer-than-the-terminal-width"
    version = "2026.11.05-with-a-long-version"
    packages = [installed(name, version=version), installed(name, version="2.0")]
    output = displayed("apt list", dict(packages=packages), width=24)
    assert f"{name} {version}" in output
    assert f"{name} 2.0" in output
    assert output.count(name) == 2
    assert "..." not in output


@pytest.mark.parametrize("command", ["apt list", "apt search"])
def test_available_and_installed_search_results_have_unambiguous_states(command):
    output = displayed(command, dict(packages=[dict(name="eigen", status="available"),
                       {**installed("glibc", automatic=True), "external": True},
                       {**installed("sqlite"), "archived": True, "active": False}]))
    assert "available" in output and "active, external" in output and "archived" in output


def test_env_list_names_the_command_for_each_section():
    output = displayed("env list", {
        "environments": [{"name": "apt", "status": "ready", "description": "Active user-local packages + ROOT",
                          "path": "/forge/env/apt/activate.sh"}],
        "profiles": [{"id": "spack-default", "active": True, "path": "/forge/apt/profiles/spack-default",
                      "backend": "spack"}],
    })
    assert "jforge env switch <name>" in output
    assert "jforge apt profile <name>" in output
    assert "jforge env metadata <name>" in output
    assert "Active user-local packages + ROOT" in output
    assert output.index("apt") < output.index("spack-default")


def test_env_metadata_lists_attached_and_omitted_builds():
    output = displayed("env metadata", {
        "path": "/forge/env/apt/env.json",
        "metadata": {
            "name": "apt", "kind": "apt", "description": "Active user-local packages + ROOT",
            "revision": "op-1", "profile": "spack-default",
            "backend_activation": "/forge/revisions/op-1/activate.sh",
            "activation": "/forge/env/apt/activate.sh",
            "builds": [{"build_id": "root-build", "output": "installation", "artifact_id": "root-1",
                        "activation": "/forge/root/activate.sh"}],
            "omitted_builds": [],
        },
    })
    assert "apt [apt]" in output
    assert "spack-default" in output
    assert "root · installation" in output
    assert "artifact root-1" in output
    assert "/forge/env/apt/env.json" in output
    assert "Omitted (0):" in output and "none" in output
    omitted = displayed("env metadata", {
        "path": "/forge/env/apt/env.json",
        "metadata": {"name": "apt", "kind": "apt", "description": "Active user-local packages",
                     "builds": [], "omitted_builds": ["root-build"], "activation": "/forge/env/apt/activate.sh"},
    })
    assert "Builds (0):" in omitted and "root" in omitted


def test_switch_says_when_this_shell_stays_unchanged():
    output = displayed("env switch", {"selected": "apt", "path": "/forge/env/apt/activate.sh",
                                      "description": "Active user-local packages", "shell": "unchanged"})
    assert "Selected environment: apt" in output
    assert "This shell is unchanged" in output
    assert "shell-init" in output
    pending = displayed("env drop", {"build_id": "root-build", "path": "/forge/env/apt/activate.sh",
                                     "description": "Active user-local packages", "shell": "pending"})
    assert "Removed from the apt environment: root" in pending
    assert "when the command returns" in pending


def test_empty_query_is_short_and_clear():
    output = displayed("apt search", dict(backend="spack", packages=[]))
    assert "0 packages" in output and "No matching packages." in output
    assert "Details:" not in output


@pytest.mark.parametrize("command", ["install", "reinstall", "upgrade", "full-upgrade", "satisfy", "remove"])
def test_transactions_summarize_roots_and_preserve_copyable_paths(command):
    packages = [installed("sqlite"), installed("readline", automatic=True)]
    log = "/very/long/" * 12 + "operations/apt-operation"
    activation = log + "/activate.sh"
    output = displayed("apt " + command, dict(status="passed", roots=["sqlite"], packages=packages,
                       path=log, activation=activation, lockfile=log + "/spack.lock"), width=50)
    assert "2 packages · 1 requested · 1 dependency" in output
    assert "sqlite" in output and "readline" not in output
    assert log + "\n" in output and activation + "\n" in output
    assert "spack.lock" not in output


def test_check_shows_complete_plan_including_uninstalled_dependencies():
    output = displayed("apt check", dict(packages=[dict(name="sqlite", version="3.53", root=True),
                       dict(name="readline", version="8.3", root=False)], status="passed"))
    assert "sqlite" in output and "readline" in output and "planned" in output
    assert "no packages built or activated" in output
    assert "list --installed" not in output


def test_public_list_json_and_show_keep_full_metadata(tmp_path, monkeypatch, capsys, linux_apt):
    package = installed("eigen", version="5.0.1")
    monkeypatch.setattr(spack_backend, "current", lambda home: dict(packages=[package]))
    monkeypatch.setattr(spack_backend, "details", lambda home, name: dict(name=name))
    common = ["--home", str(tmp_path / "absent"), "apt"]
    assert main([*common, "list", "--installed"]) == 0
    assert "Prefix:" not in capsys.readouterr().out
    assert main([*common, "list", "--installed", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["packages"] == [package]
    assert main([*common, "show", "eigen"]) == 0
    details = capsys.readouterr().out
    assert package["prefix"] in details and package["install_id"] in details
    assert not (tmp_path / "absent").exists()
