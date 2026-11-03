from __future__ import annotations

import difflib
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile

import pytest
import yaml

from jarvis_forge import official, packages
from jarvis_forge.catalog import Catalog, validate_recipe
from jarvis_forge.cli import main
from jarvis_forge.packages import install, installed, plan_install, quilt_patches, state, update
from jarvis_forge.storage import ForgeError, ForgeHome, read_json


def archive(path, contents):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as package:
        for name, content in contents.items():
            content = content.encode()
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(content), 0o755
            package.addfile(item, io.BytesIO(content))
    path.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))
    return path


@pytest.fixture
def repository(tmp_path, monkeypatch):
    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    original = "#!/usr/bin/env bash\nprintf '%s\\n' original\n"
    source = archive(tmp_path / "original.tar.gz", {"toyproject-1.0/tool": original})
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    base = {"schema_version": 1, "name": "toy", "version": "1.0", "description": "Test template",
            "source": {"url": source.as_uri(), "sha256": sha(source), "subdir": "toyproject-1.0"},
            "install": [["${python}", "-c", "from pathlib import Path; import shutil,sys; p=Path(sys.argv[1]); "
                         "(p/'bin').mkdir(exist_ok=True); shutil.copy2('tool',p/'bin/tool')", "${prefix}"]],
            "check": [["${prefix}/bin/tool"]], "environment": {"prepend": {"PATH": ["${prefix}/bin"]}}}
    (home.root / "recipes/toy.yaml").write_text(yaml.safe_dump(base))
    current = {"revision": 1, "corrupt_index": False, "corrupt_source": False}

    def materialize():
        if current.get("cmake_layout"):
            archive(source, {"toyproject-1.0/tool": original, "toyproject-1.0/CMakeLists.txt": "project(toy)\n"})
        patched = original.replace(" original", f" patched{current['revision']}")
        patch = "".join(difflib.unified_diff(original.splitlines(True), patched.splitlines(True),
                                             fromfile="a/tool", tofile="b/tool"))
        packaging = archive(tmp_path / "packaging.tar.gz", {
            "debian/patches/series": "fix.patch\n", "debian/patches/fix.patch": patch})
        row = (f"Package: toyproject\nBinary: toy-dev, toy-doc\nVersion: 1.0-{current['revision']}\n"
               "Build-Depends: cmake, debhelper-compat (= 13), libunknown-dev\n"
               "Directory: pool/main/t/toyproject\nChecksums-Sha256:\n"
               f" {sha(source)} {source.stat().st_size} toyproject_1.0.orig.tar.gz\n"
               f" {sha(packaging)} {packaging.stat().st_size} toyproject_1.0-{current['revision']}.debian.tar.gz\n\n")
        index = tmp_path / "Sources.gz"
        index.write_bytes(gzip.compress(row.encode(), mtime=0))
        release = (f"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nOrigin: {current.get('origin', 'Debian')}\nCodename: {current.get('suite', 'test')}\n"
                   f"SHA256:\n {sha(index)} {index.stat().st_size} main/source/Sources.gz\n\n"
                   "-----BEGIN PGP SIGNATURE-----\nfixture-signature-not-verified\n-----END PGP SIGNATURE-----\n")
        return release, index, packaging

    release, index, packaging = materialize()

    def fetch(url, target, logs):
        nonlocal release, index, packaging
        if url.endswith("InRelease"):
            release, index, packaging = materialize()
            target.write_text(release)
        elif url.endswith("Sources.gz"):
            target.write_bytes(b"corrupt" if current["corrupt_index"] else index.read_bytes())
        elif ".orig.tar.gz" in url:
            target.write_bytes(b"corrupt" if current["corrupt_source"] else source.read_bytes())
        elif ".debian.tar.gz" in url:
            shutil.copyfile(packaging, target)
        else:
            raise AssertionError(url)
    monkeypatch.setattr(official, "fetch", fetch)
    monkeypatch.setattr(packages, "fetch", fetch)
    return home, current


