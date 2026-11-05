"""Independent, checksum-pinned build Release catalog (never executes YAML)."""

from pathlib import Path
import hashlib
import json
import re
import shutil

from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.version import Version

from . import __version__
from .downloads import fetch
from .storage import ForgeError, digest, identifier, new_id, read_json, write_json

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


def entries(home):
    path = home.root / "builds/catalog/index.json"
    return read_json(path).get("builds", []) if path.is_file() else []


def libraries(home):
    path = home.root / "builds/catalog/index.json"
    return read_json(path).get("libraries", []) if path.is_file() else []


def update(home, url=INDEX_URL):
    from .builds import load
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
            import yaml
            template_file.write_text(yaml.safe_dump(template))
            load(template_file)
            destination = home.root / "builds/recipes" / row["sha256"] / "library.yaml"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and digest(destination) != row["sha256"]:
                raise ForgeError(f"Modified library cache: {destination}")
            shutil.copyfile(asset, destination)
            library_files[name] = destination
        for row in index["builds"]:
            name = identifier(row.get("id", ""))
            if name in seen or not re.fullmatch("[a-f0-9]{64}", str(row.get("sha256", ""))):
                raise ForgeError("Duplicate build ID or invalid SHA-256 in Release index.")
            seen.add(name)
            if not isinstance(row.get("revision"), str) or not row["revision"]:
                raise ForgeError("Build Release entry requires a revision.")
            bundle = temporary / ("bundle-" + name)
            bundle.mkdir()
            asset = bundle / "build.yaml"
            fetch(row.get("url", ""), asset, temporary / name)
            if digest(asset) != row["sha256"]:
                raise ForgeError(f"Build SHA-256 mismatch: {name}", code=1)
            for item in resources(row):
                script = bundle / item["path"]
                script.parent.mkdir(parents=True, exist_ok=True)
                fetch(item["url"], script, temporary / ("script-" + item["sha256"]))
                if digest(script) != item["sha256"]:
                    raise ForgeError(f"Companion script SHA-256 mismatch: {item['path']}", code=1)
            loaded = load(asset, home=home, library_files=library_files)
            if loaded["config"]["id"] != name:
                raise ForgeError(f"Build ID mismatch: {name}")
            declared = {v["path"]: v["sha256"] for v in resources(row)}
            required = {v["path"]: v["sha256"] for v in loaded["card_sources"] if "script_id" in v}
            if declared != required:
                raise ForgeError(f"Companion files do not match build scripts: {name}")
            destination = home.root / "builds/recipes" / snapshot_id(row) / "build.yaml"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and digest(destination) != row["sha256"]:
                raise ForgeError(f"Modified build cache: {destination}")
            shutil.copyfile(asset, destination)
            for item in resources(row):
                target = destination.parent / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and digest(target) != item["sha256"]:
                    raise ForgeError(f"Modified companion script cache: {target}")
                shutil.copyfile(bundle / item["path"], target)
        write_json(home.root / "builds/catalog/index.json", index)
        return {"builds": index["builds"], "source": url}
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
    for row in entries(home):
        if row["id"] == reference:
            path = home.root / "builds/recipes" / snapshot_id(row) / "build.yaml"
            if not path.is_file() or digest(path) != row["sha256"]:
                raise ForgeError(f"Missing or modified build snapshot: {reference}")
            for item in resources(row):
                script = path.parent / item["path"]
                if not script.is_file() or digest(script) != item["sha256"]:
                    raise ForgeError(f"Missing or modified companion script: {item['path']}")
            return path
    raise ForgeError(f"Build file or cached ID not found: {reference}",
                     hint="jforge build update; or supply a local YAML file")


def acquire(home, reference):
    """Fetch an uncached Release ID for explicit fetch/run actions; local paths stay local."""
    candidate = Path(reference).expanduser()
    if candidate.exists() or any(row["id"] == reference for row in entries(home)):
        return resolve(home, reference)
    if "/" in reference or "\\" in reference or candidate.suffix in {".yaml", ".yml"}:
        return resolve(home, reference)
    identifier(reference)
    update(home)
    return resolve(home, reference)
