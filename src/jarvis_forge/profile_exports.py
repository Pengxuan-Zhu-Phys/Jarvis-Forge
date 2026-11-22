"""Pinned package identity and variant constraints used by native transactions."""

from copy import deepcopy
import json


from .storage import ForgeError


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
            "Profile package versions or variants differ from the pinned plan:\n"
            + "\n".join(
                f"  {row['name']}: expected {json.dumps(row['expected'], sort_keys=True)}; actual {json.dumps(row['actual'], sort_keys=True)}"
                for row in differences
            )
        )
        raise ForgeError(
            message,
            code=1,
            hint="No package installation was started. Use a compatible preset or update the pinned plan.",
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