def imported(home):
    return official.import_package(home, "toy-dev", origin="debian", suite="test",
                                   template_name="toy", name="official-toy")


def test_official_import_preserves_binary_source_mapping_and_patches(repository):
    home, _ = repository
    result = imported(home)
    assert result["import_status"] == "generated"
    assert result["source_package"] == "toyproject"
    assert result["apt_signature_verified"] is False
    report = read_json(Path(result["report"]))
    assert report["patches"] == ["fix.patch"]
    assert report["source_record"]["Build-Depends"].endswith("libunknown-dev")
    assert report["recipe"]["source"]["patch_archive"]["sha256"]
    assert any(r["name"] == "official-toy" for r in Catalog(home).list())


def test_generated_recipe_installs_and_applies_real_quilt_patch(repository):
    home, _ = repository
    imported(home)
    result = install(home, ["official-toy"])
    package = next(p for p in installed(home) if p["name"] == "official-toy")
    assert "patched1" in (Path(package["prefix"]) / "bin/tool").read_text()
    assert package["provenance"]["debian_version"] == "1.0-1"
    assert result["status"] == "passed"


def test_update_and_upgrade_handle_debian_revision_with_same_upstream_version(repository, capsys):
    home, current = repository
    imported(home)
    first = install(home, ["official-toy"])
    current["revision"] = 2
    result = update(home)
    assert result["official_imports"][0]["debian_version"] == "1.0-2"
    assert main(["apt", "list", "--upgradable", "--home", str(home.root), "--json"]) == 0
    assert [p["name"] for p in json.loads(capsys.readouterr().out)["packages"]] == ["official-toy"]
    plan = plan_install(home, [], mode="upgrade")
    entry = next(p for p in plan["packages"] if p["name"] == "official-toy")
    assert entry["version"] == "1.0" and entry["action"] == "install"
    second = install(home, [], mode="upgrade")
    assert first["packages"][0]["install_id"] != second["packages"][0]["install_id"]
    active = next(p for p in installed(home) if p["active"])
    assert "patched2" in (Path(active["prefix"]) / "bin/tool").read_text()
    assert Path(first["packages"][0]["prefix"]).exists()


def test_unsupported_import_is_pending_and_not_silently_installable(repository):
    home, _ = repository
    result = official.import_package(home, "toy-doc", origin="debian", suite="test")
    assert result["import_status"] == "needs_adapter" and result["recipe_path"] is None
    with pytest.raises(ForgeError, match="need an adapter"):
        plan_install(home, ["toy-doc"])


def test_update_failure_keeps_previous_official_catalog_and_active_environment(repository):
    home, current = repository
    imported(home)
    install(home, ["official-toy"])
    before_state = state(home)
    before_catalog = (home.root / "cache/apt/official.json").read_bytes()
    current.update(revision=2, corrupt_index=True)
    with pytest.raises(ForgeError, match="SHA-256/size"):
        update(home)
    assert state(home) == before_state
    assert (home.root / "cache/apt/official.json").read_bytes() == before_catalog


def test_source_archive_checksum_is_checked(repository):
    home, current = repository
    current["corrupt_source"] = True
    with pytest.raises(ForgeError, match="SHA-256 mismatch"):
        imported(home)
    assert home.packages() == []


def test_keyring_mode_never_silently_skips_missing_gpgv(repository, tmp_path, monkeypatch):
    home, _ = repository
    monkeypatch.setattr(official.shutil, "which", lambda tool: None)
    with pytest.raises(ForgeError, match="requires gpgv"):
        official.import_package(home, "toy-dev", origin="debian", suite="test", keyring=tmp_path / "ring.gpg")
    assert not (home.root / "cache/apt/official.json").exists()


