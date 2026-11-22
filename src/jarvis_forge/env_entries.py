"""Stable environment entries.

Revision scripts stay in their operation directories. Each environment the shell
loads has one metadata file and one short activation script under ``<home>/env``.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex

from .build_names import app_name
from .paths import named_path
from .storage import ForgeError, ForgeHome, identifier, read_json, write_json, write_text


TRACKED = (
    "PATH", "CMAKE_PREFIX_PATH", "CMAKE_INCLUDE_PATH", "CMAKE_LIBRARY_PATH", "PKG_CONFIG_PATH",
    "LD_LIBRARY_PATH", "LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH",
    "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "PYTHONPATH", "ROOTSYS", "MANPATH",
    "JFORGE_ENV_NAME", "JFORGE_ENV_REVISION", "JFORGE_ACTIVE_OUTPUTS",
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


def directory(home, name, *, create=False):
    identifier(name)
    return _inside(home, named_path(home.root / "env", name, create=create))


def _selection_path(home):
    return _inside(home, home.root / "env" / "selection.json")


def read_selection(home):
    """Return the selected environment name, or None when the shell is detached.

    A missing selection keeps the historical default and loads apt.
    """
    path = _selection_path(_default_environment_home(home))
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
    path = _selection_path(_default_environment_home(home))
    if name is None:
        write_json(path, {"schema_version": 1, "detached": True})
        return
    identifier(name)
    write_json(path, {"schema_version": 1, "name": name})


def _default_environment_home(home):
    from .apt_profiles import active_home
    from .unified_profiles import record

    selected = active_home(home)
    return selected if record(selected) else home


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
    if name=='apt':
        from .unified_profiles import record as managed_record
        if current:=managed_record(_default_environment_home(home)):return current
    record = _metadata(_default_environment_home(home) if name == "apt" else home, name)
    if record is None:
        hint = "jforge profile show --path" if name == "apt" else f"jforge env show {name}"
        raise ForgeError(f"No metadata for environment {name!r}.", hint=hint)
    return record


def _referenced(script):
    text = script.read_text(encoding="utf-8", errors="replace")
    files = [script]
    for line in text.splitlines():
        if not re.match(r"^source\s", line):
            continue
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        if len(words) > 1:
            child = Path(words[1])
            if child.is_absolute() and child.is_file() and child not in files:
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


def _activation_text(name, scripts, *, managed=None, activation=None):
    lines = [
        "#!/usr/bin/env bash",
        f"# Managed by Jarvis-Forge. Source this file to load the {name} environment.",
    ]
    if managed:
        from .unified_profiles import activation_checks

        lines.extend(activation_checks(managed))
    lines.append("if typeset -f _jforge_env_restore >/dev/null 2>&1; then _jforge_env_restore; fi")
    if managed:
        lines.extend(_profile_tracking(managed, scripts, activation))
        return "\n".join(lines) + "\n"
    variables = _variables(scripts)
    for variable in variables:
        lines.append(f"_JFORGE_ENV_HAD_{variable}=${{{variable}+x}}")
        lines.append(f"_JFORGE_ENV_OLD_{variable}=${{{variable}-}}")
    for script in scripts:
        lines.append("source " + shlex.quote(str(script)) + " || return $?")
    for variable in variables:
        lines.append(f"_JFORGE_ENV_NEW_{variable}=${{{variable}-}}")
    lines.append("_jforge_env_restore() {")
    lines.append("  unset -f _jforge_apt_restore _jforge_apt_filter 2>/dev/null || :")
    for variable in variables:
        lines += [
            f'  if [ "${{{variable}-}}" = "$_JFORGE_ENV_NEW_{variable}" ]; then',
            f'    if [ "$_JFORGE_ENV_HAD_{variable}" = x ]; then export {variable}="$_JFORGE_ENV_OLD_{variable}"; else unset {variable}; fi',
            "  fi", f"  unset _JFORGE_ENV_HAD_{variable} _JFORGE_ENV_OLD_{variable} _JFORGE_ENV_NEW_{variable}",
        ]
    lines += ["  unset -f _jforge_env_restore", "}", "return 0"]
    return "\n".join(lines) + "\n"


def _profile_tracking(managed, scripts, activation):
    from .builds import BUILD_ENV_KEYS

    keys = sorted(BUILD_ENV_KEYS | {"JFORGE_PROFILE_INSTANCE"})
    probe = (f"import json,os,re; print(json.dumps({{k:os.environ[k] for k in {keys!r} if k in os.environ}})); "
             "print('\\n'.join(k for k in sorted(os.environ) "
             "if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',k) and not k.startswith('_JFORGE_') "
             "and k not in {'_', 'SHLVL', 'PWD', 'OLDPWD', 'SHELLOPTS', 'BASHOPTS'}))")
    names = shlex.join([managed["python"]["executable"], "-I", "-c", probe])
    return [r'''_jforge_profile_restore_path() {
  local rest=$1 applied=$2 before=$3 piece out='' more drop
  while :; do
    case "$rest" in *:*) piece=${rest%%:*}; rest=${rest#*:}; more=1 ;; *) piece=$rest; more=0 ;; esac
    drop=0
    case ":$applied:" in
      *":$piece:"*) case ":$before:" in *":$piece:"*) ;; *) drop=1 ;; esac ;;
    esac
    if [ "$drop" = 0 ] && [ -n "$piece" ]; then out=${out:+$out:}$piece; fi
    [ "$more" = 1 ] || break
  done
  printf '%s' "$out"
}
_jforge_env_restore() {
  local _JFORGE_key _JFORGE_had _JFORGE_old _JFORGE_new_had _JFORGE_new _JFORGE_now_had _JFORGE_now _JFORGE_restored
  unset -f _jforge_apt_restore _jforge_apt_filter 2>/dev/null || :
  while IFS= read -r _JFORGE_key; do
    [ -n "$_JFORGE_key" ] || continue
    eval "_JFORGE_had=\${_JFORGE_ENV_HAD_$_JFORGE_key-} _JFORGE_old=\${_JFORGE_ENV_OLD_$_JFORGE_key-} _JFORGE_new_had=\${_JFORGE_ENV_NEW_HAD_$_JFORGE_key-} _JFORGE_new=\${_JFORGE_ENV_NEW_$_JFORGE_key-}"
    unset "_JFORGE_ENV_HAD_$_JFORGE_key" "_JFORGE_ENV_OLD_$_JFORGE_key" "_JFORGE_ENV_NEW_HAD_$_JFORGE_key" "_JFORGE_ENV_NEW_$_JFORGE_key"
    [ "$_JFORGE_had:$_JFORGE_old" != "$_JFORGE_new_had:$_JFORGE_new" ] || continue
    eval "_JFORGE_now_had=\${$_JFORGE_key+x} _JFORGE_now=\${$_JFORGE_key-}"
    if [ "$_JFORGE_now_had:$_JFORGE_now" = "$_JFORGE_new_had:$_JFORGE_new" ]; then
      if [ "$_JFORGE_had" = x ]; then export "$_JFORGE_key=$_JFORGE_old"; else unset "$_JFORGE_key"; fi
    elif [ "$_JFORGE_now_had" = x ]; then
      case "$_JFORGE_key" in *PATH)
        _JFORGE_restored=$(_jforge_profile_restore_path "$_JFORGE_now" "$_JFORGE_new" "$_JFORGE_old")
        export "$_JFORGE_key=$_JFORGE_restored" ;;
      esac
    fi
  done <<< "$_JFORGE_ENV_KEYS"
  unset _JFORGE_ENV_KEYS _JFORGE_PROFILE_BUILD_ENV _JFORGE_ACTIVE_APT_ENTRY
  unset -f _jforge_env_restore _jforge_profile_restore_path
}
_jforge_profile_load() {
  local _JFORGE_key _JFORGE_before _JFORGE_after _JFORGE_status=0''',
            f"  _JFORGE_before=$({names}) || return $?",
            r'''  while IFS= read -r _JFORGE_key; do
    [ -n "$_JFORGE_key" ] || continue
    eval "_JFORGE_ENV_HAD_$_JFORGE_key=x _JFORGE_ENV_OLD_$_JFORGE_key=\${$_JFORGE_key-}"
  done <<< "${_JFORGE_before#*$'\n'}"''',
            f'  [ "$_JFORGE_status" != 0 ] || export JFORGE_PROFILE_INSTANCE={shlex.quote(managed["id"])}',
            *[f'  [ "$_JFORGE_status" != 0 ] || source {shlex.quote(str(script))} || _JFORGE_status=$?' for script in scripts],
            f"  _JFORGE_after=$({names}) || return $?",
            r'''  _JFORGE_ENV_KEYS="${_JFORGE_before#*$'\n'}"$'\n'"${_JFORGE_after#*$'\n'}"
  while IFS= read -r _JFORGE_key; do
    [ -n "$_JFORGE_key" ] || continue
    eval "_JFORGE_ENV_NEW_HAD_$_JFORGE_key=x _JFORGE_ENV_NEW_$_JFORGE_key=\${$_JFORGE_key-}"
  done <<< "${_JFORGE_after#*$'\n'}"
  if [ "$_JFORGE_status" = 0 ]; then
    export _JFORGE_PROFILE_BUILD_ENV="${_JFORGE_before%%$'\n'*}"$'\n'"${_JFORGE_after%%$'\n'*}"
  else
    _jforge_env_restore
  fi
  return "$_JFORGE_status"
}
if _jforge_profile_load; then
  unset -f _jforge_profile_load
else
  unset -f _jforge_profile_load
  return 1
fi''', f"_JFORGE_ACTIVE_APT_ENTRY={shlex.quote(str(activation))}", "return 0"]


def _write(home, name, *, kind, description, revision, profile, backend, builds, omitted_builds=None, managed=None, app_choices=None):
    choices = {}
    if managed:
        choices = _app_choices(home) if app_choices is None else app_choices
    folder = directory(home, name, create=True)
    activation = home.root / "activate.sh" if managed and name == "apt" else folder / "activate.sh"
    scripts = [Path(backend)] if backend else []
    if managed:
        scripts.append(Path(managed["tools"]["activation"]))
    scripts.extend(Path(item["activation"]) for item in builds)
    text = _activation_text(name, scripts, managed=managed, activation=activation)
    if not activation.is_file() or activation.read_text(encoding="utf-8") != text:
        write_text(activation, text)
    record = {
        "schema_version": 1, "name": name, "kind": kind, "description": description,
        "revision": revision, "profile": profile, "backend_activation": str(backend) if backend else None,
        "builds": builds, "activation": str(activation),
    }
    if omitted_builds is not None:
        record["omitted_builds"] = list(omitted_builds)
    if choices:
        record["app_choices"] = choices
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
    return list(dict.fromkeys(app_name(name) for name in omitted))


def _app_choices(home):
    choices = (_metadata(home, "apt") or {}).get("app_choices", {})
    if not isinstance(choices, dict) or not all(isinstance(item, str) for item in choices.values()):
        raise ForgeError(f"Invalid app choices: {directory(home, 'apt') / 'env.json'}")
    return {app_name(name): choice for name, choice in choices.items()}


def _output_manifests(base, instance):
    for path in sorted((base.root / "builds/artifacts").glob("*/manifest.json")):
        item = read_json(_inside(base, path))
        if (item.get("profile_instance") == instance and item.get("status") == "ready"
                and item.get("artifact_id") == path.parent.name
                and isinstance(item.get("prefix"), str) and isinstance(item.get("activation"), str)
                and Path(item["prefix"]).is_dir() and Path(item["activation"]).is_file()):
            yield item


def _apt_builds(base, owner, instance, omitted):
    builds = [item for item in _kept_builds(owner, "apt") if app_name(item["build_id"]) not in omitted]
    if ("root" not in omitted and (not instance or "root" not in _app_choices(owner))
            and not any(app_name(item["build_id"]) == "root" for item in builds)):
        if installed := _installed_root(base, instance):
            if not instance or not _output_conflict(base, [*builds, installed]):
                builds.append(installed)
    return builds


def _output_dependencies(base, artifact_ids):
    pending = list(artifact_ids)
    seen, found = set(), {}
    while pending:
        artifact_id = identifier(pending.pop())
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        path = _inside(base, base.root / "builds/artifacts" / artifact_id / "manifest.json")
        if path.is_file():
            record = read_json(path)
            found[artifact_id] = record
            pending.extend(record.get("dependencies", ()))
    return found


def _output_conflict(base, builds):
    by_output = {}
    for app in builds:
        dependencies = _output_dependencies(base, (app["artifact_id"],))
        for output in (app, *dependencies.values()):
            build_id, artifact_id = app_name(output["build_id"]), output["artifact_id"]
            previous = by_output.setdefault((build_id, output["output"]), artifact_id)
            if previous != artifact_id:
                return build_id, previous, artifact_id
    return None


def profile_outputs(base, profile, selector=None, *, enabled=None):
    """List and change the current managed profile's runtime output selection."""
    from .unified_profiles import record

    managed = record(profile)
    if not managed:
        raise ForgeError("Select a built-in profile to manage its apps.", hint="jforge profile")
    instance = managed["id"]
    manifests = list(_output_manifests(base, instance))

    def rows(builds):
        active = {item["artifact_id"] for item in builds}
        active.update(_output_dependencies(base, active))
        return [{**{key: item[key] for key in ("build_id", "artifact_id", "output", "version", "prefix")},
                 "app": app_name(item["build_id"]), "enabled": item["artifact_id"] in active} for item in manifests]

    if enabled is None:
        return {"profile": instance, "outputs": rows(_apt_builds(base, profile, instance, _omitted(profile, "apt")))}

    entry = _set_profile_output(base, profile, managed, manifests, selector, enabled)
    selected = next((item["build_id"] for item in manifests if selector == item["artifact_id"]), selector)
    return {"profile": instance, "outputs": rows(entry["builds"]), "changed": selector,
            "app": app_name(selected), "enabled": enabled}


