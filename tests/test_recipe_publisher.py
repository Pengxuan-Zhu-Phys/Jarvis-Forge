"""Exercise publication order and recovery without contacting GitHub."""

import copy
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "jfrecipes"
loader = importlib.machinery.SourceFileLoader("independent_recipe_publisher", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
publisher = importlib.util.module_from_spec(spec)
loader.exec_module(publisher)


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "independent-recipes"
    repo = source / "spack_repo/jforge"
    package = repo / "packages/example/package.py"
    package.parent.mkdir(parents=True)
    package.write_text("# No Forge import needed to publish this fixture\n")
    (repo / "repo.yaml").write_text("repo:\n  namespace: jforge\n  api: v2.2\n")
    (source / "release.json").write_text(json.dumps({"schema_version": 1, "engine_versions": ["1.2.2"]}))
    return source


class FakeGitHub:
    def __init__(self, tag="recipes"):
        self.tag = tag
        self.state = None
        self.payloads = {}
        self.events = []
        self.fail_stage = False
        self.fail_swap = False

    def release(self):
        return copy.deepcopy(self.state)

    def create(self):
        self.state = dict(id=1, draft=True, immutable=False, assets=[])
        self.events.append("create")

    def upload(self, file):
        if self.fail_stage and file.name.startswith("recipes-index-"):
            raise RuntimeError("stage upload interrupted")
        assert not publisher.asset_named(self.state, file.name)
        identity = len(self.payloads) + 1
        self.state["assets"].append(dict(id=identity, name=file.name))
        self.payloads[identity] = file.read_bytes()
        self.events.append("upload:" + file.name)

    def download(self, asset):
        return self.payloads[asset["id"]]

    def rename(self, asset, name):
        if self.fail_swap and name == "recipes.json" and asset["name"].startswith("recipes-index-"):
            self.fail_swap = False
            raise RuntimeError("rename interrupted")
        assert publisher.asset_named(self.state, name) is None
        next(a for a in self.state["assets"] if a["id"] == asset["id"])["name"] = name
        self.events.append("rename:" + name)

    def publish(self, release):
        assert publisher.asset_named(self.state, self.tag + ".json")
        self.state["draft"] = False
        self.events.append("publish")

    def hide(self, release):
        self.state["draft"] = True
        self.events.append("hide")

    def delete(self, asset):
        self.state["assets"] = [item for item in self.state["assets"] if item["id"] != asset["id"]]
        self.payloads.pop(asset["id"], None)
        self.events.append("delete:" + asset["name"])

    def verify(self, url, expected):
        assert not self.state["draft"]
        asset = publisher.asset_named(self.state, url.rsplit("/", 1)[1])
        if asset is None or publisher.sha(self.download(asset)) != expected:
            raise RuntimeError("anonymous download failed")
        self.events.append("verify:" + url)


def initial(source, tmp_path):
    first = tmp_path / "first"
    manifest = publisher.build("r1", source, first)
    github = FakeGitHub()
    publisher.publish(manifest, first, github, github.verify)
    return github, first


@pytest.fixture
def build_source(tmp_path):
    source = tmp_path / "independent-builds"
    (source / "scripts").mkdir(parents=True)
    (source / "release.json").write_text(json.dumps({"schema_version": 1, "requires_forge": ">=1.1.0"}))
    (source / "scripts/accept.py").write_text("print('accepted')\n")
    (source / "example.yaml").write_text(yaml.safe_dump({"name": "example-build", "checks": {
        "after": {"scripts": ["scripts/accept.py"]}}}))
    return source


def test_dry_run_is_standalone_without_forge(source, build_source, tmp_path):
    output = tmp_path / "output"
    bootstrap = """import sys,runpy
sys.path.insert(0,sys.argv.pop(1))
script=sys.argv.pop(1)
sys.argv[0]=script
class NoForge:
    def find_spec(self,name,*args):
        if name.startswith('jarvis_forge'):
            raise AssertionError('publisher imported Forge runtime')
sys.meta_path.insert(0,NoForge())
runpy.run_path(script,run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", bootstrap, str(Path(yaml.__file__).parents[1]),
                             str(SCRIPT), "independent-r1", "--source", str(source), "--build-source", str(build_source),
                             "--output", str(output), "--dry"], env={"PATH": ""}, cwd=tmp_path,
                            capture_output=True, text=True, check=True)
    manifest = json.loads((output / "recipes/recipes.json").read_text())
    assert "Dry run passed" in result.stdout
    assert manifest["engine_versions"] == ["1.2.2"]
    assert manifest["archive"]["url"].startswith(publisher.BASE_URL)
    assert "forge" not in manifest
    builds = json.loads((output / "builds/builds.json").read_text())
    assert builds["builds"][0]["id"] == "example-build"
    assert builds["builds"][0]["files"][0]["path"] == "scripts/accept.py"


def test_first_publish_exposes_verified_catalog_and_immutable_archive(source, tmp_path):
    github, first = initial(source, tmp_path)
    assert not github.state["draft"]
    assert github.events.index("rename:recipes.json") < github.events.index("publish")
    old = copy.deepcopy(github.state)
    publisher.publish(json.loads((first / "recipes.json").read_text()), first, github, github.verify)
    assert github.state == old  # Retry is idempotent.


def test_update_stages_before_switch_then_keeps_only_the_latest_assets(source, tmp_path):
    github, first = initial(source, tmp_path)
    old_index = publisher.asset_named(github.state, "recipes.json")
    (source / "spack_repo/jforge/packages/example/package.py").write_text("# updated recipe\n")
    second = tmp_path / "second"
    manifest = publisher.build("r2", source, second)
    publisher.publish(manifest, second, github, github.verify)
    archive = manifest["archive"]["url"].rsplit("/", 1)[1]
    assert {asset["name"] for asset in github.state["assets"]} == {"recipes.json", archive}
    assert json.loads(github.download(publisher.asset_named(github.state, "recipes.json")))["version"] == "r2"
    upload = next(i for i, event in enumerate(github.events) if event.startswith("upload:recipes-index-r2"))
    switched = github.events.index(f"rename:recipes-previous-{old_index['id']}.json")
    verified = max(i for i, event in enumerate(github.events) if event.startswith("verify:"))
    removed = min(i for i, event in enumerate(github.events) if event.startswith("delete:"))
    assert upload < switched < verified < removed
    assert f"delete:recipes-previous-{old_index['id']}.json" in github.events
    assert any(event.startswith("delete:jforge-spack-recipes-r1") for event in github.events)


@pytest.mark.parametrize("failure", ["stage", "swap", "public"])
def test_failed_update_restores_previous_stable_index(source, tmp_path, failure):
    github, first = initial(source, tmp_path)
    old = github.download(publisher.asset_named(github.state, "recipes.json"))
    second = tmp_path / "second"
    manifest = publisher.build("r2", source, second)
    github.fail_stage = failure == "stage"
    github.fail_swap = failure == "swap"

    def verify(url, expected):
        if failure == "public":
            raise RuntimeError("public network interrupted")
        return github.verify(url, expected)

    with pytest.raises(RuntimeError):
        publisher.publish(manifest, second, github, verify)
    assert github.download(publisher.asset_named(github.state, "recipes.json")) == old
    assert not any(event.startswith("delete:") for event in github.events)
    assert any(asset["name"].startswith("jforge-spack-recipes-r1") for asset in github.state["assets"])


def test_reusing_revision_with_changed_content_is_rejected(source, tmp_path):
    github, first = initial(source, tmp_path)
    before = copy.deepcopy(github.state)
    (source / "spack_repo/jforge/packages/example/package.py").write_text("# different recipe\n")
    output = tmp_path / "changed"
    manifest = publisher.build("r1", source, output)
    with pytest.raises(ValueError, match="new revision"):
        publisher.publish(manifest, output, github, github.verify)
    assert github.state == before


def test_existing_archive_is_never_overwritten(source, tmp_path):
    github, first = initial(source, tmp_path)
    output = tmp_path / "second"
    manifest = publisher.build("r2", source, output)
    github.upload(output / "jforge-spack-recipes-r2.tar.gz")
    asset = publisher.asset_named(github.state, "jforge-spack-recipes-r2.tar.gz")
    github.payloads[asset["id"]] = b"conflicting immutable archive"
    with pytest.raises(ValueError, match="never overwritten"):
        publisher.publish(manifest, output, github, github.verify)


def test_cannot_drop_engines_used_by_existing_clients(source, tmp_path):
    github, first = initial(source, tmp_path)
    (source / "release.json").write_text(json.dumps({"schema_version": 1, "engine_versions": ["2.0.0"]}))
    manifest = publisher.build("r2", source, tmp_path / "second")
    with pytest.raises(ValueError, match="drop supported Spack engines"):
        publisher.publish(manifest, tmp_path / "second", github, github.verify)


def test_published_bundle_matches_existing_client_download_contract(source, tmp_path, monkeypatch):
    from jarvis_forge import recipe_releases, package_operations
    from jarvis_forge.storage import ForgeHome, write_json

    output = tmp_path / "assets"
    manifest = publisher.build("independent-r3", source, output)
    assert publisher.INDEX_URL == recipe_releases.INDEX_URL
    assert publisher.INDEX_URL == "https://github.com/Pengxuan-Zhu-Phys/Jarvis-Forge/releases/download/recipes/recipes.json"
    home = ForgeHome(tmp_path / "consumer")
    home.initialize()
    write_json(home.root / "spack/config/repos.yaml", {"repos": {"builtin": "/existing/recipes"}})

    def fetch(url, target, logs):
        target.write_bytes((output / url.rsplit("/", 1)[1]).read_bytes())

    monkeypatch.setattr(recipe_releases, "fetch", fetch)
    monkeypatch.setattr(package_operations, "fetch", fetch)
    with package_operations.operation(home, "test-consumer", {}) as (op, result):
        downloaded = recipe_releases.prepare(home, op)
        recipe_releases.register(home, downloaded)
    assert downloaded["version"] == manifest["version"]
    assert (Path(downloaded["repository"]) / "packages/example/package.py").is_file()


def test_all_catalogs_publish_with_companions_and_idempotent_retry(source, build_source, tmp_path):
    recipe_output, build_output = tmp_path / "packages", tmp_path / "builds"
    packages = publisher.build("r1", source, recipe_output)
    builds = publisher.build_projects("r1", build_source, build_output)
    package_github, build_github = FakeGitHub(), FakeGitHub("builds")
    bundles = [("recipes", packages, recipe_output, package_github), ("builds", builds, build_output, build_github)]

    def verify(url, expected):
        github = build_github if "/builds/" in url else package_github
        github.verify(url, expected)

    publisher.publish_all(bundles, verify)
    assert not package_github.state["draft"] and not build_github.state["draft"]
    row = builds["builds"][0]
    assert publisher.asset_named(build_github.state, row["url"].rsplit("/", 1)[1])
    assert publisher.asset_named(build_github.state, row["files"][0]["url"].rsplit("/", 1)[1])
    state = copy.deepcopy((package_github.state, build_github.state))
    publisher.publish_all(bundles, verify)
    assert (package_github.state, build_github.state) == state


@pytest.mark.parametrize("failure", ["stage", "public"])
def test_build_publication_failure_keeps_or_restores_both_indexes(source, build_source, tmp_path, failure):
    package_github, first = initial(source, tmp_path)
    old_index = package_github.download(publisher.asset_named(package_github.state, "recipes.json"))
    second, projects = tmp_path / "second", tmp_path / "projects"
    packages = publisher.build("r2", source, second)
    builds = publisher.build_projects("r2", build_source, projects)
    build_github = FakeGitHub("builds")
    if failure == "stage":
        def broken_upload(file):
            raise RuntimeError("build asset upload failed")
        build_github.upload = broken_upload

    def verify(url, expected):
        if "/builds/" in url:
            raise RuntimeError("build public verification failed")
        package_github.verify(url, expected)

    with pytest.raises(RuntimeError):
        publisher.publish_all([("recipes", packages, second, package_github),
                               ("builds", builds, projects, build_github)], verify)
    assert package_github.download(publisher.asset_named(package_github.state, "recipes.json")) == old_index
    assert build_github.state["draft"]
    assert publisher.asset_named(build_github.state, "builds.json") is None


def test_invalid_build_prevents_any_github_calls(source, build_source, tmp_path, monkeypatch):
    (build_source / "example.yaml").write_text("name: invalid\nname: duplicate\n")
    monkeypatch.setattr(publisher, "GitHub", lambda *args: pytest.fail("invalid build reached GitHub"))
    assert publisher.main(["r1", "--source", str(source), "--build-source", str(build_source)]) == 1


def test_default_cli_publishes_both_catalogs(source, build_source, tmp_path, monkeypatch):
    githubs = {"recipes": FakeGitHub(), "builds": FakeGitHub("builds")}
    publish_all = publisher.publish_all
    monkeypatch.setattr(publisher, "GitHub", lambda tag: githubs[tag])
    monkeypatch.setattr(publisher, "publish_all", lambda bundles: publish_all(
        bundles, lambda url, expected: githubs["builds" if "/builds/" in url else "recipes"].verify(url, expected)))
    assert publisher.main(["r1", "--source", str(source), "--build-source", str(build_source),
                           "--output", str(tmp_path / "all-assets")]) == 0
    assert all(not github.state["draft"] for github in githubs.values())


@pytest.mark.parametrize("path", ["../outside.py", "/tmp/outside.py", "scripts/missing.py", "scripts/check.sh"])
def test_invalid_build_companion_is_rejected(build_source, tmp_path, path):
    (build_source / "example.yaml").write_text(yaml.safe_dump({"name": "invalid", "checks": {
        "after": {"scripts": [path]}}}))
    with pytest.raises(ValueError, match="Unsafe or missing"):
        publisher.build_projects("r1", build_source, tmp_path / "output")


def test_build_and_library_catalog_consumed_by_current_client(build_source, tmp_path, monkeypatch):
    from jarvis_forge import build_catalog
    from jarvis_forge.storage import ForgeHome

    libraries = build_source / "library"
    libraries.mkdir()
    (libraries / "tiny.yaml").write_text(yaml.safe_dump({"Library": {
        "name": "Tiny", "version": "1.0", "build": {"name": "library-template", "checks": {
            "before": {"python": {"modules": ["json"]}}}}}}))
    output = tmp_path / "assets"
    manifest = publisher.build_projects("independent-r1", build_source, output)

    def fetch(url, target, logs):
        target.write_bytes((output / url.rsplit("/", 1)[1]).read_bytes())

    monkeypatch.setattr(build_catalog, "fetch", fetch)
    home = ForgeHome(tmp_path / "consumer")
    home.initialize()
    result = build_catalog.update(home)
    assert result["builds"] == manifest["builds"]
    assert len(build_catalog.libraries(home)) == 1
    acquired = build_catalog.resolve(home, "example-build")
    assert acquired.is_file()
    assert (acquired.parent / "scripts/accept.py").read_bytes() == (build_source / "scripts/accept.py").read_bytes()


def test_all_new_payloads_are_uploaded_before_either_catalog_switches(source, build_source, tmp_path):
    packages_output, projects_output = tmp_path / "packages", tmp_path / "projects"
    packages = publisher.build("r1", source, packages_output)
    projects = publisher.build_projects("r1", build_source, projects_output)
    package_github, build_github = FakeGitHub(), FakeGitHub("builds")
    activate = package_github.rename

    def checked_rename(asset, name):
        assert any(event.startswith("upload:builds-index-") for event in build_github.events)
        return activate(asset, name)

    package_github.rename = checked_rename
    publisher.publish_all([("recipes", packages, packages_output, package_github),
                           ("builds", projects, projects_output, build_github)],
                          lambda url, expected: (build_github if "/builds/" in url else package_github).verify(url, expected))


def test_failure_restores_both_previously_public_catalogs(source, build_source, tmp_path):
    package_github, build_github = FakeGitHub(), FakeGitHub("builds")
    first = tmp_path / "first"
    packages = publisher.build("r1", source, first / "recipes")
    projects = publisher.build_projects("r1", build_source, first / "builds")
    verify = lambda url, expected: (build_github if "/builds/" in url else package_github).verify(url, expected)
    publisher.publish_all([("recipes", packages, first / "recipes", package_github),
                           ("builds", projects, first / "builds", build_github)], verify)
    old_package_index = package_github.download(publisher.asset_named(package_github.state, "recipes.json"))
    old_build_index = build_github.download(publisher.asset_named(build_github.state, "builds.json"))
    second = tmp_path / "second"
    packages = publisher.build("r2", source, second / "recipes")
    projects = publisher.build_projects("r2", build_source, second / "builds")

    def failed_verify(url, expected):
        if "/builds/" in url:
            raise RuntimeError("build verification failed")
        verify(url, expected)

    with pytest.raises(RuntimeError):
        publisher.publish_all([("recipes", packages, second / "recipes", package_github),
                               ("builds", projects, second / "builds", build_github)], failed_verify)
    assert package_github.download(publisher.asset_named(package_github.state, "recipes.json")) == old_package_index
    assert build_github.download(publisher.asset_named(build_github.state, "builds.json")) == old_build_index
    assert not package_github.state["draft"] and not build_github.state["draft"]
    assert not any(event.startswith("delete:") for event in package_github.events + build_github.events)


def test_build_update_keeps_only_the_latest_recipe_files(source, build_source, tmp_path):
    package_github, build_github = FakeGitHub(), FakeGitHub("builds")
    first = tmp_path / "first"
    verify = lambda url, expected: (build_github if "/builds/" in url else package_github).verify(url, expected)
    publisher.publish_all([
        ("recipes", publisher.build("r1", source, first / "recipes"), first / "recipes", package_github),
        ("builds", publisher.build_projects("r1", build_source, first / "builds"), first / "builds", build_github),
    ], verify)
    second = tmp_path / "second"
    (build_source / "scripts/accept.py").write_text("print('accepted again')\n")
    projects = publisher.build_projects("r2", build_source, second / "builds")
    publisher.publish_all([
        ("recipes", publisher.build("r2", source, second / "recipes"), second / "recipes", package_github),
        ("builds", projects, second / "builds", build_github),
    ], verify)
    row = projects["builds"][0]
    expected = {"builds.json", row["url"].rsplit("/", 1)[1], row["files"][0]["url"].rsplit("/", 1)[1]}
    assert {asset["name"] for asset in build_github.state["assets"]} == expected
    assert not any("2026" in asset["name"] or asset["name"].startswith("builds-previous-")
                   for asset in build_github.state["assets"])
    assert any(event.startswith("delete:builds-previous-") for event in build_github.events)
    assert any(event.startswith("delete:") and "r1" in event for event in build_github.events)


def test_root_release_bundle_is_accepted_by_current_client(tmp_path, monkeypatch):
    from jarvis_forge import build_catalog
    from jarvis_forge.storage import ForgeHome

    source = SCRIPT.parent / "builds"
    if not (source / "root-build.yaml").is_file():
        pytest.skip("Independent recipe sources are not shipped in the Python distribution")
    output = tmp_path / "assets"
    manifest = publisher.build_projects("2026.11.05.1", source, output)
    root = next(row for row in manifest["builds"] if row["id"] == "root-build")
    assert any(resource["path"] == "scripts/root_acceptance.py" for resource in root["files"])
    monkeypatch.setattr(build_catalog, "fetch", lambda url, target, logs: target.write_bytes((output / url.rsplit("/", 1)[1]).read_bytes()))
    home = ForgeHome(tmp_path / "home")
    home.initialize()
    build_catalog.update(home)
    assert build_catalog.resolve(home, "root-build").is_file()
