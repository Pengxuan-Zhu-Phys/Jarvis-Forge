"""Recipe catalogs and explicit version constraints for user-local packages."""

from __future__ import annotations

from importlib.resources import files
from dataclasses import dataclass
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version
import yaml

from .downloads import fetch
from .storage import ForgeError, ForgeHome, digest, now, package_name, read_json, write_json
from .suite import UniqueLoader



@dataclass(frozen=True)
class DebianSpecifier:
    relation: str
    version: str

    def __str__(self):
        return ("==" if self.relation == "=" else self.relation) + self.version

    def __contains__(self, value):
        from .dependencies import version_matches
        return version_matches(str(value), self.relation, self.version)


@dataclass(frozen=True)
class PackageRequirement:
    """Forge-owned identity and version metadata; never mutate parser objects."""
    name: str
    specifier: SpecifierSet | DebianSpecifier
    forge_debian_constraint: tuple[str, str] | None = None
    forge_exact_version: str | None = None


def requirement(text: str) -> PackageRequirement:
    value = re.sub(r"\s*\(([^()]+)\)\s*$", r"\1", text.strip())
    match = re.fullmatch(r"([a-z0-9][a-z0-9+._-]{0,127})(.*)", value)
    if not match:
        raise ForgeError(f"Invalid package constraint: {text!r}")
    name, constraint = match.groups()
    constraint = constraint.strip()
    if constraint and not constraint.startswith(("=", "<", ">", "!", "~", "[", ";", "@")):
        raise ForgeError(f"Invalid package constraint: {text!r}")
    if constraint.startswith("=") and not constraint.startswith("=="):
        constraint = "=" + constraint
    try:
        # Debian names can contain '+', which PEP 508 distribution names omit.
        # Reuse its version/marker parser with a placeholder, then retain the
        # validated Forge/Debian package identity for dependency resolution.
        req = Requirement("forge-package" + constraint)
    except InvalidRequirement as exc:
        debian = re.fullmatch(r"(<<|<=|==|=|>=|>>|<|>)\s*([0-9][A-Za-z0-9.+:~\-]*)", constraint)
        if debian:
            relation = {"==": "=", "<": "<<", ">": ">>"}.get(debian[1], debian[1])
            from debian.debian_support import Version as DebianVersion
            try:
                DebianVersion(debian[2])
            except ValueError:
                raise ForgeError(f"Invalid Debian version in package constraint: {text!r}") from exc
            return PackageRequirement(package_name(name), DebianSpecifier(relation, debian[2]))
        raise ForgeError(f"Invalid package constraint: {text!r}") from exc
    if req.extras or req.marker or req.url:
        raise ForgeError(f"Unsupported package constraint: {text!r}")
    debian_constraint = None
    exact_version = None
    native_version = re.fullmatch(r"(<<|<=|==|=|>=|>>|<|>)\s*([0-9][A-Za-z0-9.+:~\-]*)", constraint)
    if native_version and any(c in native_version[2] for c in "-:~"):
        debian_constraint = ({"==": "=", "<": "<<", ">": ">>"}.get(native_version[1], native_version[1]), native_version[2])
    if constraint.startswith("==") and "*" not in constraint:
        exact_version = constraint[2:].strip()
    return PackageRequirement(package_name(name), req.specifier, debian_constraint, exact_version)


def validate_recipe(recipe: dict, origin: str) -> dict:
    schema = json.loads(files("jarvis_forge").joinpath("schemas/recipe.schema.json").read_text())
    error = next(iter(Draft202012Validator(schema).iter_errors(recipe)), None)
    if error:
        raise ForgeError(f"Invalid recipe in {origin}: {error.message}")
    if recipe.get("provenance", {}).get("status") == "draft":
        raise ForgeError(f"Recipe is a draft and needs a build adapter: {origin}")
    if recipe["source"].get("patch_archive") and "patch" not in recipe.get("requires_tools", []):
        raise ForgeError(f"Recipes with patch archives must require the patch tool: {origin}")
    try:
        Version(recipe["version"])
    except InvalidVersion as exc:
        raise ForgeError(f"Invalid package version in {origin}: {recipe['version']}") from exc
    for artifact in recipe.get("artifacts", []):
        path = Path(artifact)
        if path.is_absolute() or ".." in path.parts or artifact == ".":
            raise ForgeError(f"Invalid installed artifact path in {origin}: {artifact}")
    for section in recipe.get("environment", {}).values():
        if any(name.startswith("_JFORGE_") for name in section):
            raise ForgeError(f"Reserved Forge activation variable in {origin}.")
    component_names = [c["name"].lower() for c in recipe["source"].get("components", [])]
    if "debian" in component_names or len(component_names) != len(set(component_names)):
        raise ForgeError(f"Duplicate or reserved source component name in {origin}.")
    subdir = Path(recipe["source"]["subdir"])
    if subdir.is_absolute() or ".." in subdir.parts:
        raise ForgeError(f"Invalid source subdirectory in {origin}.")
    for field in ("depends", "build_depends", "run_depends"):
        for dep in recipe.get(field, []):
            requirement(dep)
    return recipe