def _set_profile_output(base, profile, managed, manifests, selector, enabled):
    from .builds import artifact
    from .package_operations import environment_path
    from .shell_repair import artifact_activation

    identifier(selector)
    instance = managed["id"]
    with _output_lock(profile, managed):
        omitted = _omitted(profile, "apt")
        choices = _app_choices(profile)
        builds = _apt_builds(base, profile, instance, omitted)
        exact = next((item for item in manifests if selector == item["artifact_id"]), None)
        build_id = app_name(exact["build_id"] if exact else selector)
        matches = [item for item in manifests if app_name(item["build_id"]) == build_id]
        current = next((item for item in builds if app_name(item["build_id"]) == build_id), None)
        if enabled:
            if not matches:
                raise ForgeError(f"No installed output matches {selector!r} in {instance}.",
                                 hint="jforge build list")
            preferred = choices.get(build_id) or (current and current["artifact_id"])
            value = exact or next((item for item in matches if item["artifact_id"] == preferred), None)
            if value is None and len(matches) == 1 and not preferred:
                value = matches[0]
            if value is None:
                raise ForgeError(f"Choose an exact output ID for {build_id}.", hint="jforge build list")
            value = artifact(base, value["artifact_id"])
            dependencies = _output_dependencies(base, value.get("dependencies", ()))
            if blocked := sorted({app_name(item["build_id"]) for item in dependencies.values()} & set(omitted)):
                raise ForgeError(f"{build_id} needs disabled output {blocked[0]}.",
                                 hint=f"jforge build install {blocked[0]}")
            builds = [item for item in builds if app_name(item["build_id"]) != build_id]
            builds.append({"build_id": value["build_id"], "artifact_id": value["artifact_id"],
                           "output": value["output"], "activation": str(artifact_activation(base, value))})
            if conflict := _output_conflict(base, builds):
                name, first, second = conflict
                raise ForgeError(f"This selection would load two versions of {name}: {first} and {second}.",
                                 hint="Turn off apps that need the other version, then retry with an exact output ID.")
            choices[build_id] = value["artifact_id"]
            omitted = [name for name in omitted if name != build_id]
        else:
            if not matches and not current and build_id not in omitted:
                raise ForgeError(f"No installed output matches {selector!r} in {instance}.",
                                 hint="jforge build list")
            selected_id = current["artifact_id"] if current else choices.get(build_id)
            disable = (not exact or selected_id == selector
                       or (selected_id is None and len(matches) == 1))
            keep = [item for item in builds if not disable or app_name(item["build_id"]) != build_id]
            for item in keep:
                dependencies = _output_dependencies(base, (item["artifact_id"],))
                if any(dep["artifact_id"] != item["artifact_id"]
                       and (app_name(dep["build_id"]) == build_id if disable else dep["artifact_id"] == selector)
                       for dep in dependencies.values()):
                    dependent = app_name(item["build_id"])
                    raise ForgeError(f"{dependent} still loads {build_id}.",
                                     hint=f"jforge build list")
            if disable:
                builds = keep
                if current or exact:
                    choices[build_id] = current["artifact_id"] if current else exact["artifact_id"]
                if build_id not in omitted:
                    omitted.append(build_id)
        state = read_json(profile.root / "packages.json")
        return _write(profile, "apt", kind="apt", description=_apt_description(builds),
                      revision=state.get("last_operation_id"), profile=instance, backend=environment_path(profile),
                      builds=builds, omitted_builds=omitted, managed=managed, app_choices=choices)


