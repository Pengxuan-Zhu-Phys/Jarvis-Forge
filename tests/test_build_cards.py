"""Cards and acceptance helpers added for in-place CBS and MG5+Pythia8."""

import importlib.util
from pathlib import Path
import json


import pytest

from jarvis_forge import builds, cmake, build_sources
from jarvis_forge.storage import ForgeHome


@pytest.fixture
def home(tmp_path):
    value = ForgeHome(tmp_path / "home")
    value.initialize()
    return value


ROOT = Path(__file__).parents[1]


def _load(name, relative):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cbs_build_card_is_in_place_and_uses_the_preset_directory():
    loaded = builds.load(ROOT / "builds/cbs.yaml")
    step = next(item for item in loaded["config"]["steps"] if item["uses"] == "cmake.project")
    assert step["with"]["in_place"] is True
    assert step["with"]["binary_dir"] == "preset"
    assert step["with"]["lock"] == "source"
    assert step["with"]["configure_preset"] == "cbs-gnu"
    assert step["with"]["phases"] == ["configure", "build"]
    assert step["with"]["required_files"] == ["CBS"]
    assert builds.host_requirements(loaded["config"]) == {
        "platforms": ["linux"], "apt_backend": "spack"}
    assert loaded["config"]["requires"]["root"]["build"] == "root"
    assert loaded["config"]["environment"]["outputs_off"] == ["rivet"]


def test_cbs_plan_does_not_pass_a_private_binary_dir(home):
    planned = builds.plan(home, str(ROOT / "builds/cbs.yaml"))
    commands = next(row["argv"] for row in planned["cmake_commands"] if row["build"] == "cbs")
    configure = commands["configure"]
    assert "-B" not in configure
    assert "cbs-gnu" in configure
    assert commands["build"][1:4] == ["--build", "--preset", "cbs-gnu"]


def test_mg5_card_requires_the_pythia8_variant():
    loaded = builds.load(ROOT / "builds/mg5-pythia8.yaml")
    specs = next(item["with"]["specs"] for item in loaded["config"]["steps"] if item["uses"] == "apt.install")
    assert "jforge-mg5amc@3.6.3 +pythia8" in specs
    assert builds.host_requirements(loaded["config"])["platforms"] == ["linux"]


def test_cbs_build_acceptance_requires_registered_analyses():
    module = _load("cbs_acceptance", "builds/scripts/cbs_acceptance.py")
    assert module.listed_count("CBS analyses: 2 (from /src)\n\nAnalysis ID\n") == 2
    assert not module.listed_count("No ColliderBit analyses pass the validation policy.")
    assert not module.listed_count("CBS analyses: 0 (from /src)\n")


def test_mg5_acceptance_rejects_an_update_enabled_configuration(tmp_path):
    module = _load("mg5_acceptance", "builds/scripts/mg5_pythia8_acceptance.py")
    text = "\n".join([
        "auto_update = 0",
        "automatic_html_opening = False",
        "lhapdf_py3 = /opt/lhapdf-config",
        "pythia8_path = /opt/pythia8",
        "hepmc_path = /opt/hepmc3",
        "mg5amc_py8_interface_path = /opt/interface",
    ])
    assert module.configuration_errors(text) == []
    assert any("auto_update" in item for item in module.configuration_errors(text.replace("auto_update = 0", "auto_update = 7")))
    proc, launch = module.write_smoke(tmp_path)
    assert "p p > t t~" in proc.read_text()
    assert "shower=Pythia8" in launch.read_text()
    assert "set nevents 100" in launch.read_text()


