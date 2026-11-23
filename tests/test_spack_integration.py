"""Opt in with JFORGE_SPACK_INTEGRATION=1; downloads Spack and its solver only."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from jarvis_forge import spack_backend as b
from jarvis_forge.storage import ForgeError, ForgeHome, read_json, write_json
from jarvis_forge.apt_profiles import active_home, archived_packages, switch
from jarvis_forge.cli import main

pytestmark = pytest.mark.skipif(
    os.environ.get("JFORGE_SPACK_INTEGRATION") != "1",
    reason="real Spack acceptance is explicitly opt-in",
)


def test_real_installer_plans_and_guarded_cache_fallback(tmp_path):
    base = ForgeHome(tmp_path / "forge")
    base.initialize()
    switch(base, "spack")
    home = active_home(base)
    repo = Path(__file__).parent / "fixtures/spack_repo/forge_test"
    config = b.root(home) / "config/repos.yaml"
    repos = yaml.safe_load(config.read_text())["repos"]
    config.write_text(yaml.safe_dump({"repos": {"forge_test": str(repo.resolve()), **repos}}))

    # No indexed binary: the combined plan includes source build tools without
    # paying for a doomed binary-only install or a second planning process.
    result = b.transact(home, "install", ["forge-builder"], timeout=600)
    op = Path(result["path"])
    assert result["state_committed"]
    assert {p["name"] for p in read_json(op / "plan.json")["packages"]} == {"forge-builder", "forge-lib"}
    assert read_json(op / "plan.json")["cache_policy"] == "never"
    assert not (op / "source-plan.json").exists()
    assert result["spack_invocations"] == 3
    assert {p["name"] for p in result["packages"]} == {"forge-builder"}

    # The installed builder retains its build DAG in the lock, but its tool is
    # not required again. This must hold even with cache=never.
    result = b.transact(home, "check", cache="never", timeout=120)
    assert {p["name"] for p in result["packages"]} == {"forge-builder"}
    assert {p["name"] for p in read_json(Path(result["path"]) / "dag.json")["packages"]} == {
        "forge-builder", "forge-lib"}

    # A library that is a real runtime dependency of another root must stay.
    result = b.transact(home, "check", ["forge-app"], cache="only", timeout=120)
    assert {p["name"] for p in result["packages"]} == {"forge-builder", "forge-app", "forge-lib"}

    # Rebuilding an installed root makes its build dependencies required again.
    result = b.transact(home, "reinstall", ["forge-builder"], cache="never", timeout=120)
    assert {p["name"] for p in read_json(Path(result["path"]) / "plan.json")["packages"]} == {
        "forge-builder", "forge-lib"}


def test_real_dependency_install_activation_and_failure(tmp_path, capsys):
    base = ForgeHome(tmp_path / "forge")
    base.initialize()
    write_json(
        base.root / "packages.json", {"schema_version": 1, "active": {}, "manual": []}
    )
    write_json(
        base.root / "packages/old/1/previous/manifest.json",
        {
            "name": "old",
            "version": "1",
            "install_id": "previous",
            "prefix": str(base.root / "old-prefix"),
        },
    )
    old_state = (base.root / "packages.json").read_bytes()
    switch(base, "spack")
    home = active_home(base)
    assert home != base
    assert archived_packages(base)[0]["name"] == "old"
    assert (base.root / "packages.json").read_bytes() == old_state
    repo = Path(__file__).parent / "fixtures/spack_repo/forge_test"
    config = b.root(home) / "config/repos.yaml"
    repos = yaml.safe_load(config.read_text())["repos"]
    config.write_text(
        yaml.safe_dump({"repos": {"forge_test": str(repo.resolve()), **repos}})
    )
    first = b.transact(
        home, "install", ["forge-app@=1.0", "forge-lib"], cache="never", timeout=600
    )
    assert {p["name"] for p in first["packages"]} == {"forge-app", "forge-lib"}
    assert main(["--home", str(base.root), "apt", "env", "--path"]) == 0
    printed = capsys.readouterr().out.strip()
    assert first["activation"] in Path(printed).read_text()
    assert "revisions" not in printed
    for shell in ["bash", "zsh"]:
        if not shutil.which(shell):
            continue
        script = """export FORGE_FIXTURE=before
source "$1" || exit
forge-fixture || exit
[ "$FORGE_FIXTURE" != before ] || exit 10
source "$1" || exit
export PATH="/user/added:$PATH"
_jforge_apt_restore
[ "$FORGE_FIXTURE" = before ] || exit 11
case "$PATH" in /user/added:*) ;; *) exit 12 ;; esac
case "$PATH" in *forge-app*) exit 13 ;; esac
"""
        result = subprocess.run(
            [shell, "-c", script, shell, first["activation"]],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "fixture-ok\n"
    before = (home.root / "packages.json").read_bytes()
    check = b.transact(home, "check", timeout=120)
    assert not check["state_committed"] and not check["builds_executed"]
    assert Path(check["lockfile"]).is_file()
    with pytest.raises(ForgeError):
        b.transact(
            home,
            "install",
            ["no-such-jforge-fixture-package"],
            cache="never",
            timeout=120,
        )
    assert (home.root / "packages.json").read_bytes() == before
    with pytest.raises(ForgeError, match="forge-app already occupies"):
        b.transact(home, "install", ["forge-app@=2.0"], cache="never", timeout=120)
    assert (home.root / "packages.json").read_bytes() == before
    removed = b.transact(home, "remove", ["forge-app"], timeout=120)
    assert [p["name"] for p in removed["packages"]] == ["forge-lib"]
    assert removed["store_retained"]
    assert all(Path(p["prefix"]).exists() for p in first["packages"])


def test_real_incremental_build_tool_reuse(tmp_path):
    """Solver-only; supply engine, builtin repo and prebuilt clingo via env paths."""
    import json
    import sys
    names = ('JFORGE_SPACK_ENGINE', 'JFORGE_SPACK_BUILTIN_REPO', 'JFORGE_SPACK_SOLVER_PATH')
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.skip('solver fixture paths required: ' + ', '.join(missing))
    probe = Path(__file__).with_name('spack_reuse_probe.py')
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
    process = subprocess.run([sys.executable, str(probe), *(os.environ[n] for n in names),
                              str(tmp_path / 'solver')], env=environment, capture_output=True, text=True, timeout=240)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout.splitlines()[-1])
    assert result['status'] == 'passed' and result['historical'] != result['native']