def _output_matches(item, names):
    return bool(names.intersection({app_name(item["build_id"]), app_name(item.get("package", ""))}))


def require_outputs_off(base, profile, names, environment):
    from .unified_profiles import record

    managed = record(profile)
    instance = managed["id"] if managed else None
    visible = {app_name(name) for name in environment.get("JFORGE_ACTIVE_OUTPUTS", "").split(":")}
    hint = "Reload the profile in a fresh shell and retry the build."
    if found := sorted(names & visible):
        raise ForgeError(f"Excluded output {found[0]} is still loaded in the build environment.", hint=hint)
    # Older output scripts lack an activation marker. Check their known prefixes too.
    search = {Path(part).resolve() for key, value in environment.items() if key.endswith("PATH")
              for part in value.split(":") if part.startswith("/")}
    for item in _output_manifests(base, instance):
        if _output_matches(item, names):
            prefix = Path(item["prefix"]).resolve()
            if any(path.is_relative_to(prefix) for path in search):
                raise ForgeError(f"Excluded output {app_name(item['build_id'])} has paths in the build environment.", hint=hint)


def _before_profile(environment, instance):
    """Undo the activated profile's build-facing exports, preserving later user additions."""
    result = dict(environment)
    if snapshot := result.pop("_JFORGE_PROFILE_BUILD_ENV", None):
        try:
            before, after = (json.loads(line) for line in snapshot.splitlines())
            if (not all(isinstance(values, dict) and all(isinstance(value, str) for value in values.values())
                        for values in (before, after)) or after.get("JFORGE_PROFILE_INSTANCE") != instance):
                raise ValueError("Profile instance changed.")
        except (ValueError, TypeError, KeyError) as exc:
            raise ForgeError("Cannot read the active profile environment.",
                             hint="Reload the profile in a fresh shell and retry.") from exc
        for key in before.keys() | after.keys():
            old, applied = before.get(key), after.get(key)
            if old == applied:
                continue
            current = result.get(key)
            if current == applied:
                if old is None:
                    result.pop(key, None)
                else:
                    result[key] = old
            elif key.endswith("PATH") and current is not None:
                added = set((applied or "").split(":")) - set((old or "").split(":"))
                result[key] = ":".join(part for part in current.split(":") if part not in added)
    return result


