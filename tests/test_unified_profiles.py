import json
import os
from pathlib import Path
from types import SimpleNamespace
import shlex
import shutil
import subprocess

import pytest
import yaml

from jarvis_forge import apt_profiles, builds, spack_backend, toolchains, unified_profiles
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeError, ForgeHome, read_json, write_json


LINUX = {"platform": "linux", "system": "almalinux", "version": "9.8",
         "machine": "x86_64", "libc": "glibc 2.34"}


def command(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + text + "\n")
    path.chmod(0o700)


@pytest.fixture
def fake_spack(tmp_path, monkeypatch, linux_apt):
    forge = ForgeHome(tmp_path / "forge")
    monkeypatch.setattr(unified_profiles, "host", lambda: LINUX)
    monkeypatch.setattr(toolchains, "_linux", lambda: None)
    selected_python = unified_profiles._python()
    monkeypatch.setattr(unified_profiles, "_prerequisites",
                        lambda base, preset, current, target, **kwargs: (target or "x86_64", selected_python))
    monkeypatch.setattr(apt_profiles, "sys", SimpleNamespace(platform="linux"))
    calls = []

    def switch(base, *, name, select, progress, **kwargs):
        assert not select
        profile = ForgeHome(base.root / "apt/profiles" / kwargs.get("instance", "spack-" + name))
        profile.initialize()
        engine = profile.root / "spack/engine"
        command(engine / "bin/spack", ":")
        inputs = profile.root / "apt/profile-input.json"
        if inputs.is_file():
            write_json(profile.root / "config.json", {"apt_base_home": str(base.root), "profile_store": True})
        write_json(profile.root / "apt/spack.json",
                   {"schema_version": 1, "backend": "spack", "engine": str(engine), "python": selected_python,
                    "install_tree": spack_backend._new_install_tree(profile)})
        activation = profile.root / "apt/activate.sh"
        activation.write_text("export CMAKE_PREFIX_PATH=/packages/" + name + "\n")
        write_json(profile.root / "apt/state.json", {"activation": str(activation), "last_operation_id": "test",
                                                    "spack": {"roots": [], "packages": []}})
        return {"apt_home": str(profile.root)}

    def transact(profile, action, specs, **kwargs):
        calls.append(specs)
        packages = []
        for spec in specs:
            name, version = spec.split()[0].split("@=")
            prefix = profile.root / "installed" / name
            write_json(prefix / ".spack/spec.json", {"spec": spec})
            binaries = toolchains.DRIVERS.values() if name == "gcc" else toolchains.TOOL_COMMANDS[name]
            for binary in binaries:
                command(prefix / "bin" / binary, f"printf '%s\\n' {shlex.quote(binary + ' ' + version)}")
            packages.append({"name": name, "version": version, "root": True, "external": False, "prefix": str(prefix)})
        return {"packages": packages, "path": str(profile.root)}

    def inspect(record):
        runtime = Path(record["prefix"]) / "lib64/libgcc_s.so.1"
        runtime.parent.mkdir(exist_ok=True)
        runtime.touch()
        return dict(os.environ), {runtime.name: runtime}

    monkeypatch.setattr(apt_profiles, "switch", switch)
    monkeypatch.setattr(spack_backend, "bootstrap", lambda *args, **kwargs: {})
    monkeypatch.setattr(spack_backend, "transact", transact)
    monkeypatch.setattr("jarvis_forge.package_operations.environment_path", lambda home: home.root / "apt/activate.sh")
    monkeypatch.setattr(toolchains, "_inspect", inspect)
    monkeypatch.setattr(toolchains, "check", lambda home, name: calls.append(name))
    return forge, calls


