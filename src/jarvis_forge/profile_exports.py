"""Portable profile descriptions lock managed package inputs, never host paths."""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re

import yaml

from .storage import ForgeError, read_json, write_json
from .suite import UniqueLoader


FORMAT = "jforge-profile/1"


def _variants(row):
    return deepcopy(row.get("node", {}).get("parameters", {}))


def package_identity(row):
    return {
        "name": row["name"],
        "version": row["version"],
        "variants": _variants(row),
        "external": bool(row.get("external")),
    }


def compare(expected, actual):
    """Native DAG hashes and external prefixes deliberately vary across hosts."""
    wanted = {row["name"]: row for row in expected if not row.get("external")}
    managed = [row for row in actual if not row.get("external")]
    found = {row["name"]: package_identity(row) for row in managed}
    if len(found) != len(managed):
        raise ForgeError(
            "The resolved profile contains multiple managed instances of a package name."
        )
    differences = []
    for name in sorted(wanted.keys() | found.keys()):
        if name not in wanted:
            differences.append({"name": name, "expected": None, "actual": found[name]})
        elif name not in found:
            differences.append({"name": name, "expected": wanted[name], "actual": None})
        elif any(
            wanted[name].get(key, {}) != found[name].get(key, {})
            for key in ("version", "variants")
        ):
            differences.append(
                {"name": name, "expected": wanted[name], "actual": found[name]}
            )
    if differences:
        message = (
            "Profile package versions or variants differ from the exported plan:\n"
            + "\n".join(
                f"  {row['name']}: expected {json.dumps(row['expected'], sort_keys=True)}; actual {json.dumps(row['actual'], sort_keys=True)}"
                for row in differences
            )
        )
        raise ForgeError(
            message,
            code=1,
            hint="No package installation was started. Use a compatible preset or update the exported profile.",
        )


def constraints(rows):
    """Build only versions and variants stated by the export, using native syntax."""
    output = {}
    for row in rows:
        if row.get("external"):
            continue
        text = "@=" + row["version"]
        for key, value in sorted(row.get("variants", {}).items()):
            if key in {"cflags", "cxxflags", "fflags", "cppflags", "ldflags", "ldlibs"}:
                if value:
                    text += " " + key + "=" + json.dumps(" ".join(value))
            elif isinstance(value, bool):
                text += " " + ("+" if value else "~") + key
            elif isinstance(value, (list, tuple)):
                if value:
                    text += " " + key + "=" + ",".join(map(str, value))
            else:
                text += " " + key + "=" + str(value)
        output[row["name"]] = text
    return output


def _app_bundle(base, app):
    from .builds import artifact

    manifest = artifact(base, app["artifact_id"])
    graph = read_json(base.root / "runs" / manifest["run_id"] / "graph.json")
    node = graph["nodes"][manifest["recipe"]]
    config = deepcopy(node["config"])
    config["parameters"] = deepcopy(node["parameters"])
    if any("path" in source for source in config.get("sources", {}).values()):
        raise ForgeError(
            f"App {app['name']} uses local sources and cannot be reproduced from a profile export.",
            hint="Publish a source archive or a build recipe with a verified source URL before exporting.",
        )
    if config.get("environment", {}).get("shell") or config.get("environment", {}).get(
        "name"
    ):
        raise ForgeError(
            f"App {app['name']} depends on a local shell environment; provide a portable recipe first."
        )
    scripts = []
    text = json.dumps(config)
    for resource in node.get("card_sources", []):
        if not {"path", "sha256", "content"}.issubset(resource):
            continue
        scripts.append({key: resource[key] for key in ("path", "sha256", "content")})
        if "script_id" in resource:
            text = text.replace(
                "${scripts." + resource["script_id"] + "}",
                "${build_dir}/" + resource["path"],
            )
    config = json.loads(text)
    # Dependencies are resolved by app name in the destination profile.
    for name, child in node.get("requirements", {}).items():
        config["requires"][name]["build"] = graph["nodes"][child]["config"][
            "id"
        ].removesuffix("-build")
        config["requires"][name].pop("artifact", None)
    if scripts:
        config["resources"] = [
            {key: row[key] for key in ("path", "sha256")} for row in scripts
        ]
    body = yaml.safe_dump(config, sort_keys=False)
    return {
        "text": body,
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
        "files": scripts,
    }


def export(base, name):
    from .build_apps import reading
    from . import unified_profiles as profiles

    scope = profiles._scoped_home(base, "profiles", name)
    if not profiles.record(scope):
        raise ForgeError(f"No profile named {name}.")
    with reading(scope):
        return _export(base, name)


def _export(base, name):
    from . import unified_profiles as profiles

    profile = profiles._scoped_home(base, "profiles", name)
    data = profiles.record(profile)
    if not data:
        raise ForgeError(f"No profile named {name}.")
    profiles.verify(profile, data)
    platform = profiles.platform_record(base, data["platform"])
    state = read_json(profile.root / "packages.json")[data["backend"]]
    if data["backend"] == "spack":
        from .spack_backend import locked_packages

        rows = locked_packages(profile)
    else:
        rows = state["packages"]
    return {
        "schema_version": 1,
        "kind": FORMAT,
        "name": name,
        "platform": {
            "name": data["platform"],
            "preset": platform["preset"],
            "revision": platform["revision"],
            "target": data["target"],
        },
        "python": {"version": data["python"]["version"]},
        "backend": data["backend"],
        "roots": state["roots"],
        "packages": [package_identity(row) for row in rows],
        "apps": [
            {
                "name": app["name"],
                "version": app["version"],
                "recipe": _app_bundle(profiles.base_home(base), app),
            }
            for app in data.get("apps", [])
            if app.get("enabled", True)
        ],
    }