def prepare_build_environment(base, profile, managed, names, environment, script):
    """Activate a filtered view of the existing profile in a build subprocess."""
    from . import cmake
    from .builds import safe_environment
    from .package_operations import environment_path
    from .unified_profiles import activation_checks

    instance = managed["id"]
    if environment.get("JFORGE_PROFILE_INSTANCE") not in {None, "", instance}:
        raise ForgeError("Shell profile differs from the saved build profile.",
                         hint="Load the build's profile in a fresh shell and retry.")
    clean = _before_profile(environment, instance)
    require_outputs_off(base, profile, names, clean)
    with _output_lock(profile, managed):
        apps = ([row for row in managed.get("apps",[]) if row.get("enabled") and not row.get("external")]
                if managed.get("schema_version")==3 else _apt_builds(base, profile, instance, _omitted(profile, "apt")))
        scripts = [environment_path(profile), Path(managed["tools"]["activation"])]
        suppressed = []
        for app in apps:
            dependencies = _output_dependencies(base, (app["artifact_id"],))
            if any(_output_matches(item, names) for item in (app, *dependencies.values())):
                suppressed.append(app_name(app["build_id"]))
            else:
                scripts.append(app["activation"])
        lines = activation_checks(managed)
        lines.append("export JFORGE_PROFILE_INSTANCE="+shlex.quote(instance))
        lines.extend("source " + shlex.quote(str(item)) + " || return $?" for item in scripts)
        write_text(script, "\n".join(lines) + "\n")
    result = cmake.capture(script, environment=safe_environment(clean))
    require_outputs_off(base, profile, names, result)
    return result, suppressed


