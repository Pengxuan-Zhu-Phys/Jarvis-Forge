"""Download versioned native Spack recipes independently of the Python package."""

from pathlib import Path
import re

import yaml

from .downloads import fetch
from .storage import ForgeError, read_json, write_json

INDEX_URL = "https://github.com/Pengxuan-Zhu-Phys/Jarvis-Forge/releases/download/recipes/recipes.json"


def current(home):
    path = home.root / "apt/recipe-release.json"
    return read_json(path) if path.exists() else None


def prepare(home, op):
    from .spack_backend import ENGINE_VERSION, root, _unpack

    index = op / "recipe-release-download.json"
    fetch(INDEX_URL, index, op / "recipe-index-download")
    value = read_json(index)
    archive = value.get("archive", {})
    if not isinstance(archive, dict):
        raise ForgeError("Invalid recipe archive metadata.")
    if (
        value.get("schema_version") != 1
        or not isinstance(value.get("version"), str)
        or not isinstance(value.get("engine_versions"), list)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", str(value.get("version", ""))
        )
        or ENGINE_VERSION not in value.get("engine_versions", [])
        or not re.fullmatch(r"[0-9a-f]{64}", str(archive.get("sha256", "")))
        or not str(archive.get("url", "")).startswith("https://")
    ):
        raise ForgeError("Invalid or incompatible recipe release manifest.")
    directory = root(home) / "repositories" / ("jforge-release-" + archive["sha256"])
    _unpack(home, archive, directory, op, "recipe-archive-download")
    repo = directory / "spack_repo/jforge"
    if not (repo / "repo.yaml").is_file():
        raise ForgeError("Recipe release is missing spack_repo/jforge/repo.yaml.")
    descriptor = yaml.safe_load((repo / "repo.yaml").read_text())
    if descriptor != {"repo": {"namespace": "jforge", "api": "v2.2"}}:
        raise ForgeError("Recipe release uses an unsupported Spack repository API.")
    names = sorted(
        p.parent.name.replace("_", "-") for p in repo.glob("packages/*/package.py")
    )
    if not names or value.get("packages") != names:
        raise ForgeError("Recipe release package list does not match its archive.")
    return {**value, "repository": str(repo), "index_url": INDEX_URL}


def register(home, release=None):
    """Use downloaded snapshots, retaining existing user-local extension snapshots."""
    from .package_operations import _inside
    from .spack_backend import root

    file = root(home) / "config/repos.yaml"
    repositories = yaml.safe_load(file.read_text())["repos"]
    value = release or current(home)
    if value:
        repo = _inside(home, Path(value["repository"]))
        if not (repo / "repo.yaml").is_file():
            raise ForgeError(
                "Downloaded recipe snapshot is missing.", hint="jforge apt update"
            )
        repositories.pop("jforge", None)
        write_json(file, {"repos": {"jforge": str(repo), **repositories}})
        return repo
    # Upgrading from a bundled-recipe client can reuse its private snapshot offline.
    if "jforge" in repositories:
        repo = _inside(home, Path(repositories["jforge"]))
        if (repo / "repo.yaml").is_file():
            return repo
    return None


def ensure(home, op):
    if register(home) is None:
        release = prepare(home, op)
        register(home, release)
        write_json(home.root / "apt/recipe-release.json", release)
    return register(home)