def load(path):
    from .paths import component

    try:
        value = yaml.load(Path(path).read_text(), Loader=UniqueLoader)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read profile export {path}: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("kind") != FORMAT
        or value.get("schema_version") != 1
    ):
        raise ForgeError("Unsupported profile export format.")
    try:
        for name in ("name", "preset"):
            component(value["platform"][name])
        component(value["name"])
        component(value["platform"]["target"])
        if (
            type(value["platform"]["revision"]) is not int
            or value["platform"]["revision"] < 1
        ):
            raise ValueError("platform revision")
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value["python"]["version"]):
            raise ValueError("Python version")
        if value["backend"] not in {"spack", "homebrew"}:
            raise ValueError("backend")
        if not isinstance(value["roots"], list) or any(
            not isinstance(root, str)
            or not root.strip()
            or "\n" in root
            or "\x00" in root
            for root in value["roots"]
        ):
            raise ValueError("roots")
        if not isinstance(value["packages"], list) or not isinstance(
            value["apps"], list
        ):
            raise ValueError("packages/apps")
        seen = set()
        for row in value["packages"]:
            component(row["name"])
            component(row["version"])
            if row["name"] in seen:
                raise ValueError("duplicate package")
            seen.add(row["name"])
            if not isinstance(row.get("external"), bool) or not isinstance(
                row.get("variants"), dict
            ):
                raise ValueError("package identity")
            for key, item in row["variants"].items():
                component(key)
                values = item if isinstance(item, list) else [item]
                if any(
                    not isinstance(part, (str, bool, int, float))
                    or isinstance(part, float)
                    and not math.isfinite(part)
                    or isinstance(part, str)
                    and ("\n" in part or "\x00" in part)
                    for part in values
                ):
                    raise ValueError("variant value")
        seen = set()
        for app in value["apps"]:
            component(app["name"])
            component(app["version"])
            if app["name"] in seen:
                raise ValueError("duplicate app")
            seen.add(app["name"])
            bundle = app["recipe"]
            if hashlib.sha256(bundle["text"].encode()).hexdigest() != bundle["sha256"]:
                raise ValueError("recipe checksum")
            files = set()
            for row in bundle["files"]:
                if row["path"] in files:
                    raise ValueError("duplicate script path")
                files.add(row["path"])
                file = Path(row["path"])
                if (
                    file.is_absolute()
                    or ".." in file.parts
                    or "\\" in row["path"]
                    or file.suffix != ".py"
                ):
                    raise ValueError("script path")
                if hashlib.sha256(row["content"].encode()).hexdigest() != row["sha256"]:
                    raise ValueError("script checksum")
    except (KeyError, TypeError, ValueError, ForgeError) as exc:
        raise ForgeError(f"Invalid profile export: {exc}") from exc
    return value


def reproduce(
    base,
    name,
    path,
    *,
    platform=None,
    preset=None,
    stage=None,
    target=None,
    progress=None,
    confirm=None,
):
    from . import unified_profiles as profiles, spack_backend, homebrew_backend

    document = load(path)
    current = profiles._python()
    if (
        current["version"].split(".")[:2]
        != document["python"]["version"].split(".")[:2]
    ):
        raise ForgeError(
            f"Select Python {'.'.join(document['python']['version'].split('.')[:2])} with pyenv before reproducing this profile."
        )
    definition = profiles.presets().get(document["platform"]["preset"])
    if (
        not definition
        or definition["revision"] != document["platform"]["revision"]
        or definition["backend"] != document["backend"]
    ):
        raise ForgeError("The exported platform preset revision is unavailable.")
    if preset and preset != document["platform"]["preset"]:
        raise ForgeError("--preset differs from the exported platform.")
    if target and target != document["platform"].get("target"):
        raise ForgeError("--target differs from the exported platform.")
    result = profiles.create(
        base,
        name,
        platform=platform or document["platform"]["name"],
        preset=document["platform"]["preset"],
        stage=stage,
        target=target or document["platform"].get("target"),
        progress=progress,
        confirm=confirm,
    )
    scope = profiles._scoped_home(base, "profiles", name)
    try:
        if document["backend"] == "spack":
            spack_backend.transact(
                scope,
                "install",
                document["roots"],
                expected_packages=document["packages"],
                progress=progress,
                confirm=confirm,
            )
        else:
            plan = homebrew_backend.transact(
                scope, "install", document["roots"], dry_run=True
            )
            compare(
                document["packages"],
                plan["packages"] + plan.get("resolved_packages", []),
            )
            homebrew_backend.transact(
                scope, "install", document["roots"], progress=progress, confirm=confirm
            )
        if document["apps"]:
            from . import build_apps

            workspaces = [
                build_apps.import_bundle(base, scope, app) for app in document["apps"]
            ]
            for workspace in workspaces:
                installed = build_apps.install(
                    base,
                    str(workspace),
                    scope=scope,
                    progress=progress,
                    confirm=confirm,
                )
                if installed["status"] != "passed":
                    raise ForgeError(
                        "Reproduced app failed: " + str(workspace),
                        code=1,
                        hint=installed.get("error"),
                    )
        profiles.sync_definition(scope)
        return {**result, "reproduced_from": str(Path(path).resolve())}
    except BaseException:
        data = profiles.record(scope)
        data["status"] = "failed"
        write_json(scope.root / "metadata.json", data)
        raise
