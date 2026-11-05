"""Select Spack profiles; old source directories are read-only history."""

from __future__ import annotations

from .storage import ForgeError, ForgeHome, identifier, read_json, write_json
from .package_operations import _inside, environment_path, locked, operation


def configured(home):
    return (home.root / "apt/spack.json").is_file()


def _selection(base):
    path = _inside(base, base.root / "apt/selection.json")
    if not path.exists():
        return None
    value = read_json(path)
    if value.get("schema_version") != 1 or not isinstance(value.get("id"), str):
        raise ForgeError(
            f"Invalid package environment selection: {path}",
            hint="jforge apt switch spack",
        )
    return identifier(value["id"])


def _profile(base, profile_id):
    return ForgeHome(_inside(base, base.root / "apt/profiles" / identifier(profile_id)))


def active_home(base):
    selected = _selection(base)
    selection_file = base.root / "apt/selection.json"
    if selection_file.exists() and read_json(selection_file).get("location") == "root":
        if selected != "direct" or not configured(base):
            raise ForgeError(
                "The selected root Spack environment is unavailable.",
                hint="jforge apt switch spack",
            )
        return base
    if selected and selected.startswith("spack-"):
        target = _profile(base, selected)
        if not (target.root / "config.json").is_file():
            raise ForgeError(
                f"Selected Spack profile is missing: {target.root}",
                hint="jforge apt switch spack",
            )
        return target
    # Existing direct Spack deployments remain valid. Legacy selections do not
    # execute the removed backend and are replaced only after a successful write.
    if selected in {None, "legacy"} and configured(base):
        return base
    return _profile(base, "spack-default")


def archived_homes(base):
    homes = [base] + [
        _profile(base, path.parent.name)
        for path in sorted((base.root / "apt/profiles").glob("*/config.json"))
    ]
    return [
        home
        for home in homes
        if not configured(home)
        and (
            any((home.root / "packages").glob("*/*/*/manifest.json"))
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
    homes = ([base] if configured(base) else []) + [
        _profile(base, path.parent.name)
        for path in sorted((base.root / "apt/profiles").glob("*/config.json"))
        if configured(_profile(base, path.parent.name))
    ]
    rows = [
        dict(
            id="direct" if home == base else home.root.name,
            path=str(home.root),
            backend="spack",
            active=home == selected,
        )
        for home in homes
    ]
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


def ensure_active(base, *, progress=None):
    selected = active_home(base)
    if configured(selected):
        return selected
    switch(
        base, "spack", name=selected.root.name.removeprefix("spack-"), progress=progress
    )
    return active_home(base)


def switch(
    base, origin="spack", *, name="default", python=None, direct=False, progress=None
):
    from . import spack_backend as backend

    if origin != "spack":
        raise ForgeError(
            "Ubuntu/Debian source backends have been removed; use Spack.",
            hint="jforge apt switch spack; jforge apt switch --archived",
        )
    if direct:
        if name != "default" or not backend.configured(base):
            raise ForgeError(
                "--direct selects an existing root Spack environment and cannot use --name."
            )
        prepared = backend.bootstrap(base, python=python, progress=progress)
        with (
            locked(base),
            operation(base, "switch", {"profile": "direct", "backend": "spack"}) as (
                directory,
                result,
            ),
        ):
            write_json(
                _inside(base, base.root / "apt/selection.json"),
                {"schema_version": 1, "id": "direct", "location": "root"},
            )
            result.update(
                profile="direct",
                backend="spack",
                bootstrap=prepared,
                apt_home=str(base.root),
                user_home=str(base.root),
                packages=str(backend.store_root(base)),
                activation=environment_path(base),
                state_committed=True,
            )
            return result
    profile_id = identifier("spack-" + identifier(name))
    target = _profile(base, profile_id)
    with (
        locked(base),
        operation(base, "switch", {"profile": profile_id, "backend": "spack"}) as (
            directory,
            result,
        ),
    ):
        target.initialize()
        config = read_json(target.root / "config.json")
        config["apt_base_home"] = str(base.root)
        write_json(target.root / "config.json", config)
        result["bootstrap"] = backend.bootstrap(
            target, python=python, progress=progress
        )
        write_json(
            _inside(base, base.root / "apt/selection.json"),
            {"schema_version": 1, "id": profile_id},
        )
        result.update(
            profile=profile_id,
            backend="spack",
            apt_home=str(target.root),
            user_home=str(base.root),
            packages=str(backend.store_root(target)),
            activation=environment_path(target),
            state_committed=True,
        )
        return result
