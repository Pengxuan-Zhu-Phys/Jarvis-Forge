"""Spack selection and passive legacy history, without a legacy backend."""

import json
from pathlib import Path
import shutil
import shlex
import subprocess

import pytest

from jarvis_forge import spack_backend as b
from jarvis_forge.apt_profiles import active_home, archived_packages, profiles, switch
from jarvis_forge.cli import main
from jarvis_forge.shell_activation import activation
from jarvis_forge.storage import ForgeError, ForgeHome, read_json, write_json


@pytest.fixture
def base(tmp_path, monkeypatch):
    base = ForgeHome(tmp_path / "user home")
    base.initialize()

    def bootstrap(home, python=None, progress=None):
        home.initialize()
        write_json(
            home.root / "apt/spack.json", {"schema_version": 1, "backend": "spack"}
        )
        file = home.root / "environments/apt/revisions/empty/activate.sh"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(activation(home, []))
        write_json(
            home.root / "apt/state.json",
            {
                "schema_version": 1,
                "backend": "spack",
                "active": {},
                "manual": [],
                "activation": str(file),
                "last_operation_id": "empty",
                "spack": {"roots": [], "packages": [], "environment": None},
            },
        )
        return {"backend": "spack", "state_committed": True}

    monkeypatch.setattr(b, "bootstrap", bootstrap)
    return base


def legacy(base, profile=None):
    home = base if profile is None else ForgeHome(base.root / "apt/profiles" / profile)
    home.initialize()
    file = home.root / "packages/libgsl-dev/2.7.1/old/manifest.json"
    write_json(
        file,
        {
            "name": "libgsl-dev",
            "version": "2.7.1",
            "install_id": "old",
            "prefix": str(home.root / "old-prefix"),
        },
    )
    write_json(
        home.root / "apt/state.json",
        {"schema_version": 1, "active": {"libgsl-dev": "old"}, "manual": ["old"]},
    )
    return home


def test_old_selection_routes_to_spack_without_modifying_history(base):
    old = legacy(base, "ubuntu-noble-main-universe")
    write_json(
        base.root / "apt/selection.json", {"schema_version": 1, "id": old.root.name}
    )
    before = {f: f.read_bytes() for f in base.root.rglob("*") if f.is_file()}
    assert active_home(base).root == base.root / "apt/profiles/spack-default"
    assert archived_packages(base)[0]["archived"]
    assert all(p.read_bytes() == content for p, content in before.items())
    switched = switch(base, "spack")
    assert switched["backend"] == "spack"
    assert active_home(base).root == base.root / "apt/profiles/spack-default"
    assert all(
        p.read_bytes() == content
        for p, content in before.items()
        if p.name != "selection.json"
    )
    rows = profiles(base, archived=True)
    assert {r["backend"] for r in rows} == {"spack", "archived"}
    assert next(r for r in rows if r["backend"] == "archived")["active"] is False


def test_two_spack_profiles_keep_separate_records_and_shared_home(base):
    first = switch(base, "spack", name="gambit")
    one = active_home(base)
    second = switch(base, "spack", name="root")
    two = active_home(base)
    assert (
        one != two
        and one.root.is_relative_to(base.root)
        and two.root.is_relative_to(base.root)
    )
    assert first["user_home"] == second["user_home"] == str(base.root)
    assert Path(first["activation"]).is_file() and Path(second["activation"]).is_file()
    assert read_json(two.root / "config.json")["apt_base_home"] == str(base.root)
    assert len(profiles(base)) == 2
    switch(base, "spack", name="gambit")
    assert active_home(base) == one


def test_failed_switch_keeps_previous_spack_selection(base, monkeypatch):
    switch(base, "spack", name="working")
    before = (base.root / "apt/selection.json").read_bytes()

    def fail(*args, **kwargs):
        raise ForgeError("backend unavailable", code=1)

    monkeypatch.setattr(b, "bootstrap", fail)
    with pytest.raises(ForgeError):
        switch(base, "spack", name="broken")
    assert (base.root / "apt/selection.json").read_bytes() == before
    assert active_home(base).root.name == "spack-working"


