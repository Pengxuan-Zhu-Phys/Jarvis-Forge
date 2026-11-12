"""Stable environment entries.

Revision scripts stay in their operation directories. Each environment the shell
loads has one metadata file and one short activation script under ``<home>/env``.
"""

from __future__ import annotations

from pathlib import Path
import re
import shlex

from .storage import ForgeError, ForgeHome, identifier, read_json, write_json, write_text


TRACKED = (
    "PATH", "CMAKE_PREFIX_PATH", "CMAKE_INCLUDE_PATH", "CMAKE_LIBRARY_PATH", "PKG_CONFIG_PATH",
    "LD_LIBRARY_PATH", "LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH",
    "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "PYTHONPATH", "ROOTSYS", "MANPATH",
    "JFORGE_ENV_NAME", "JFORGE_ENV_REVISION",
)
_EXPORT = re.compile(r"(?m)^export ([A-Za-z_][A-Za-z0-9_]*)=")


def forge_base(home):
    """User Forge home that owns a profile directory."""
    config = home.root / "config.json"
    if config.is_file():
        recorded = read_json(config).get("apt_base_home")
        if isinstance(recorded, str) and recorded:
            return ForgeHome(Path(recorded))
    return home


def _inside(home, path):
    if not path.resolve().is_relative_to(home.root.resolve()):
        raise ForgeError(f"Environment path escapes the Forge home: {path}")
    for parent in (path, *path.parents):
        if parent == home.root:
            break
        if parent.is_symlink():
            raise ForgeError(f"Managed environment paths must not be symbolic links: {parent}")
    return path


def directory(home, name):
    identifier(name)
    return _inside(home, home.root / "env" / name)


def _selection_path(home):
    return _inside(home, home.root / "env" / "selection.json")


def read_selection(home):
    """Return the selected environment name, or None when the shell is detached.

    A missing selection keeps the historical default and loads apt.
    """
    path = home.root / "env" / "selection.json"
    if not path.is_file():
        return "apt"
    value = read_json(path)
    if value.get("schema_version") != 1:
        raise ForgeError(f"Invalid environment selection: {path}")
    if value.get("detached") is True:
        return None
    name = value.get("name")
    if not isinstance(name, str):
        raise ForgeError(f"Invalid environment selection: {path}")
    identifier(name)
    return name


def write_selection(home, name):
    path = _selection_path(home)
    if name is None:
        write_json(path, {"schema_version": 1, "detached": True})
        return
    identifier(name)
    write_json(path, {"schema_version": 1, "name": name})


def _metadata(home, name):
    path = directory(home, name) / "env.json"
    if not path.is_file():
        return None
    value = read_json(path)
    if value.get("schema_version") != 1 or value.get("name") != name or not isinstance(value.get("builds"), list):
        raise ForgeError(f"Invalid environment metadata: {path}")
    return value


def load_metadata(home, name):
    """Read one environment's metadata file without rewriting the entry."""
    identifier(name)
    record = _metadata(home, name)
    if record is None:
        hint = "jforge apt env" if name == "apt" else f"jforge env show {name}"
        raise ForgeError(f"No metadata for environment {name!r}.", hint=hint)
    return record


def _referenced(script):
    text = script.read_text(encoding="utf-8", errors="replace")
    files = [script]
    for quoted in re.findall(r"(?m)^source\s+'([^']+)'", text):
        child = Path(quoted)
        if child.is_file() and child not in files:
            files.append(child)
    return files


def _variables(scripts):
    names = set(TRACKED)
    seen = []
    for script in scripts:
        for path in _referenced(script):
            if path in seen:
                continue
            seen.append(path)
            for name in _EXPORT.findall(path.read_text(encoding="utf-8", errors="replace")):
                if not name.startswith("_JFORGE_"):
                    names.add(name)
    return sorted(names)


