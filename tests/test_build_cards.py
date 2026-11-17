"""Cards and acceptance helpers added for in-place CBS and MG5+Pythia8."""

import importlib.util
from pathlib import Path

import pytest

from jarvis_forge import builds
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
    loaded = builds.load(ROOT / "builds/cbs-build.yaml")
    step = next(item for item in loaded["config"]["steps"] if item["uses"] == "cmake.project")
    assert step["with"]["in_place"] is True
    assert step["with"]["binary_dir"] == "preset"
    assert step["with"]["lock"] == "source"
    assert step["with"]["configure_preset"] == "cbs-gnu"
    assert step["with"]["phases"] == ["configure", "build"]
    assert step["with"]["required_files"] == ["CBS"]
    assert builds.host_requirements(loaded["config"]) == {
        "platforms": ["linux", "darwin"], "apt_backend": "spack"}


def test_cbs_plan_does_not_pass_a_private_binary_dir(home):
    planned = builds.plan(home, str(ROOT / "builds/cbs-build.yaml"))
    configure = planned["cmake_commands"][0]["argv"]["configure"]
    assert "-B" not in configure
    assert "cbs-gnu" in configure
    assert planned["cmake_commands"][0]["argv"]["build"][2] == "<preset-binary-dir>"


def test_mg5_card_requires_the_pythia8_variant():
    loaded = builds.load(ROOT / "builds/mg5-pythia8.yaml")
    specs = next(item["with"]["specs"] for item in loaded["config"]["steps"] if item["uses"] == "apt.install")
    assert "jforge-mg5amc@3.6.3 +pythia8" in specs
    assert builds.host_requirements(loaded["config"])["platforms"] == ["linux"]


def test_cbs_acceptance_detects_a_signal_region():
    module = _load("cbs_acceptance", "builds/scripts/cbs_acceptance.py")
    assert module.has_signal_region({
        "analyses": {"ATLAS_EXOT_2019_04": {"signal_regions": {"SR_1b": {"n_sig_scaled": 1.0}}}}
    })
    assert not module.has_signal_region({"analyses": {"ATLAS_EXOT_2019_04": {"signal_regions": {}}}})
    assert module.sample_path(["script", "/src"], __file__) is None


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
