"""The argv and JSON contract for projects that own their test runner."""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys

from .storage import ForgeError


def command_argv(values):
    return [sys.executable if value == "${python}" else value for value in values]


def inventory(project, source):
    argv = command_argv(source["list"])
    try:
        run = subprocess.run(argv, cwd=project["root"], capture_output=True, text=True,
                             check=True, timeout=20)
        payload = json.loads(run.stdout)
    except subprocess.CalledProcessError as exc:
        raise ForgeError(f"Catalog command exited {exc.returncode}. {exc.stderr.strip()}") from exc
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ForgeError(f"Cannot read command catalog: {exc}") from exc
    rows = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ForgeError("Command catalog must return a cases array.")
    result = []
    ids = set()
    prefix = f"{project['id']}/{source['id']}/"
    for row in rows:
        case = row.get("id") if isinstance(row, dict) else None
        if not isinstance(case, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:,+-]{0,255}", case):
            raise ForgeError(f"Invalid command case ID: {case!r}")
        if case in ids:
            raise ForgeError(f"Duplicate command case ID: {case}")
        ids.add(case)
        estimate = row.get("estimate_seconds")
        if estimate is not None and (type(estimate) not in {int, float} or not math.isfinite(estimate) or estimate < 0):
            raise ForgeError(f"Invalid duration estimate for {case}.")
        title = row.get("title", case)
        tags = row.get("tags", [])
        if (not isinstance(title, str) or not title.strip() or not isinstance(tags, list)
                or not all(isinstance(tag, str) for tag in tags)):
            raise ForgeError(f"Invalid title or tags for {case}.")
        result.append({"id": prefix + case, "title": title, "tags": tags, "estimate_seconds": estimate,
                       "project": project["id"], "source": source["id"], "shelf": project["shelf"],
                       "kind": "command", "subjects": [project["id"], *source.get("subjects", [])],
                       "profiles": [], "needs": []})
    return result
