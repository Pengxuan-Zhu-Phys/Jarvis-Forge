"""Source-isolated APT environments inside one shared Forge user directory."""

from __future__ import annotations

from pathlib import Path

from .storage import ForgeError, ForgeHome, read_json, write_json, new_id


def active_home(base):
    selection = base.root / "apt/selection.json"
    if not selection.exists():
        return base
    selected = read_json(selection)
    if selected.get("schema_version") != 1 or not isinstance(selected.get("id"), str):
        raise ForgeError(f"Invalid APT environment selection: {selection}")
    if selected["id"] == "legacy":
        return base
    from .packages import _inside
    from .storage import identifier

    identifier(selected["id"])
    root = _inside(base, base.root / "apt/profiles" / selected["id"])
    if not (root / "config.json").is_file():
        raise ForgeError(
            f"Selected APT environment is missing: {root}",
            hint="jforge apt switch legacy",
        )
    return ForgeHome(root)


def profiles(base):
    from .official import default_source

    selected = active_home(base)
    rows = [
        {
            "id": "legacy",
            "path": str(base.root),
            "source": default_source(base),
            "active": selected == base,
        }
    ]
    directory = base.root / "apt/profiles"
    for path in sorted(directory.glob("*/config.json")):
        from .packages import _inside

        home = ForgeHome(_inside(base, path.parent))
        rows.append(
            {
                "id": path.parent.name,
                "path": str(home.root),
                "source": default_source(home),
                "active": home == selected,
                "backend": "spack"
                if (home.root / "apt/spack.json").is_file()
                else "source",
            }
        )
    return rows


def switch(
    base,
    origin,
    *,
    suite=None,
    component=None,
    keyring=None,
    name="default",
    python=None,
    progress=None,
):
    from .official import (
        REPOSITORIES,
        _selector,
        import_repository,
        default_source,
        source_components,
        source_family,
    )
    from .packages import locked, operation, _inside, _publish, state, environment_path

    if origin == "spack":
        if suite or component or keyring:
            raise ForgeError(
                "Spack uses its own recipes and mirrors; do not supply Debian/Ubuntu source options."
            )
        from .storage import identifier
        from . import spack_backend as backend

        identifier(name)
        profile_id = identifier("spack-" + name)
        target = ForgeHome(_inside(base, base.root / "apt/profiles" / profile_id))
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
                packages=str(backend.root(target) / "store"),
                activation=environment_path(target),
                state_committed=True,
            )
            return result
    if origin != "legacy" and origin not in REPOSITORIES:
        raise ForgeError("Choose spack, debian, ubuntu or legacy.")
    source = None
    if origin == "legacy":
        if suite or component or keyring:
            raise ForgeError(
                "legacy selects the existing environment; do not supply new source options."
            )
        profile_id, target = "legacy", base
    else:
        suite = suite or {"ubuntu": "noble", "debian": "bookworm"}[origin]
        component = component or ("main,universe" if origin == "ubuntu" else "main")
        source = _selector(
            {
                "origin": origin,
                "suite": suite,
                "component": component,
                "keyring": str(Path(keyring).expanduser().resolve())
                if keyring
                else None,
            }
        )
        # Readable identifiers; conflicting existing selections are rejected below.
        tokens = [origin, suite, *source_components(source), *source.get("pockets", [])]
        profile_id = "-".join(tokens)
        import re

        if len(profile_id) > 120 or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", profile_id
        ):
            from hashlib import sha256

            profile_id = (
                origin
                + "-"
                + re.sub(r"[^a-zA-Z0-9_.-]", "-", suite[:30])
                + "-"
                + sha256(repr(tokens).encode()).hexdigest()[:20]
            )
        target = ForgeHome(_inside(base, base.root / "apt/profiles" / profile_id))
    with (
        locked(base),
        operation(base, "switch", {"profile": profile_id, "source": source}) as (
            directory,
            result,
        ),
    ):
        target.initialize()
        if target != base:
            config_path = target.root / "config.json"
            config = read_json(config_path)
            config["apt_base_home"] = str(base.root)
            write_json(config_path, config)
            old_source = default_source(target)
            # No source change within an existing profile. Refresh metadata only
            # when first creating it or after its source configuration was lost.
            if old_source is not None and source_family(old_source) != source_family(
                source
            ):
                raise ForgeError(f"Source profile identity collision: {profile_id}")
            if old_source is not None and keyring is None:
                keyring = old_source.get("keyring")
                source["keyring"] = keyring
            if (
                old_source != source
                or not (target.root / "cache/apt/official-repository.json").is_file()
            ):
                result["source_import"] = import_repository(
                    target,
                    origin=origin,
                    suite=suite,
                    component=",".join(source_components(source)),
                    keyring=keyring,
                )
        with locked(target) if target != base else _nullcontext():
            current = state(target)
            # Publish even an empty profile so shell integration can cleanly
            # remove the previous source's exported paths and variables.
            _publish(target, current, new_id("switch-env"))
        write_json(
            _inside(base, base.root / "apt/selection.json"),
            {"schema_version": 1, "id": profile_id},
        )
        result.update(
            profile=profile_id,
            source=default_source(target),
            user_home=str(base.root),
            apt_home=str(target.root),
            packages=str(target.root / "packages"),
            activation=environment_path(target),
            state_committed=True,
        )
        return result


from contextlib import nullcontext as _nullcontext