def read_recipes(text: str, origin: str) -> list[dict]:
    try:
        data = yaml.load(text, Loader=UniqueLoader)
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ForgeError(f"Cannot read recipe source {origin}: {exc}") from exc
    if isinstance(data, dict) and "packages" in data:
        if set(data) != {"schema_version", "packages"} or data["schema_version"] != 1 or not isinstance(data["packages"], list):
            raise ForgeError(f"Invalid recipe index: {origin}")
        recipes = data["packages"]
    else:
        recipes = [data]
    return [validate_recipe(item, origin) for item in recipes]


class Catalog:
    def __init__(self, home: ForgeHome):
        self.home = home
        self.items = {}
        for path in sorted(files("jarvis_forge").joinpath("recipes").iterdir(), key=lambda p: p.name):
            if path.name.endswith(".yaml"):
                self._add(read_recipes(path.read_text(), f"bundled:{path.name}"), f"bundled:{path.name}")
        cached = home.root / "cache/apt/catalog.json"
        if cached.exists():
            record = read_json(cached)
            if record.get("schema_version") != 1 or not isinstance(record.get("packages"), list):
                raise ForgeError(f"Invalid cached catalog: {cached}")
            for item in record["packages"]:
                if not isinstance(item, dict) or not isinstance(item.get("origin"), str):
                    raise ForgeError(f"Invalid cached recipe entry: {cached}")
                self._add([validate_recipe(item.get("recipe"), item["origin"])], item["origin"])
        official = home.root / "cache/apt/official.json"
        if official.exists():
            record = read_json(official)
            if record.get("schema_version") != 1 or not isinstance(record.get("packages"), list):
                raise ForgeError(f"Invalid official-source catalog: {official}")
            for item in record["packages"]:
                self._add([validate_recipe(item["recipe"], item["origin"])], item["origin"])
        for path in sorted((home.root / "recipes").glob("*.yaml")):
            self._add(read_recipes(path.read_text(encoding="utf-8"), str(path)), str(path))

    def _add(self, recipes, origin):
        for recipe in recipes:
            self.items[(recipe["name"], recipe["version"])] = {"recipe": recipe, "origin": origin}

    def list(self, query="") -> list[dict]:
        return [dict(item["recipe"], origin=item["origin"]) for _, item in sorted(self.items.items())
                if query.lower() in (item["recipe"]["name"] + " " + item["recipe"]["description"]).lower()]

    def resolve(self, specs: list[str]) -> list[dict]:
        from .dependencies import resolve
        return resolve([item["recipe"] for item in self.items.values()], specs)


def source_config(home: ForgeHome) -> dict:
    path = home.root / "apt/sources.json"
    config = read_json(path) if path.exists() else {"schema_version": 1, "sources": []}
    if (config.get("schema_version") != 1 or not isinstance(config.get("sources"), list)
            or not all(isinstance(s, str) for s in config["sources"])):
        raise ForgeError(f"Invalid source configuration: {path}")
    return config


def update_catalog(home: ForgeHome, operation: Path) -> dict:
    entries, sources = [], []
    for index, source in enumerate(source_config(home)["sources"]):
        if source.startswith("https://"):
            path = operation / f"source-{index}.yaml"
            fetch(source, path, operation / f"source-{index}")
            paths = [path]
        else:
            location = Path(source).expanduser()
            if not location.is_absolute():
                raise ForgeError(f"Local recipe sources must be absolute paths: {source}")
            paths = sorted(location.glob("*.yaml")) if location.is_dir() else [location]
            if not location.exists():
                raise ForgeError(f"Recipe source does not exist: {source}")
        for path in paths:
            for recipe in read_recipes(path.read_text(encoding="utf-8"), source):
                entries.append({"recipe": recipe, "origin": source})
            sources.append({"source": source, "path": str(path), "sha256": digest(path)})
    result = {"schema_version": 1, "updated_at": now(), "packages": entries, "sources": sources}
    write_json(home.root / "cache/apt/catalog.json", result)
    return {"sources": sources, "available": len(Catalog(home).list()), "updated_at": result["updated_at"]}
