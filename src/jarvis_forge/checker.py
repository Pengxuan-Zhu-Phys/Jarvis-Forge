"""Declared-input regression selection with successful evidence reuse."""

from pathlib import Path
import json
import math
import shutil

import yaml

from . import cmake, builds
from .environment_registry import show as registered_environment
from .package_operations import run_command
from .storage import ForgeError, digest, identifier, new_id, now, read_json, write_json
from .suite import UniqueLoader


def load(path):
    path = Path(path).expanduser().resolve()
    try:
        text = path.read_text()
        config = yaml.load(text, Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ForgeError(f"Cannot read checker configuration: {exc}") from exc
    if not isinstance(config, dict) or config.get("kind") != "checker" or config.get("schema_version") != 1:
        raise ForgeError("Checker YAML requires kind: checker and schema_version: 1.")
    if set(config) - {"schema_version", "kind", "id", "project", "environment", "checks"}:
        raise ForgeError("Unknown checker configuration fields.")
    identifier(config.get("id", ""))
    project = (path.parent / config.get("project", ".")).resolve()
    if not project.is_dir():
        raise ForgeError(f"Checker project directory missing: {project}")
    checks = config.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ForgeError("Checker requires a nonempty checks list.")
    ids = []
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"id", "inputs", "needs", "argv", "build", "workflow", "check", "parameters", "cache", "timeout_seconds", "artifacts"}:
            raise ForgeError("Invalid checker check fields.")
        ids.append(identifier(check.get("id", "")))
        if sum(key in check for key in ("argv", "build", "workflow", "check")) != 1:
            raise ForgeError("Each check selects argv, build or a standard check.")
        if "workflow" in check:
            check["build"] = check.pop("workflow")
        if "check" in check:
            from .standard_checks import validate
            validate(check["check"])
        if "argv" in check and (not isinstance(check["argv"], list) or not check["argv"] or not all(isinstance(x, str) for x in check["argv"])):
            raise ForgeError("Checker argv must be a nonempty string list.")
        if check.get("cache", "check" not in check) and not check.get("inputs"):
            raise ForgeError("Cached checks must declare inputs globs; use cache: false for external/always-run checks.")
        for field in ("inputs", "needs", "artifacts"):
            if not isinstance(check.get(field, []), list) or not all(isinstance(x, str) for x in check.get(field, [])):
                raise ForgeError(f"Check {field} must be a list of strings.")
        for pattern in [*check.get("inputs", []), *check.get("artifacts", [])]:
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ForgeError("Checker inputs/artifacts must be relative to project.")
        timeout = check.get("timeout_seconds", 3600)
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ForgeError("Checker timeout must be finite and positive.")
    if len(ids) != len(set(ids)):
        raise ForgeError("Checker check IDs must be unique.")
    pending, ordered, done = list(checks), [], set()
    while pending:
        ready = [c for c in pending if set(c.get("needs", [])) <= done]
        if not ready:
            raise ForgeError("Checker dependency cycle or unknown prerequisite.")
        for check in ready:
            pending.remove(check)
            done.add(check["id"])
            ordered.append(check)
    environment = config.get("environment", {})
    if not isinstance(environment, dict) or set(environment) - {"name", "shell", "revision", "variables"}:
        raise ForgeError("Invalid checker environment.")
    if environment.get("name") and environment.get("shell"):
        raise ForgeError("Select an environment name or shell, not both.")
    return {"config": config, "text": text, "path": str(path), "project": str(project), "ordered": ordered}


def input_manifest(project, patterns):
    records = {}
    for pattern in patterns:
        matches = []
        for path in sorted(project.glob(pattern)):
            if path.is_file():
                if not path.resolve().is_relative_to(project.resolve()):
                    raise ForgeError(f"Checker input symlink escapes project: {path}")
                matches.append((str(path.relative_to(project)), digest(path)))
        records[pattern] = matches  # Keep empty matches and deletions visible.
    return records


