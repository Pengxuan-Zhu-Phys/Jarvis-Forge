"""Load a bench card and expand its cases, settings and variables.

Design: Jarvis-Books Docs/Jarvis-Forge/BENCH_DESIGN.md.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import shlex
from pathlib import Path

import yaml

from . import bench_project
from .storage import ForgeError, digest, identifier, package_name
from .suite import UniqueLoader


RESERVED_STEPS = {"bench", "case", "settings", "software", "seed", "repeat", "resources", "task", "fetch"}
TOP_LEVEL = {"kind", "name", "subject", "environment", "software", "settings", "cases", "steps",
             "profiles", "default_profile", "coverage", "report", "fetch"}
STEP_FIELDS = {"name", "after", "software", "repeat", "seed", "reuse", "files", "templates", "commands",
               "compare", "outputs", "verify", "clean", "env", "resources", "retries"}
SOFTWARE_FIELDS = {"build", "source", "set", "rebuild", "version", "features", "artifact", "package", "entrypoint"}
PROFILE_FIELDS = {"cases", "tags", "settings", "data", "require"}
COMPARE_FIELDS = {"actual", "select", "value", "error", "expected", "tolerance"}
TOLERANCE_FIELDS = {"abs", "rel", "sigma", "max_ratio", "min_ratio"}
RESOURCE_FIELDS = {"cores", "memory", "time", "gpus"}
_SEGMENT = r"[A-Za-z_][A-Za-z0-9_-]*"
_VAR = re.compile(rf"\$\{{({_SEGMENT}(?:\.{_SEGMENT})*)\}}")
_WHOLE = re.compile(rf"^\$\{{({_SEGMENT}(?:\.{_SEGMENT})*)\}}$")
_TIME = re.compile(r"^(\d+)([smhd])$")
PATH_VARIABLES = {"bench.dir", "project.dir", "case.dir", "task.dir", "seed", "repeat.index"}


def load(path):
    """Read one bench card. Relative paths stay unresolved until a case is selected."""
    path = Path(path).expanduser().resolve()
    try:
        text = path.read_text(encoding="utf-8")
        raw = yaml.load(text, Loader=UniqueLoader)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read bench card {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ForgeError(f"Expected a bench card mapping: {path}")
    unknown = set(raw) - TOP_LEVEL
    if unknown:
        raise ForgeError("Unknown bench card fields: " + ", ".join(sorted(unknown)))
    if raw.get("kind") != "bench":
        raise ForgeError("Bench card kind must be bench.")
    name = identifier(raw.get("name", ""))
    card = {
        "path": str(path),
        "dir": str(path.parent),
        "project": bench_project.discover(path.parent),
        "sha256": digest(path),
        "name": name,
        "subject": _optional_name(raw.get("subject"), "subject"),
        "environment": _optional_string(raw.get("environment"), "environment"),
        "software": _software(raw.get("software")),
        "settings": _scalars(raw.get("settings") or {}, "settings"),
        "cases": raw.get("cases"),
        "steps": _steps(raw.get("steps")),
        "profiles": _profiles(raw.get("profiles") or {}),
        "default_profile": _optional_name(raw.get("default_profile"), "default_profile"),
        "coverage": raw.get("coverage"),
        "report": raw.get("report"),
        "fetch": raw.get("fetch") or [],
    }
    if card["subject"] and card["subject"] not in card["software"]:
        raise ForgeError(f"subject {card['subject']!r} is not a software item.")
    if card["default_profile"] and card["default_profile"] not in card["profiles"]:
        raise ForgeError(f"Unknown default profile: {card['default_profile']}")
    known_software = set(card["software"])
    for step in card["steps"]:
        if card["project"] and step["name"] == "project":
            raise ForgeError("A bench project reserves the step name 'project'.")
        missing = [item for item in step["software"] if item not in known_software]
        if missing:
            raise ForgeError(f"{step['name']} uses unknown software: " + ", ".join(missing))
    for profile in card["profiles"].values():
        data = profile.get("data")
        if data and data not in card["profiles"]:
            raise ForgeError(f"Profile data refers to an unknown profile: {data}")
    _coverage(card["coverage"])
    _report(card["report"])
    _fetch(card["fetch"])
    if not isinstance(card["cases"], (str, list, dict)):
        raise ForgeError("cases is required.")
    return card


def profile_of(card, name):
    if name is None:
        name = card.get("default_profile")
    if name is None:
        return {"name": "default", "cases": None, "tags": None, "settings": {}, "data": None, "require": None}
    if name not in card["profiles"]:
        raise ForgeError(f"Unknown profile: {name}")
    return {"name": name, **card["profiles"][name]}


def select_cases(card, *, profile, cases=(), tags=()):
    """Cases selected by the profile, then narrowed by --case and --tag."""
    selected = profile_of(card, profile)
    found = _discover(card)
    if selected["cases"] or selected["tags"]:
        allowed = set()
        for pattern in selected["cases"] or []:
            allowed.update(item["name"] for item in found if _match_name(item["name"], pattern))
        for item in found:
            if selected["tags"] and set(item["tags"]) & set(selected["tags"]):
                allowed.add(item["name"])
        found = [item for item in found if item["name"] in allowed]
    if cases:
        found = [item for item in found if any(_match_name(item["name"], pattern) for pattern in cases)]
    if tags:
        found = [item for item in found if set(item["tags"]) & set(tags)]
    if not found:
        raise ForgeError("No cases selected.")
    merged = []
    for item in found:
        if item.get("skip"):
            merged.append(item)
            continue
        settings = {**card["settings"], **item.get("settings", {}), **selected["settings"]}
        merged.append({**item, "settings": settings})
    return selected, merged


def check_references(card, cases, software=None):
    """Reference files named by compare steps must already have the right shape."""
    for case in cases:
        if case.get("skip"):
            continue
        for step in card["steps"]:
            compare = step.get("compare")
            if not compare:
                continue
            bindings = bindings_for(card, case, step, index=0, count=1, task=None, upstream={},
                                    software=software or {})
            expected = expand(compare["expected"], bindings, where=f"{case['name']} {step['name']} expected")
            if expected == "baseline":
                continue
            if not isinstance(expected, str):
                raise ForgeError(f"{step['name']} expected must be a file path.")
            path = Path(expected)
            if not path.is_file():
                raise ForgeError(f"Reference file missing: {path}")
            _reference(path)


def used_variables(step, bindings=None):
    """Placeholders that enter the input record. repeat and resources do not.

    Template paths are scanned, and so is the template text. A setting that
    appears only inside a template, such as ``${settings.events}``, is part
    of the result.
    """
    blobs = []
    for key in ("files", "templates", "commands", "env", "outputs", "verify", "compare"):
        blobs.append(step.get(key))
    found = set()

    def visit(value):
        if isinstance(value, str):
            text = value.replace("$${", "")
            found.update(_VAR.findall(text))
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)

    for blob in blobs:
        visit(blob)
    if bindings is not None:
        for raw in step.get("templates") or []:
            visit(_template_text(raw, bindings, step["name"]))
    return {name for name in found if not _excluded(name)}


def _template_text(raw, bindings, step):
    try:
        expanded = expand(raw, bindings, where=f"{step} templates")
    except ForgeError:
        return ""
    if not isinstance(expanded, str):
        return ""
    path = Path(expanded)
    files = []
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = [item for item in sorted(path.rglob("*")) if item.is_file()]
    chunks = []
    for item in files:
        try:
            chunks.append(item.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(chunks)


def recorded_values(step, bindings):
    values = {}
    for name in sorted(used_variables(step, bindings)):
        values[name] = lookup(name, bindings, where=step["name"])
    return values


def expand(value, bindings, *, where):
    """Replace ${…}. A value that is only one placeholder keeps its type."""
    if isinstance(value, list):
        expanded = []
        for item in value:
            result = expand(item, bindings, where=where)
            if isinstance(item, str) and _WHOLE.match(item.replace("$${", "")) and isinstance(result, list):
                expanded.extend(result)
            else:
                expanded.append(result)
        return expanded
    if isinstance(value, dict):
        return {key: expand(item, bindings, where=where) for key, item in value.items()}
    if not isinstance(value, str) or "${" not in value:
        return value
    token = "\0JFORGE_DOLLAR\0"
    text = value.replace("$${", token)
    whole = _WHOLE.match(text)
    if whole and token not in text:
        return lookup(whole.group(1), bindings, where=where)
    def replace(match):
        found = lookup(match.group(1), bindings, where=where)
        if isinstance(found, list):
            return " ".join(shlex.quote(str(item)) for item in found)
        if isinstance(found, dict):
            raise ForgeError(f"Cannot format a mapping in {where}: ${{{match.group(1)}}}")
        return str(found)
    return _VAR.sub(replace, text).replace(token, "${")


def lookup(name, bindings, *, where):
    current = bindings
    for part in name.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ForgeError(f"Unknown variable ${{{name}}} in {where}.")
        current = current[part]
    return current


def referenced_fetch(step, bindings=None):
    """Fetch ids named by ``${fetch.<id>.path}``. The digest is an input; the path is not."""
    blobs = []
    for key in ("files", "templates", "commands", "env", "outputs", "verify", "compare"):
        blobs.append(step.get(key))
    found = set()

    def visit(value):
        if isinstance(value, str):
            found.update(_VAR.findall(value.replace("$${", "")))
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)

    for blob in blobs:
        visit(blob)
    if bindings is not None:
        for raw in step.get("templates") or []:
            visit(_template_text(raw, bindings, step["name"]))
    ids = []
    for name in sorted(found):
        if name.startswith("fetch.") and name.endswith((".path", ".entrypoint")):
            ident = name[len("fetch."):-len(".path")]
            if ident and "." not in ident and ident not in ids:
                ids.append(ident)
    return ids


def bindings_for(card, case, step, *, index, count, task, upstream, software, fetch=None):
    seed = None if step.get("seed") is None else step["seed"] + index
    resources = step.get("resources") or {}
    case_vars = {"name": case["name"], "dir": case.get("dir")}
    for key, value in case.items():
        if key in {"dir", "path", "skip", "tags", "settings"}:
            continue
        if re.fullmatch(_SEGMENT, key):
            case_vars[key] = value
    software_vars = {name: {"path": item["path"], "entrypoint": item.get("entrypoint")}
                     for name, item in software.items()}
    upstream_vars = {}
    for name, item in upstream.items():
        upstream_vars[name] = {"outputs": item.get("outputs", {}), "dirs": item.get("dirs", [])}
    return {
        "bench": {"dir": card["dir"]},
        "project": {"dir": str(card["project"]["root"])} if card.get("project") else {},
        "case": case_vars,
        "settings": case["settings"],
        "seed": seed,
        "repeat": {"index": index, "count": count},
        "resources": resources,
        "software": software_vars,
        "task": {"dir": None if task is None else str(task)},
        "fetch": {name: {"path": item["path"]} for name, item in (fetch or {}).items()},
        **upstream_vars,
    }


def step_repeat(step, bindings):
    if "repeat" not in step:
        return 1
    value = expand(step["repeat"], bindings, where=f"{step['name']} repeat")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ForgeError(f"{step['name']} repeat must be a positive integer.")
    return value


def duration_seconds(value):
    if not isinstance(value, str):
        return None
    match = _TIME.fullmatch(value)
    if not match:
        return None
    number, unit = int(match.group(1)), match.group(2)
    return number * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def definition(step):
    payload = {key: step.get(key) for key in ("commands", "outputs", "env", "files", "templates")}
    digest_hex = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return "sha256:" + digest_hex


def _excluded(name):
    if name in PATH_VARIABLES or name.startswith("resources.") or name.startswith("fetch."):
        return True
    if name.startswith("software.") and name.endswith((".path", ".entrypoint")):
        return True
    if ".outputs." in name or name.endswith(".dirs"):
        return True
    return False


def card_root(card):
    return card["project"]["root"] if card.get("project") else Path(card["dir"])


def command_argv(card, command):
    root = card_root(card)
    argv = []
    for part in command:
        path = Path(part)
        if not path.is_absolute() and "/" in part:
            path = bench_project.inside(root, part) if card.get("project") else root / path
            part = str(path)
        argv.append(part)
    return argv


def _discover(card):
    spec = card["cases"]
    base = card_root(card)
    if isinstance(spec, dict):
        return _matrix(spec)
    patterns = [spec] if isinstance(spec, str) else list(spec)
    if not patterns or not all(isinstance(item, str) and item for item in patterns):
        raise ForgeError("cases must be a glob, a list of globs, or a matrix.")
    found = []
    seen = set()
    for pattern in patterns:
        if pattern.startswith("/") or ".." in Path(pattern).parts:
            raise ForgeError(f"Case pattern must stay under its input root: {pattern}")
        matches = sorted(path for path in base.glob(pattern) if path.is_dir())
        if not matches:
            raise ForgeError(f"No case directories match {pattern}.")
        for directory in matches:
            if card.get("project"):
                bench_project.inside(base, str(directory.relative_to(base) / "case.yaml"))
            name = directory.name
            if name in seen:
                raise ForgeError(f"Duplicated case name: {name}")
            seen.add(name)
            identifier(name)
            found.append(_case_file(directory, name))
    return found


def _case_file(directory, name):
    path = directory / "case.yaml"
    if not path.is_file():
        raise ForgeError(f"Case description missing: {path}")
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read case {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ForgeError(f"Expected a case mapping: {path}")
    if "name" in raw or "dir" in raw:
        raise ForgeError(f"{path} cannot set name or dir; the directory name is the case name.")
    tags = raw.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(item, str) and item for item in tags):
        raise ForgeError(f"{path} tags must be a list of strings.")
    settings = _scalars(raw.get("settings") or {}, f"{name} settings")
    skip = raw.get("skip")
    if skip is not None and not isinstance(skip, str):
        raise ForgeError(f"{path} skip must be a reason string.")
    fields = {key: value for key, value in raw.items() if key not in {"tags", "settings", "skip"}}
    return {"name": name, "dir": str(directory), "path": str(path), "tags": tags,
            "settings": settings, "skip": skip, **fields}


def _matrix(spec):
    unknown = set(spec) - {"matrix", "name"}
    if unknown or not isinstance(spec.get("matrix"), dict) or not isinstance(spec.get("name"), str):
        raise ForgeError("A matrix needs matrix and name.")
    axes = spec["matrix"]
    if not axes:
        raise ForgeError("A matrix needs at least one axis.")
    reserved = {"name", "dir", "path", "tags", "settings", "skip"} & set(axes)
    if reserved:
        raise ForgeError("A matrix axis cannot be named " + ", ".join(sorted(reserved)) + ".")
    names = []
    values = []
    for key, items in axes.items():
        if not re.fullmatch(_SEGMENT, key):
            raise ForgeError(f"Invalid matrix axis: {key}")
        if not isinstance(items, list) or not items:
            raise ForgeError(f"Matrix axis {key} needs a list of values.")
        for item in items:
            if not _scalar(item):
                raise ForgeError(f"Matrix axis {key} values must be scalars.")
        names.append(key)
        values.append(items)
    found = []
    seen = set()
    for combo in itertools.product(*values):
        binding = dict(zip(names, combo))
        label = expand(spec["name"], binding, where="matrix name")
        if not isinstance(label, str):
            raise ForgeError("A matrix case name must be a string.")
        identifier(label)
        if label in seen:
            raise ForgeError(f"Duplicated case name: {label}")
        seen.add(label)
        found.append({"name": label, "dir": None, "path": None, "tags": [], "settings": {}, "skip": None, **binding})
    return found


def _match_name(name, pattern):
    return Path(name).match(pattern)


def _software(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ForgeError("software must be a mapping.")
    items = {}
    for name, spec in value.items():
        identifier(name)
        if not isinstance(spec, dict):
            raise ForgeError(f"software.{name} must be a mapping.")
        unknown = set(spec) - SOFTWARE_FIELDS
        if unknown:
            raise ForgeError(f"Unknown software.{name} fields: " + ", ".join(sorted(unknown)))
        if spec.get("package"):
            package_name(spec["package"])
            if spec.get("build") or spec.get("source"):
                raise ForgeError(f"software.{name} package cannot be combined with build or source.")
        elif not spec.get("build") and not spec.get("source"):
            raise ForgeError(f"software.{name} needs build, package or source.")
        entrypoint = spec.get("entrypoint")
        if entrypoint is not None:
            bench_project.relative(entrypoint)
            if not (spec.get("package") or spec.get("build")):
                raise ForgeError(f"software.{name} entrypoint needs build or package.")
        rebuild = spec.get("rebuild", "auto")
        if rebuild not in {"auto", "never"}:
            raise ForgeError(f"software.{name} rebuild must be auto or never.")
        features = spec.get("features") or []
        if not isinstance(features, list) or not all(isinstance(item, str) for item in features):
            raise ForgeError(f"software.{name} features must be a list of strings.")
        parameters = _scalars(spec.get("set") or {}, f"software.{name} set")
        items[name] = {
            "build": spec.get("build"),
            "package": spec.get("package"),
            "entrypoint": entrypoint,
            "source": spec.get("source"),
            "set": parameters,
            "rebuild": rebuild,
            "version": spec.get("version"),
            "features": features,
            "artifact": spec.get("artifact"),
        }
    return items


def _steps(value):
    if not isinstance(value, list) or not value:
        raise ForgeError("steps must be a non-empty list.")
    steps = []
    names = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ForgeError("Each step must be a mapping.")
        unknown = set(raw) - STEP_FIELDS
        if unknown:
            raise ForgeError("Unknown step fields: " + ", ".join(sorted(unknown)))
        name = raw.get("name")
        identifier(name or "")
        if name in RESERVED_STEPS:
            raise ForgeError(f"Step name is reserved: {name}")
        if name in names:
            raise ForgeError(f"Duplicated step name: {name}")
        names.add(name)
        compare = raw.get("compare")
        commands = raw.get("commands")
        if compare and commands:
            raise ForgeError(f"{name} has both commands and compare.")
        if compare:
            _compare(name, compare)
        elif commands is not None:
            _commands(name, commands)
        after = raw.get("after") or []
        if not isinstance(after, list) or not all(isinstance(item, str) for item in after):
            raise ForgeError(f"{name} after must be a list of step names.")
        software = raw.get("software") or []
        if not isinstance(software, list) or not all(isinstance(item, str) for item in software):
            raise ForgeError(f"{name} software must be a list of names.")
        outputs = raw.get("outputs") or {}
        if not isinstance(outputs, dict) or not all(isinstance(key, str) and isinstance(item, str) and item
                                                     for key, item in outputs.items()):
            raise ForgeError(f"{name} outputs must map names to relative paths.")
        for key, item in outputs.items():
            if not re.fullmatch(_SEGMENT, key):
                raise ForgeError(f"Invalid output name: {key}")
            if Path(item).is_absolute() or ".." in Path(item).parts:
                raise ForgeError(f"{name} output {key} must stay inside the task directory.")
        env = raw.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in env.items()):
            raise ForgeError(f"{name} env must be a mapping of strings.")
        resources = _resources(name, raw.get("resources") or {})
        retries = raw.get("retries", 1)
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ForgeError(f"{name} retries must be a non-negative integer.")
        seed = raw.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ForgeError(f"{name} seed must be an integer.")
        reuse = raw.get("reuse", False)
        clean = raw.get("clean", False)
        if not isinstance(reuse, bool) or not isinstance(clean, bool):
            raise ForgeError(f"{name} reuse and clean must be booleans.")
        steps.append({
            "name": name,
            "after": after,
            "software": software,
            "repeat": raw.get("repeat", 1),
            "seed": seed,
            "reuse": reuse,
            "files": _paths(name, "files", raw.get("files") or []),
            "templates": _paths(name, "templates", raw.get("templates") or []),
            "commands": commands or [],
            "compare": compare,
            "outputs": outputs,
            "verify": raw.get("verify"),
            "clean": clean,
            "env": env,
            "resources": resources,
            "retries": retries,
        })
    known = {step["name"] for step in steps}
    for step in steps:
        missing = [item for item in step["after"] if item not in known]
        if missing:
            raise ForgeError(f"{step['name']} follows an unknown step: " + ", ".join(missing))
        if step["name"] in step["after"]:
            raise ForgeError(f"{step['name']} cannot follow itself.")
    _order(steps)
    return steps


def _order(steps):
    pending = {step["name"]: step for step in steps}
    done = set()
    ordered = []
    while pending:
        ready = [name for name, step in pending.items() if set(step["after"]) <= done]
        if not ready:
            raise ForgeError("Step dependencies contain a cycle.")
        # Keep the written order when several steps are ready.
        ready.sort(key=lambda name: [item["name"] for item in steps].index(name))
        for name in ready:
            ordered.append(pending.pop(name))
            done.add(name)
    steps[:] = ordered


def _compare(name, value):
    if not isinstance(value, dict):
        raise ForgeError(f"{name} compare must be a mapping.")
    unknown = set(value) - COMPARE_FIELDS
    if unknown:
        raise ForgeError(f"Unknown {name} compare fields: " + ", ".join(sorted(unknown)))
    for key in ("actual", "select", "expected"):
        if key not in value:
            raise ForgeError(f"{name} compare needs {key}.")
    value.setdefault("value", "value")
    value.setdefault("error", "error")
    if not isinstance(value["value"], str) or not isinstance(value["error"], str):
        raise ForgeError(f"{name} compare value and error must be field names.")
    _tolerance(value.get("tolerance"), f"{name} compare tolerance")


def _tolerance(value, where):
    if value is None:
        return
    if not isinstance(value, dict) or not value:
        raise ForgeError(f"{where} must be a mapping.")
    unknown = set(value) - TOLERANCE_FIELDS
    if unknown:
        raise ForgeError(f"Unknown {where} fields: " + ", ".join(sorted(unknown)))
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ForgeError(f"{where} {key} must be a number.")


def _reference(path):
    try:
        if path.suffix == ".json":
            import json
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read reference {path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("values"), dict) or not raw["values"]:
        raise ForgeError(f"Reference file needs a values mapping: {path}")
    _tolerance(raw.get("tolerance"), f"{path} tolerance")
    for key, item in raw["values"].items():
        if isinstance(item, dict):
            if "value" not in item:
                raise ForgeError(f"{path} value {key} needs value.")
            _tolerance(item.get("tolerance"), f"{path} {key} tolerance")
        elif isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ForgeError(f"{path} value {key} must be a number or a mapping.")


def _commands(name, value):
    if not isinstance(value, list) or not value:
        raise ForgeError(f"{name} commands must be a non-empty list.")
    for item in value:
        if isinstance(item, str):
            if not item.strip():
                raise ForgeError(f"{name} has an empty command.")
        elif not (isinstance(item, list) and item and all(isinstance(part, str) and part for part in item)):
            raise ForgeError(f"{name} commands must be strings or argv lists.")


def _paths(name, field, value):
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ForgeError(f"{name} {field} must be a list of paths.")
    return value


def _resources(name, value):
    if not isinstance(value, dict):
        raise ForgeError(f"{name} resources must be a mapping.")
    unknown = set(value) - RESOURCE_FIELDS
    if unknown:
        raise ForgeError(f"Unknown {name} resources: " + ", ".join(sorted(unknown)))
    cores = value.get("cores", 1)
    gpus = value.get("gpus", 0)
    if isinstance(cores, bool) or not isinstance(cores, int) or cores < 1:
        raise ForgeError(f"{name} cores must be a positive integer.")
    if isinstance(gpus, bool) or not isinstance(gpus, int) or gpus < 0:
        raise ForgeError(f"{name} gpus must be a non-negative integer.")
    for key in ("memory", "time"):
        if key in value and not isinstance(value[key], str):
            raise ForgeError(f"{name} {key} must be a string.")
    return {"cores": cores, "gpus": gpus, **{key: value[key] for key in ("memory", "time") if key in value}}


def _profiles(value):
    if not isinstance(value, dict):
        raise ForgeError("profiles must be a mapping.")
    profiles = {}
    for name, spec in value.items():
        identifier(name)
        if not isinstance(spec, dict):
            raise ForgeError(f"Profile {name} must be a mapping.")
        unknown = set(spec) - PROFILE_FIELDS
        if unknown:
            raise ForgeError(f"Unknown profile {name} fields: " + ", ".join(sorted(unknown)))
        cases = spec.get("cases")
        if cases is not None and not (isinstance(cases, list) and all(isinstance(item, str) and item for item in cases)):
            raise ForgeError(f"Profile {name} cases must be a list of names or globs.")
        tags = spec.get("tags")
        if tags is not None and not (isinstance(tags, list) and all(isinstance(item, str) and item for item in tags)):
            raise ForgeError(f"Profile {name} tags must be a list of strings.")
        require = spec.get("require")
        if require is not None and require not in {"passed", "complete"}:
            raise ForgeError(f"Profile {name} require must be passed or complete.")
        data = spec.get("data")
        if data is not None:
            identifier(data)
        profiles[name] = {"cases": cases, "tags": tags, "settings": _scalars(spec.get("settings") or {}, f"{name} settings"),
                          "data": data, "require": require}
    return profiles


def _coverage(value):
    if value is None:
        return
    if not isinstance(value, dict):
        raise ForgeError("coverage must be a mapping.")
    unknown = set(value) - {"list", "exclude"}
    if unknown:
        raise ForgeError("Unknown coverage fields: " + ", ".join(sorted(unknown)))
    _command_list(value.get("list"), "coverage list")
    exclude = value.get("exclude") or {}
    if not isinstance(exclude, dict) or not all(isinstance(key, str) and isinstance(item, str) and item
                                                 for key, item in exclude.items()):
        raise ForgeError("coverage exclude must map a case name to a reason.")


def _report(value):
    if value is None:
        return
    _command_list(value, "report")


def _fetch(value):
    if value is None:
        return
    if not isinstance(value, list):
        raise ForgeError("fetch must be a list.")
    for item in value:
        if not isinstance(item, dict):
            raise ForgeError("Each fetch item must be a mapping.")
        unknown = set(item) - {"id", "url", "sha256", "unpack"}
        if unknown:
            raise ForgeError("Unknown fetch fields: " + ", ".join(sorted(unknown)))
        identifier(item.get("id", ""))
        if not isinstance(item.get("url"), str) or not item["url"]:
            raise ForgeError(f"fetch {item.get('id')} needs a url.")
        if not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64:
            raise ForgeError(f"fetch {item.get('id')} needs a sha256.")


def _command_list(value, where):
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ForgeError(f"{where} must be an argv list.")


def _scalars(value, where):
    if not isinstance(value, dict):
        raise ForgeError(f"{where} must be a mapping.")
    for key, item in value.items():
        if not re.fullmatch(_SEGMENT, str(key)):
            raise ForgeError(f"Invalid {where} name: {key}")
        if not _scalar(item):
            raise ForgeError(f"{where}.{key} must be a string, number or boolean.")
    return dict(value)


def _scalar(value):
    return isinstance(value, (str, int, float, bool)) and not (isinstance(value, float) and value != value)


def _optional_name(value, where):
    if value is None:
        return None
    return identifier(value)


def _optional_string(value, where):
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ForgeError(f"{where} must be a string.")
    return value