def _activation_text(name, scripts):
    variables = _variables(scripts)
    lines = [
        "#!/usr/bin/env bash",
        f"# Managed by Jarvis-Forge. Source this file to load the {name} environment.",
        "if typeset -f _jforge_env_restore >/dev/null 2>&1; then _jforge_env_restore; fi",
    ]
    for variable in variables:
        lines.append(f"_JFORGE_ENV_HAD_{variable}=${{{variable}+x}}")
        lines.append(f"_JFORGE_ENV_OLD_{variable}=${{{variable}-}}")
    for script in scripts:
        lines.append("source " + shlex.quote(str(script)) + " || return $?")
    for variable in variables:
        lines.append(f"_JFORGE_ENV_NEW_{variable}=${{{variable}-}}")
    lines.append("_jforge_env_restore() {")
    lines.append("  unset -f _jforge_apt_restore _jforge_apt_filter")
    for variable in variables:
        lines += [
            f'  if [ "${{{variable}-}}" = "$_JFORGE_ENV_NEW_{variable}" ]; then',
            f'    if [ "$_JFORGE_ENV_HAD_{variable}" = x ]; then export {variable}="$_JFORGE_ENV_OLD_{variable}"; else unset {variable}; fi',
            "  fi",
            f"  unset _JFORGE_ENV_HAD_{variable} _JFORGE_ENV_OLD_{variable} _JFORGE_ENV_NEW_{variable}",
        ]
    lines += ["  unset -f _jforge_env_restore", "}", "return 0"]
    return "\n".join(lines) + "\n"


def _write(home, name, *, kind, description, revision, profile, backend, builds, omitted_builds=None):
    folder = directory(home, name)
    activation = folder / "activate.sh"
    scripts = [Path(backend)] if backend else []
    scripts.extend(Path(item["activation"]) for item in builds)
    text = _activation_text(name, scripts)
    if not activation.is_file() or activation.read_text(encoding="utf-8") != text:
        write_text(activation, text)
    record = {
        "schema_version": 1, "name": name, "kind": kind, "description": description,
        "revision": revision, "profile": profile, "backend_activation": str(backend) if backend else None,
        "builds": builds, "activation": str(activation),
    }
    if omitted_builds is not None:
        record["omitted_builds"] = list(omitted_builds)
    path = folder / "env.json"
    if not path.is_file() or read_json(path) != record:
        write_json(path, record)
    return record


def _kept_builds(home, name):
    previous = _metadata(home, name) or {}
    kept = []
    for item in previous.get("builds", []):
        activation = item.get("activation")
        if isinstance(activation, str) and Path(activation).is_file() and isinstance(item.get("build_id"), str):
            kept.append({key: item[key] for key in ("build_id", "output", "artifact_id", "activation")})
    return kept


def _omitted(home, name):
    previous = _metadata(home, name) or {}
    omitted = previous.get("omitted_builds", [])
    if not isinstance(omitted, list) or not all(isinstance(item, str) for item in omitted):
        raise ForgeError(f"Invalid environment metadata: {directory(home, name) / 'env.json'}")
    return list(dict.fromkeys(omitted))


def _output_label(item):
    if item.get("build_id") == "root-build":
        return "ROOT"
    return item.get("output") or ""


def _apt_description(builds):
    description = "Active user-local packages"
    names = [name for item in builds if (name := _output_label(item))]
    if names:
        description += " + " + ", ".join(names)
    return description


def _root_record(home, manifest):
    """Return the apt attachment for one ready root-build, when its install is still present."""
    if manifest.get("build_id") != "root-build" or manifest.get("status") != "ready":
        return None
    recorded = manifest.get("activation")
    prefix = manifest.get("prefix")
    if not isinstance(recorded, str) or not Path(recorded).is_file():
        return None
    if not isinstance(prefix, str) or not Path(prefix).is_dir():
        return None
    from .shell_repair import artifact_activation

    try:
        activation = Path(artifact_activation(home, manifest))
    except ForgeError:
        activation = Path(recorded)
    if not activation.is_file():
        return None
    return {
        "build_id": "root-build",
        "output": manifest.get("output") or "ROOT",
        "artifact_id": manifest.get("artifact_id") or "",
        "activation": str(activation),
    }


def _installed_root(home):
    """Use the newest ready root-build when the apt entry does not already name one."""
    directory = home.root / "builds/artifacts"
    if not directory.is_dir():
        return None
    chosen = None
    chosen_mtime = -1.0
    for path in directory.glob("*/manifest.json"):
        try:
            manifest = read_json(path)
            mtime = path.stat().st_mtime
        except (ForgeError, OSError):
            continue
        record = _root_record(home, manifest)
        if record is None or mtime < chosen_mtime:
            continue
        chosen = record
        chosen_mtime = mtime
    return chosen


