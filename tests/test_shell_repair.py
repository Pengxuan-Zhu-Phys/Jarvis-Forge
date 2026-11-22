"""Exercise shell repair through the CLI and real Bash activation."""

import json
from pathlib import Path
import shlex
import subprocess
import sys

from jarvis_forge.cli import main
from jarvis_forge.environment_registry import add, show
from jarvis_forge.storage import ForgeHome, digest, write_json


def test_workspace_repairs_nested_links_without_editing_build_artifacts(tmp_path, capsys, monkeypatch):
    from jarvis_forge.shell_repair import BUILD_HEADER, WORKSPACE_HEADER

    monkeypatch.chdir(tmp_path)
    install = tmp_path / "ROOT install"
    bin_dir = install / "bin"
    bin_dir.mkdir(parents=True)
    vendor = bin_dir / "thisroot.sh"
    vendor.write_text('export ROOTSYS=${BASH_SOURCE[0]%/bin/*}\n')
    dead = tmp_path / "dangling"
    dead.symlink_to(tmp_path / "deleted")
    wrong_type = tmp_path / "not-a-directory"
    wrong_type.touch()
    legacy = tmp_path / "ROOT.install-activate.sh"
    original = (BUILD_HEADER + "set -e\nset -o pipefail\n"
                + "source " + shlex.quote(str(dead)) + "\n"
                + f'export PATH="{bin_dir}:{wrong_type}:$PATH"\n'
                + f'export CMAKE_PREFIX_PATH="{install}:{dead}${{CMAKE_PREFIX_PATH:+:${{CMAKE_PREFIX_PATH}}}}"\n'
                + "source " + shlex.quote(str(vendor)) + "\n")
    legacy.write_text(original)
    entry = tmp_path / "activate.sh"
    wrapper = WORKSPACE_HEADER + "source " + shlex.quote(str(legacy)) + "\n"
    entry.write_text(wrapper)
    entry.chmod(0o750)

    assert main(["env", "repair", "--shell", str(entry), "--dry-run", "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["removed"] == 5
    assert entry.read_text() == wrapper and not list(tmp_path.glob(".activate.sh.*"))

    assert main(["env", "repair", "--shell", str(entry), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["removed"] == preview["removed"]
    assert Path(report["backup"]).read_text() == wrapper
    assert json.loads(Path(report["report"]).read_text())["settings"] == report["settings"]
    assert legacy.read_text() == original and dead.is_symlink()
    assert entry.stat().st_mode & 0o777 == 0o750
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-ic",
         'export PATH=/usr/bin:/bin CMAKE_PREFIX_PATH=/usr; flags=$-; source "$1" || exit 12; '
         'test "$flags" = "$-" || exit 13; false | true; test $? = 0 || exit 14; '
         'false; printf "%s\\n" "$ROOTSYS" "$PATH" "$CMAKE_PREFIX_PATH"',
         "fixture", str(entry)], check=True, text=True, capture_output=True,
    )
    assert result.stdout.splitlines() == [str(install), f"{bin_dir}:/usr/bin:/bin", f"{install}:/usr"]

    assert main(["env", "repair", "--shell", str(entry), "--json"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert not repeated["changed"] and repeated["removed"] == 0


def test_shell_repair_leaves_compound_and_dynamic_settings_unexecuted(tmp_path, capsys, monkeypatch):
    entry = tmp_path / "activate.sh"
    marker = tmp_path / "executed"
    startup = tmp_path / "bash-env"
    startup.write_text("touch " + shlex.quote(str(marker)) + "\n")
    monkeypatch.setenv("BASH_ENV", str(startup))
    missing = tmp_path / "missing"
    original = (f"cat <<'SH'\nsource {missing}\nSH\n"
                + f"load() {{\n  source {missing}\n}}\n"
                + f"if true; then\n  export PATH={missing}\nfi\n"
                + f'export PATH="$(touch {marker})"\n'
                + 'source "$HOME/optional.sh"\n'
                + "export API_TOKEN=do-not-record-this-value\n")
    entry.write_text(original + f"export ROOTSYS={missing} # retired\n")
    assert main(["env", "repair", "--shell", str(entry), "--json"]) == 0
    result = capsys.readouterr().out
    assert json.loads(result)["removed"] == 1
    assert "do-not-record-this-value" not in result and not marker.exists()
    assert entry.read_text() == original + "# retired\n"

    entry.write_text("if true; then\n")
    assert main(["env", "repair", "--shell", str(entry), "--json"]) == 2
    assert "Invalid Bash activation script" in capsys.readouterr().out
    assert entry.read_text() == "if true; then\n"


def test_managed_repair_discovers_legacy_workspaces_and_keeps_rebuild_links(tmp_path, capsys, monkeypatch):
    from jarvis_forge.build_workspace import MARKER
    from jarvis_forge.shell_repair import WORKSPACE_HEADER

    home = ForgeHome(tmp_path / "forge")
    missing = tmp_path / "projects" / "old ROOT's workspace"
    live = tmp_path / "projects" / "live"
    for index, directory in enumerate((missing, live)):
        write_json(home.root / f"runs/build-{index}/request.json", {"kind": "build", "workspace": str(directory)})
    write_json(live / MARKER, {"schema_version": 1, "build": "build.yaml", "home": str(home.root)})
    activation = live / "activate.sh"
    activation.write_text(WORKSPACE_HEADER + f"source {tmp_path}/gone.sh || return $?\n")
    (tmp_path / "alias").symlink_to(missing.parent, target_is_directory=True)
    missing_source = tmp_path / "alias" / missing.name / "activate.sh"
    user_script = tmp_path / "env.sh"
    user_script.write_text(f"source {shlex.quote(str(missing_source))}\nsource {tmp_path}/unrelated.sh\n")
    old = add(home, "default", user_script)
    personal = tmp_path / "activate.sh"
    personal.write_text("source /not/managed\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    args = ["--home", str(home.root), "env", "repair", "--json"]

    assert main([*args, "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["changed"] == 2 and preview["removed"] == 2 and preview["deferred"] == 1
    assert show(home, "default")["revision"] == old["revision"]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert {target["kind"] for target in report["targets"]} == {"named", "workspace"}
    assert personal.read_text() == "source /not/managed\n"
    assert activation.read_text() == WORKSPACE_HEADER

    repaired = show(home, "default", strict=True)
    code = ('set -e; python_before=$(python3 -c "$2"); source "$1"; '
            'test "$python_before" = "$(python3 -c "$2")"; echo "${FIXTURE_ROOT-unset}"')
    identity = "import sys; print(sys.executable, sys.prefix, sys.base_prefix)"
    env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"}
    command = ["bash", "-c", code, "test", repaired["activation"], identity]
    assert subprocess.check_output(command, env=env, text=True).strip() == "unset"
    missing.mkdir()
    (missing / "activate.sh").write_text("export FIXTURE_ROOT=reinstalled\n")
    assert subprocess.check_output(command, env=env, text=True).strip() == "reinstalled"
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 0
    assert show(home, "default")["revision"] == repaired["revision"]


def test_managed_repair_continues_after_a_broken_record(tmp_path, capsys, monkeypatch):
    home = ForgeHome(tmp_path / "forge")
    monkeypatch.setenv("HOME", str(tmp_path))
    script = tmp_path / "env.sh"
    script.write_text(f"source {tmp_path}/missing.sh\n")
    add(home, "good", script)
    add(home, "broken", script)
    (home.root / "environments/broken/current.json").write_text("broken\n")
    assert main(["--home", str(home.root), "env", "repair", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"] and result["removed"] == 1
    assert result["errors"][0]["name"] == "broken"


def test_managed_repair_refreshes_independent_build_export(tmp_path, capsys, monkeypatch):
    from jarvis_forge.cmake import write_environment

    home = ForgeHome(tmp_path / "forge")
    monkeypatch.setenv("HOME", str(tmp_path))
    prefix = home.root / "builds/installs/test/package"
    prefix.mkdir(parents=True)
    script = write_environment(home.root / "builds/work/package/test-activate.sh",
                               paths={"PATH": [tmp_path / "missing"]})
    record = home.root / "builds/artifacts/test-installation/manifest.json"
    write_json(record, {"artifact_id": "test-installation", "status": "ready", "prefix": str(prefix),
                        "activation": str(script), "evidence": {str(script): digest(script)}, "files": {}})
    original = {p: p.read_bytes() for p in (record, script)}
    args = ["--home", str(home.root)]
    assert main([*args, "env", "repair", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["changed"] == report["removed"] == 1
    assert report["targets"][0]["kind"] == "artifact"
    entry = Path(report["targets"][0]["activation"])
    assert entry == Path(report["targets"][0]["activation"])
    assert "export PATH=" not in entry.read_text()
    assert all(p.read_bytes() == contents for p, contents in original.items())


def test_repair_hook_registers_once_and_restores_managed_startup_block(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    rc = tmp_path / ".bashrc"
    login = tmp_path / ".profile"
    original = "export USER_SETTING=custom\n"
    rc.write_text(original)
    rc.chmod(0o640)
    login.write_text("export LOGIN_SETTING=custom\n")
    args = ["--home", str(tmp_path / "forge"), "env", "repair", "--json"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["targets"] == []
    assert main([*args, "--hook", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 2
    assert rc.read_text() == original
    assert main([*args, "--hook"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 2
    expected = rc.read_text() + "export AFTER_BLOCK=custom\n"
    rc.write_text(expected.replace("eval \"$_jforge_hook\"", ": # missing hook"))
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 1
    assert rc.read_text() == expected and rc.stat().st_mode & 0o777 == 0o640
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 0


def test_repair_leaves_pyenv_installer_entries_and_startup_blocks_alone(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    rc = tmp_path / ".bashrc"
    text = "# >>> jforge pyenv setup >>>\nexport PYENV_ROOT=/user/pyenv\n# <<< jforge pyenv setup <<<\n"
    rc.write_text(text)
    args = ["--home", str(tmp_path / "forge"), "env", "repair", "--json"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 0
    assert rc.read_text() == text
    assert not (tmp_path / ".jarvis-pyenv").exists()