def _output_label(item):
    if app_name(item.get("build_id") or "") == "root":
        return "ROOT"
    return item.get("output") or ""


def _apt_description(builds):
    description = "Active user-local packages"
    names = [name for item in builds if (name := _output_label(item))]
    if names:
        description += " + " + ", ".join(names)
    return description


def _root_record(home, manifest):
    """Return the apt attachment for one ready ROOT installation, when its install is still present."""
    if app_name(manifest.get("build_id") or "") != "root" or manifest.get("status") != "ready":
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
        "build_id": manifest["build_id"],
        "output": manifest.get("output") or "ROOT",
        "artifact_id": manifest.get("artifact_id") or "",
        "activation": str(activation),
    }


def _installed_root(home, instance=None):
    """Use the newest ready ROOT when the apt entry does not already name one."""
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
        record = _root_record(home, manifest) if manifest.get("profile_instance") == instance else None
        if record is None or mtime < chosen_mtime:
            continue
        chosen = record
        chosen_mtime = mtime
    return chosen


def publish_apt(base, profile, backend, *, profile_record=None):
    """Point the stable apt entry at this profile revision, keeping attached builds."""
    backend = Path(backend)
    if not backend.is_file():
        raise ForgeError(f"Apt activation script is missing: {backend}")
    from .unified_profiles import record

    managed = profile_record or record(profile)
    if managed and managed.get('schema_version')==3:
        from .unified_profiles import refresh_activation
        entry=refresh_activation(profile)
        state=read_json(profile.root/'packages.json')
        return {'name':'apt','kind':'apt','description':'Profile '+managed['id'],'revision':state.get('last_operation_id'),
                'profile':managed['id'],'backend_activation':str(backend),'activation':entry,
                'builds':managed.get('apps',[])}
    owner = profile if managed else base
    with _output_lock(owner, managed):
        state_path = profile.root / "packages.json"
        state = read_json(state_path) if state_path.is_file() else {}
        omitted = _omitted(owner, "apt")
        builds = _apt_builds(base, owner, managed["id"] if managed else None, omitted)
        profile_id = "direct" if profile.root == base.root else profile.root.name
        return _write(
            owner, "apt", kind="apt", description=_apt_description(builds),
            revision=state.get("last_operation_id"), profile=profile_id, backend=backend, builds=builds,
            omitted_builds=omitted, managed=managed,
        )


