"""Forge/Spack boundary tests; no scientific packages or network are required."""

import json
from pathlib import Path
import sys
import subprocess

import pytest
import yaml

from jarvis_forge import spack_backend as b
from jarvis_forge.apt_profiles import active_home, switch
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeError, ForgeHome, read_json, write_json


@pytest.mark.parametrize(
    "requested,expected",
    [
        ("libgsl-dev", "gsl"),
        ("sqlite3=3.46", "sqlite@=3.46"),
        ("gfortran=15", "gcc@=15 languages=c,c++,fortran"),
        ("libeigen3-dev>=3.4", "eigen@3.4:"),
        ("gsl<=2.8", "gsl@:2.8"),
        ("root@6.36 +python ^python@3.13", "root@6.36 +python ^python@3.13"),
    ],
)
def test_spec_translation(requested, expected):
    assert b.normalize(requested) == expected


@pytest.mark.parametrize(
    "requested", ["", "--help", "-e /tmp", "gsl\n--help", "gsl\x00"]
)
def test_spec_options_are_not_commands(requested):
    with pytest.raises(ForgeError):
        b.normalize(requested)


def test_fresh_queries_and_dry_run_are_offline(tmp_path, capsys):
    path = tmp_path / "absent"
    for args in [
        ["apt", "list"],
        ["apt", "sources"],
        ["apt", "show", "gsl"],
        ["apt", "install", "sqlite3", "--dry-run"],
    ]:
        assert main(["--home", str(path), *args, "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["ok"]
    assert not path.exists()


def test_legacy_data_is_not_a_backend(tmp_path):
    from jarvis_forge.apt_profiles import active_home

    home = ForgeHome(tmp_path / "forge")
    write_json(
        home.root / "apt/state.json", {"schema_version": 1, "active": {}, "manual": []}
    )
    assert active_home(home).root == home.root / "apt/profiles/spack-default"
    with pytest.raises(ForgeError, match="archived"):
        b.current(home)


@pytest.fixture
def managed(tmp_path, monkeypatch):
    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    engine = b.root(home) / "engine/1.2.2"
    (engine / "bin").mkdir(parents=True)
    (engine / "bin/spack").write_text("# placeholder, execution is intercepted")
    write_json(
        home.root / "apt/spack.json",
        {
            "schema_version": 1,
            "backend": "spack",
            "engine": str(engine),
            "python": {
                "executable": sys.executable,
                "version": "3.13.13",
                "prefix": sys.prefix,
            },
            "repository_commit": b.REPO_COMMIT,
        },
    )
    config = b.root(home) / "config"
    config.mkdir()
    repo = b.root(home) / "repositories/previous/spack_repo/jforge"
    repo.mkdir(parents=True)
    (repo / "repo.yaml").write_text("repo: {namespace: jforge, api: v2.2}\n")
    write_json(home.root / "apt/recipe-release.json", {"repository": str(repo)})
    (config / "repos.yaml").write_text(yaml.safe_dump({"repos": {"jforge": str(repo)}}))
    (config / "mirrors.yaml").write_text("mirrors: {}\n")
    calls = []

    def run(home, args, op, label, **kw):
        calls.append((args, kw))
        if args[0] == "install" and (home.root / "fail").exists():
            raise ForgeError("fixture install failed", code=1)
        if args[0] == "python":
            env = kw["envdir"]
            roots = yaml.safe_load((env / "spack.yaml").read_text())["spack"]["specs"]
            names = {b.name_of(r) for r in roots}
            if "fixture-app" in names:
                names.add("fixture-lib")
            rows = []
            for name in sorted(names):
                prefix = b.root(home) / "store" / name
                installed = label == "installed" or prefix.is_dir()
                if label == "installed":
                    prefix.mkdir(parents=True, exist_ok=True)
                rows.append(
                    dict(
                        name=name,
                        version="1.0",
                        hash="hash-" + name,
                        prefix=str(prefix),
                        external=False,
                        installed=installed,
                        root=name in {b.name_of(r) for r in roots},
                        spec=name + "@1.0",
                        node={},
                    )
                )
            if args[0] == "python":
                return json.dumps({"packages": rows, "modifications": []})
        if args[0] == "list":
            return "fixture-app\nfixture-lib\n"
        return ""

    monkeypatch.setattr(b, "run", run)
    return home, calls


def test_transaction_dependency_snapshot_and_cache_policy(managed):
    home, calls = managed
    result = b.transact(home, "install", ["fixture-app"], cache="only")
    assert result["state_committed"]
    assert {p["name"] for p in result["packages"]} == {"fixture-app", "fixture-lib"}
    assert [p["name"] for p in result["packages"] if p["automatic"]] == ["fixture-lib"]
    argv = next(a for a, k in calls if a[0] == "install")
    assert argv[argv.index("--use-buildcache") + 1] == "only"
    assert "--verbose" in argv and "--show-log-on-error" in argv
    assert (Path(result["path"]) / "plan.json").is_file()
    assert Path(result["activation"]).is_file()
    assert read_json(home.root / "apt/state.json")["spack"]["roots"] == ["fixture-app"]


def named_layout(home):
    (b.root(home) / 'config/config.yaml').write_text(yaml.safe_dump({'config':{'install_tree':{
        'root':str(b.root(home) / 'store'), 'padded_length':False, 'projections':{'all':'{name}'}}}}))


def test_name_only_layout_blocks_changed_build_before_install(managed, monkeypatch):
    home, calls = managed
    named_layout(home)
    first = b.transact(home, 'install', ['fixture-app'], cache='never')
    old_state = (home.root / 'apt/state.json').read_bytes()
    installs = len([args for args, kwargs in calls if args[0]=='install'])
    run = b.run
    def changed(home, args, op, label, **kwargs):
        output = run(home,args,op,label,**kwargs)
        if label == 'plan':
            plan = json.loads(output)
            plan['packages'][0].update(hash='another-build', installed=False)
            return json.dumps(plan)
        return output
    monkeypatch.setattr(b,'run',changed)
    with pytest.raises(ForgeError, match='another build'):
        b.transact(home,'upgrade',cache='never')
    assert (home.root / 'apt/state.json').read_bytes() == old_state
    assert len([args for args, kwargs in calls if args[0]=='install']) == installs
    assert Path(first['activation']).is_file()


def test_name_only_layout_rejects_duplicate_package_builds(managed, monkeypatch):
    home, calls = managed
    named_layout(home)
    run = b.run
    def duplicate(home,args,op,label,**kwargs):
        output = run(home,args,op,label,**kwargs)
        if label=='plan':
            plan = json.loads(output)
            plan['packages'].append({**plan['packages'][0], 'hash':'different-variant'})
            return json.dumps(plan)
        return output
    monkeypatch.setattr(b,'run',duplicate)
    with pytest.raises(ForgeError,match='multiple instances'):
        b.transact(home,'install',['fixture-app'],cache='never')
    assert not any(args[0]=='install' for args,kwargs in calls)
    assert not (home.root / 'apt/state.json').exists()


def test_name_only_layout_allows_same_build_reuse(managed):
    home,calls = managed
    named_layout(home)
    first = b.transact(home,'install',['fixture-app'],cache='never')
    second = b.transact(home,'install',['fixture-app'],cache='never')
    assert first['packages'][0]['prefix'] == second['packages'][0]['prefix']
    assert second['state_committed']


@pytest.mark.parametrize("cache", ["auto", "only", "never"])
def test_installation_plan_uses_spack_installer_policy(managed, cache):
    home, calls = managed
    named_layout(home)
    result = b.transact(home, "install", ["fixture-app"], cache=cache)
    expected = "only" if cache == "auto" else cache
    plan_args = next(args for args, _ in calls if "installation-plan" in args)
    assert plan_args[-3:] == ["installation-plan", expected, "[]"]
    assert read_json(Path(result["path"]) / "plan.json")["cache_policy"] == expected
    assert (Path(result["path"]) / "dag.json").is_file()
    install_args = [args for args, _ in calls if args[0] == "install"]
    assert len(install_args) == 1
    assert install_args[0][install_args[0].index("--use-buildcache") + 1] == expected


def test_binary_install_keeps_full_dag_without_rejecting_unused_builds(managed, monkeypatch):
    home, calls = managed
    named_layout(home)
    run = b.run

    def historical(home, args, op, label, **kwargs):
        output = run(home, args, op, label, **kwargs)
        if label == "dag":
            data = json.loads(output)
            data["packages"].append({**data["packages"][0], "hash": "historical-build"})
            return json.dumps(data)
        return output

    monkeypatch.setattr(b, "run", historical)
    result = b.transact(home, "install", ["fixture-app"])
    directory = Path(result["path"])
    assert len(read_json(directory / "dag.json")["packages"]) == 3
    assert len(read_json(directory / "plan.json")["packages"]) == 2
    assert result["state_committed"]


@pytest.mark.parametrize("conflict", [None, "duplicate", "occupied"])
def test_auto_cache_failure_validates_source_plan_before_fallback(managed, monkeypatch, conflict):
    home, calls = managed
    named_layout(home)
    first = b.transact(home, "install", ["fixture-app"], cache="never")
    before = (home.root / "apt/state.json").read_bytes()
    calls.clear()
    run = b.run

    def cache_miss(home, args, op, label, **kwargs):
        output = run(home, args, op, label, **kwargs)
        if label == "install-cache":
            write_json(op / "command-install-cache.json", {"status": "failed", "exit_code": 1})
            raise ForgeError("fixture cache miss", code=1)
        if label == "source-plan":
            data = json.loads(output)
            if conflict == "duplicate":
                data["packages"].append({**data["packages"][0], "hash": "other-build"})
            elif conflict == "occupied":
                data["packages"][0].update(hash="other-build", installed=False)
            return json.dumps(data)
        return output

    monkeypatch.setattr(b, "run", cache_miss)
    if conflict:
        with pytest.raises(ForgeError, match="multiple instances|another build"):
            b.transact(home, "install", ["fixture-app"])
        assert (home.root / "apt/state.json").read_bytes() == before
        assert Path(first["activation"]).is_file()
    else:
        result = b.transact(home, "install", ["fixture-app"])
        assert result["state_committed"]
        assert (Path(result["path"]) / "source-plan.json").is_file()
    policies = [args[args.index("--use-buildcache") + 1]
                for args, _ in calls if args[0] == "install"]
    assert policies == (["only"] if conflict else ["only", "auto"])
    assert any("installation-plan" in args and args[-2] == "auto" for args, _ in calls)


@pytest.mark.parametrize("status,exit_code", [("timeout", -15), ("interrupted", -2), ("failed", None), ("failed", -15)])
def test_auto_does_not_retry_timeout_interruption_or_launch_failure(managed, monkeypatch, status, exit_code):
    home, calls = managed
    named_layout(home)
    run = b.run

    def fail(home, args, op, label, **kwargs):
        output = run(home, args, op, label, **kwargs)
        if label == "install-cache":
            write_json(op / "command-install-cache.json", {"status": status, "exit_code": exit_code})
            raise ForgeError("fixture stopped", code=1)
        return output

    monkeypatch.setattr(b, "run", fail)
    with pytest.raises(ForgeError, match="fixture stopped"):
        b.transact(home, "install", ["fixture-app"])
    assert len([args for args, _ in calls if args[0] == "install"]) == 1
    assert not (home.root / "apt/state.json").exists()


def test_reinstall_plan_marks_requested_overwrites(managed):
    home, calls = managed
    named_layout(home)
    b.transact(home, "install", ["fixture-app"], cache="never")
    calls.clear()
    b.transact(home, "reinstall", ["fixture-app"], cache="never")
    args = next(args for args, _ in calls if "installation-plan" in args)
    assert json.loads(args[-1]) == ["fixture-app"]


def test_cache_only_failure_does_not_fall_back_to_source(managed, monkeypatch):
    home, calls = managed
    named_layout(home)
    (home.root / "fail").touch()
    with pytest.raises(ForgeError, match="fixture install failed"):
        b.transact(home, "install", ["fixture-app"], cache="only")
    assert len([args for args, _ in calls if args[0] == "install"]) == 1
    assert not (home.root / "apt/state.json").exists()


def test_check_reports_conditional_source_fallback_without_installing(managed):
    home, calls = managed
    named_layout(home)
    result = b.transact(home, "check", ["fixture-app"])
    assert result["source_fallback_requires_validation"]
    assert "Binary-only" in result["note"]
    assert not any(args[0] == "install" for args, _ in calls)
    assert not (home.root / "apt/state.json").exists()
    empty = b.transact(home, "check")
    assert not empty["source_fallback_requires_validation"]
    assert "note" not in empty


def test_failure_keeps_active_state_and_archives_candidate(managed):
    home, calls = managed
    first = b.transact(home, "install", ["fixture-app"], cache="never")
    before = (home.root / "apt/state.json").read_bytes()
    (home.root / "fail").touch()
    with pytest.raises(ForgeError, match="fixture install failed"):
        b.transact(home, "install", ["other"], cache="never")
    assert (home.root / "apt/state.json").read_bytes() == before
    assert Path(first["activation"]).is_file()
    failed = [
        p
        for p in (home.root / "operations").glob("*/result.json")
        if read_json(p)["status"] == "failed"
    ]
    assert len(failed) == 1
    assert (failed[0].parent / "candidate-environment/spack.yaml").is_file()
    assert len(list((b.root(home) / "environments").iterdir())) == 1


def test_remove_retains_old_environment_and_store(managed):
    home, calls = managed
    first = b.transact(home, "install", ["fixture-app"], cache="never")
    result = b.transact(home, "remove", ["fixture-app"])
    assert result["packages"] == [] and result["store_retained"]
    assert Path(first["environment"]).is_dir()
    assert all(Path(p["prefix"]).is_dir() for p in first["packages"])
    assert len([a for a, k in calls if a[0] == "install"]) == 1


def test_check_does_not_install_publish_or_register_gc_root(managed):
    home, calls = managed
    result = b.transact(home, "check", ["fixture-app"])
    assert not result["builds_executed"] and not result["state_committed"]
    assert not (home.root / "apt/state.json").exists()
    assert not list((b.root(home) / "environments").iterdir())
    assert not any(a[0] == "install" for a, k in calls)
    assert Path(result["plan"]).is_file()


def test_upgrade_refreshes_solver_and_frozen_config_survives(managed):
    home, calls = managed
    first = b.transact(home, "install", ["fixture-app"], cache="never")
    original = (Path(first["environment"]) / ".jforge-config/repos.yaml").read_text()
    (b.root(home) / "config/repos.yaml").write_text("repos: {changed: somewhere}\n")
    result = b.transact(home, "upgrade", cache="never")
    assert any(a == ["concretize", "--fresh", "--force"] for a, k in calls)
    assert (
        Path(first["environment"]) / ".jforge-config/repos.yaml"
    ).read_text() == original
    assert result["state_committed"]


def test_gc_preserves_registered_environment_revisions(managed):
    home, calls = managed
    result = b.autoremove(home)
    assert result["historical_environments_retained"]
    assert calls[0][0] == [
        "gc",
        "--except-any-environment",
        "--keep-build-dependencies",
        "--yes-to-all",
    ]


def test_catalog_never_executes_spack(managed, monkeypatch):
    home, calls = managed
    write_json(b.root(home) / "catalog.json", {"packages": ["gsl", "root", "sqlite"]})
    monkeypatch.setattr(b, "run", lambda *a, **k: pytest.fail("list invoked Spack"))
    assert [r["name"] for r in b.catalog(home, query="sl")] == ["gsl"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"add": "bad", "url": "http://bad"},
        {"add": "spack-public-binaries", "url": "https://bad"},
        {"url": "https://example.com"},
        {"trust_key": "/absent/signing-key"},
    ],
)
def test_invalid_mirror_input_never_bootstraps(tmp_path, monkeypatch, kwargs):
    home = ForgeHome(tmp_path / "absent")
    monkeypatch.setattr(
        b, "bootstrap", lambda *a, **k: pytest.fail("invalid input bootstrapped")
    )
    with pytest.raises(ForgeError):
        b.sources(home, **kwargs)
    assert not home.root.exists()