@pytest.mark.parametrize("app,program", [("gambit", "gambit"), ("cbs", "CBS")])
def test_gambit_and_cbs_automatically_share_profile_sources(home, tmp_path, app, program):
    scope = ForgeHome(home.root / "profiles/ubuntu24")
    graph = builds.graph(home, ROOT / "builds" / (app + ".yaml"))
    node = graph["nodes"][graph["root"]]
    spec = next(iter(node["config"]["sources"].values()))
    source = build_sources.location(scope, spec)
    assert source == scope.root / "sources/gambit-2.7.0"
    assert not source.exists()
    other = builds.load(ROOT / "builds" / ("cbs.yaml" if app == "gambit" else "gambit.yaml"))
    assert spec == next(iter(other["config"]["sources"].values()))
    assert "source" not in node["parameters"]
    step = next(s for s in node["ordered"] if s["uses"] == "cmake.project")
    options = step["with"]
    assert options["in_place"] and options["lock"] == "source"
    assert options["configure_preset"] == options["build_preset"] == app + "-gnu"
    assert options["required_files"] == [program]
    argv = cmake.commands(options, source, source / "build", source)
    assert "-B" not in argv["configure"] and "--fresh" not in argv["configure"]
    assert argv["build"][1:4] == ["--build", "--preset", app + "-gnu"]
    assert not any(str(source / "build") == arg for arg in argv["build"])
    assert graph["nodes"][node["requirements"]["root"]]["config"]["id"] == "root"


def test_preset_user_include_and_first_parent_precedence(tmp_path):
    (tmp_path / "CMakePresets.json").write_text(json.dumps({"version": 4, "configurePresets": [
        {"name": "first", "hidden": True, "binaryDir": "${sourceDir}/build"},
        {"name": "second", "hidden": True, "binaryDir": "${sourceDir}/wrong"},
    ]}))
    (tmp_path / "CMakeUserPresets.json").write_text(json.dumps({"version": 4,
        "include": ["CMakePresets.json"], "configurePresets": [
            {"name": "local", "inherits": ["first", "second"]}]}))
    assert cmake.preset_binary_dir(tmp_path, "local") == tmp_path / "build"


def test_shared_preset_cache_reconfigures_after_switch_or_manual_change(home, tmp_path, monkeypatch):
    source = tmp_path / "checkout"
    source.mkdir()
    build = source / "build"
    (source / "CMakePresets.json").write_text(json.dumps({"version": 3, "configurePresets": [
        {"name": name, "binaryDir": "${sourceDir}/build"} for name in ("cbs-gnu", "gambit-gnu")
    ]}))
    calls = []
    def run(argv, cwd, script, logs, number, timeout, **kwargs):
        calls.append(argv)
        if "--build" not in argv:
            (build / "CMakeCache.txt").write_text("PRESET:STRING=" + argv[argv.index("--preset") + 1] + "\n")
    monkeypatch.setattr(builds, "run_command", run)
    monkeypatch.setattr(cmake, "capture", lambda *a, **k: {})
    def execute(preset, index):
        logs = tmp_path / str(index)
        logs.mkdir()
        return builds._step(home, {"id": "project", "uses": "cmake.project"}, {
            "source": str(source), "configure_preset": preset, "build_preset": preset,
            "binary_dir": "preset", "in_place": True, "lock": "source", "phases": ["configure", "build"],
        }, tmp_path / "work", logs, {}, {}, {"cxx_standard": "unspecified"}, {}, {}, 2,
            lambda *a, **k: None, None)
    for index, preset in enumerate(("cbs-gnu", "gambit-gnu", "cbs-gnu", "cbs-gnu")):
        execute(preset, index)
    configure = [argv for argv in calls if "--build" not in argv]
    assert [argv[argv.index("--preset") + 1] for argv in configure] == ["cbs-gnu", "gambit-gnu", "cbs-gnu"]
    (build / "CMakeCache.txt").write_text("PRESET:STRING=external-change\n")
    execute("cbs-gnu", 4)
    assert len([argv for argv in calls if "--build" not in argv]) == 4
    assert all("--fresh" not in argv and "-B" not in argv for argv in calls)