def test_profile_check_is_read_only(tmp_path, monkeypatch, capsys):
    path = tmp_path / "absent"
    monkeypatch.setattr(unified_profiles, "host", lambda: LINUX)
    monkeypatch.setattr(unified_profiles.shutil, "which", lambda name: None)
    args = ["--home", str(path), "apt", "profile"]
    assert main([*args, "check", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {row["name"]: row["status"] for row in data["profiles"]} == {
        "ubuntu-22.04": "blocked", "ubuntu-24.04": "blocked", "ubuntu-26.04": "blocked", "macos": "unsupported"}
    assert main([*args, "check", "macos", "--json"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["ok"] is False and error["profiles"][0]["status"] == "unsupported"
    assert not path.exists()


def test_profile_check_accepts_missing_patch_for_source_build(tmp_path, monkeypatch):
    home = ForgeHome(tmp_path / "absent")
    monkeypatch.setattr(unified_profiles, "host", lambda: LINUX)
    monkeypatch.setattr(unified_profiles, "shutil", SimpleNamespace(
        which=lambda name: None if name == "patch" else f"/usr/bin/{name}",
    ))
    monkeypatch.setattr(unified_profiles, "_python", lambda: {
        "prefix": "/opt/pyenv/versions/3.13.13", "base_prefix": "/opt/pyenv/versions/3.13.13",
    })
    profile, = unified_profiles.check(home, "ubuntu-24.04")["profiles"]
    assert profile["status"] == "buildable"
    assert not home.root.exists()


def test_profile_build_reuses_apt_and_default_is_host_scoped(fake_spack, monkeypatch, capsys):
    forge, calls = fake_spack
    args = ["--home", str(forge.root), "apt", "profile"]
    assert main([*args, "build", "ubuntu-24.04", "--json"]) == 0
    built = json.loads(capsys.readouterr().out)
    assert calls[0] == ["gcc@=13.3.0 languages=c,c++,fortran target=x86_64",
                        "cmake@=3.28.6 target=x86_64", "gmake@=4.3 target=x86_64",
                        "autoconf@=2.71 target=x86_64", "automake@=1.16.5 target=x86_64"]
    assert built["status"] == "built" and unified_profiles.selection(forge) is None
    assert built["toolchain"]["deprecated_versions"] == unified_profiles.presets()["ubuntu-24.04"]["versions"]
    profile = ForgeHome(Path(built["activation"]).parent)
    view = profile.root / "env/bin"
    assert {p.name for p in view.iterdir()} >= {"gcc", "g++", "gfortran", "cc", "c++", "cmake", "make", "automake"}
    assert not (view / "python3").exists()
    assert main([*args, "global", "ubuntu-24.04", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == built["id"]
    assert apt_profiles.active_home(forge) == profile
    assert main([*args, "build", "ubuntu-24.04", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "reused"
    assert len(calls) == 2
    monkeypatch.setattr(unified_profiles, "host", lambda: {**LINUX, "version": "10"})
    assert apt_profiles.active_home(forge) != profile
    assert main([*args, "global", "ubuntu-24.04", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_install_summary_keeps_activation_in_the_selected_profile(fake_spack, monkeypatch, capsys):
    forge, _ = fake_spack
    built = unified_profiles.build(forge, "ubuntu-24.04")
    unified_profiles.set_default(forge, built["id"])
    profile = apt_profiles.active_home(forge)
    backend = profile.root / "apt/activate.sh"

    def transact(home, action, specs, *, dry_run=False, **kwargs):
        assert home == profile
        return {"dry_run": dry_run, "state_committed": not dry_run,
                "activation": str(backend), "packages": [], "status": "passed"}

    monkeypatch.setattr(spack_backend, "transact", transact)
    assert main(["--home", str(forge.root), "apt", "install", "tree", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["environment_activation"] == str(profile.root / "activate.sh")
    assert result["activation"] == str(backend)
    assert not (forge.root / "env/apt/activate.sh").exists()


def test_apt_requires_healthy_selected_profile_before_operations(fake_spack, monkeypatch, capsys, tmp_path):
    forge, _ = fake_spack
    loader = tmp_path / "loader"
    command(loader, "printf '%s\n' '  x86-64-v3 (supported, searched)'")
    monkeypatch.setattr(unified_profiles, "X86_LOADERS", (str(loader),))
    selected = unified_profiles.build(forge, "ubuntu-24.04", target="x86_64_v3")
    unified_profiles.set_default(forge, selected["id"])
    profile = apt_profiles.active_home(forge)
    pointer = unified_profiles.selection_path(forge)
    before = pointer.read_bytes()
    args = ["--home", str(forge.root), "apt"]
    monkeypatch.setattr(spack_backend, "transact", lambda *a, **kw: pytest.fail("Backend called before validation"))

    python = tmp_path / "bin/python3"
    command(python, "printf '%s\n' '{}'")
    with monkeypatch.context() as patch:
        patch.setenv("PATH", f"{python.parent}:{os.environ['PATH']}")
        assert main([*args, "check", "tree", "--json"]) == 2
        assert "with pyenv" in json.loads(capsys.readouterr().out)["error"]["message"]

    with monkeypatch.context() as patch:
        patch.setattr(unified_profiles, "X86_LOADERS", ())
        assert main([*args, "install", "tree", "--json"]) == 2
        assert "could not be verified" in json.loads(capsys.readouterr().out)["error"]["message"]

    (profile.root / "apt/spack.json").unlink()
    assert main([*args, "update", "--json"]) == 2
    assert "backend is incomplete" in json.loads(capsys.readouterr().out)["error"]["message"]
    with pytest.raises(ForgeError, match="backend is incomplete"):
        apt_profiles.initialize_profile(forge)
    with pytest.raises(ForgeError, match="backend is incomplete"):
        apt_profiles.ensure_active(forge)
    assert pointer.read_bytes() == before
    assert not (forge.root / "apt/profiles/spack-default").exists()


def test_spack_profile_inventory_and_toolchain_belong_to_each_instance(fake_spack):
    forge, _ = fake_spack
    profiles = [unified_profiles.build(forge, preset) for preset in ("ubuntu-22.04", "ubuntu-24.04")]
    stores, compilers = [], []
    for data in profiles:
        profile = ForgeHome(Path(data["activation"]).parent)
        assert read_json(profile.root / "apt/profile.json")["id"] == data["id"]
        assert (profile.root / "env/apt/env.json").is_file()
        store = spack_backend.store_root(profile)
        assert store == profile.root / "pkg"
        stores.append(store)
        compiler = Path(data["toolchain"]["drivers"]["CC"])
        assert compiler.is_relative_to(profile.root)
        compilers.append(compiler)
    assert len(set(stores)) == len(set(compilers)) == 2



def test_failed_build_keeps_previous_global(fake_spack, monkeypatch):
    forge, _ = fake_spack
    built = unified_profiles.build(forge, "ubuntu-22.04")
    unified_profiles.set_default(forge, "ubuntu-22.04")

    def fail(*args, **kwargs):
        raise ForgeError("Probe failed.", code=1)

    monkeypatch.setattr(toolchains, "check", fail)
    with pytest.raises(ForgeError, match="Probe failed"):
        unified_profiles.build(forge, "ubuntu-26.04")
    assert unified_profiles.selection(forge)["id"] == built["id"]
    assert len(unified_profiles.catalog(forge)["instances"]) == 1


def test_activation_rechecks_cpu_on_shared_host(fake_spack, tmp_path, monkeypatch):
    forge, _ = fake_spack
    loader = tmp_path / "loader"
    monkeypatch.setattr(unified_profiles, "X86_LOADERS", (str(loader),))
    command(loader, "printf '%s\\n' '  x86-64-v3 (supported, searched)'")
    activation = unified_profiles.build(forge, "ubuntu-26.04", target="x86_64_v3")["activation"]
    argv = ["bash", "-c", 'source "$1"', "profile", activation]
    assert subprocess.run(argv, capture_output=True).returncode == 0
    command(loader, "printf '%s\\n' '  x86-64-v3'")
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode == 1 and "CPU target x86_64_v3 is unavailable" in result.stderr


def test_macos_profile_uses_audited_homebrew_backend(tmp_path, monkeypatch):
    from jarvis_forge import homebrew_backend

    forge = ForgeHome(tmp_path / "forge")
    current = {"platform": "darwin", "system": "darwin", "version": "27.2", "machine": "arm64", "libc": ""}
    monkeypatch.setattr(unified_profiles, "host", lambda: current)
    monkeypatch.setattr(apt_profiles, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(homebrew_backend, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(homebrew_backend, "find_brew", lambda: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(homebrew_backend, "discover", lambda: {"prefix": str(tmp_path), "brew": str(tmp_path / "brew")})
    monkeypatch.setattr("jarvis_forge.package_operations.environment_path", lambda home: home.root / "apt/activate.sh")
    calls = []

    def transact(profile, action, formulae, **kwargs):
        calls.append(formulae)
        packages = []
        for formula in formulae:
            prefix = tmp_path / "cellar" / formula / "1"
            key = "gmake" if formula == "make" else formula
            for binary in toolchains.TOOL_COMMANDS[key]:
                path = prefix / ("libexec/gnubin" if binary == "make" else "bin") / binary
                command(path, "echo 1")
            packages.append({"name": formula, "prefix": str(prefix), "version": "1"})
        script = profile.root / "apt/activate.sh"
        script.write_text("# fixture\n")
        write_json(profile.root / "apt/state.json", {"activation": str(script), "last_operation_id": "test", "homebrew": {"packages": []}})
        return {"packages": packages}

    monkeypatch.setattr(homebrew_backend, "transact", transact)
    assert unified_profiles.check(forge, "macos")["profiles"][0]["status"] == "buildable"
    profile = unified_profiles.build(forge, "macos")
    assert calls == [["cmake", "make", "autoconf", "automake"]]
    assert set(profile["toolchain"]["drivers"]) == {"CC", "CXX"}
    make = profile["toolchain"]["tools"]["gmake"]["commands"]["make"]
    assert Path(make).is_file()
    assert unified_profiles.set_default(forge, "macos")["backend"] == "homebrew"


def test_shell_and_project_outputs_remain_scoped(fake_spack, tmp_path, capsys):
    forge, _ = fake_spack
    from jarvis_forge import cmake, env_entries

    first = unified_profiles.build(forge, "ubuntu-22.04")
    second = unified_profiles.build(forge, "ubuntu-26.04")
    manifests = []
    for name in ("root-build", "gambit-build"):
        for data in (first, second):
            artifact_id = name + "-" + data["preset"]
            prefix = tmp_path / artifact_id
            (prefix / "bin").mkdir(parents=True)
            script = tmp_path / (artifact_id + ".sh")
            cmake.write_environment(script, paths={"PATH": [prefix / "bin"]}, profile_instance=data["id"])
            manifest = {"status": "ready", "artifact_id": artifact_id, "build_id": name, "output": "installation",
                        "prefix": str(prefix), "activation": str(script), "version": "1", "files": {},
                        "profile_instance": data["id"]}
            write_json(forge.root / "builds/artifacts" / artifact_id / "manifest.json", manifest)
            env_entries.attach_root_build(forge, {"status": "passed", "outputs": {name: manifest},
                                                 "profile_instance": data["id"],
                                                 "profile_path": str(Path(data["activation"]).parent)})
            manifests.append(manifest)
    unified_profiles.set_default(forge, first["preset"])
    args = ["--home", str(forge.root), "build"]
    assert main([*args, "list", "--json"]) == 0
    assert {row["artifact_id"] for row in json.loads(capsys.readouterr().out)["builds"]} == {
        "root-build-ubuntu-22.04", "gambit-build-ubuntu-22.04"}
    assert main([*args, "env", "root-build-ubuntu-26.04", "--json"]) == 2
    assert "different profile" in json.loads(capsys.readouterr().out)["error"]["message"]
    bash = '''
      before=$(python3 -I -c 'import sys; print(sys.executable,sys.prefix,sys.base_prefix)')
      . "$1" || exit 1
      case "$PATH" in *root-build-ubuntu-22.04*:*gambit-build-ubuntu-22.04*|*gambit-build-ubuntu-22.04*:*root-build-ubuntu-22.04*) ;; *) exit 2 ;; esac
      export PATH=/user/added:$PATH
      if . "$3"; then exit 3; fi
      . "$2" || exit 4
      case "$PATH" in *ubuntu-22.04*) exit 5 ;; esac
      case "$PATH" in */user/added*) ;; *) exit 6 ;; esac
      case "$PATH" in *root-build-ubuntu-26.04*) ;; *) exit 7 ;; esac
      after=$(python3 -I -c 'import sys; print(sys.executable,sys.prefix,sys.base_prefix)')
      test "$before" = "$after"
    '''
    for shell, options in (("bash", ["--noprofile", "--norc"]), ("zsh", ["-f"])):
        if executable := shutil.which(shell):
            result = subprocess.run([executable, *options, "-c", bash, "profile-probe", first["activation"],
                                     second["activation"], manifests[1]["activation"]], capture_output=True, text=True)
            assert result.returncode == 0, result
            assert result.stderr.strip() == f"This build requires profile {second['id']}."


def test_build_without_apt_step_records_selected_profile(fake_spack, tmp_path):
    forge, _ = fake_spack
    card = tmp_path / "probe.yaml"
    card.write_text(yaml.safe_dump({"schema_version": 1, "kind": "build", "id": "profile-probe", "steps": [
        {"id": "tools", "uses": "command", "with": {"argv": ["cmake", "--version"]}},
    ]}))
    instances = set()
    for preset, cmake_version in (("ubuntu-22.04", "3.22.6"), ("ubuntu-26.04", "4.2.3")):
        data = unified_profiles.build(forge, preset)
        unified_profiles.set_default(forge, preset)
        result = builds.execute(forge, card)
        assert result["status"] == "passed", result
        assert result["profile_instance"] == data["id"]
        output, = Path(result["attempt"]).glob("*/tools/stdout.log")
        assert cmake_version in output.read_text()
        instances.add(next(iter(result["nodes"].values()))["instance"])
    assert len(instances) == 2


def test_profile_resume_keeps_saved_tools(fake_spack, tmp_path, monkeypatch):
    forge, _ = fake_spack
    first = unified_profiles.build(forge, "ubuntu-22.04")
    second = unified_profiles.build(forge, "ubuntu-26.04")
    marker = tmp_path / "ready"
    card = tmp_path / "probe.yaml"
    card.write_text(yaml.safe_dump({"schema_version": 1, "kind": "build", "id": "profile-probe", "steps": [
        {"id": "tools", "uses": "command", "with": {"argv": ["sh", "-c", 'test -f "$1" && cmake --version',
                                                                "probe", str(marker)]}},
    ]}))
    unified_profiles.set_default(forge, first["preset"])
    failed = builds.execute(forge, card)
    assert failed["status"] == "failed"
    unified_profiles.set_default(forge, second["preset"])
    monkeypatch.setenv("JFORGE_PROFILE_INSTANCE", first["id"])
    stale = builds.execute(forge, card)
    assert stale["status"] == "failed" and "differs from the build profile" in stale["error"]
    monkeypatch.setenv("JFORGE_PROFILE_INSTANCE", second["id"])
    marker.touch()
    resumed = builds.execute(forge, resume=failed["run_id"])
    assert resumed["status"] == "passed", resumed
    assert resumed["profile_instance"] == first["id"]
    output, = Path(resumed["attempt"]).glob("*/tools/stdout.log")
    assert "3.22.6" in output.read_text()


def test_profile_output_switch_and_build_conflict(fake_spack, tmp_path, monkeypatch, capsys):
    from jarvis_forge import cmake, env_entries

    forge, _ = fake_spack
    active = unified_profiles.build(forge, "ubuntu-24.04")
    profile = ForgeHome(Path(active["activation"]).parent)
    unified_profiles.set_default(forge, active["id"])
    prefix = tmp_path / "rivet-install"
    (prefix / "bin").mkdir(parents=True)
    external = prefix / "rivetenv.sh"
    external.write_text('name=RIVET_DATA_PATH\nexport "$name=/rivet/data"\n')
    script = tmp_path / "rivet-activate.sh"
    cmake.write_environment(script, scripts=[external], paths={"PATH": [prefix / "bin"]},
                            profile_instance=active["id"], build_id="rivet-build")
    item = {"artifact_id": "rivet-release", "build_id": "rivet-build", "output": "installation", "version": "1",
            "profile_instance": active["id"], "status": "ready", "prefix": str(prefix),
            "activation": str(script), "files": {}}
    write_json(forge.root / "builds/artifacts/rivet-release/manifest.json", item)
    root = tmp_path / "root-install"
    (root / "bin").mkdir(parents=True)
    root_script = tmp_path / "root-activate.sh"
    cmake.write_environment(root_script, paths={"PATH": [root / "bin"]},
                            profile_instance=active["id"], build_id="root-build")
    write_json(forge.root / "builds/artifacts/root-release/manifest.json",
               {**item, "artifact_id": "root-release", "build_id": "root-build", "prefix": str(root),
                "activation": str(root_script)})
    args = ["--home", str(forge.root), "apt", "profile", "apps"]

    def rivet_enabled():
        return next(row["enabled"] for row in json.loads(capsys.readouterr().out)["outputs"]
                    if row["build_id"] == "rivet-build")

    assert main([*args, "--json"]) == 0
    assert not rivet_enabled()
    assert main([*args, "rivet-release", "--on", "--json"]) == 0
    assert rivet_enabled()
    enabled_script = tmp_path / "profile-on.sh"
    enabled_script.write_text(Path(active["activation"]).read_text())

    card = tmp_path / "gambit-probe.yaml"
    card.write_text(yaml.safe_dump({"schema_version": 1, "kind": "build", "id": "gambit-probe",
                                   "environment": {"outputs_off": ["rivet-build"]}, "steps": [
        {"id": "probe", "uses": "command", "with": {"argv": ["sh", "-c", 'test -z "$RIVET_DATA_PATH" && '
            'case "$PATH" in *rivet-install*) exit 1 ;; *root-install*) exit 0 ;; *) exit 2 ;; esac']}},
    ]}))
    with monkeypatch.context() as patch:
        shell = cmake.capture(enabled_script)
        for key in ("PATH", "RIVET_DATA_PATH", "JFORGE_PROFILE_INSTANCE", "JFORGE_ACTIVE_OUTPUTS", "_JFORGE_PROFILE_BUILD_ENV"):
            patch.setenv(key, shell[key])
        built = builds.execute(forge, card)
        assert built["status"] == "passed", built
        assert "rivet-install" in os.environ["PATH"] and os.environ["RIVET_DATA_PATH"] == "/rivet/data"
        assert "Temporarily disabled for this build: rivet" in (Path(built["attempt"]) / "build.log").read_text()
    assert main([*args, "--json"]) == 0
    assert rivet_enabled()
    assert main([*args, "rivet", "--off", "--json"]) == 0
    assert not rivet_enabled()
    assert script.is_file() and prefix.is_dir()
    assert env_entries.attach_build(forge, profile, build_id="rivet-build", output="installation",
                                    artifact_id="rivet-release", activation=script) is None
    env_entries.publish_apt(forge, profile, profile.root / "apt/activate.sh")
    assert builds.execute(forge, card)["status"] == "passed"
    with monkeypatch.context() as patch:
        patch.setenv("JFORGE_ACTIVE_OUTPUTS", "rivet-build")
        assert builds.execute(forge, card)["status"] == "failed"

    probe = '''
      source "$1" || exit 1
      [ "$RIVET_DATA_PATH" = /rivet/data ] || exit 2
      export PATH=/user/added:$PATH
      source "$2" || exit 3
      [ -z "${RIVET_DATA_PATH+x}" ] || exit 4
      case "$PATH" in *rivet-install*|*:/user/added) exit 5 ;; *:/user/added:*|/user/added:*) ;; *) exit 6 ;; esac
    '''
    for shell in ("bash", "zsh"):
        if executable := shutil.which(shell):
            result = subprocess.run([executable, "-c", probe, "profile", enabled_script, active["activation"]],
                                    capture_output=True, text=True)
            assert result.returncode == 0, result


    new_prefix = tmp_path / "rivet-later"
    new_prefix.mkdir()
    later = {**item, "artifact_id": "rivet-later", "version": "2", "prefix": str(new_prefix)}
    write_json(forge.root / "builds/artifacts/rivet-later/manifest.json", later)
    assert main([*args, "rivet", "--on", "--json"]) == 0
    assert [row["artifact_id"] for row in json.loads(capsys.readouterr().out)["outputs"]
            if row["build_id"] == "rivet-build" and row["enabled"]] == ["rivet-release"]
    assert env_entries.attach_build(forge, profile, build_id="rivet-build", output="installation",
                                    artifact_id="rivet-later", activation=script) is None
    assert main([*args, "rivet-later", "--off", "--json"]) == 0
    assert [row["artifact_id"] for row in json.loads(capsys.readouterr().out)["outputs"]
            if row["build_id"] == "rivet-build" and row["enabled"]] == ["rivet-release"]
    consumer = {**item, "build_id": "consumer-build", "artifact_id": "consumer-release", "dependencies": ["rivet-later"]}
    write_json(forge.root / "builds/artifacts/consumer-release/manifest.json", consumer)
    with pytest.raises(ForgeError, match="two versions of rivet"):
        env_entries.profile_outputs(forge, profile, "consumer-build", enabled=True)
    env_entries.profile_outputs(forge, profile, "rivet-later", enabled=True)
    env_entries.profile_outputs(forge, profile, "consumer-build", enabled=True)
    with pytest.raises(ForgeError, match="consumer still loads rivet"):
        env_entries.drop_build(forge, profile, "rivet-build")


def test_app_switch_refreshes_only_the_profile_loaded_in_this_shell(fake_spack, tmp_path, capsys):
    from jarvis_forge import env_entries
    from jarvis_forge.environment_registry import add

    forge, _ = fake_spack
    app = unified_profiles.build(forge, "ubuntu-24.04")
    profile = ForgeHome(Path(app["activation"]).parent)
    application = tmp_path / "app.sh"
    application.write_text("export FIXTURE_APP=on\n")
    env_entries.attach_build(forge, profile, build_id="my-app", output="installation",
                             artifact_id="my-app-v1", activation=application)
    custom = tmp_path / "custom.sh"
    custom.write_text("export FIXTURE_APP=custom\n")
    named = add(forge, "custom", custom)
    entry = Path(app["activation"])
    before = entry.read_text()
    backend = profile.root / "apt/activate.sh"
    after = env_entries._activation_text("apt", [backend, profile.root / "env/toolchain.sh"],
                                         managed=unified_profiles.record(profile), activation=entry)
    replacement = tmp_path / "replacement.sh"
    replacement.write_text(after)
    assert main(["apt", "shell-init"]) == 0
    hook = capsys.readouterr().out
    command(tmp_path / "jforge", r'''case "$*" in
  'env current --path') printf '%s\n' "${SELECTED-}" ;;
  'apt profile apps my-app --off')
    cp "$REPLACEMENT" "$PROFILE"
    if [ "${_JFORGE_SHELL_ENTRY-}" = "$PROFILE" ]; then printf 'source\0%s\0' "$PROFILE" >&9; fi ;;

  'apt profile apps') : ;;
  'env detach') printf 'detach\0\0' >&9 ;;
  *) exit 2 ;;
esac''')
    probe = r'''
export PATH="$1:$PATH" PROFILE="$2" SELECTED="$2" REPLACEMENT="$3"
python_identity() { python3 -c 'import sys; print(sys.executable, sys.prefix, sys.base_prefix)'; }
before_python=$(python_identity) || exit 9
eval "$4"
[ "$FIXTURE_APP" = on ] || exit 11
jforge apt profile apps || exit 12
[ "$FIXTURE_APP" = on ] || exit 13
jforge apt profile apps my-app --off || exit 14
[ -z "${FIXTURE_APP+x}" ] || exit 15
source "$5" || exit 16
export SELECTED="$5"
jforge apt profile apps my-app --off || exit 17
[ "$FIXTURE_APP" = custom ] || exit 18
jforge env detach || exit 19
export SELECTED=''
jforge apt profile apps my-app --off || exit 20
[ -z "${FIXTURE_APP+x}" ] && [ -z "${JFORGE_PROFILE_INSTANCE+x}" ] || exit 21
[ "$before_python" = "$(python_identity)" ] || exit 22
'''
    for shell in ("bash", "zsh"):
        if executable := shutil.which(shell):
            entry.write_text(before)
            options = ["--noprofile", "--norc"] if shell == "bash" else ["-f"]
            result = subprocess.run([executable, *options, "-ic", probe, "test", tmp_path, entry, replacement, hook,
                                     named["entry"]], capture_output=True, text=True)
            assert result.returncode == 0, result


def test_workspace_activate_selects_output_for_current_profile(tmp_path, monkeypatch):
    from jarvis_forge import build_workspace, cmake, managed_envs

    home = ForgeHome(tmp_path / "forge")
    write_json(tmp_path / build_workspace.MARKER, {"home": str(home.root), "schema_version": 1, "build": "build.yaml"})
    write_json(home.root / "builds/workspaces/fixture/workspace.json", {"workspace": str(tmp_path)})
    for instance in ("ubuntu-22.04", "ubuntu-26.04"):
        script = tmp_path / (instance + ".sh")
        cmake.write_environment(script, variables={"ROOTSYS": instance}, profile_instance=instance)
        build_workspace.publish(tmp_path, {"run_id": instance, "profile_instance": instance, "outputs": {
            "installation": {"artifact_id": instance, "prefix": str(tmp_path / instance), "activation": str(script)},
        }})
    check = '''
      export JFORGE_PROFILE_INSTANCE=ubuntu-22.04
      source "$1" && test "$ROOTSYS" = ubuntu-22.04 || exit 1
      export JFORGE_PROFILE_INSTANCE=ubuntu-26.04
      source "$1" && test "$ROOTSYS" = ubuntu-26.04 || exit 2
      export JFORGE_PROFILE_INSTANCE=ubuntu-24.04
      if source "$1"; then exit 3; fi
    '''
    result = subprocess.run(["bash", "-c", check, "workspace", tmp_path / "activate.sh"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result
    monkeypatch.setattr(managed_envs, "startup_files", lambda: [])
    monkeypatch.setattr(managed_envs, "repair_bootstrap", lambda *args, **kwargs: None)
    (tmp_path / "ubuntu-22.04.sh").unlink()
    report = managed_envs.repair_managed(home, dry_run=True)
    changed = [Path(row["activation"]) for row in report["targets"] if row["kind"] == "workspace" and row["changed"]]
    assert changed == [tmp_path / ".jforge/profiles/ubuntu-22.04/activate.sh"]