def test_failed_switch_preserves_selection(tmp_path, monkeypatch):
    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    write_json(home.root / "apt/selection.json", {"schema_version": 1, "id": "legacy"})

    def fail(*a, **k):
        raise ForgeError("cannot bootstrap")

    monkeypatch.setattr(b, "bootstrap", fail)
    with pytest.raises(ForgeError):
        switch(home, "spack")
    assert read_json(home.root / "apt/selection.json")["id"] == "legacy"


def test_satisfy_json_command_name(managed, capsys):
    home, calls = managed
    assert (
        main(
            [
                "--home",
                str(home.root),
                "apt",
                "satisfy",
                "gsl>=2.8",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["command"] == "apt satisfy"


def test_changing_root_version_requests_native_reconcretization(managed):
    home, calls = managed
    b.transact(home, "install", ["fixture-app@1.0"], cache="never")
    b.transact(home, "install", ["fixture-app@2.0"], cache="never")
    assert any(a == ["concretize", "--force"] for a, k in calls)


def test_reinstall_overwrites_only_requested_specs(managed):
    home, calls = managed
    b.transact(home, "install", ["fixture-app", "other"], cache="never")
    b.transact(home, "reinstall", ["fixture-app"], cache="never")
    argv = [a for a, k in calls if a[0] == "install"][-1]
    assert argv[-3:] == ["--overwrite", "-y", "fixture-app"]


def test_empty_check_still_archives_a_plan(managed):
    home, calls = managed
    result = b.transact(home, "check")
    assert Path(result["plan"]).is_file()
    assert result["lockfile"] is None


def test_activation_cleans_inherited_spack_store_paths(managed, tmp_path):
    from jarvis_forge.shell_activation import activation

    home, calls = managed
    script = tmp_path / "activate.sh"
    script.write_text(activation(home, []))
    old = home.root / "spack/store/platform/old/bin"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'export PATH="$2:/usr/bin:/bin"; source "$1"; printf "%s" "$PATH"',
            "bash",
            str(script),
            str(old),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "/usr/bin:/bin"


def test_recipe_update_rolls_back_policy_and_catalog_on_native_failure(
    managed, monkeypatch
):
    from jarvis_forge import recipe_releases, downloads

    home, calls = managed
    write_json(b.root(home) / "catalog.json", {"packages": ["working"]})
    paths = [
        b.root(home) / "config/repos.yaml",
        b.root(home) / "catalog.json",
        home.root / "apt/recipe-release.json",
    ]
    before = {p: p.read_bytes() for p in paths}
    staged = b.root(home) / "repositories/staged/spack_repo/jforge"
    staged.mkdir(parents=True)
    (staged / "repo.yaml").write_text("repo: {namespace: jforge, api: v2.2}\n")
    monkeypatch.setattr(
        recipe_releases,
        "prepare",
        lambda *a: {"version": "next", "repository": str(staged)},
    )

    def fetch(url, target, logs):
        target.write_bytes(
            b'{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
            if "api.github.com" in url
            else b"archive"
        )

    monkeypatch.setattr(downloads, "fetch", fetch)
    monkeypatch.setattr(b, "_unpack", lambda *a: None)

    def refresh(*args):
        write_json(b.root(home) / "catalog.json", {"packages": ["broken"]})
        raise ForgeError("native recipe load failed")

    monkeypatch.setattr(b, "_refresh_catalog", refresh)
    with pytest.raises(ForgeError, match="native recipe load failed"):
        b.update(home)
    assert all(p.read_bytes() == content for p, content in before.items())


def test_update_refreshes_extension_recipes_without_rebuilding_packages(managed, monkeypatch):
    from jarvis_forge import recipe_releases, downloads

    home, calls = managed
    b.transact(home, "install", ["fixture-app"], cache="never")
    state_before = (home.root / "apt/state.json").read_bytes()
    staged = b.root(home) / "repositories/new-release/spack_repo/jforge"
    staged.mkdir(parents=True)
    (staged / "repo.yaml").write_text("repo: {namespace: jforge, api: v2.2}\n")
    refreshed = []

    def prepare(selected, operation):
        refreshed.append(selected.root)
        return {"version": "independent-r2", "repository": str(staged), "index_url": recipe_releases.INDEX_URL}

    def fetch(url, target, logs):
        target.write_bytes(b'{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}' if "api.github.com" in url else b"archive")

    monkeypatch.setattr(recipe_releases, "prepare", prepare)
    monkeypatch.setattr(downloads, "fetch", fetch)
    monkeypatch.setattr(b, "_unpack", lambda *args: None)
    monkeypatch.setattr(b, "_refresh_catalog", lambda *args: ["example", "neofetch"])
    result = b.update(home)
    assert refreshed == [home.root]
    assert result["recipe_release"] == "independent-r2"
    assert read_json(home.root / "apt/recipe-release.json")["index_url"] == recipe_releases.INDEX_URL
    assert yaml.safe_load((b.root(home) / "config/repos.yaml").read_text())["repos"]["jforge"] == str(staged)
    assert (home.root / "apt/state.json").read_bytes() == state_before
    assert len([args for args, kwargs in calls if args[0] == "install"]) == 1


def test_eigen_activation_exports_legacy_header_hints(managed):
    home, calls = managed
    include = b.root(home) / 'store/eigen/include/eigen3'
    (include / 'Eigen').mkdir(parents=True)
    (include / 'Eigen/Core').write_text('// fixture header\n')
    result = b.transact(home, 'install', ['eigen'], cache='never')
    snapshot = read_json(Path(result['activation']).parent / 'snapshot.json')
    hints = {m['name']:m.get('value') for m in snapshot['modifications']}
    assert hints['EIGEN3_INCLUDE_DIR'] == str(include)
    assert hints['EIGEN3_ROOT'] == str(include.parent.parent)
    text = Path(result['activation']).read_text()
    assert '_JFORGE_APT_HAD_EIGEN3_INCLUDE_DIR' in text
    assert '_JFORGE_APT_HAD_EIGEN3_ROOT' in text