def publish_apt(base, profile, backend):
    """Point the stable apt entry at this profile revision, keeping attached builds."""
    backend = Path(backend)
    if not backend.is_file():
        raise ForgeError(f"Apt activation script is missing: {backend}")
    state_path = profile.root / "apt/state.json"
    state = read_json(state_path) if state_path.is_file() else {}
    omitted = _omitted(base, "apt")
    builds = _kept_builds(base, "apt")
    if ("root-build" not in omitted and not any(item.get("build_id") == "root-build" for item in builds)):
        installed = _installed_root(base)
        if installed is not None:
            builds.append(installed)
    profile_id = "direct" if profile.root == base.root else profile.root.name
    return _write(
        base, "apt", kind="apt", description=_apt_description(builds),
        revision=state.get("last_operation_id"), profile=profile_id, backend=backend, builds=builds,
        omitted_builds=omitted,
    )


def publish_named(home, record):
    """Point a named environment's stable entry at one retained revision."""
    backend = Path(record["activation"])
    return _write(
        home, record["name"], kind="shell", description=record.get("description") or "",
        revision=record["revision"], profile=None, backend=backend, builds=[],
    )


def attach_build(base, profile, *, build_id, output, artifact_id, activation):
    """Record a verified project activation in the apt entry."""
    identifier(build_id)
    activation = Path(activation)
    if not activation.is_file():
        raise ForgeError(f"Build activation script is missing: {activation}")
    backend = None
    state_path = profile.root / "apt/state.json"
    if state_path.is_file() and read_json(state_path).get("activation"):
        from .package_operations import environment_path

        try:
            backend = Path(environment_path(profile))
        except ForgeError:
            recorded = Path(read_json(state_path)["activation"])
            backend = recorded if recorded.is_file() else None
    builds = [item for item in _kept_builds(base, "apt") if item["build_id"] != build_id]
    builds.append({
        "build_id": build_id, "output": output, "artifact_id": artifact_id, "activation": str(activation),
    })
    profile_id = "direct" if profile.root == base.root else profile.root.name
    state = read_json(state_path) if state_path.is_file() else {}
    omitted = [item for item in _omitted(base, "apt") if item != build_id]
    return _write(
        base, "apt", kind="apt", description=_apt_description(builds),
        revision=state.get("last_operation_id"), profile=profile_id, backend=backend, builds=builds,
        omitted_builds=omitted,
    )


def drop_build(base, profile, build_id):
    """Remove one build from the apt entry without deleting its installation."""
    identifier(build_id)
    builds = _kept_builds(base, "apt")
    omitted = _omitted(base, "apt")
    attached = any(item["build_id"] == build_id for item in builds)
    on_disk = build_id == "root-build" and build_id not in omitted and _installed_root(base) is not None
    if not attached and not on_disk and build_id not in omitted:
        raise ForgeError(
            f"The apt environment has no attached build {build_id!r}.",
            hint="jforge env metadata apt",
        )
    builds = [item for item in builds if item["build_id"] != build_id]
    if build_id not in omitted:
        omitted.append(build_id)
    state_path = profile.root / "apt/state.json"
    state = read_json(state_path) if state_path.is_file() else {}
    recorded = state.get("activation")
    backend = Path(recorded) if isinstance(recorded, str) and Path(recorded).is_file() else None
    profile_id = "direct" if profile.root == base.root else profile.root.name
    return _write(
        base, "apt", kind="apt", description=_apt_description(builds),
        revision=state.get("last_operation_id"), profile=profile_id, backend=backend, builds=builds,
        omitted_builds=omitted,
    )


def attach_root_build(home, result):
    """Add ROOT's activation to the apt entry after that build has passed."""
    if result.get("status") != "passed":
        return None
    selected = None
    groups = [result.get("outputs") or {}]
    groups.extend(node.get("outputs") or {} for node in (result.get("nodes") or {}).values())
    for group in groups:
        for output in group.values():
            if output.get("build_id") == "root-build" and output.get("activation"):
                selected = output
    if selected is None:
        return None
    from .apt_profiles import active_home

    return attach_build(
        home, active_home(home), build_id="root-build", output=selected.get("output") or "ROOT",
        artifact_id=selected.get("artifact_id") or "", activation=selected["activation"],
    )
