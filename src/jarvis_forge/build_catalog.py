"""Independent, checksum-pinned build Release catalog (never executes YAML)."""

from pathlib import Path
import hashlib
import json
import re
import shutil
import sys
from tempfile import NamedTemporaryFile

import yaml
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.version import Version

from . import __version__
from .downloads import fetch
from .storage import ForgeError, digest, identifier, new_id, read_json, write_json
from .suite import UniqueLoader

INDEX_URL = "https://github.com/Pengxuan-Zhu-Phys/Jarvis-Forge/releases/download/builds/builds.json"


def resources(row):
    values = row.get("files", [])
    if not isinstance(values, list):
        raise ForgeError("Build files must be a list.")
    seen = set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {"path", "url", "sha256"}:
            raise ForgeError("Companion files require path, url and sha256.")
        path = item["path"]
        if (not isinstance(path, str) or not path or "\\" in path or Path(path).is_absolute()
                or ".." in Path(path).parts or Path(path).suffix != ".py" or str(Path(path)) in seen):
            raise ForgeError("Companion scripts must have distinct relative Python file paths.")
        if not re.fullmatch("[a-f0-9]{64}", str(item["sha256"])):
            raise ForgeError("Invalid companion script SHA-256.")
        seen.add(str(Path(path)))
    return values


def snapshot_id(row):
    values = resources(row)
    if not values:
        return row["sha256"]
    content = {"yaml": row["sha256"], "files": sorted((v["path"], v["sha256"]) for v in values)}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def copy_bundle(source, output, *, home=None):
    """Copy an editable YAML and its scripts; preflight every collision before writing."""
    from .builds import load
    loaded = load(source, home=home)
    output = output.expanduser().resolve()
    copies = [(output, source.read_bytes())]
    for item in loaded["card_sources"]:
        if "script_id" in item:
            destination = output.parent / item["path"]
            if not destination.resolve().is_relative_to(output.parent):
                raise ForgeError("Companion destination escapes the output directory.")
            copies.append((destination, item["content"].encode("utf-8")))
    for destination, content in copies:
        if destination.exists() and (destination == output or not destination.is_file() or destination.read_bytes() != content):
            raise ForgeError(f"Refusing to overwrite existing file: {destination}")
    for destination, content in copies:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            with destination.open("xb") as stream:
                stream.write(content)
    return output


def _index(home):
    path = home.root / "builds/catalog/index.json"
    return read_json(path) if path.is_file() else {}


def entries(home):
    return _index(home).get("builds", [])


def libraries(home):
    return _index(home).get("libraries", [])


def _cache_asset(source, destination, sha256, kind):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if digest(destination) != sha256:
            raise ForgeError(f"Modified {kind} cache: {destination}")
        return
    with NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        staged = Path(stream.name)
    try:
        shutil.copyfile(source, staged)
        if digest(staged) != sha256:
            raise ForgeError(f"{kind.capitalize()} cache copy SHA-256 mismatch: {destination}")
        staged.replace(destination)
    finally:
        staged.unlink(missing_ok=True)


def _platforms(value, name):
    if not isinstance(value, list) or not value or any(item not in ("linux", "darwin") for item in value):
        raise ForgeError(f"Invalid supported platforms for build {name}.")
    return value


