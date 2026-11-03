"""Descriptive history comparison; the prototype compares archived file bytes."""

from __future__ import annotations

from pathlib import Path

from .storage import ForgeError, ForgeHome, digest, new_id, now, read_json, write_json


def _normalized(value, root: str):
    if isinstance(value, str):
        return value.replace(root, "${run_dir}")
    if isinstance(value, list):
        return [_normalized(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _normalized(item, root) for key, item in value.items()}
    return value


def compare(home: ForgeHome, older_id: str, newer_id: str) -> dict:
    older, newer = home.show(older_id), home.show(newer_id)
    if older["suite_id"] != newer["suite_id"]:
        raise ForgeError("Runs belong to different suites; choose runs with the same suite ID.")
    old_dir, new_dir = Path(older["path"]), Path(newer["path"])
    missing = [str(directory / name) for directory in (old_dir, new_dir)
               for name in ("plan.json", "environment.json") if not (directory / name).is_file()]
    if missing:
        result = {"schema_version": 1, "comparison_id": new_id("cmp"), "created_at": now(),
                  "older_run_id": older_id, "newer_run_id": newer_id,
                  "status": "not_comparable", "missing_records": missing,
                  "steps": [], "check_passed": False}
        write_json(home.root / "comparisons" / result["comparison_id"] / "result.json", result)
        return result
    old_plan, new_plan = read_json(old_dir / "plan.json"), read_json(new_dir / "plan.json")
    old_env, new_env = read_json(old_dir / "environment.json"), read_json(new_dir / "environment.json")
    old_steps = {step["id"]: step for step in older.get("steps", [])}
    new_steps = {step["id"]: step for step in newer.get("steps", [])}
    old_commands = {s["id"]: _normalized(s, str(old_dir)) for s in old_plan["steps"]}
    new_commands = {s["id"]: _normalized(s, str(new_dir)) for s in new_plan["steps"]}
    changes, invalid, missing_activations = [], [], []
    for step_id in sorted(old_steps.keys() | new_steps.keys()):
        left, right = old_steps.get(step_id), new_steps.get(step_id)
        item = {"id": step_id, "before": left and left["status"],
                "after": right and right["status"],
                "command_changed": old_commands.get(step_id) != new_commands.get(step_id),
                "elapsed_before": left and left.get("elapsed_seconds"),
                "elapsed_after": right and right.get("elapsed_seconds"), "artifacts": []}
        activations = []
        for record, directory in ((left, old_dir), (right, new_dir)):
            activation = directory / "steps" / step_id / "activation.json"
            if record and record["status"] != "blocked":
                if activation.is_file():
                    activations.append(_normalized(read_json(activation), str(directory)))
                else:
                    missing_activations.append(str(activation))
                    activations.append(None)
            else:
                activations.append(None)
        item["activation_changed"] = activations[0] != activations[1]
        a, b = (left or {}).get("artifacts", {}), (right or {}).get("artifacts", {})
        for name in sorted(a.keys() | b.keys()):
            for record, parent in ((a.get(name), old_dir), (b.get(name), new_dir)):
                if record:
                    path = Path(record["snapshot"])
                    if (not path.resolve().is_relative_to(parent.resolve()) or not path.is_file()
                            or digest(path) != record["sha256"]):
                        invalid.append(f"{parent.name}:{step_id}/{name}")
            item["artifacts"].append({"name": name,
                                      "before": a.get(name, {}).get("sha256"),
                                      "after": b.get(name, {}).get("sha256"),
                                      "equal": name in a and name in b and a[name]["sha256"] == b[name]["sha256"]})
        changes.append(item)
    terminal = {"passed", "failed", "interrupted"}
    comparable = not invalid and not missing_activations and older["status"] in terminal and newer["status"] in terminal
    same = (older["status"] == newer["status"] and old_env == new_env
            and old_plan["suite_sha256"] == new_plan["suite_sha256"]
            and all(s["before"] == s["after"] and not s["command_changed"] and not s["activation_changed"]
                    and all(a["equal"] for a in s["artifacts"]) for s in changes))
    status = "not_comparable" if not comparable else ("equal" if same else "different")
    result = {"schema_version": 1, "comparison_id": new_id("cmp"), "created_at": now(),
              "older_run_id": older_id, "newer_run_id": newer_id, "status": status,
              "suite_changed": old_plan["suite_sha256"] != new_plan["suite_sha256"],
              "environment_changed": old_env != new_env or any(s["activation_changed"] for s in changes), "steps": changes,
              "missing_records": missing_activations,
              "invalid_artifacts": invalid,
              "mode": "Exact artifact bytes and recorded commands; no scientific or performance inference.",
              "check_passed": comparable and same and newer["status"] == "passed"}
    write_json(home.root / "comparisons" / result["comparison_id"] / "result.json", result)
    return result