def test_archive_queries_never_execute_source_scripts(base, capsys):
    old = legacy(base)
    dangerous = old.root / "packages/libgsl-dev/2.7.1/old/environment.sh"
    dangerous.write_text("exit 77\n")
    before = dangerous.read_bytes()
    for args in [
        ["list", "--archived"],
        ["apt", "list", "--archived"],
        ["info", "libgsl-dev", "--archived"],
        ["apt", "switch", "--archived"],
    ]:
        assert main(["--home", str(base.root), *args, "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
    assert main(["--home", str(base.root), "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["packages"] == []
    assert (
        main(["--home", str(base.root), "info", "libgsl-dev", "--archived", "--path"])
        == 0
    )
    assert capsys.readouterr().out.strip() == str(old.root / "old-prefix")
    assert dangerous.read_bytes() == before
    assert not (base.root / "apt/profiles/spack-default").exists()


def test_switch_recovers_missing_or_corrupt_selection(base, capsys):
    (base.root / "apt/selection.json").write_text("{broken")
    assert main(["--home", str(base.root), "apt", "switch", "spack", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == "spack-default"


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ("apt search install", False),
        ("apt install toy --dry-run", False),
        ("apt switch --help", False),
        ("apt switch", False),
        ("apt switch spack --name gambit", True),
        ("apt import spack", True),
        ("apt install toy", True),
    ],
)
def test_shell_hook_only_refreshes_after_changes(tmp_path, capsys, arguments, expected):
    assert main(["--home", str(tmp_path / "home"), "apt", "shell-init"]) == 0
    hook = capsys.readouterr().out
    fake = tmp_path / "jforge"
    fake.write_text(
        '#!/bin/bash\nif [ "$1 $2 $3" = "apt env --path" ]; then printf "%s\\n" "$ACTIVATION"; fi\n'
    )
    fake.chmod(0o755)
    file = tmp_path / "activate.sh"
    file.write_text('printf "activated\\n"\n')
    result = subprocess.run(
        [
            "bash",
            "-c",
            'export PATH="$1:$PATH" ACTIVATION="$2"; eval "$3" >/dev/null; jforge '
            + arguments,
            "test",
            str(tmp_path),
            str(file),
            hook,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert ("activated" in result.stdout) == expected


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_spack_profile_activation_removes_old_managed_paths(base, tmp_path, shell):
    if not shutil.which(shell):
        pytest.skip("shell is not installed")
    paths = []
    for name in ["first", "second"]:
        home = ForgeHome(base.root / "apt/profiles" / ("spack-" + name))
        home.initialize()
        write_json(
            home.root / "config.json",
            {**read_json(home.root / "config.json"), "apt_base_home": str(base.root)},
        )
        prefix = home.root / "spack/store/toy/bin"
        prefix.mkdir(parents=True)
        env = tmp_path / (name + ".env")
        env.write_text(
            "export PATH="
            + shlex.quote(str(prefix))
            + ':"$PATH"\nexport FIXTURE_ROOT='
            + name
            + "\n"
        )
        out = tmp_path / (name + ".sh")
        out.write_text(activation(home, [env], extra_variables=["FIXTURE_ROOT"]))
        paths.append((out, str(prefix)))
    result = subprocess.run(
        [
            shell,
            "-c",
            'source "$1"; export PATH="/user/extra:$PATH"; source "$2"; source "$2"; printf "%s\\n%s" "$PATH" "$FIXTURE_ROOT"',
            shell,
            str(paths[0][0]),
            str(paths[1][0]),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert paths[0][1] not in result.stdout
    assert result.stdout.count(paths[1][1]) == 1 and "/user/extra" in result.stdout
    assert result.stdout.endswith("second")


def test_existing_root_spack_can_be_reselected(base):
    b.bootstrap(base)
    switch(base, "spack", name="other")
    assert active_home(base) != base
    result = switch(base, "spack", direct=True)
    assert active_home(base) == base and result["profile"] == "direct"


def test_direct_root_option_requires_existing_spack(tmp_path, capsys):
    home = tmp_path / "absent"
    assert (
        main(["--home", str(home), "apt", "switch", "spack", "--direct", "--json"]) == 2
    )
    assert not home.exists()
