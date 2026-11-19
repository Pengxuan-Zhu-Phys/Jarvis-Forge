"""Run the two arithmetic cases from the SARAH-Jarvis demo checkout."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from .storage import read_json, write_json


def _argv(subcommand):
    return [sys.executable, "tests/check", subcommand, "--manifest", "tests/acceptance/demo.json"]


def main(args):
    if args == ["list"]:
        data = json.loads(subprocess.check_output(_argv("collect"), text=True))
        cases = []
        for item in data["cases"]:
            a, b = item["parameters"]["a"], item["parameters"]["b"]
            cases.append({"id": item["id"], "title": f"Sum and product · {a} and {b}",
                          "tags": [item["family"]], "estimate_seconds": item["estimate"]})
        print(json.dumps({"cases": cases}))
        return 0
    if len(args) != 3 or args[0] != "run":
        raise SystemExit("Use list or run REQUEST RESULT_DIR.")
    request, output = read_json(Path(args[1])), Path(args[2]).resolve()
    report = output / "sarah.json"
    argv = _argv("run") + ["--no-promote", "--state-root", str(output / "sarah"),
                          "--report", str(report), "--jobs", str(request["jobs"])]
    for case in request["cases"]:
        argv.extend(("--case", case))
    if request["fresh"]:
        argv.append("--fresh")
    code = subprocess.call(argv)
    statuses = {"fresh_pass": "passed", "reused_pass": "passed", "change_accepted": "passed",
                "excluded": "skipped", "interrupted": "interrupted"}
    rows = []
    if report.is_file():
        for item in read_json(report)["rows"]:
            rows.append({"id": item["case"], "status": statuses.get(item["status"], "failed"),
                         "reason": item.get("reason"), "seconds": item.get("seconds")})
    write_json(output / "result.json", {"cases": rows})
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