@pytest.mark.parametrize("has_consumer", [False, True])
def test_install_rechecks_changed_shared_cache_before_reusing_an_app(home, tmp_path, monkeypatch, has_consumer):
    from jarvis_forge import build_apps, unified_profiles as profiles
    from jarvis_forge.storage import ForgeError, read_json, write_json
    from profile_fixtures import project_profile
    import yaml

    scope = project_profile(home)
    source = tmp_path / "checkout"
    source.mkdir()
    card = tmp_path / "app.yaml"
    card.write_text(yaml.safe_dump({"name": "app", "build": {"name": "App", "version": "1.0",
        "source": str(source), "in_place": True, "cmake": {"phases": ["configure", "build"]}}}))
    workspace = build_apps.fetch(home, str(card), scope=scope)
    activation = tmp_path / "activate.sh"
    activation.write_text("return 0\n")
    receipt = tmp_path / "receipt.json"
    write_json(receipt, {"dependencies": []})
    data = profiles.record(scope)
    data["apps"] = [{"name": "app", "version": "1.0", "enabled": True,
        "prefix": str(source), "activation": str(activation), "artifact_id": "old", "metadata": str(receipt), "workspace": str(workspace)}]
    if has_consumer:
        consumer_receipt = tmp_path / "consumer.json"
        write_json(consumer_receipt, {"dependencies": ["old"]})
        data["apps"].append({"name": "consumer", "version": "1.0", "enabled": True,
            "prefix": str(source), "activation": str(activation), "artifact_id": "consumer",
            "metadata": str(consumer_receipt)})
    write_json(scope.root / "metadata.json", data)
    metadata = read_json(workspace / "metadata.json")
    metadata["install_state"] = {"status": "passed", "fingerprint": build_apps._input(home, scope, workspace, None)}
    write_json(workspace / "metadata.json", metadata)
    def stale(*args):
        raise ForgeError("In-place CMake cache changed")
    monkeypatch.setattr(builds, "artifact", stale)
    called = []
    def execute(*args, **kwargs):
        called.append(args)
        return {"status": "failed", "run_id": "test-run", "error": "simulated build failure"}
    monkeypatch.setattr(builds, "execute", execute)
    if has_consumer:
        with pytest.raises(ForgeError, match="required by enabled apps"):
            build_apps._install(home, str(workspace), scope=scope, confirm=lambda plan: None)
        assert not called
        return
    result = build_apps._install(home, str(workspace), scope=scope, confirm=lambda plan: None)
    assert called and result["status"] == "failed"
    assert result["runs"][0]["error"] == "simulated build failure"


@pytest.mark.parametrize('mode', ['selected', 'conflicting-request', 'conflicting-result'])
def test_cmake_uses_the_native_config_reported_by_the_selected_python(home, tmp_path, monkeypatch, mode):
    import sys
    from jarvis_forge.storage import ForgeError
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'CMakePresets.json').write_text(json.dumps({'version': 3, 'configurePresets': [
        {'name': 'default', 'binaryDir': '${sourceDir}/build'}]}))
    native = tmp_path / 'native'
    other = tmp_path / 'other'
    logs = tmp_path / 'logs'
    logs.mkdir()
    calls = []
    def run(argv, cwd, script, log, number, timeout, **kwargs):
        calls.append(argv)
        directory = other if mode == 'conflicting-result' else native
        (source / 'build/CMakeCache.txt').write_text('FixtureNative_DIR:PATH=' + str(directory) + '\n')
    monkeypatch.setattr(builds, 'run_command', run)
    monkeypatch.setattr(cmake, 'capture', lambda *a, **k: {})
    cache = {'FixtureNative_DIR': {'type': 'PATH', 'value': str(other)}} if mode == 'conflicting-request' else {}
    options = {'source': str(source), 'configure_preset': 'default', 'binary_dir': 'preset',
               'in_place': True, 'phases': ['configure'], 'dependencies': ['preflight'], 'cache': cache}
    records = {'preflight': {'report': {'python': {'path': sys.executable}, 'modules': {
        'fixture': {'cmake_package': 'FixtureNative', 'cmake_dir': str(native)}}}}}
    def execute():
        return builds._step(home, {'id': 'project', 'uses': 'cmake.project'}, options,
            tmp_path / 'work', logs, {}, {'python': {'path': sys.executable}}, {'cxx_standard': 'unspecified'},
            records, {}, 2, lambda *a, **k: None, None)
    if mode == 'selected':
        execute()
        assert '-DFixtureNative_DIR:PATH=' + str(native) in calls[0]
    else:
        with pytest.raises(ForgeError, match='FixtureNative_DIR'):
            execute()
        assert bool(calls) is (mode == 'conflicting-result')