@pytest.mark.parametrize("path", ["../escape", "/etc/passwd", "pool/%2e%2e/escape", "https://other/escape", "pool/x?query"])
def test_unsafe_repository_paths_are_rejected(path):
    with pytest.raises(ForgeError):
        official.safe_relative(path)


def test_control_parser_preserves_multiline_fields_and_rejects_duplicates():
    rows = list(official.paragraphs(["Package: foo", "Binary: foo,", " bar", "Version: 1.0", ""]))
    assert rows[0]["Binary"] == "foo,\nbar"
    with pytest.raises(ForgeError, match="duplicate"):
        list(official.paragraphs(["Package: foo", "Package: other"]))


def test_clear_signature_shape_and_sha_records_are_validated():
    with pytest.raises(ForgeError):
        official.release_body("Origin: Debian")
    with pytest.raises(ForgeError):
        official.release_body("-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nOrigin: Debian")
    with pytest.raises(ForgeError):
        official.checksums("bad 5 source.tar.gz")


def test_patch_path_escape_is_rejected(tmp_path):
    base = tmp_path / "debian/patches"
    base.mkdir(parents=True)
    (base / "series").write_text("bad.patch\n")
    (base / "bad.patch").write_text("--- a/../../escape\n+++ b/../../escape\n@@ -1 +1 @@\n-a\n+b\n")
    with pytest.raises(ForgeError, match="Unsafe patch target"):
        quilt_patches(tmp_path)


def test_no_new_signature_verification_claim_for_default_https_import(repository):
    home, _ = repository
    result = imported(home)
    assert result["trust"] == "official-HTTPS-and-checksums"
    assert read_json(Path(result["report"]))["snapshot"]["apt_signature_verified"] is False


def test_import_help_and_default_suite(capsys, monkeypatch, tmp_path):
    assert main(["apt", "import", "-h"]) == 0
    assert "--suite" in capsys.readouterr().out
    captured = {}
    def import_package(home, package, **kwargs):
        captured.update(package=package, **kwargs)
        return {"import_status": "needs_adapter"}
    monkeypatch.setattr(official, "import_package", import_package)
    assert main(["--home", str(tmp_path / "home"), "apt", "import", "foo", "--json"]) == 0
    assert captured["suite"] == "bookworm" and captured["origin"] == "debian"
    assert json.loads(capsys.readouterr().out)["ok"]


def test_unchanged_official_update_does_not_force_reinstallation(repository):
    home, _ = repository
    imported(home)
    install(home, ["official-toy"])
    update(home)
    assert all(p["action"] == "reuse" for p in plan_install(home, [], mode="upgrade")["packages"])


def test_ubuntu_uses_its_own_origin_and_repository(repository):
    home, current = repository
    current["origin"] = "Ubuntu"
    result = official.import_package(home, "toy-dev", origin="ubuntu", suite="test", template_name="toy", name="ubuntu-toy")
    report = read_json(Path(result["report"]))
    assert report["recipe"]["source"]["url"].startswith("https://archive.ubuntu.com/ubuntu/")


def test_wrong_suite_is_rejected_before_recipe_registration(repository):
    home, current = repository
    current["suite"] = "wrong-suite"
    with pytest.raises(ForgeError, match="suite/codename"):
        imported(home)
    assert not (home.root / "cache/apt/official.json").exists()


def test_generic_cmake_draft_is_saved_but_cannot_enter_install_catalog(repository):
    home, current = repository
    current["cmake_layout"] = True
    result = official.import_package(home, "toy-doc", origin="debian", suite="test")
    assert result["import_status"] == "needs_adapter"
    draft = yaml.safe_load(Path(result["recipe_path"]).read_text())
    assert draft["build"][0][0] == "cmake"
    assert "libunknown-dev" in draft["provenance"]["unresolved_build_dependencies"]["Build-Depends"]
    with pytest.raises(ForgeError, match="draft"):
        validate_recipe(draft, "generated draft")
    assert not any(r["name"] == "toy-doc" for r in Catalog(home).list())
