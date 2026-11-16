"""jforge test: schema 2 checker cards, test catalogs, profile selection and sharding.

Design: Jarvis-Books Docs/Jarvis-Forge/TEST_CATALOG_DESIGN.md. Loading, validation
and planning never import the project, run pytest or touch the Forge home.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

import yaml

from . import checker
from .storage import ForgeError, identifier
from .suite import UniqueLoader


CARD_FIELDS = frozenset({"schema_version", "kind", "id", "project", "environment", "checks", "groups",
                         "profiles", "default_profile", "coverage"})
GROUP_FIELDS = frozenset({"id", "isolate", "import_mode", "match", "omit", "markers", "pythonpath",
                          "distributions", "env", "inputs", "needs", "cache", "timeout_seconds",
                          "pytest_args", "workers", "skip_allow", "exclusive", "artifacts"})
CATALOG_FIELDS = frozenset({"schema_version", "kind", "id", "root", "cards"})
GATE_PROFILES = ("acceptance", "release")
GROUP_ID_LIMIT = 96
BATCH_LIMIT = 9999
RESERVED_VARIABLES = frozenset({"JFORGE_RUN_DIR", "JFORGE_STEP_DIR", "PYTHONPATH"})
_DISTRIBUTION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TRACEBACKS = ("short", "long", "line", "no", "native")


# Reading and field helpers

def _read(path):
    path = Path(path).expanduser().resolve()
    try:
        text = path.read_text(encoding="utf-8")
        config = yaml.load(text, Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read test configuration {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ForgeError(f"Test configuration must be a YAML mapping: {path}")
    return path, text, config


def _strings(value, what, *, nonempty=False):
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value) or (nonempty and not value):
        raise ForgeError(f"{what} must be a {'nonempty ' if nonempty else ''}list of nonempty strings.")
    return list(value)


def _relative(patterns, what):
    for pattern in patterns:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ForgeError(f"{what} must be relative to the project without '..': {pattern}")
    return patterns


def _identifiers(values, what):
    return [identifier(value) for value in _strings(values, what)]


def _positive_number(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ForgeError(f"{what} must be finite and positive.")
    return value


def _pytest_args(values, group_id):
    """Keep a short whitelist; the executor owns every other pytest option."""
    values = _strings(values, f"Group {group_id} pytest_args")
    allowed = "-q, -vv, --tb=short|long|line|no|native, -p no:cacheprovider, --maxfail=N"
    result, seen, index = [], set(), 0
    while index < len(values):
        arg = values[index]
        flag, _, value = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        if flag in {"--tb", "--maxfail", "-p"} and not value:
            index += 1
            if index >= len(values):
                raise ForgeError(f"Group {group_id} pytest_args: {flag} needs a value.")
            value = values[index]
        key = "verbosity" if flag in {"-q", "-vv"} else flag
        if flag in {"-q", "-vv"} and not value:
            result.append(flag)
        elif flag == "--tb" and value in _TRACEBACKS:
            result.append(f"--tb={value}")
        elif flag == "--maxfail" and value.isdigit() and int(value) > 0:
            result.append(f"--maxfail={int(value)}")
        elif flag == "-p" and value == "no:cacheprovider":
            result.extend(["-p", "no:cacheprovider"])
        else:
            raise ForgeError(f"Group {group_id} pytest_args does not allow {arg!r}.", hint=f"Allowed: {allowed}.")
        if key in seen:
            raise ForgeError(f"Group {group_id} pytest_args repeats {flag}.")
        seen.add(key)
        index += 1
    return result


def _package(spec):
    if not isinstance(spec, dict) or set(spec) - {"distribution", "modules", "commands"}:
        raise ForgeError("Package checks accept distribution, modules and commands.")
    distribution = spec.get("distribution")
    if not isinstance(distribution, str) or not _DISTRIBUTION.fullmatch(distribution):
        raise ForgeError(f"Package check needs a valid distribution name: {distribution!r}")
    for module in _strings(spec.get("modules", []), "Package check modules"):
        if not _MODULE.fullmatch(module):
            raise ForgeError(f"Invalid Python module name: {module}")
    commands = spec.get("commands", [])
    if not isinstance(commands, list):
        raise ForgeError("Package check commands must be a list of argv lists.")
    for command in commands:
        _strings(command, "Each package check command", nonempty=True)


def _standard(spec):
    if isinstance(spec, dict) and "package" in spec:
        if set(spec) != {"package"}:
            raise ForgeError("A package check cannot be combined with python or tools checks.")
        _package(spec["package"])
    else:
        from .standard_checks import validate
        validate(spec)


def check_kind(check):
    if "argv" in check:
        return "argv"
    if "build" in check:
        return "build"
    return "package" if "package" in check["check"] else "standard"


def _group(raw):
    if not isinstance(raw, dict):
        raise ForgeError("Each test group must be a mapping.")
    unknown = set(raw) - GROUP_FIELDS
    if unknown:
        raise ForgeError("Unknown test group fields: " + ", ".join(sorted(unknown)))
    group_id = identifier(raw.get("id", ""))
    if len(group_id) > GROUP_ID_LIMIT:
        raise ForgeError(f"Group ID is longer than {GROUP_ID_LIMIT} characters: {group_id}")
    label = f"Group {group_id}"
    isolate = raw.get("isolate", "source")
    if isolate not in {"source", "installed"}:
        raise ForgeError(f"{label} isolate must be source or installed.")
    import_mode = raw.get("import_mode", "prepend" if isolate == "source" else "importlib")
    if import_mode not in {"prepend", "importlib"}:
        raise ForgeError(f"{label} import_mode must be prepend or importlib.")
    markers = raw.get("markers", "")
    if not isinstance(markers, str):
        raise ForgeError(f"{label} markers must be a string.")
    distributions = _strings(raw.get("distributions", []), f"{label} distributions")
    for name in distributions:
        if not _DISTRIBUTION.fullmatch(name):
            raise ForgeError(f"{label} has an invalid distribution name: {name}")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ForgeError(f"{label} env must map variable names to strings; quote numbers such as \"1\".")
    for name in env:
        if not _VARIABLE.fullmatch(name):
            raise ForgeError(f"{label} env has an invalid variable name: {name}")
        if name in RESERVED_VARIABLES:
            raise ForgeError(f"{label} env cannot set {name}; Forge sets it." if name != "PYTHONPATH"
                             else f"{label} env cannot set PYTHONPATH; use the pythonpath field.")
    cache = raw.get("cache", True)
    if not isinstance(cache, bool):
        raise ForgeError(f"{label} cache must be true or false.")
    inputs = _relative(_strings(raw.get("inputs", []), f"{label} inputs"), f"{label} inputs")
    if cache and not inputs:
        raise ForgeError(f"{label} is cached and must declare inputs; use cache: false for always-run groups.")
    workers = raw.get("workers")
    if workers is not None and workers != "auto" and (isinstance(workers, bool) or not isinstance(workers, int) or workers < 1):
        raise ForgeError(f"{label} workers must be auto or a positive integer.")
    skip_allow = raw.get("skip_allow")
    if skip_allow is not None:
        for pattern in _strings(skip_allow, f"{label} skip_allow"):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ForgeError(f"{label} skip_allow has an invalid pattern {pattern!r}: {exc}") from exc
        skip_allow = list(skip_allow)
    group = {
        "id": group_id, "kind": "pytest", "isolate": isolate, "import_mode": import_mode,
        "match": _relative(_strings(raw.get("match"), f"{label} match", nonempty=True), f"{label} match"),
        "omit": _relative(_strings(raw.get("omit", []), f"{label} omit"), f"{label} omit"),
        "markers": markers.strip(),
        "pythonpath": _relative(_strings(raw.get("pythonpath", []), f"{label} pythonpath"), f"{label} pythonpath"),
        "distributions": distributions, "env": dict(env), "inputs": inputs,
        "needs": _identifiers(raw.get("needs", []), f"{label} needs"), "cache": cache,
        "timeout_seconds": _positive_number(raw.get("timeout_seconds", 3600), f"{label} timeout_seconds"),
        "pytest_args": _pytest_args(raw.get("pytest_args", []), group_id), "workers": workers,
        "skip_allow": skip_allow, "exclusive": _identifiers(raw.get("exclusive", []), f"{label} exclusive"),
        "artifacts": _relative(_strings(raw.get("artifacts", []), f"{label} artifacts"), f"{label} artifacts"),
    }
    if isolate == "installed":
        if not distributions:
            raise ForgeError(f"{label} uses isolate: installed and must list distributions.")
        if group["pythonpath"]:
            raise ForgeError(f"{label} uses isolate: installed and cannot set pythonpath.")
    return group


def _order(items):
    """Return IDs in prerequisite order; within a round keep card order."""
    known = {item_id for item_id, _ in items}
    for item_id, needs in items:
        missing = sorted(set(needs) - known)
        if missing:
            raise ForgeError(f"{item_id} needs unknown IDs: " + ", ".join(missing))
    pending, ordered, done = list(items), [], set()
    while pending:
        ready = [item for item in pending if set(item[1]) <= done]
        if not ready:
            raise ForgeError("Dependency cycle among: " + ", ".join(item_id for item_id, _ in pending))
        for item in ready:
            pending.remove(item)
            done.add(item[0])
            ordered.append(item[0])
    return ordered


# Cards and catalogs

def load_card(path):
    path, text, config = _read(path)
    if config.get("kind") != "checker" or config.get("schema_version") not in (1, 2):
        raise ForgeError(f"Test cards require kind: checker and schema_version 1 or 2: {path}")
    if config["schema_version"] == 1:
        loaded = checker.load(path)
        checks = {check["id"]: check for check in loaded["ordered"]}
        return {"schema_version": 1, "id": loaded["config"]["id"], "path": str(path), "text": text,
                "project": loaded["project"], "environment": loaded["config"].get("environment", {}),
                "groups": {}, "checks": checks, "order": list(checks),
                "profiles": {}, "default_profile": None, "coverage": None, "_files": {}}
    unknown = set(config) - CARD_FIELDS
    if unknown:
        raise ForgeError("Unknown test card fields: " + ", ".join(sorted(unknown)))
    card_id = identifier(config.get("id", ""))
    project = (path.parent / config.get("project", ".")).resolve()
    if not project.is_dir():
        raise ForgeError(f"Test card project directory missing: {project}")
    checker.validate_environment(config.get("environment", {}))
    raw_checks, raw_groups = config.get("checks", []), config.get("groups", [])
    if not isinstance(raw_checks, list) or not isinstance(raw_groups, list):
        raise ForgeError("checks and groups must be lists.")
    if not raw_checks and not raw_groups:
        raise ForgeError("A schema 2 card needs at least one group or check.")
    checker.validate_checks(raw_checks, extra_fields={"exclusive"}, standard=_standard)
    for check in raw_checks:
        _identifiers(check.get("exclusive", []), f"Check {check['id']} exclusive")
    groups = [_group(raw) for raw in raw_groups]
    ids = [check["id"] for check in raw_checks] + [group["id"] for group in groups]
    duplicates = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
    if duplicates:
        raise ForgeError("Group and check IDs must be unique: " + ", ".join(duplicates))
    sections = {"checks": [(c["id"], c.get("needs", [])) for c in raw_checks],
                "groups": [(g["id"], g["needs"]) for g in groups]}
    order = _order([item for key in config if key in sections for item in sections[key]])

    profiles, default = config.get("profiles"), config.get("default_profile")
    if profiles is None:
        if default is not None:
            raise ForgeError("default_profile requires profiles.")
        profiles = {}
    else:
        if not isinstance(profiles, dict) or not profiles:
            raise ForgeError("profiles must map profile names to lists of group or check IDs.")
        for name, members in profiles.items():
            identifier(name)
            members = _strings(members, f"Profile {name}", nonempty=True)
            unknown_ids = sorted(set(members) - set(ids))
            if unknown_ids:
                raise ForgeError(f"Profile {name} names unknown IDs: " + ", ".join(unknown_ids))
            if len(members) != len(set(members)):
                raise ForgeError(f"Profile {name} repeats an ID.")
        if default is None:
            raise ForgeError("Cards with profiles must set default_profile.")
        if default not in profiles:
            raise ForgeError(f"default_profile {default!r} is not a profile of this card.")
        profiles = {name: list(members) for name, members in profiles.items()}

    coverage = config.get("coverage")
    if coverage is not None:
        if not isinstance(coverage, dict) or set(coverage) - {"paths", "exclude"}:
            raise ForgeError("coverage accepts paths and exclude.")
        paths = _relative(_strings(coverage.get("paths"), "coverage.paths", nonempty=True), "coverage.paths")
        exclude = coverage.get("exclude", [])
        if not isinstance(exclude, list):
            raise ForgeError("coverage.exclude must be a list of path and reason entries.")
        entries = []
        for entry in exclude:
            if not isinstance(entry, dict) or set(entry) != {"path", "reason"}:
                raise ForgeError("Each coverage.exclude entry needs exactly path and reason.")
            if not isinstance(entry["path"], str) or not entry["path"]:
                raise ForgeError("coverage.exclude path must be a nonempty string.")
            _relative([entry["path"]], "coverage.exclude path")
            if not isinstance(entry["reason"], str) or not entry["reason"].strip():
                raise ForgeError(f"coverage.exclude {entry['path']} needs a reason.")
            entries.append({"path": Path(entry["path"]).as_posix(), "reason": entry["reason"].strip()})
        if len({e["path"] for e in entries}) != len(entries):
            raise ForgeError("coverage.exclude repeats a path.")
        coverage = {"paths": paths, "exclude": entries}

    return {"schema_version": 2, "id": card_id, "path": str(path), "text": text, "project": str(project),
            "environment": config.get("environment", {}), "groups": {group["id"]: group for group in groups}, "checks": {c["id"]: c for c in raw_checks},
            "order": order, "profiles": profiles, "default_profile": default, "coverage": coverage, "_files": {}}


def load_catalog(path, root=None):
    path, _, config = _read(path)
    if config.get("kind") != "test-catalog" or config.get("schema_version") != 2:
        raise ForgeError(f"Test catalogs require kind: test-catalog and schema_version: 2: {path}")
    unknown = set(config) - CATALOG_FIELDS
    if unknown:
        raise ForgeError("Unknown test catalog fields: " + ", ".join(sorted(unknown)))
    catalog_id = identifier(config.get("id", ""))
    if root is not None:
        base = Path(root).expanduser().resolve()
    else:
        value = config.get("root", ".")
        if not isinstance(value, str) or not value:
            raise ForgeError("Catalog root must be a nonempty path string.")
        base = (path.parent / Path(value).expanduser()).resolve()
    if not base.is_dir():
        raise ForgeError(f"Catalog root directory missing: {base}", hint="Pass --root <dir> to point at the repositories.")
    cards = config.get("cards")
    if not isinstance(cards, list) or not cards:
        raise ForgeError("A test catalog needs a nonempty cards list.")
    entries = []
    for raw in cards:
        if not isinstance(raw, dict) or set(raw) - {"id", "path", "env"} or not {"id", "path"} <= set(raw):
            raise ForgeError("Each catalog card needs id and path, and may set env.")
        entry_id = identifier(raw["id"])
        if not isinstance(raw["path"], str) or not raw["path"] or Path(raw["path"]).is_absolute():
            raise ForgeError(f"Catalog card {entry_id} path must be relative to the catalog root.")
        card_path = (base / raw["path"]).resolve()
        if not card_path.is_relative_to(base):
            raise ForgeError(f"Catalog card {entry_id} path leaves the catalog root: {raw['path']}")
        if not card_path.is_file():
            raise ForgeError(f"Catalog card {entry_id} file missing: {card_path}",
                             hint="Pass --root <dir> if the repositories live elsewhere.")
        env = raw.get("env")
        if env is not None:
            identifier(env)
        try:
            card = load_card(card_path)
        except ForgeError as exc:
            raise ForgeError(f"Catalog card {entry_id}: {exc}", hint=exc.hint) from exc
        entries.append({"id": entry_id, "path": str(card_path), "env": env, "card": card})
    ids = [entry["id"] for entry in entries]
    if len(ids) != len(set(ids)):
        raise ForgeError("Catalog card IDs must be unique.")
    return {"kind": "catalog", "id": catalog_id, "path": str(path), "root": str(base), "entries": entries}


def load_target(path, *, root=None, project=None):
    _, _, config = _read(path)
    if config.get("kind") == "test-catalog":
        catalog = load_catalog(path, root)
        if project is not None:
            entries = [entry for entry in catalog["entries"] if entry["id"] == project]
            if not entries:
                raise ForgeError(f"Catalog has no card {project!r}.",
                                 hint="Catalog cards: " + ", ".join(e["id"] for e in catalog["entries"]))
            catalog["entries"] = entries
        return catalog
    if root is not None or project is not None:
        raise ForgeError("--root and --project apply to test catalogs only.")
    card = load_card(path)
    return {"kind": "card", "id": None, "path": card["path"], "root": None,
            "entries": [{"id": None, "path": card["path"], "env": None, "card": card}]}


# Selection, files and shards

def _needs(card, item_id):
    item = card["groups"].get(item_id) or card["checks"][item_id]
    return item.get("needs", [])


def select(card, profile=None, groups=()):
    groups = list(groups)
    if profile and groups:
        raise ForgeError("--profile and --group are mutually exclusive.")
    if groups:
        unknown = [item_id for item_id in groups if item_id not in card["order"]]
        if unknown:
            raise ForgeError(f"Card {card['id']} has no group or check " + ", ".join(unknown) + ".")
        seeds = groups
    elif profile:
        if profile not in card["profiles"]:
            names = ", ".join(card["profiles"]) or "none"
            raise ForgeError(f"Card {card['id']} has no profile {profile!r}.", hint=f"Profiles: {names}.")
        seeds = card["profiles"][profile]
    elif card["profiles"]:
        profile = card["default_profile"]
        seeds = card["profiles"][profile]
    else:
        seeds = card["order"]
    wanted, pending = set(), list(seeds)
    while pending:
        item_id = pending.pop()
        if item_id not in wanted:
            wanted.add(item_id)
            pending.extend(_needs(card, item_id))
    ids = [item_id for item_id in card["order"] if item_id in wanted]
    if not ids:
        raise ForgeError(f"Card {card['id']} selects nothing.")
    return {"profile": profile, "seeds": list(seeds), "ids": ids}


def expand(card, patterns):
    project = Path(card["project"])
    found = set()
    for pattern in patterns:
        if pattern not in card["_files"]:
            matches = []
            for path in project.glob(pattern):
                if path.is_file():
                    if not path.resolve().is_relative_to(project):
                        raise ForgeError(f"Test path symlink leaves the project: {path}")
                    matches.append(path.relative_to(project).as_posix())
            card["_files"][pattern] = matches
        found.update(card["_files"][pattern])
    return found


def group_files(card, group):
    return sorted(expand(card, group["match"]) - expand(card, group["omit"]))


def check_intersections(card, ids, profile=None):
    owners = {}
    for item_id in ids:
        group = card["groups"].get(item_id)
        if group is None:
            continue
        for path in group_files(card, group):
            other = owners.setdefault((path, group["markers"]), item_id)
            if other != item_id:
                where = f" in profile {profile}" if profile else ""
                raise ForgeError(f"Groups {other} and {item_id} both select {path} with markers {group['markers']!r}{where}.",
                                 hint="Select one of them, give them different markers, or omit the file from one group.")


def parse_batch(text):
    if text is None:
        return 1, 1
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", text.strip())
    if not match or int(match[1]) > int(match[2]) or int(match[2]) > BATCH_LIMIT:
        raise ForgeError(f"--batch takes k/n with 1 <= k <= n <= {BATCH_LIMIT}: {text!r}")
    return int(match[1]), int(match[2])


def bucket(path, count):
    """Stable shard bucket: adding a file never moves other files between shards."""
    return int(hashlib.sha256(path.encode("utf-8")).hexdigest()[:8], 16) % count


def shard_set_digest(paths, count, markers):
    value = {"count": count, "paths": sorted(paths), "markers": markers}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def step_id(group_id, index, count):
    return group_id if count == 1 else f"{group_id}--{index}of{count}"


# Validation and plans

def check_coverage(card):
    coverage = card["coverage"]
    project = Path(card["project"])
    for entry in coverage["exclude"]:
        if not (project / entry["path"]).is_file():
            raise ForgeError(f"coverage.exclude names a missing file: {entry['path']}")
    if "acceptance" in card["profiles"]:
        scope, ids = "acceptance", select(card, profile="acceptance")["ids"]
    else:
        scope, ids = "card", card["order"]
    covered = set()
    for item_id in ids:
        if item_id in card["groups"]:
            covered.update(group_files(card, card["groups"][item_id]))
    missing = sorted(expand(card, coverage["paths"]) - covered - {e["path"] for e in coverage["exclude"]})
    if missing:
        shown = ", ".join(missing[:10]) + (f" and {len(missing) - 10} more" if len(missing) > 10 else "")
        raise ForgeError(f"Card {card['id']}: test files outside every {scope} group: {shown}",
                         hint="Add them to a group's match, or list them under coverage.exclude with a reason.")


def validate_card(card, *, require_acceptance=False):
    if card["schema_version"] == 1:
        if require_acceptance:
            raise ForgeError(f"Card {card['id']} uses schema 1; catalog cards need schema 2 with an acceptance profile.")
        return []
    if require_acceptance and "acceptance" not in card["profiles"]:
        raise ForgeError(f"Card {card['id']} has no acceptance profile; every catalog card needs one.")
    for name in card["profiles"] or [None]:
        check_intersections(card, select(card, profile=name)["ids"], name)
    gates = [name for name in GATE_PROFILES if name in card["profiles"]]
    for name in gates:
        for item_id in select(card, profile=name)["ids"]:
            group = card["groups"].get(item_id)
            if group is not None and group["skip_allow"] is None:
                raise ForgeError(f"Group {item_id} is in gate profile {name} and must declare skip_allow.",
                                 hint="Use skip_allow: [] to allow no skips.")
    if gates and card["groups"] and card["coverage"] is None:
        raise ForgeError(f"Card {card['id']} has a gate profile and pytest groups, so it must declare coverage.")
    if card["coverage"] is not None:
        check_coverage(card)
    return [f"Group {group_id} matches no files." for group_id, group in card["groups"].items()
            if not group_files(card, group)]


def _context(entry, catalog):
    return f"Catalog card {entry['id']}: " if catalog else ""


def validate(path, *, root=None):
    target = load_target(path, root=root)
    catalog = target["kind"] == "catalog"
    cards = []
    for entry in target["entries"]:
        card = entry["card"]
        try:
            warnings = validate_card(card, require_acceptance=catalog)
        except ForgeError as exc:
            raise ForgeError(_context(entry, catalog) + str(exc), hint=exc.hint) from exc
        cards.append({"project_id": entry["id"], "suite_id": card["id"], "path": card["path"],
                      "project": card["project"], "env": entry["env"], "schema_version": card["schema_version"],
                      "groups": len(card["groups"]), "checks": len(card["checks"]),
                      "profiles": card["profiles"], "default_profile": card["default_profile"],
                      "coverage": card["coverage"] is not None, "warnings": warnings})
    return {"target": target["kind"], "path": target["path"], "catalog_id": target["id"],
            "root": target["root"], "cards": cards}


def plan(path, *, profile=None, groups=(), batch=None, root=None, project=None):
    if profile and groups:
        raise ForgeError("--profile and --group are mutually exclusive.")
    index, count = parse_batch(batch)
    target = load_target(path, root=root, project=project)
    catalog = target["kind"] == "catalog"
    cards = []
    for entry in target["entries"]:
        card = entry["card"]
        try:
            selection = select(card, profile, groups)
            check_intersections(card, selection["ids"], selection["profile"])
            steps = []
            for item_id in selection["ids"]:
                group = card["groups"].get(item_id)
                if group is None:
                    check = card["checks"][item_id]
                    steps.append({"id": item_id, "kind": check_kind(check), "needs": check.get("needs", [])})
                    continue
                files = group_files(card, group)
                if not files:
                    raise ForgeError(f"Selected group {item_id} matches no files after omit.")
                paths = [file for file in files if bucket(file, count) == index - 1]
                steps.append({"id": step_id(item_id, index, count), "kind": "pytest", "group_id": item_id,
                              "batch": {"index": index, "count": count}, "isolate": group["isolate"],
                              "import_mode": group["import_mode"], "markers": group["markers"],
                              "needs": group["needs"], "files": len(files), "paths": paths,
                              "reason": None if paths else "empty batch",
                              "shard_set_digest": shard_set_digest(files, count, group["markers"])})
        except ForgeError as exc:
            raise ForgeError(_context(entry, catalog) + str(exc), hint=exc.hint) from exc
        warnings = [f"Group {group_id} matches no files." for group_id, group in card["groups"].items()
                    if group_id not in selection["ids"] and not group_files(card, group)]
        cards.append({"project_id": entry["id"], "suite_id": card["id"], "path": card["path"],
                      "env": entry["env"], "profile": selection["profile"], "steps": steps, "warnings": warnings})
    return {"target": target["kind"], "path": target["path"], "catalog_id": target["id"],
            "batch": {"index": index, "count": count}, "cards": cards}