def test_cbs_recipe_declares_the_full_preset_prerequisites():
    import yaml
    raw = yaml.safe_load((ROOT / 'builds/cbs.yaml').read_text())
    options = raw['build']['cmake']['options']
    features = {'WITH_ROOT', 'WITH_HDF5', 'WITH_SQLite3', 'WITH_RESTFRAMES',
                'WITH_ONNXRUNTIME', 'CBS_WITH_RIVET_CONTUR', 'CUTFLOW'}
    assert all(options[name] in (True, 'ON') for name in features)
    modules = raw['checks']['before']['python']['modules']
    assert set(modules) >= {'yaml', 'pybind11', 'numpy', 'cython', 'configobj', 'pandas', 'matplotlib'}
    assert modules['pybind11']['cmake'] == 'pybind11'
    assert raw['checks']['before']['python']['shared']
    for name in ('cbs', 'gambit'):
        card = yaml.safe_load((ROOT / 'builds' / (name + '.yaml')).read_text())
        assert 'zlib' not in card['dependencies']
        assert {'zlib-ng+compat+shared', 'pkgconf', 'patch', 'libtool', 'curl'} <= set(card['dependencies'])
    preflight = _load('cbs_platform', 'builds/scripts/cbs_preflight.py')
    assert preflight.supported('Linux', 'x86_64')
    assert not preflight.supported('Linux', 'aarch64')


def test_profile_build_caches_stay_under_the_profile_by_default(home, monkeypatch):
    from jarvis_forge import unified_profiles as profiles
    from profile_fixtures import project_profile
    import os
    scope = project_profile(home)
    environment = {key: value for key, value in os.environ.items() if key not in {'MPLCONFIGDIR', 'XDG_CACHE_HOME'}}
    result = profiles.build_environment(scope, profiles.record(scope), environment)
    assert result['XDG_CACHE_HOME'] == str(scope.root / 'cache')
    assert result['MPLCONFIGDIR'] == str(scope.root / 'cache/matplotlib')
    captured = builds.safe_environment(result)
    assert captured['MPLCONFIGDIR'] == result['MPLCONFIGDIR']
    explicit = profiles.build_environment(scope, profiles.record(scope), {**environment, 'MPLCONFIGDIR': '/chosen/mpl'})
    assert explicit['MPLCONFIGDIR'] == '/chosen/mpl'


@pytest.mark.parametrize('mode', ['passed', 'failed', 'empty'])
def test_cbs_startup_check_uses_the_binary_without_reinterpreting_cmake(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace
    module = _load('cbs_startup', 'builds/scripts/cbs_acceptance.py')
    executable = tmp_path / 'CBS'
    executable.write_text('fixture')
    monkeypatch.setattr(module.sys, 'argv', ['cbs_acceptance.py', str(tmp_path)])
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1 if mode == 'failed' else 0,
            stdout='CBS analyses: 2 (from /src)\n' if mode == 'passed' else 'CBS analyses: 0\n',
            stderr='fixture startup failed' if mode == 'failed' else '')
    monkeypatch.setattr(module.subprocess, 'run', run)
    if mode == 'passed':
        module.main()
    else:
        with pytest.raises(SystemExit, match='fixture startup failed' if mode == 'failed' else 'no analysis entries'):
            module.main()
    assert calls == [[str(executable), '--list-analyses']]