def _output_lock(owner, managed):
    if managed:
        from .builds import file_lock

        return file_lock(_inside(owner, owner.root / "env/apt.lock"), blocking=True)
    from contextlib import nullcontext

    return nullcontext()


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
    state_path = profile.root / "packages.json"
    if state_path.is_file() and read_json(state_path).get("activation"):
        from .package_operations import environment_path

        try:
            backend = Path(environment_path(profile))
        except ForgeError:
            recorded = Path(read_json(state_path)["activation"])
            backend = recorded if recorded.is_file() else None
    from .unified_profiles import record

    managed = record(profile)
    if managed and managed.get('schema_version')==3:
        manifest=base.root/'builds/artifacts'/artifact_id/'manifest.json'
        output_record=read_json(manifest) if manifest.is_file() else {}
        app={'name':app_name(build_id),'build_id':build_id,'output':output,'artifact_id':artifact_id,
             'activation':str(activation),'prefix':output_record.get('prefix'),
             'packages':output_record.get('runtime_packages',[]),'external':output_record.get('workspace_external',bool(output_record.get('workspace'))),
             'workspace':output_record.get('workspace'),'in_place':bool(output_record.get('in_place')),
             'enabled':True,'version':output_record.get('version'),'metadata':str(manifest)}
        existing=[row for row in managed.get('apps',[]) if row['artifact_id']!=artifact_id and not (row['name']==app['name'] and row['version']==app['version'])]
        for row in existing:
            if row['name']==app['name']:row['enabled']=False
        managed['apps']=existing+[app]
        write_json(profile.root/'metadata.json',managed)
        return publish_apt(base,profile,backend)
    owner = profile if managed else base
    name = app_name(build_id)
    with _output_lock(owner, managed):
        omitted = _omitted(owner, "apt")
        if managed:
            dependencies = _output_dependencies(base, (artifact_id,)) if artifact_id else {}
            chosen = _app_choices(owner).get(name)
            if (name in omitted or (chosen is not None and chosen != artifact_id)
                    or any(app_name(item["build_id"]) in omitted for item in dependencies.values())):
                return None
        builds = [item for item in _kept_builds(owner, "apt") if app_name(item["build_id"]) != name]
        builds.append({
            "build_id": build_id, "output": output, "artifact_id": artifact_id, "activation": str(activation),
        })
        if managed and _output_conflict(base, builds):
            return None
        profile_id = "direct" if profile.root == base.root else profile.root.name
        state = read_json(state_path) if state_path.is_file() else {}
        return _write(
            owner, "apt", kind="apt", description=_apt_description(builds),
            revision=state.get("last_operation_id"), profile=profile_id, backend=backend, builds=builds,
            omitted_builds=[item for item in omitted if item != name], managed=managed,
        )


