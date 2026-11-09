"""Exercise shell repair through the CLI and real Bash activation."""

import json
from pathlib import Path
import shlex
import subprocess

from jarvis_forge.cli import main


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

    assert main(["env", "repair", "--dry-run", "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["removed"] == 5
    assert entry.read_text() == wrapper and not list(tmp_path.glob(".activate.sh.*"))

    assert main(["env", "repair", "--json"]) == 0
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

    assert main(["env", "repair", "--json"]) == 0
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
