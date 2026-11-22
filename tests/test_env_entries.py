"""Stable environment entries, ROOT attachment, and shell switching."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from jarvis_forge.apt_profiles import active_home
from jarvis_forge.cli import main
from jarvis_forge.env_entries import attach_build, attach_build_outputs, drop_build, publish_apt, write_selection
from jarvis_forge.environment_registry import add
from jarvis_forge.storage import ForgeHome, write_json


@pytest.fixture
def home(tmp_path):
    forge = ForgeHome(tmp_path / "forge")
    forge.initialize()
    return forge


def write_backend(profile, revision="op-1", text="export FIXTURE_PKG=1\n"):
    script = profile.root / "environments/apt/revisions" / revision / "activate.sh"
    script.parent.mkdir(parents=True)
    script.write_text(text)
    write_json(profile.root / "packages.json", {
        "backend": "spack", "spack": {"roots": [], "packages": []},
        "activation": str(script), "last_operation_id": revision,
    })
    return script


def test_profile_entry_is_stable_and_queries_do_not_write(home,linux_apt,capsys):
    from profile_fixtures import project_profile
    profile=project_profile(home,'ubuntu24');short=profile.root/'activate.sh'
    original=short.read_bytes();mtime=short.stat().st_mtime_ns
    assert main(['--home',str(home.root),'profile','show','ubuntu24','--path'])==0
    assert Path(capsys.readouterr().out.strip())==short
    assert short.read_bytes()==original and short.stat().st_mtime_ns==mtime


@pytest.mark.parametrize("command", ["install", "upgrade", "remove"])
def test_package_changes_publish_the_stable_entry_without_a_shell_hook(home, linux_apt, monkeypatch, capsys, command):
    from jarvis_forge import apt_cli, spack_backend

    from profile_fixtures import project_profile
    profile=project_profile(home)
    revisions = []

    def transact(target, action, specs, *, dry_run=False, **kwargs):
        if dry_run:
            return {"dry_run": True, "roots": list(specs)}
        backend = write_backend(target, f"op-{len(revisions) + 1}")
        revisions.append(backend)
        return {"status": "passed", "state_committed": True, "activation": str(backend),
                "packages": [], "roots": list(specs)}

    monkeypatch.setattr(spack_backend, "transact", transact)
    monkeypatch.setattr(apt_cli, "ensure_active", lambda *args, **kwargs: profile)
    args = [] if command == "full-upgrade" else ["fixture"]
    if command == "remove":
        args.append("--yes")
    common = ["--home", str(home.root), "apt", command, *args]
    short = profile.root / "activate.sh"
    assert main(common) == 0
    output = capsys.readouterr().out
    assert str(short) in output
    assert str(revisions[0]) not in output
    assert "source " in output
    assert short.is_file() and str(revisions[0]) in short.read_text()

    assert main([*common, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["activation"] == str(revisions[1])
    assert result["environment_activation"] == str(short)
    assert result["source_command"]=="source "+str(short)
    assert str(revisions[1]) in short.read_text()
    assert str(revisions[0]) not in short.read_text()
    assert revisions[0].is_file()


def test_profile_show_reports_metadata_without_backend_revision(home,linux_apt,capsys):
    from profile_fixtures import project_profile
    profile=project_profile(home,'ubuntu24')
    assert main(['--home',str(home.root),'profile','show','ubuntu24'])==0
    output=capsys.readouterr().out
    assert str(profile.root/'activate.sh') in output and '0 packages' in output


def test_entry_can_be_sourced_twice_and_then_restored(home):
    backend = write_backend(home, text="export PATH=/pkg:${PATH:-}\nexport FIXTURE_PKG=1\n")
    root = home.root / "root-activate.sh"
    root.write_text("export PATH=/root:${PATH:-}\nexport ROOTSYS=/opt/root\n")
    entry = Path(attach_build(
        home, home, build_id="root-build", output="ROOT", artifact_id="root-1", activation=root,
    )["activation"])
    assert entry == home.root / "env/apt/activate.sh"
    assert str(backend) in entry.read_text() and str(root) in entry.read_text()
    code = r'''
set -e
export PATH=/usr/bin
source "$1"
source "$1"
printf '%s %s %s\n' "$PATH" "$ROOTSYS" "$FIXTURE_PKG"
_jforge_env_restore
printf '%s %s %s\n' "$PATH" "${ROOTSYS-unset}" "${FIXTURE_PKG-unset}"
'''
    result = subprocess.run(["bash", "--noprofile", "--norc", "-c", code, "test", str(entry)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    loaded, restored = result.stdout.splitlines()
    path, rootsys, package = loaded.split(" ")
    assert path.split(":").count("/root") == 1
    assert path.split(":").count("/pkg") == 1
    assert path.endswith("/usr/bin")
    assert rootsys == "/opt/root" and package == "1"
    restored_path, restored_root, restored_package = restored.split(" ")
    assert restored_path == "/usr/bin"
    assert restored_root == "unset" and restored_package == "unset"


def test_root_is_attached_only_after_success(home, monkeypatch):
    write_backend(home)
    root = home.root / "root-activate.sh"
    root.write_text("export ROOTSYS=/opt/root\n")
    monkeypatch.setattr("jarvis_forge.apt_profiles.active_home", lambda base: home)
    output = {"build_id": "root-build", "output": "ROOT", "artifact_id": "root-1", "activation": str(root)}
    assert attach_build_outputs(home, {"status": "failed", "outputs": {"ROOT": output}}) is None
    assert not (home.root / "env/apt/activate.sh").exists()
    attached = attach_build_outputs(home, {"status": "passed", "outputs": {"ROOT": output}})
    text = Path(attached["activation"]).read_text()
    assert str(root) in text
    metadata = json.loads((home.root / "env/apt/env.json").read_text())
    assert metadata["description"] == "Active user-local packages + ROOT"
    assert metadata["builds"][0]["artifact_id"] == "root-1"


def _root_manifest(home, name, script, *, status="ready", prefix=None, mtime=None):
    directory = home.root / "builds/artifacts" / name
    write_json(directory / "manifest.json", {
        "artifact_id": name, "build_id": "root-build", "output": "installation",
        "status": status, "prefix": str(home.root if prefix is None else prefix),
        "activation": str(script),
    })
    if mtime is not None:
        os.utime(directory / "manifest.json", (mtime, mtime))


def test_installed_root_is_added_to_the_apt_entry(home):
    backend = write_backend(home)
    older = home.root / "older-activate.sh"
    newer = home.root / "newer-activate.sh"
    missing = home.root / "missing-activate.sh"
    failed = home.root / "failed-activate.sh"
    for script, text in ((older, "export ROOTSYS=/older\n"), (newer, "export ROOTSYS=/newer\n"),
                         (failed, "export ROOTSYS=/failed\n")):
        script.write_text(text)
    _root_manifest(home, "root-old", older, mtime=10)
    _root_manifest(home, "root-new", newer, mtime=20)
    _root_manifest(home, "root-failed", failed, status="failed", mtime=30)
    _root_manifest(home, "root-gone", missing, prefix=home.root / "gone", mtime=40)
    entry = publish_apt(home, home, backend)
    text = Path(entry["activation"]).read_text()
    assert str(newer) in text
    assert str(older) not in text and str(failed) not in text and str(missing) not in text
    metadata = json.loads((home.root / "env/apt/env.json").read_text())
    assert metadata["description"] == "Active user-local packages + ROOT"
    assert metadata["builds"] == [{
        "build_id": "root-build", "output": "installation", "artifact_id": "root-new", "activation": str(newer),
    }]
    kept = publish_apt(home, home, backend)
    assert kept["builds"] == metadata["builds"]


def test_drop_keeps_root_out_until_the_next_successful_build(home, linux_apt, capsys, monkeypatch):
    monkeypatch.delenv("_JFORGE_HOOK_LOADED", raising=False)
    profile = active_home(home)
    profile.initialize()
    backend = write_backend(profile)
    script = home.root / "root-activate.sh"
    script.write_text("export ROOTSYS=/opt/root\n")
    _root_manifest(home, "root-live", script, mtime=10)
    first = publish_apt(home, profile, backend)
    assert str(script) in Path(first["activation"]).read_text()
    assert main(["--home", str(home.root), "env", "drop", "root-build"]) == 0
    dropped = capsys.readouterr().out
    assert "Removed from the apt environment: root" in dropped
    assert "This shell is unchanged" in dropped
    text = (home.root / "env/apt/activate.sh").read_text()
    assert str(script) not in text and str(backend) in text
    again = publish_apt(home, profile, backend)
    assert again["builds"] == []
    assert "omitted_builds" in json.loads((home.root / "env/apt/env.json").read_text())
    assert main(["--home", str(home.root), "env", "drop", "missing-build"]) == 2
    restored = attach_build_outputs(home, {"status": "passed", "outputs": {"installation": {
        "build_id": "root-build", "output": "installation", "artifact_id": "root-live", "activation": str(script),
    }}})
    assert str(script) in Path(restored["activation"]).read_text()
    assert json.loads((home.root / "env/apt/env.json").read_text())["omitted_builds"] == []
    assert (home.root / "builds/artifacts/root-live/manifest.json").is_file()


def test_shell_hook_reloads_after_dropping_a_build(home, capsys):
    if not shutil.which("bash"):
        pytest.skip("bash is not installed")
    before = home.root / "with-root.sh"
    after = home.root / "packages.sh"
    before.write_text("export FIXTURE_ENV=with-root\n")
    after.write_text("export FIXTURE_ENV=packages\n")
    assert main(["--home", str(home.root), "shell-init"]) == 0
    hook = capsys.readouterr().out
    marker = home.root / "dropped"
    fake = home.root / "jforge"
    fake.write_text(
        "#!/bin/bash\n"
        'case "$1 $2${3:+ $3}" in\n'
        '  "env current --path")\n'
        '    if [ -f "$MARKER" ]; then printf "%s\\n" "$AFTER"; else printf "%s\\n" "$BEFORE"; fi ;;\n'
        '  "env drop root-build") touch "$MARKER"; printf "source\\0%s\\0" "$AFTER" >&9 ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    fake.chmod(0o700)
    code = r'''
export PATH="$1:$PATH" MARKER="$2" BEFORE="$3" AFTER="$4"
eval "$5"
jforge env drop root-build
printf '%s\n' "${FIXTURE_ENV-unset}"
'''
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-ic", code, "test", str(home.root), str(marker),
         str(before), str(after), hook],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "packages"
    assert "Could not refresh" not in result.stderr


def test_switch_and_detach_select_what_a_new_shell_loads(home, tmp_path, capsys):
    script = tmp_path / "cbs.sh"
    script.write_text("export FIXTURE_ENV=cbs\n")
    add(home, "cbs", script)
    assert main(["--home", str(home.root), "--json", "env", "switch", "cbs"]) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["selected"] == "cbs"
    assert main(["--home", str(home.root), "env", "current", "--path"]) == 0
    current = Path(capsys.readouterr().out.strip())
    assert current == Path(selected["path"])
    loaded = subprocess.run(["bash", "--noprofile", "--norc", "-c", 'source "$1"; printf "%s" "$FIXTURE_ENV"',
                             "test", str(current)], capture_output=True, text=True, check=True)
    assert loaded.stdout == "cbs"
    assert main(["--home", str(home.root), "--json", "env", "detach"]) == 0
    assert json.loads(capsys.readouterr().out)["detached"] is True
    assert main(["--home", str(home.root), "env", "current", "--path"]) == 0
    assert capsys.readouterr().out == ""
    assert (home.root / "env/cbs/env.json").is_file()
    assert main(["--home", str(home.root), "env", "switch", "missing"]) == 2


def test_shell_hook_switches_and_detaches_the_current_shell(home, capsys):
    if not shutil.which("bash"):
        pytest.skip("bash is not installed")
    script = write_backend(home, text="export FIXTURE_ENV=selected\n")
    entry = publish_apt(home, home, script)["activation"]
    assert main(["--home", str(home.root), "shell-init"]) == 0
    hook = capsys.readouterr().out
    fake = home.root / "jforge"
    fake.write_text(
        "#!/bin/bash\n"
        'case "$1 $2${3:+ $3}" in\n'
        '  "env current --path") printf "%s\\n" "$ACTIVATION" ;;\n'
        '  "env switch cbs") printf "source\\0%s\\0" "$ACTIVATION" >&9 ;;\n'
        '  "env detach") printf "detach\\0\\0" >&9 ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    fake.chmod(0o700)
    code = r'''
export PATH="$1:$PATH" ACTIVATION="$2"
eval "$3"
jforge env switch cbs
printf 'switch %s\n' "${FIXTURE_ENV-unset}"
jforge env detach
printf 'detach %s\n' "${FIXTURE_ENV-unset}"
'''
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-ic", code, "test", str(home.root), entry, hook],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-2:] == ["switch selected", "detach unset"]


def test_metadata_command_reads_the_file_without_rewriting_it(home, linux_apt, capsys, tmp_path):
    profile = active_home(home)
    profile.initialize()
    backend = write_backend(profile)
    script = home.root / "root-activate.sh"
    script.write_text("export ROOTSYS=/opt/root\n")
    entry = attach_build(home, profile, build_id="root-build", output="ROOT", artifact_id="root-1", activation=script)
    path = home.root / "env/apt/env.json"
    record = json.loads(path.read_text())
    record["description"] = "kept by metadata"
    path.write_text(json.dumps(record))
    before = path.read_bytes()
    assert main(["--home", str(home.root), "env", "metadata"]) == 0
    shown = capsys.readouterr().out
    assert "apt [apt]" in shown
    assert "kept by metadata" in shown
    assert "root · ROOT" in shown
    assert str(backend) in shown and str(script) in shown
    assert "Omitted (0):" in shown
    assert path.read_bytes() == before
    assert entry["activation"] in shown
    assert main(["--home", str(home.root), "env", "metadata", "apt", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "env metadata"
    assert payload["path"] == str(path)
    assert payload["metadata"]["builds"][0]["artifact_id"] == "root-1"
    assert payload["metadata"]["description"] == "kept by metadata"
    assert path.read_bytes() == before
    assert main(["--home", str(home.root), "env", "drop", "root-build"]) == 0
    capsys.readouterr()
    assert main(["--home", str(home.root), "env", "metadata", "apt"]) == 0
    omitted = capsys.readouterr().out
    assert "Builds (0):" in omitted and "Omitted (1):" in omitted and "root" in omitted
    shell = tmp_path / "hep.sh"
    shell.write_text("export HEP=1\n")
    add(home, "hep", shell, description="HEP stack")
    assert main(["--home", str(home.root), "env", "metadata", "hep"]) == 0
    named = capsys.readouterr().out
    assert "hep [shell]" in named and "HEP stack" in named
    assert "Omitted" not in named
    assert str(home.root / "env/hep/env.json") in named
    assert main(["--home", str(home.root), "env", "metadata", "missing"]) == 2
    assert "No metadata" in capsys.readouterr().err
    write_selection(home, None)
    assert main(["--home", str(home.root), "env", "metadata"]) == 2
    assert "No environment is selected" in capsys.readouterr().err
