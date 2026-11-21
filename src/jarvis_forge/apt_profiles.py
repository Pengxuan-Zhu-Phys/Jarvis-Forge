"""Select a project profile without implicit Linux compiler installation."""

from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import sys

from .storage import ForgeError, ForgeHome, read_json
from .paths import named_path


PROFILE_OVERRIDE = ContextVar("forge_profile_override", default=None)


@contextmanager
def using_profile(base, scope):
    from .unified_profiles import base_home, record

    expected = base_home(base).root / "profiles"
    if scope.root.parent != expected or not record(scope):
        raise ForgeError("The requested profile does not belong to this Forge home.")
    token = PROFILE_OVERRIDE.set(str(scope.root))
    try:
        yield scope
    finally:
        PROFILE_OVERRIDE.reset(token)


def configured(home):
    return (home.root / "spack/backend.json").is_file()


def backend_name(home):
    from .unified_profiles import record

    data = record(home)
    if data:
        return data["backend"]
    marker = home.root / "metadata.json"
    if marker.is_file() and read_json(marker).get("backend") in {"spack", "homebrew"}:
        return read_json(marker)["backend"]
    return "homebrew" if (home.root / "homebrew.json").is_file() else "spack"


def _profile(base, name):
    from .unified_profiles import base_home

    return ForgeHome(named_path(base_home(base).root / "profiles", name))


def active_home(base):
    from .unified_profiles import base_home, record, selection

    if record(base):
        return base
    base = base_home(base)
    if override := PROFILE_OVERRIDE.get():
        profile = (
            ForgeHome(Path(override))
            if Path(override).is_absolute()
            else _profile(base, override)
        )
        if (
            profile.root.is_symlink()
            or profile.root.parent != base.root / "profiles"
            or not record(profile)
        ):
            raise ForgeError("The requested profile is unavailable in this Forge home.")
        return profile
    saved = selection(base)
    if saved:
        result = _profile(base, saved["id"])
        if not record(result):
            raise ForgeError(
                f"Default profile {saved['id']} is missing.", hint="jforge profile"
            )
        return result
    pending = _profile(base, "default")
    if record(pending):
        raise ForgeError(
            "No project profile is selected.", hint="jforge profile use default"
        )
    return pending


def require_healthy_profile(profile, data):
    file = profile.root / (
        "homebrew.json" if data["backend"] == "homebrew" else "spack/backend.json"
    )
    state = profile.root / "packages.json"
    if not file.is_file() or not state.is_file():
        raise ForgeError(
            f"Package backend is incomplete for profile {data['id']}.",
            hint="Recreate this profile explicitly; no fallback profile is selected.",
        )
    value = read_json(state)
    if (
        not value.get(data["backend"])
        or not Path(value.get("activation", "")).is_file()
    ):
        raise ForgeError(f"Package activation is missing for profile {data['id']}.")
    if data["backend"] == "spack":
        from .spack_backend import _executable

        _executable(profile)


def validate_active(profile, *, environment=None):
    from .unified_profiles import record, verify, validate_python

    if data := record(profile):
        verify(profile, data)
        if data["backend"] == "homebrew":
            from .homebrew_backend import settings

            settings(profile)
        validate_python(data, os.environ if environment is None else environment)


def ensure_active(base, *, progress=None, selected=None, confirm=None):
    from .unified_profiles import record, base_home, create, set_default, host

    profile = selected or active_home(base)
    if record(profile):
        validate_active(profile)
        return profile
    if profile.root.exists():
        raise ForgeError(
            f"Profile metadata is missing: {profile.root}.",
            hint="Use a fresh named profile.",
        )
    base = base_home(base)
    if host()["platform"] == "darwin":
        if progress:
            progress("Create default profile using the macos platform")
        create(base, "default", platform="macos", progress=progress, confirm=confirm)
        set_default(base, "default")
        return active_home(base)
    raise ForgeError(
        "No project profile is selected.",
        hint="jforge profile create default --platform ubuntu24\njforge profile use default",
    )


def initialize_profile(base):
    from .unified_profiles import selection

    saved = selection(base)
    return (
        {
            "id": saved["id"],
            "backend": backend_name(active_home(base)),
            "status": "selected",
        }
        if saved
        else None
    )


def profiles(base, *, archived=False):
    from .unified_profiles import _instances, selection

    if archived:
        raise ForgeError("Old layouts are unsupported. Use a fresh Forge home.")
    chosen = (selection(base) or {}).get("id")
    return [
        {**data, "path": str(profile.root), "active": data["id"] == chosen}
        for profile, data in _instances(base)
    ]


def use(base, name):
    from .unified_profiles import set_default

    return set_default(base, name)


def switch(*args, **kwargs):
    raise ForgeError(
        "The old apt profile/switch/import interface has been removed.",
        hint="jforge profile create NAME --platform ubuntu24",
    )


def create_homebrew(base, name):
    from .unified_profiles import create

    return create(base, name, platform="macos")


def archived_homes(base):
    return []


def archived_packages(base):
    raise ForgeError("Old package layouts are unsupported; recreate the profile.")
