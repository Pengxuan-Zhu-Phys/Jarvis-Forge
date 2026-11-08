"""Strict, versioned command suites and read-only planning."""

from __future__ import annotations

from importlib.resources import files
import json
import math
from pathlib import Path
import re
import shutil
import sys

from jsonschema import Draft202012Validator
import yaml

from .storage import ForgeError, ForgeHome, digest
from .environment_registry import show as registered_environment


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ForgeError(f"Duplicate or non-string YAML key: {key!r}.")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_suite(path: Path) -> dict:
    path = path.expanduser().resolve()
    try:
        text = path.read_text(encoding="utf-8")
        config = yaml.load(text, Loader=UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read suite {path}: {exc}") from exc
    schema = json.loads(files("jarvis_forge").joinpath("schemas/suite.schema.json").read_text())
    errors = list(Draft202012Validator(schema).iter_errors(config))
    if errors:
        first = errors[0]
        location = ".".join(str(x) for x in first.absolute_path) or "root"
        raise ForgeError(f"Invalid suite at {location}: {first.message}")
    steps = config["steps"]
    ids = [step["id"] for step in steps]
    if len(ids) != len(set(ids)):
        raise ForgeError("Step IDs must be unique.")
    for step in steps:
        if not math.isfinite(step.get("timeout_seconds", 600)):
            raise ForgeError(f"Non-finite timeout for step {step['id']}.")
        for dep in step.get("needs", []):
            if dep not in ids:
                raise ForgeError(f"Step {step['id']} needs unknown step {dep}.")
        for artifact in step.get("artifacts", []):
            item = Path(artifact)
            if item.is_absolute() or ".." in item.parts or artifact == ".":
                raise ForgeError(f"Artifact must be a relative file path: {artifact}")
    pending = list(steps)
    ordered = []
    done = set()
    while pending:
        ready = [step for step in pending if set(step.get("needs", [])) <= done]
        if not ready:
            raise ForgeError("Suite contains a dependency cycle.")
        for step in ready:
            ordered.append(step)
            done.add(step["id"])
            pending.remove(step)
    return {"path": str(path), "text": text, "sha256": digest(path),
            "config": config, "ordered": ordered}


def expand(text: str, bindings: dict[str, str]) -> str:
    def replace(match):
        key = match.group(1)
        if key not in bindings:
            raise ForgeError(f"Unknown placeholder: ${{{key}}}")
        return bindings[key]
    return re.sub(r"\$\{([^}]+)\}", replace, text)


def make_plan(suite: dict, run_dir: Path, home: ForgeHome | None = None, environment_name: str | None = None) -> dict:
    source = Path(suite["path"]).parent
    config = suite["config"]
    bindings = {"python": sys.executable, "controller_python": sys.executable, "project_python": "python3",
                "suite_dir": str(source),
                "run_dir": str(run_dir)}
    inputs = {}
    for key, value in config.get("inputs", {}).items():
        path = (source / Path(value).expanduser()).resolve()
        if not path.is_file():
            raise ForgeError(f"Input is not a file: {path}")
        inputs[key] = {"source": str(path), "sha256": digest(path),
                       "snapshot": str(run_dir / "inputs" / key / path.name)}
        bindings[f"input:{key}"] = inputs[key]["snapshot"]
    environment = config.get("environment", {})
    shell = None
    profile = None
    selected = environment_name or environment.get("name")
    if selected:
        revision = environment.get("revision") if environment_name is None else None
        profile = registered_environment(home or ForgeHome.resolve(), selected, revision, strict=True)
        shell = {"path": profile["activation"], "sha256": profile["files"]["activate.sh"]}
    elif environment.get("shell"):
        path = (source / Path(environment["shell"]).expanduser()).resolve()
        if not path.is_file():
            raise ForgeError(f"Environment script is not a file: {path}")
        shell = {"path": str(path), "sha256": digest(path)}
    bash = shutil.which("bash")
    if not bash:
        raise ForgeError("Bash is required for local command execution.")
    steps = []
    for step in suite["ordered"]:
        local = {**bindings, "step_dir": str(run_dir / "steps" / step["id"])}
        cwd = Path(expand(step.get("cwd", "${run_dir}/workspace"), local)).expanduser()
        if not cwd.is_absolute():
            cwd = source / cwd
        cwd = cwd.resolve()
        if not cwd.is_dir() and not cwd.is_relative_to(run_dir):
            raise ForgeError(f"Working directory does not exist: {cwd}")
        if cwd.exists() and not cwd.is_dir():
            raise ForgeError(f"Working directory is not a directory: {cwd}")
        item = {"id": step["id"], "cwd": str(cwd), "needs": step.get("needs", []),
                "timeout_seconds": step.get("timeout_seconds", 600),
                "expect_exit_code": step.get("expect_exit_code", 0),
                "artifacts": step.get("artifacts", [])}
        if "argv" in step:
            item["argv"] = [expand(value, local) for value in step["argv"]]
        else:
            item["script"] = expand(step["script"], local)
        item["variables"] = {key: expand(value, local)
                             for key, value in environment.get("variables", {}).items()}
        steps.append(item)
    return {"schema_version": 1, "suite_id": config["id"], "suite_path": suite["path"],
            "suite_sha256": suite["sha256"], "bash": bash,
            "environment_shell": shell, "environment_profile": profile,
            "environment_selection_source": "cli" if environment_name else "suite",
            "inputs": inputs, "steps": steps}