def drop_build(base, profile, build_id):
    """Remove one build from the apt entry without deleting its installation."""
    identifier(build_id)
    from .unified_profiles import record

    managed = record(profile)
    if managed and managed.get('schema_version')==3:
        from .build_apps import installed,_dependent_apps,locked
        from .unified_profiles import refresh_activation,sync_definition
        with locked(profile):
            managed=record(profile)
            rows=[row for row in installed(base,profile) if row['name']==app_name(build_id) and row.get('enabled')]
            if not rows:raise ForgeError(f'No enabled app named {app_name(build_id)}.')
            _dependent_apps(base,profile,rows)
            for row in managed.get('apps',[]):
                if row['name']==app_name(build_id):row['enabled']=False
            write_json(profile.root/'metadata.json',managed)
            refresh_activation(profile);sync_definition(profile)
            state=read_json(profile.root/'packages.json')
            return publish_apt(base,profile,state['activation'])
    if managed:
        manifests = list(_output_manifests(base, managed["id"]))
        return _set_profile_output(base, profile, managed, manifests, build_id, False)
    build_id = app_name(build_id)
    owner = base
    builds = _kept_builds(owner, "apt")
    omitted = _omitted(owner, "apt")
    attached = any(app_name(item["build_id"]) == build_id for item in builds)
    on_disk = (build_id == "root" and build_id not in omitted
               and _installed_root(base) is not None)
    if not attached and not on_disk and build_id not in omitted:
        raise ForgeError(
            f"The apt environment has no attached build {build_id!r}.",
            hint="jforge env metadata apt",
        )
    builds = [item for item in builds if app_name(item["build_id"]) != build_id]
    if build_id not in omitted:
        omitted.append(build_id)
    state_path = profile.root / "packages.json"
    state = read_json(state_path) if state_path.is_file() else {}
    recorded = state.get("activation")
    backend = Path(recorded) if isinstance(recorded, str) and Path(recorded).is_file() else None
    profile_id = "direct" if profile.root == base.root else profile.root.name
    return _write(
        owner, "apt", kind="apt", description=_apt_description(builds),
        revision=state.get("last_operation_id"), profile=profile_id, backend=backend, builds=builds,
        omitted_builds=omitted, managed=managed,
    )


def attach_build_outputs(home, result):
    """Register every successfully verified app output in its owning profile."""
    if result.get("status") != "passed":
        return None
    from .apt_profiles import active_home

    profile = ForgeHome(Path(result["profile_path"])) if result.get("profile_instance") else active_home(home)
    attached = None
    groups = [result.get("outputs") or {}]
    groups.extend(node.get("outputs") or {} for node in (result.get("nodes") or {}).values())
    for group in groups:
        for output in group.values():
            if output.get("activation"):
                entry = attach_build(home, profile, build_id=output["build_id"], output=output.get("output") or "ROOT",
                                     artifact_id=output.get("artifact_id") or "", activation=output["activation"])
                attached = entry or attached
    return attached
