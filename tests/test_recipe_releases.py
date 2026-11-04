"""Exercise release download, integrity, compatibility and offline reuse."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from jarvis_forge import recipe_releases as releases
from jarvis_forge.package_operations import operation
from jarvis_forge.storage import ForgeError, ForgeHome, write_json


@pytest.fixture
def published(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "asset_builder", Path(__file__).parents[1] / "tools/build_recipe_assets.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Build an independent release fixture, without shipping production recipes in sdist.
    mod.__file__ = str(tmp_path / "authoring/tools/build_recipe_assets.py")
    repo = tmp_path / "authoring/recipes/spack_repo/jforge"
    (repo / "packages/neofetch").mkdir(parents=True)
    (repo / "repo.yaml").write_text("repo: {namespace: jforge, api: v2.2}\n")
    (repo / "packages/neofetch/package.py").write_text(
        "# independent release fixture\n"
    )
    output = tmp_path / "release"
    archive = mod.build("2026.11.04.1", output)
    manifest = json.loads((output / "recipes.json").read_text())
    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    config = home.root / "spack/config/repos.yaml"
    write_json(config, {"repos": {"builtin": "/engine-compatible/builtin"}})

    def fetch(url, target, logs):
        target.write_bytes(
            (output / "recipes.json").read_bytes()
            if url == releases.INDEX_URL
            else archive.read_bytes()
        )

    monkeypatch.setattr(releases, "fetch", fetch)
    monkeypatch.setattr("jarvis_forge.package_operations.fetch", fetch)
    return home, output, archive, manifest


def test_download_is_immutable_and_install_is_offline(published, monkeypatch):
    home, output, archive, manifest = published
    with operation(home, "test", {}) as (op, result):
        release = releases.prepare(home, op)
    assert not (home.root / "apt/recipe-release.json").exists()
    repo = Path(release["repository"])
    original = (repo / "packages/neofetch/package.py").read_bytes()
    assert (
        hashlib.sha256(archive.read_bytes()).hexdigest() == release["archive"]["sha256"]
    )
    write_json(home.root / "apt/recipe-release.json", release)
    monkeypatch.setattr(
        releases, "fetch", lambda *a: pytest.fail("offline install downloaded metadata")
    )
    assert releases.ensure(home, home.root / "operations") == repo
    assert (repo / "packages/neofetch/package.py").read_bytes() == original
    assert (
        yaml.safe_load((home.root / "spack/config/repos.yaml").read_text())["repos"][
            "builtin"
        ]
        == "/engine-compatible/builtin"
    )


@pytest.mark.parametrize("damage", ["checksum", "engine", "layout"])
def test_failed_release_never_switches_policy(published, damage):
    home, output, archive, manifest = published
    before = (home.root / "spack/config/repos.yaml").read_bytes()
    if damage == "checksum":
        manifest["archive"]["sha256"] = "0" * 64
    elif damage == "engine":
        manifest["engine_versions"] = ["unsupported"]
    else:
        manifest["packages"] = ["imaginary-package"]
    (output / "recipes.json").write_text(json.dumps(manifest))
    with operation(home, "test", {}) as (op, result):
        with pytest.raises(ForgeError):
            releases.prepare(home, op)
    assert (home.root / "spack/config/repos.yaml").read_bytes() == before
    assert not (home.root / "apt/recipe-release.json").exists()


def test_previous_bundled_snapshot_remains_usable_offline(published, monkeypatch):
    home, output, archive, manifest = published
    with operation(home, "test", {}) as (op, result):
        release = releases.prepare(home, op)
    releases.register(home, release)
    monkeypatch.setattr(
        releases, "fetch", lambda *a: pytest.fail("migration requires no download")
    )
    assert releases.ensure(home, home.root / "operations") == Path(
        release["repository"]
    )
