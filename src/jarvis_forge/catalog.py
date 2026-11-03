"""Recipe catalogs and explicit version constraints for user-local packages."""

from __future__ import annotations

from importlib.resources import files
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version
import yaml

from .downloads import fetch
from .storage import ForgeError, ForgeHome, digest, now, read_json, write_json
from .suite import UniqueLoader


def requirement(text: str) -> Requirement:
    value = re.sub(r"\s*\(([^()]+)\)\s*$", r"\1", text.strip())
    value = re.sub(r"(?<=[A-Za-z0-9_])=(?!=)", "==", value)
    try:
        req = Requirement(value)
    except InvalidRequirement as exc:
        raise ForgeError(f"Invalid package constraint: {text!r}") from exc
    if req.extras or req.marker or req.url or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", req.name):
        raise ForgeError(f"Unsupported package constraint: {text!r}")
    return req


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
    subdir = Path(recipe["source"]["subdir"])
    if subdir.is_absolute() or ".." in subdir.parts:
        raise ForgeError(f"Invalid source subdirectory in {origin}.")
    for dep in recipe.get("depends", []):
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
        def solve(pending, selected, constraints):
            if not pending:
                return selected
            req, *rest = pending
            limits = {key: list(value) for key, value in constraints.items()}
            limits.setdefault(req.name, []).append(req.specifier)
            matches = lambda v: all(v in spec for spec in limits[req.name])
            if req.name in selected:
                return solve(rest, selected, limits) if matches(Version(selected[req.name]["version"])) else None
            choices = [item["recipe"] for (name, _), item in self.items.items()
                       if name == req.name and matches(Version(item["recipe"]["version"]))]
            for choice in sorted(choices, key=lambda r: Version(r["version"]), reverse=True):
                found = solve([requirement(dep) for dep in choice.get("depends", [])] + rest,
                              {**selected, req.name: choice}, limits)
                if found is not None:
                    return found
            return None

        selected = solve([requirement(s) for s in specs], {}, {})
        if selected is None:
            raise ForgeError(f"No compatible recipes satisfy: {', '.join(specs)}", hint="jforge apt search <name>")
        ordered, visiting, done = [], set(), set()

        def visit(name):
            if name in visiting:
                raise ForgeError(f"Recipe dependency cycle involving {name}.")
            if name in done:
                return
            visiting.add(name)
            for spec in selected[name].get("depends", []):
                visit(requirement(spec).name)
            visiting.remove(name)
            done.add(name)
            ordered.append(selected[name])

        for name in selected:
            visit(name)
        return ordered


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