def run(home, path, *, full=False, selected=(), environment_name=None, jobs=1, progress=None):
    home.require_initialized()
    loaded = load(path)
    config, project = loaded["config"], Path(loaded["project"])
    wanted = set(selected) if selected else {c["id"] for c in loaded["ordered"]}
    ids = {c["id"] for c in loaded["ordered"]}
    if wanted - ids:
        raise ForgeError("Unknown checker IDs: " + ", ".join(sorted(wanted - ids)))
    for check in reversed(loaded["ordered"]):
        if check["id"] in wanted:
            wanted.update(check.get("needs", []))
    run_id = new_id("checker")
    directory = home.root / "runs" / run_id
    directory.mkdir(parents=True)
    request = {"kind": "checker", "run_id": run_id, "suite_id": config["id"], "started_at": now(), "full": full}
    write_json(directory / "request.json", request)
    (directory / "checker.yaml").write_text(loaded["text"])
    environment_config = config.get("environment", {})
    name = environment_name or environment_config.get("name")
    shell = None
    environment_origin = {"inherited": True}
    if name:
        profile = registered_environment(home, name, environment_config.get("revision"), strict=True)
        shell = Path(profile["activation"])
        environment_origin = {"name": name, "revision": profile["revision"], "files": profile["files"]}
    elif environment_config.get("shell"):
        shell = (Path(loaded["path"]).parent / environment_config["shell"]).resolve()
        environment_origin = {"shell": str(shell), "sha256": digest(shell)}
    variables = environment_config.get("variables", {})
    environment = builds.safe_environment({**cmake.capture(shell), **variables}, variables)
    observed = cmake.tools(environment)
    write_json(directory / "environment.json", {"variables": environment, "tools": observed, "source": environment_origin})
    env_script = cmake.write_environment(directory / "environment.sh", base=environment)
    key = builds.fingerprint({"id": config["id"], "project": str(project)})
    state_file = home.root / "checker/state" / (key + ".json")
    result = {**request, "path": str(directory), "status": "running", "steps": []}
    write_json(directory / "result.json", result)

    from .build_logging import ProcessLog
    event = ProcessLog(directory / "checker.log", "Jarvis-Forge.Checker", progress)

    with builds.file_lock(home.root / "checker/locks" / (key + ".lock")):
        baseline = read_json(state_file) if state_file.exists() else {"checks": {}}
        fingerprints, statuses = {}, {}
        try:
            for check in loaded["ordered"]:
                check_id = check["id"]
                if check_id not in wanted:
                    continue
                record = {"id": check_id, "started_at": now(), "status": "running"}
                step_dir = directory / "steps" / check_id
                step_dir.mkdir(parents=True)
                inputs = input_manifest(project, check.get("inputs", []))
                build_graph = None
                if check.get("build"):
                    from .build_catalog import resolve
                    target = resolve(home, check["build"], Path(loaded["path"]).parent)
                    build_graph = builds.graph(home, str(target), parameters=check.get("parameters"))
                signature = builds.fingerprint({"check": check, "inputs": inputs, "build": build_graph,
                    "standard_checks": digest(Path(__file__).with_name("standard_checks.py")) if "check" in check else None,
                    "environment": environment, "environment_origin": environment_origin, "tools": observed,
                    "prerequisites": {n: fingerprints[n] for n in check.get("needs", [])}})
                fingerprints[check_id] = signature
                write_json(step_dir / "inputs.json", {"fingerprint": signature, "inputs": inputs})
                cached = baseline["checks"].get(check_id)
                can_reuse = bool(check.get("cache", "check" not in check) and not full and cached and cached["fingerprint"] == signature)
                if can_reuse:
                    for filename, sha in cached.get("evidence", {}).items():
                        if not Path(filename).is_file() or digest(Path(filename)) != sha:
                            can_reuse = False
                    for artifact_id in cached.get("build_artifacts", []):
                        try:
                            builds.artifact(home, artifact_id)
                        except ForgeError:
                            can_reuse = False
                prerequisites = [statuses[n] for n in check.get("needs", [])]
                if any(s not in {"passed", "reused"} for s in prerequisites):
                    record.update(status="blocked", reason="A prerequisite did not pass.")
                elif can_reuse:
                    record.update(status="reused", reused_from=cached["run_id"], reason="Declared inputs, commands, dependencies and environment unchanged.")
                    fingerprints[check_id] = cached.get("dependency_token", signature)
                else:
                    event(f"Check {check_id}: running")
                    try:
                        evidence, artifact_ids, output_hashes = {}, [], []
                        if build_graph:
                            child = builds.execute(home, str(target), parameters=check.get("parameters"), environment_name=name, jobs=jobs, progress=progress)
                            record["build_run_id"] = child["run_id"]
                            if child["status"] != "passed":
                                raise ForgeError(child.get("error", "Build check failed."), code=1)
                            artifact_ids = [v["artifact_id"] for v in child.get("outputs", {}).values()]
                            child_result = Path(child["path"]) / "result.json"
                            evidence[str(child_result)] = digest(child_result)
                        else:
                            bindings = {"project": str(project), "python": observed.get("python", {}).get("path", "python3"),
                                        "run_dir": str(directory), "step_dir": str(step_dir)}
                            def invoke(argv):
                                return run_command(argv, project, env_script, step_dir, "1", check.get("timeout_seconds", 3600), progress=progress, process_env=environment)
                            try:
                                if "check" in check:
                                    from .standard_checks import execute
                                    execute(check["check"], bindings["python"], invoke, step_dir)
                                    evidence[str(step_dir / "checks.json")] = digest(step_dir / "checks.json")
                                else:
                                    invoke(builds.interpolate(check["argv"], bindings))
                            finally:
                                for stream in ("stdout", "stderr"):
                                    source = step_dir / ("command-1." + stream + ".log")
                                    if source.is_file():
                                        shutil.copyfile(source, step_dir / (stream + ".log"))
                            for artifact in check.get("artifacts", []):
                                file = project / artifact
                                if not file.resolve().is_relative_to(project) or not file.is_file():
                                    raise ForgeError(f"Missing checker output: {file}", code=1)
                                destination = step_dir / "artifacts" / artifact
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copyfile(file, destination)
                                evidence[str(destination)] = digest(destination)
                                output_hashes.append((artifact, digest(destination)))
                            for stream in ("stdout", "stderr"):
                                file = step_dir / (stream + ".log")
                                evidence[str(file)] = digest(file)
                        # A check that rewrites its declared inputs cannot cache its old result.
                        unchanged = inputs == input_manifest(project, check.get("inputs", []))
                        record.update(status="passed", inputs_unchanged=unchanged)
                        if unchanged:
                            token = builds.fingerprint({"signature": signature, "outputs": output_hashes,
                                "execution": run_id if not check.get("cache", "check" not in check) else None})
                            fingerprints[check_id] = token
                            baseline["checks"][check_id] = {"fingerprint": signature, "run_id": run_id,
                                "evidence": evidence, "build_artifacts": artifact_ids, "dependency_token": token}
                        else:
                            fingerprints[check_id] = builds.fingerprint({"signature": signature, "execution": run_id})
                            baseline["checks"].pop(check_id, None)
                    except ForgeError as exc:
                        record.update(status="failed", error=str(exc))
                if record["status"] in {"failed", "blocked"}:
                    baseline["checks"].pop(check_id, None)
                record["finished_at"] = now()
                statuses[check_id] = record["status"]
                result["steps"].append(record)
                write_json(step_dir / "result.json", record)
                write_json(state_file, baseline)
                write_json(directory / "result.json", result)
                event(f"Check {check_id}: {record['status']}",
                      level="ERROR" if record["status"] == "failed" else "WARNING" if record["status"] == "blocked" else "INFO")
            result["status"] = "passed" if all(s in {"passed", "reused"} for s in statuses.values()) else "failed"
        except KeyboardInterrupt:
            result.update(status="interrupted", error="Interrupted.")
        except (ForgeError, OSError, ValueError) as exc:
            result.update(status="failed", error=str(exc))
        finally:
            result["finished_at"] = now()
            result["executed"] = sum(s["status"] in {"passed", "failed"} for s in result["steps"])
            result["reused"] = sum(s["status"] == "reused" for s in result["steps"])
            write_json(directory / "result.json", result)
    return result
