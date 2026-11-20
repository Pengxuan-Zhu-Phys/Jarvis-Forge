"""Select package profiles and preserve old source installations as history."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from .paths import named_path
from .storage import ForgeError, ForgeHome, identifier, read_json, write_json
from .package_operations import _inside, environment_path, locked, operation


def configured(home):
    return (home.root / "apt/spack.json").is_file()


def backend_name(home):
    metadata = home.root / "apt/profile.json"
    inputs = home.root / "apt/profile-input.json"
    value = (read_json(metadata) if metadata.is_file() else
             read_json(inputs).get("inputs", {}).get("preset", {}) if inputs.is_file() else {})
    if value.get("backend") in {"homebrew", "spack"}:
        return value["backend"]
    is_brew_profile = (home.root.name.startswith("homebrew-")
                       and home.root.parent.name == "profiles" and home.root.parent.parent.name == "apt")
    return "homebrew" if is_brew_profile or _is_configured_homebrew(home) else "spack"


def _selection(base):
    from .unified_profiles import selection

    if record := selection(base):
        return record
    path = _inside(base, base.root / "apt/selection.json")
    if not path.exists():
        return None
    value = read_json(path)
    if (value.get("schema_version") != 1 or not isinstance(value.get("id"), str)
            or ("platform" in value and not isinstance(value["platform"], str))
            or ("pending" in value and not isinstance(value["pending"], bool))):
        raise ForgeError(
            f"Invalid package environment selection: {path}",
            hint="jforge apt import spack",
        )
    identifier(value["id"])
    return value


def _profile(base, profile_id):
    return ForgeHome(_inside(base, named_path(base.root / "apt/profiles", identifier(profile_id))))


def _native_profile(base):
    return _profile(base, "homebrew-mac" if sys.platform == "darwin" else "spack-default")


def _recorded_profile(base, record):
    selected = record.get("id")
    if record.get("platform") not in {None, sys.platform}:
        return None
    if record.get("location") == "root":
        return base if selected == "direct" and configured(base) else None
    if selected and selected not in {"legacy", "direct"}:
        target = _profile(base, selected)
        if (target.root / "config.json").is_file() and (
                record.get("pending") or configured(target) or _is_configured_homebrew(target)):
            return target
    return None


def active_home(base):
    from .unified_profiles import record, selection

    if managed := selection(base):
        target = _profile(base, managed["id"])
        if not record(target):
            raise ForgeError(f"The default profile instance is missing: {managed['id']}.",
                             hint="Run jforge apt profile check and select a ready profile with apt profile global.")
        return target
    record = _selection(base) or {}
    target = _recorded_profile(base, record)
    native = _native_profile(base)
    if target and (record.get("platform") or backend_name(target) == backend_name(native)):
        return target
    # Existing direct Spack deployments remain valid. Legacy selections do not
    # execute the removed backend and are replaced only after a successful write.
    if record.get("id") in {None, "legacy"} and configured(base) and backend_name(native) == "spack":
        return base
    return native


def archived_homes(base):
    homes = [base] + [
        ForgeHome(_inside(base, path.parent))
        for path in sorted((base.root / "apt/profiles").glob("*/config.json"))
    ]
    return [
        home
        for home in homes
        if not configured(home) and not _is_configured_homebrew(home)
        and (
            any((home.root / "packages").rglob("manifest.json"))
            or any(
                (home.root / "apt" / name).exists()
                for name in ("state.json", "official.json", "sources.json")
            )
        )
    ]


def archived_packages(base):
    return [
        dict(
            row,
            archived=True,
            active=False,
            profile="legacy" if home == base else home.root.name,
        )
        for home in archived_homes(base)
        for row in home.packages()
    ]


def profiles(base, *, archived=False):
    selected = active_home(base)
    record = _selection(base) or {}
    recorded = _recorded_profile(base, record)
    homes = [base] if configured(base) else []
    for path in sorted((base.root / "apt/profiles").glob("*/config.json")):
        target = ForgeHome(_inside(base, path.parent))
        if read_json(path).get("toolchain_store") and target != selected:
            continue
        if configured(target) or _is_configured_homebrew(target):
            homes.append(target)
    if selected not in homes:
        homes.append(selected)
    rows = [
        dict(
            id="direct" if home == base else home.root.name,
            path=str(home.root),
            backend=backend_name(home),
            active=home == selected,
            **({"configured": False} if not configured(home) and not _is_configured_homebrew(home) else {}),
        )
        for home in homes
    ]
    missing = record.get("id", "")
    if missing and missing != "legacy" and (
            record.get("platform") in {None, sys.platform} and recorded is None):
        missing_path = base if missing == "direct" else _profile(base, missing)
        row = {"id": missing, "path": str(missing_path.root), "backend": backend_name(missing_path),
               "active": False, "configured": False, "status": "missing"}
        if missing_path == selected:
            rows = [row if item["id"] == missing else item for item in rows]
        else:
            rows.append(row)
    if archived:
        rows += [
            dict(
                id="legacy" if home == base else home.root.name,
                path=str(home.root),
                backend="archived",
                active=False,
                operations=str(home.root / "operations"),
            )
            for home in archived_homes(base)
        ]
    return rows


def require_healthy_profile(profile, data):
    """Fail closed for a published profile; only pending profiles may bootstrap."""
    backend = data["backend"]
    marker = profile.root / "apt" / ("spack.json" if backend == "spack" else "homebrew.json")
    state_path = profile.root / "apt/state.json"
    if not (marker.is_file() and state_path.is_file()):
        raise ForgeError(f"Package backend is incomplete for profile {data['id']}: {profile.root / 'apt'}.",
                         hint="Restore this profile's backend records or explicitly select another profile.")
    state = read_json(_inside(profile, state_path))
    activation = state.get("activation")
    if not state.get(backend) or not isinstance(activation, str) or not _inside(profile, Path(activation)).is_file():
        raise ForgeError(f"Published package environment is incomplete for profile {data['id']}: {state_path}.",
                         hint="Restore this profile's backend records or explicitly select another profile.")
    if backend == "spack":
        from . import spack_backend

        spack_backend._executable(profile)


def validate_active(profile, *, environment=None):
    """Verify a unified profile before apt can read or write its backend."""
    from . import unified_profiles

    if data := unified_profiles.record(profile):
        unified_profiles.verify(profile, data)
        unified_profiles.validate_python(data, os.environ if environment is None else environment)


def ensure_active(base, *, progress=None, selected=None):
    from .unified_profiles import record

    target = selected or active_home(base)
    if data := record(target):
        require_healthy_profile(target, data)
        return target
    if selected and configured(selected) and (selected.root / "apt/state.json").is_file():
        return selected
    active = active_home(base) if selected else target
    selected = target
    pending = selected == active and (_selection(base) or {}).get("pending")
    if (configured(selected) and not pending) or _is_configured_homebrew(selected):
        return selected
    if backend_name(selected) == "homebrew":
        create_homebrew(base, selected.root.name)
        return active_home(base)
    switch(
        base, "spack", name=selected.root.name.removeprefix("spack-"), progress=progress,
        select=selected == active,
    )
    return selected


def _is_configured_homebrew(home):
    from .homebrew_backend import configured as homebrew_configured

    return homebrew_configured(home)


def initialize_profile(base):
    """Select a host profile without downloading packages or bootstrapping Spack."""
    from . import homebrew_backend

    if sys.platform not in {"linux", "darwin"}:
        return None
    with locked(base):
        from .unified_profiles import record as managed_record, selection

        if selection(base):
            selected = active_home(base)
            managed = managed_record(selected)
            require_healthy_profile(selected, managed)
            return {"id": managed["id"], "backend": managed["backend"], "status": "selected"}
        record = _selection(base) or {}
        selected = active_home(base)
        native = "homebrew" if sys.platform == "darwin" else "spack"
        selected_backend = backend_name(selected)
        profile_id = "direct" if selected == base else selected.root.name
        if (record.get("platform") == sys.platform and not record.get("pending")
                and _recorded_profile(base, record) == selected):
            return {"id": profile_id, "backend": selected_backend, "status": "selected"}
        if selected_backend == native and (configured(selected) or _is_configured_homebrew(selected)):
            if (selected.root / "apt/state.json").is_file():
                _write_selection(base, selected, profile_id)
                return {"id": profile_id, "backend": native, "status": "selected"}
        profile_id = "homebrew-mac" if native == "homebrew" else "spack-default"
        target = _profile(base, profile_id)
        if native == "homebrew" and homebrew_backend.find_brew():
            with operation(base, "initialize-profile", {"profile": profile_id, "backend": native}) as (_, result):
                if not (_is_configured_homebrew(target) and (target.root / "apt/state.json").is_file()):
                    with locked(target):
                        homebrew_backend.initialize(target, base, homebrew_backend.discover())
                _save_selection(base, target, profile_id, result)
            return {"id": profile_id, "backend": native, "status": "selected"}
        with locked(target):
            _write_selection(base, target, profile_id, pending=True)
        return {"id": profile_id, "backend": native, "status": "pending"}


def _write_selection(base, target, profile_id, *, pending=False):
    from .unified_profiles import clear_default

    selection = {"schema_version": 1, "id": profile_id, "platform": sys.platform}
    if target == base:
        selection["location"] = "root"
    if pending:
        selection["pending"] = True
    write_json(_inside(base, base.root / "apt/selection.json"), selection)
    clear_default(base)


def _save_selection(base, target, profile_id, result):
    from .spack_backend import store_root

    backend = backend_name(target)
    activation = environment_path(target)
    if backend == "homebrew":
        from .homebrew_backend import settings

        packages = settings(target)["prefix"]
    else:
        packages = str(store_root(target))
    _write_selection(base, target, profile_id)
    result.update(profile=profile_id, backend=backend, apt_home=str(target.root),
                  user_home=str(base.root), packages=packages, activation=activation, state_committed=True)
    return result


def use(base, name):
    """Select an existing profile without bootstrapping or installing packages."""
    identifier(name)
    ids = [name] if name == "direct" or _profile(base, name).root.is_dir() else [
        identifier(kind + "-" + name) for kind in ("spack", "homebrew")]
    candidates = [(profile_id, base if profile_id == "direct" else _profile(base, profile_id)) for profile_id in ids]
    found = [(profile_id, target) for profile_id, target in candidates
             if configured(target) or _is_configured_homebrew(target)]
    if not found:
        raise ForgeError(f"No configured package profile: {name}", hint="jforge apt profile")
    if len(found) != 1:
        raise ForgeError(f"Ambiguous profile name: {name}. Select {' or '.join(key for key, _ in found)}.")
    profile_id, target = found[0]
    from .unified_profiles import record, set_default

    if record(target):
        result = set_default(base, profile_id)
        return {**result, "profile": profile_id, "apt_home": str(target.root),
                "user_home": str(base.root), "state_committed": True}
    with locked(base), operation(base, "use", {"profile": profile_id, "backend": backend_name(target)}) as (_, result):
        return _save_selection(base, target, profile_id, result)


def create_homebrew(base, name):
    from . import homebrew_backend

    profile_id = named_path(base.root / "apt/profiles", identifier("homebrew-" + identifier(name.removeprefix("homebrew-"))), create=True).name
    target = _profile(base, profile_id)
    config = homebrew_backend.discover()
    with locked(base), operation(base, "create", {"profile": profile_id, "backend": "homebrew"}) as (_, result):
        if homebrew_backend.configured(target) and (target.root / "apt/state.json").is_file():
            raise ForgeError(f"Homebrew profile already exists: {profile_id}", hint=f"jforge apt profile {profile_id}")
        with locked(target):
            homebrew_backend.initialize(target, base, config)
        return _save_selection(base, target, profile_id, result)


def switch(
    base, origin="spack", *, name="default", direct=False, progress=None, create_only=False, select=True, extensions=None,
    toolchain_store=False, instance=None,
):
    from . import spack_backend as backend

    if origin != "spack":
        raise ForgeError(
            "Ubuntu/Debian source backends have been removed; use Spack.",
            hint="jforge apt import spack\njforge apt profile --archived",
        )
    if direct:
        if name != "default" or not backend.configured(base):
            raise ForgeError(
                "--direct selects an existing root Spack environment and cannot use --name."
            )
        prepared = backend.bootstrap(base, progress=progress)
        with (
            locked(base),
            operation(base, "switch", {"profile": "direct", "backend": "spack"}) as (
                directory,
                result,
            ),
        ):
            result["bootstrap"] = prepared
            return _save_selection(base, base, "direct", result)
    profile_id = identifier(instance) if instance else named_path(base.root / "apt/profiles", identifier("spack-" + identifier(name)), create=True).name
    target = _profile(base, profile_id)
    with (
        locked(base),
        operation(base, "create" if create_only else "switch", {"profile": profile_id, "backend": "spack"}) as (
            directory,
            result,
        ),
    ):
        if create_only and configured(target):
            state = _inside(target, target.root / "apt/state.json")
            if state.is_file() and read_json(state).get("activation"):
                raise ForgeError(f"Spack profile already exists: {profile_id}", hint=f"jforge apt profile {profile_id}")
        target.initialize()
        config = read_json(target.root / "config.json")
        config["apt_base_home"] = str(base.root)
        if (inputs := target.root / "apt/profile-input.json").is_file():
            config["profile_store"] = read_json(inputs).get("layout") == 2
        if toolchain_store:
            config["toolchain_store"] = True
        write_json(target.root / "config.json", config)
        options = {} if extensions is None else {"extensions": extensions}
        result["bootstrap"] = backend.bootstrap(target, progress=progress, **options)
        if select:
            return _save_selection(base, target, profile_id, result)
        result.update(profile=profile_id, apt_home=str(target.root))
        return result
