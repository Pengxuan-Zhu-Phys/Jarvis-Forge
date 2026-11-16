"""Exercise the toolchain registry and its profile and build integration."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import yaml

from jarvis_forge import apt_profiles, builds, spack_backend, toolchains
from jarvis_forge.storage import ForgeHome, read_json, write_json


def test_install_bind_and_apply_toolchain(tmp_path, monkeypatch, linux_apt):
    monkeypatch.setattr(toolchains, "sys", SimpleNamespace(platform="linux"))
    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    project = home.root / "apt/profiles/spack-project"
    ForgeHome(project).initialize()
    write_json(project / "apt/spack.json", {"schema_version": 1, "backend": "spack"})
    write_json(project / "apt/state.json", {"spack": {"roots": [], "packages": []}})
    pointer = home.root / "apt/selection.json"
    write_json(pointer, {"schema_version": 1, "platform": "linux", "id": "spack-project"})
    selection = pointer.read_bytes()
    prefix = home.root / "apps/gcc"
    (prefix / "bin").mkdir(parents=True)
    write_json(prefix / ".spack/spec.json", {"spec": "fake GCC"})
    for binary in toolchains.DRIVERS.values():
        path = prefix / "bin" / binary
        path.write_text("#!/bin/sh\necho 14.3.0\n")
        path.chmod(0o700)
    calls = []

    def switch(base, *, name, select, extensions, toolchain_store, progress):
        assert base == home and not select and not extensions and toolchain_store
        return {"apt_home": str(home.root / "apt/profiles" / ("spack-" + name))}

    def install(profile, action, specs, **kwargs):
        calls.append(specs)
        return {"packages": [{"name": "gcc", "version": "14.3.0", "prefix": str(prefix),
                              "root": True, "external": False}], "path": str(profile.root)}

    monkeypatch.setattr(apt_profiles, "switch", switch)
    monkeypatch.setattr(spack_backend, "transact", install)
    monkeypatch.setattr(toolchains, "shutil", SimpleNamespace(which=lambda program: "/usr/bin/" + program))
    runtime = prefix / "lib64/libgcc_s.so.1"
    runtime.parent.mkdir()
    runtime.touch()
    monkeypatch.setattr(toolchains, "_inspect", lambda record: ({}, {runtime.name: runtime}))
    assert toolchains.install(home, "gcc@14.3.0", target="x86_64_v3")["status"] == "installed"
    name = "gcc-14.3.0-x86_64_v3"
    assert toolchains.install(home, "gcc@14.3.0", target="x86_64_v3")["status"] == "reused"
    assert calls == [["gcc@=14.3.0 languages=c,c++,fortran target=x86_64_v3"]]
    assert pointer.read_bytes() == selection

    toolchains.use(home, name)
    record = toolchains.binding(ForgeHome(project))
    packages = {"python": {"buildable": False}}
    toolchains.spack_requirements(record, packages)
    assert packages["gcc"]["externals"][0]["prefix"] == str(prefix)
    assert packages["c"]["require"] == packages["cxx"]["require"] == packages["fortran"]["require"] == ["gcc@=14.3.0"]
    assert packages["python"] == {"buildable": False}
    assert packages["all"]["target"] == ["x86_64_v3"]
    cmake_cache = {"CMAKE_INSTALL_RPATH": {"type": "STRING", "value": "/project/lib"}}
    toolchains.cmake_runtime_paths(record, cmake_cache)
    assert cmake_cache["CMAKE_BUILD_RPATH"]["value"] == str(runtime.parent)
    assert cmake_cache["CMAKE_INSTALL_RPATH"]["value"] == f"{runtime.parent};/project/lib"

    card = tmp_path / "compile.yaml"
    card.write_text(yaml.safe_dump({"schema_version": 1, "kind": "build", "id": "toolchain-probe", "steps": [
        {"id": "check", "uses": "command", "with": {"argv": [sys.executable, "-c",
         "import json, os; print(json.dumps({k: os.environ[k] for k in ('CC','CXX','FC')}))"]}}
    ]}))
    result = builds.execute(home, card)
    assert result["status"] == "passed", result
    assert result["toolchain"]["id"] == name
    stdout, = Path(result["attempt"]).glob("*/check/stdout.log")
    assert json.loads(stdout.read_text()) == record["drivers"]
    assert read_json(pointer)["id"] == "spack-project"
