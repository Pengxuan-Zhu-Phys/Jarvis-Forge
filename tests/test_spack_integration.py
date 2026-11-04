"""Opt in with JFORGE_SPACK_INTEGRATION=1; downloads Spack and its solver only."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from jarvis_forge import spack_backend as b
from jarvis_forge.storage import ForgeError, ForgeHome, write_json
from jarvis_forge.apt_profiles import active_home, archived_packages, switch
from jarvis_forge.cli import main

pytestmark = pytest.mark.skipif(
    os.environ.get("JFORGE_SPACK_INTEGRATION") != "1",
    reason="real Spack acceptance is explicitly opt-in",
)


def test_real_dependency_install_activation_and_failure(tmp_path, capsys):
    base = ForgeHome(tmp_path / "forge")
    base.initialize()
    write_json(
        base.root / "apt/state.json", {"schema_version": 1, "active": {}, "manual": []}
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
    old_state = (base.root / "apt/state.json").read_bytes()
    switch(base, "spack")
    home = active_home(base)
    assert home != base
    assert archived_packages(base)[0]["name"] == "old"
    assert (base.root / "apt/state.json").read_bytes() == old_state
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
    assert capsys.readouterr().out.strip() == first["activation"]
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
    before = (home.root / "apt/state.json").read_bytes()
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
    assert (home.root / "apt/state.json").read_bytes() == before
    changed = b.transact(
        home, "install", ["forge-app@=2.0"], cache="never", timeout=120
    )
    assert (
        next(p for p in changed["packages"] if p["name"] == "forge-app")["version"]
        == "2.0"
    )
    removed = b.transact(home, "remove", ["forge-app"], timeout=120)
    assert [p["name"] for p in removed["packages"]] == ["forge-lib"]
    assert removed["store_retained"]
    assert all(Path(p["prefix"]).exists() for p in first["packages"])