def update(home, url=INDEX_URL, *, platform=sys.platform):
    from .builds import host_requirements, load
    home.require_initialized()
    temporary = home.root / "builds/catalog" / new_id("download")
    temporary.mkdir(parents=True)
    try:
        fetch(url, temporary / "index.json", temporary / "index")
        index = read_json(temporary / "index.json")
        try:
            compatible = Version(__version__) in SpecifierSet(index.get("requires_forge", ""))
        except (InvalidSpecifier, TypeError):
            compatible = False
        if index.get("schema_version") != 1 or not compatible or not isinstance(index.get("builds"), list):
            raise ForgeError("Invalid or incompatible build Release index.")
        seen = set()
        selected, excluded = [], {}
        library_files = {}
        from .build_cards import read_library
        if not isinstance(index.get("libraries", []), list):
            raise ForgeError("Invalid build library catalog.")
        for row in index.get("libraries", []):
            name = identifier(row.get("id", ""))
            if name in library_files or not re.fullmatch("[a-f0-9]{64}", str(row.get("sha256", ""))):
                raise ForgeError("Duplicate library ID or invalid library SHA-256.")
            asset = temporary / ("library-" + name + ".yaml")
            fetch(row.get("url", ""), asset, temporary / ("library-" + name))
            if digest(asset) != row["sha256"]:
                raise ForgeError(f"Library SHA-256 mismatch: {name}", code=1)
            template = read_library(asset)["build"]
            template_file = temporary / ("template-" + name + ".yaml")
            template_file.write_text(yaml.safe_dump(template))
            load(template_file)
            destination = home.root / "builds/recipes" / row["sha256"] / "library.yaml"
            _cache_asset(asset, destination, row["sha256"], "library")
            library_files[name] = destination
        for row in index["builds"]:
            name = identifier(row.get("id", ""))
            if name in seen or not re.fullmatch("[a-f0-9]{64}", str(row.get("sha256", ""))):
                raise ForgeError("Duplicate build ID or invalid SHA-256 in Release index.")
            seen.add(name)
            if not isinstance(row.get("revision"), str) or not row["revision"]:
                raise ForgeError("Build Release entry requires a revision.")
            companions = resources(row)
            declared_platforms = _platforms(row["platforms"], name) if "platforms" in row else None
            if platform and declared_platforms and platform not in declared_platforms:
                excluded[name] = declared_platforms
                continue
            bundle = temporary / ("bundle-" + name)
            bundle.mkdir()
            asset = bundle / "build.yaml"
            fetch(row.get("url", ""), asset, temporary / name)
            if digest(asset) != row["sha256"]:
                raise ForgeError(f"Build SHA-256 mismatch: {name}", code=1)
            try:
                raw = yaml.load(asset.read_text(), Loader=UniqueLoader)
            except (ValueError, yaml.YAMLError) as exc:
                raise ForgeError(f"Cannot read build {name}: {exc}") from exc
            platforms = host_requirements(raw).get("platforms") if isinstance(raw, dict) else None
            if platforms:
                _platforms(platforms, name)
            if declared_platforms and platforms != declared_platforms:
                raise ForgeError(f"Build platform metadata does not match the recipe: {name}")
            if platform and platforms and platform not in platforms:
                excluded[name] = platforms
                continue
            for item in companions:
                script = bundle / item["path"]
                script.parent.mkdir(parents=True, exist_ok=True)
                fetch(item["url"], script, temporary / ("script-" + item["sha256"]))
                if digest(script) != item["sha256"]:
                    raise ForgeError(f"Companion script SHA-256 mismatch: {item['path']}", code=1)
            loaded = load(asset, home=home, library_files=library_files)
            if loaded["config"]["id"] != name:
                raise ForgeError(f"Build ID mismatch: {name}")
            platforms = loaded["config"].get("platforms")
            if platform and platforms and platform not in platforms:
                excluded[name] = platforms
                continue
            declared = {v["path"]: v["sha256"] for v in companions}
            required = {v["path"]: v["sha256"] for v in loaded["card_sources"] if "script_id" in v}
            if declared != required:
                raise ForgeError(f"Companion files do not match build scripts: {name}")
            destination = home.root / "builds/recipes" / snapshot_id(row) / "build.yaml"
            _cache_asset(asset, destination, row["sha256"], "build")
            for item in companions:
                target = destination.parent / item["path"]
                _cache_asset(bundle / item["path"], target, item["sha256"], "companion script")
            selected.append(row)
        write_json(home.root / "builds/catalog/index.json",
                   {**index, "builds": selected, "platform": platform, "excluded_builds": excluded})
        return {"builds": selected, "source": url, "platform": platform, "excluded_builds": excluded}
    finally:
        shutil.rmtree(temporary)


def resolve(home, reference, parent=None):
    candidate = Path(reference).expanduser()
    if not candidate.is_absolute() and parent:
        candidate = parent / candidate
    if candidate.is_dir():
        from .build_workspace import locate
        if locate(candidate):
            candidate = candidate / "build.yaml"
        else:
            raise ForgeError(f"Not a build workspace: {candidate}")
    if candidate.is_file():
        return candidate.resolve()
    index = _index(home)
    for row in index.get("builds", []):
        if row["id"] == reference:
            path = home.root / "builds/recipes" / snapshot_id(row) / "build.yaml"
            if not path.is_file() or digest(path) != row["sha256"]:
                raise ForgeError(f"Missing or modified build snapshot: {reference}")
            for item in resources(row):
                script = path.parent / item["path"]
                if not script.is_file() or digest(script) != item["sha256"]:
                    raise ForgeError(f"Missing or modified companion script: {item['path']}")
            return path
    platforms = index.get("excluded_builds", {}).get(str(reference))
    if platforms and index.get("platform") == sys.platform:
        supported = ", ".join(platforms)
        raise ForgeError(f"Build {reference} requires platform {supported}. Current platform is {sys.platform}.",
                         hint=f"Run this build on a supported host ({supported}).")
    raise ForgeError(f"Build file or cached ID not found: {reference}",
                     hint="jforge build update; or supply a local YAML file")


def acquire(home, reference):
    """Fetch an uncached Release ID for explicit fetch/run actions; local paths stay local."""
    candidate = Path(reference).expanduser()
    index = _index(home)
    if (candidate.exists() or any(row["id"] == reference for row in index.get("builds", []))
            or index.get("platform") == sys.platform and reference in index.get("excluded_builds", {})):
        return resolve(home, reference)
    if "/" in reference or "\\" in reference or candidate.suffix in {".yaml", ".yml"}:
        return resolve(home, reference)
    identifier(reference)
    update(home)
    return resolve(home, reference)
