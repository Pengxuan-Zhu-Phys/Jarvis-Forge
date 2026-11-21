"""Use the user's Homebrew installation for audited native formulae."""

from contextlib import contextmanager
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from . import cmake
from .package_operations import _inside, locked, operation
from .shell_activation import activation
from .paths import object_path
from .storage import ForgeError, digest, new_id, read_json, write_json, write_text


FORMULA = re.compile(r"[a-z0-9][a-z0-9+._-]*(?:@[0-9][a-z0-9.+_-]*)?")
PYTHON = re.compile(r"(?:python|cpython|pypy|micropython)(?:$|[@0-9-])")
PYTHON_TOOLS = {
    "pyenv",
    "pyenv-virtualenv",
    "pip",
    "pipx",
    "pipenv",
    "uv",
    "virtualenv",
    "conda",
    "miniconda",
    "anaconda",
    "miniforge",
    "mamba",
    "micromamba",
}
BREW_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_NO_INSTALL_UPGRADE": "1",
    "HOMEBREW_NO_INSTALL_CLEANUP": "1",
    "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
    "HOMEBREW_NO_SUDO": "1",
    "HOMEBREW_NO_ANALYTICS": "1",
    "NONINTERACTIVE": "1",
    "HOMEBREW_NO_ASK": "1",
}


class UnusableKeg(ForgeError):
    """A root or its installed dependencies cannot be activated."""


def configured(home):
    return (home.root / "homebrew.json").is_file()


def _darwin():
    if sys.platform != "darwin":
        raise ForgeError("Forge Homebrew profiles require macOS.")


def _invoke(brew, args, *, timeout=60, logs=None, jobs=None):
    from .runner import _stop

    argv = [str(brew), *args]
    env = {**os.environ, **BREW_ENV}
    if jobs is not None:
        env["HOMEBREW_MAKE_JOBS"] = str(jobs)
    if logs:
        write_json(logs / "brew-command.json", {"argv": argv})
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise ForgeError(
            f"Cannot run the Homebrew launcher {brew}: {exc}", code=1
        ) from exc
    with process:
        try:
            out, err = process.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            _stop(process)
            out, err = process.communicate()
            if logs:
                (logs / "brew.stdout.log").write_bytes(out)
                (logs / "brew.stderr.log").write_bytes(err)
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise ForgeError(
                f"Homebrew {args[0]} timed out after {timeout:g} seconds.", code=1
            ) from exc
    if logs:
        (logs / "brew.stdout.log").write_bytes(out)
        (logs / "brew.stderr.log").write_bytes(err)
    if process.returncode:
        detail = (
            err.decode(errors="replace").strip() or out.decode(errors="replace").strip()
        )
        raise ForgeError(
            f"Homebrew {args[0]} failed (exit {process.returncode})."
            + (f"\n{detail}" if detail else ""),
            code=1,
        )
    return out.decode("utf-8")


def _brew_path(brew, option):
    value = Path(_invoke(brew, [option]).strip())
    if not value.is_absolute():
        raise ForgeError(f"Homebrew {option} did not return an absolute path.")
    return str(value.resolve())


def find_brew():
    brew = shutil.which("brew")
    if brew:
        return brew
    prefix = "/opt/homebrew" if os.uname().machine == "arm64" else "/usr/local"
    path = Path(prefix) / "bin/brew"
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def discover():
    _darwin()
    brew = find_brew()
    if not brew:
        raise ForgeError(
            "Homebrew was not found on PATH.",
            hint="Make the existing brew executable available in your shell.",
        )
    # Preserve the invoked launcher path. Its target may change with a Brew update.
    brew = str(Path(brew).absolute())
    prefix = _brew_path(brew, "--prefix")
    cellar = _brew_path(brew, "--cellar")
    if not Path(prefix).is_dir() or not Path(cellar).is_relative_to(Path(prefix)):
        raise ForgeError(f"Unsupported Homebrew prefix or Cellar: {prefix}, {cellar}")
    return {"schema_version": 1, "brew": brew, "prefix": prefix, "cellar": cellar}


def settings(home, *, allow_unregistered=False):
    _darwin()
    path = _inside(home, home.root / "homebrew.json")
    if not path.is_file():
        if allow_unregistered:
            return discover()
        raise ForgeError(
            "The Homebrew profile is pending.",
            hint="Make Homebrew available, then run jforge init.",
        )
    data = read_json(path)
    if data.get("schema_version") != 1 or not all(
        isinstance(data.get(key), str) and Path(data[key]).is_absolute()
        for key in ("brew", "prefix", "cellar")
    ):
        raise ForgeError(f"Invalid Homebrew profile: {path}")
    if (
        _brew_path(data["brew"], "--prefix") != data["prefix"]
        or _brew_path(data["brew"], "--cellar") != data["cellar"]
    ):
        raise ForgeError(f"The Homebrew prefix or Cellar has changed for {home.root}.")
    return data


def current(home):
    path = _inside(home, home.root / "packages.json")
    state = read_json(path).get("homebrew") if path.is_file() else None
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("roots"), list)
        or not isinstance(state.get("packages"), list)
        or not isinstance(state.get("activation"), str)
    ):
        raise ForgeError(
            f"No Homebrew environment has been published: {home.root}",
            hint="jforge init",
        )
    return state


def name_of(value):
    value = value.removeprefix("homebrew/core/")
    return value


def _formula(value):
    name = name_of(value)
    if not FORMULA.fullmatch(name):
        raise ForgeError(f"Expected an official Homebrew formula name: {value!r}")
    return name


def _forbid_python(name, root):
    if PYTHON.match(name) or name in PYTHON_TOOLS:
        raise UnusableKeg(
            f"Homebrew formula {root!r} requires {name!r}. Python belongs to pyenv.",
            hint="Choose native dependencies that work with your pyenv Python.",
        )


def _info(config, args):
    try:
        raw = _invoke(config["brew"], ["info", "--json=v2", *args])
    except ForgeError as exc:
        if re.search(
            r"No (?:available formula|formulae?|similarly named formulae)|Unknown formula",
            str(exc),
            re.IGNORECASE,
        ):
            raise ForgeError(
                str(exc), code=exc.code, hint="jforge apt search <term>"
            ) from exc
        raise
    try:
        document = json.loads(raw)
        rows = document["formulae"]
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("name"), str)
            for row in rows
        ):
            raise ValueError("invalid formula records")
    except (TypeError, KeyError, ValueError) as exc:
        raise ForgeError("Homebrew returned invalid formula metadata.", code=1) from exc
    return rows


def _installed(config):
    return _info(config, ["--installed"])


def _keg(config, formula, installed):
    version = installed.get("version")
    if (
        not isinstance(version, str)
        or not version
        or Path(version).name != version
        or version in {".", ".."}
    ):
        raise ForgeError(
            f"Invalid Homebrew installation version for {formula['name']}."
        )
    name = _formula(formula["name"])
    prefix = Path(config["cellar"]) / name / version
    receipt = prefix / "INSTALL_RECEIPT.json"
    return {
        "name": name,
        "formula": formula.get("full_name", name),
        "version": version,
        "prefix": str(prefix),
        "opt_prefix": str(Path(config["prefix"]) / "opt" / name),
        "install_id": f"{name}@{version}",
        "backend": "homebrew",
        "status": "installed",
        "receipt": str(receipt),
        "receipt_sha256": digest(receipt) if receipt.is_file() else None,
    }


def inventory(home, *, config=None):
    config = config or settings(home, allow_unregistered=True)
    state = current(home) if configured(home) else {"packages": []}
    selected = {row["install_id"]: row for row in state["packages"]}
    rows = []
    for formula in _installed(config):
        for installation in formula.get("installed", []):
            row = _keg(config, formula, installation)
            if Path(row["prefix"]).is_dir():
                active = (
                    formula.get("tap") == "homebrew/core"
                    and row["install_id"] in selected
                )
                rows.append(
                    {
                        **row,
                        "selected": active,
                        "scope": "shared",
                        "automatic": selected[row["install_id"]]["automatic"]
                        if active
                        else None,
                    }
                )
    return rows


def catalog(home, pattern="*", query=None):
    config = settings(home, allow_unregistered=True)
    installed = {row["name"]: row for row in inventory(home, config=config)}
    names = _invoke(config["brew"], ["formulae"]).splitlines()
    return [
        installed.get(
            name,
            {
                "name": name,
                "backend": "homebrew",
                "status": "available",
                "scope": "shared",
            },
        )
        for name in names
        if fnmatch.fnmatchcase(name, pattern)
        and (query is None or query.lower() in name.lower())
    ]


def details(home, name):
    config = settings(home, allow_unregistered=True)
    name = _formula(name)
    rows = _info(config, ["--formula", "homebrew/core/" + name])
    if len(rows) != 1 or rows[0].get("tap") != "homebrew/core":
        raise ForgeError(f"No official Homebrew formula: {name}")
    return rows[0]


def _core_installations(config):
    return {
        row["name"]: row
        for row in _installed(config)
        if row.get("tap") == "homebrew/core"
    }


def _selected_kegs(config, installed, names, *, roots, deferred=None):
    """Follow the recorded runtime dependency versions of installed Core kegs."""
    packages = {}

    def visit(selection, name, root, version=None):
        name = _formula(name)
        _forbid_python(name, root)
        row = installed.get(name)
        if not row:
            raise UnusableKeg(
                f"Required Homebrew formula {name!r} is missing for {root!r}."
            )
        versions = row.get("installed", [])
        if version is None:
            opt = Path(config["prefix"]) / "opt" / name
            try:
                version = opt.resolve(strict=True).name
            except FileNotFoundError:
                if len(versions) != 1:
                    raise UnusableKeg(
                        f"Cannot select the active Homebrew keg for {name!r}."
                    ) from None
                version = versions[0]["version"]
        match = next(
            (item for item in versions if item.get("version") == version), None
        )
        if match is None or not isinstance(match.get("runtime_dependencies"), list):
            raise UnusableKeg(
                f"Cannot audit the Homebrew installation of {name}@{version}."
            )
        if name in selection:
            if selection[name]["version"] != version:
                raise UnusableKeg(
                    f"Homebrew profile requires more than one keg of {name!r}."
                )
            return
        record = _keg(config, row, match)
        prefix = Path(record["prefix"])
        if (
            not prefix.is_dir()
            or not prefix.resolve().is_relative_to(Path(config["cellar"]))
            or not record["receipt_sha256"]
        ):
            raise UnusableKeg(
                f"Missing Homebrew installation evidence for {name}@{version}."
            )
        dependencies = []
        for dep in match["runtime_dependencies"]:
            if not isinstance(dep, dict) or not isinstance(dep.get("full_name"), str):
                raise ForgeError(f"Invalid Homebrew dependency record for {name!r}.")
            dependency = _formula(dep["full_name"])
            _forbid_python(dependency, root)
            dep_version = dep.get("pkg_version") or dep.get("version")
            if not isinstance(dep_version, str) or not dep_version:
                raise ForgeError(
                    f"Missing Homebrew dependency version for {dependency!r}."
                )
            dependencies.append((dependency, dep_version))
        record["node"] = {"dependencies": [{"name": dep[0]} for dep in dependencies]}
        record["hash"] = record["install_id"]
        selection[name] = {**record, "automatic": name not in roots}
        for dependency, dep_version in dependencies:
            visit(selection, dependency, root, dep_version)

    for name in names:
        candidate = packages.copy() if deferred is not None else packages
        try:
            visit(candidate, name, name)
        except UnusableKeg as exc:
            if deferred is None:
                raise
            deferred[name] = str(exc)
        else:
            packages = candidate
    return list(packages.values())


def _audit_install(config, roots):
    audited = {}
    for name in roots:
        _forbid_python(name, name)
        target = "homebrew/core/" + name
        raw = _invoke(
            config["brew"],
            [
                "deps",
                "--formula",
                "--full-name",
                "--include-build",
                "--include-implicit",
                target,
            ],
        )
        dependencies = list(
            dict.fromkeys(_formula(dependency) for dependency in raw.splitlines())
        )
        for dependency in dependencies:
            _forbid_python(dependency, name)
        # Explicitly check formula identity, including when brew resolved an alias.
        metadata = _info(config, ["--formula", target])
        if (
            len(metadata) != 1
            or metadata[0].get("tap") != "homebrew/core"
            or metadata[0]["name"] != name
        ):
            raise ForgeError(
                f"Cannot audit official Homebrew formula {name!r}. Use its canonical formula name."
            )
        for key in ("dependencies", "build_dependencies", "recommended_dependencies"):
            for dependency in metadata[0].get(key, []):
                _forbid_python(_formula(dependency), name)
        audited[name] = dependencies
    return audited


def _activation_paths(packages):
    paths = {"CMAKE_PREFIX_PATH": [], "PKG_CONFIG_PATH": []}
    for row in packages:
        prefix = Path(row["prefix"])
        if prefix.is_dir():
            paths["CMAKE_PREFIX_PATH"].append(prefix)
        paths["PKG_CONFIG_PATH"].extend(
            path
            for path in (prefix / "lib/pkgconfig", prefix / "share/pkgconfig")
            if path.is_dir()
        )
    return {key: [str(path) for path in values] for key, values in paths.items()}


def _publish(home, roots, packages, revision):
    directory = _inside(home, home.root / "runtime")
    paths = _activation_paths(packages)
    adapter = cmake.write_environment(
        directory / "homebrew-environment.sh", paths=paths
    )
    entry = directory / "activate.sh"
    write_text(entry, activation(home, [adapter]))
    state = {
        "roots": roots,
        "packages": packages,
        "activation": str(entry),
        "paths": paths,
        "script_sha256": {path.name: digest(path) for path in (entry, adapter)},
    }
    write_json(
        _inside(home, home.root / "packages.json"),
        {
            "schema_version": 1,
            "backend": "homebrew",
            "activation": str(entry),
            "last_operation_id": revision,
            "homebrew": state,
        },
    )
    return state


def initialize(home, base, config):
    home.initialize()
    file = _inside(home, home.root / "homebrew.json")
    if file.exists() and read_json(file) != config:
        raise ForgeError(
            f"Homebrew profile is already bound to a different installation: {home.root}"
        )
    settings_file = _inside(home, home.root / "config.json")
    write_json(
        settings_file, {**read_json(settings_file), "apt_base_home": str(base.root)}
    )
    write_json(file, config)
    return _publish(home, [], [], "initialize")


def _activation_intact(home, state):
    try:
        entry = _inside(home, Path(state["activation"]))
    except ForgeError:
        return False
    expected = state.get("script_sha256", {})
    for name in ("activate.sh", "homebrew-environment.sh"):
        path = entry.with_name(name)
        if (
            path.is_symlink()
            or not path.is_file()
            or digest(path) != expected.get(name)
        ):
            return False
    return entry.name == "activate.sh" and state.get("paths") == _activation_paths(
        state["packages"]
    )


def environment_path(home):
    _darwin()
    state = current(home)
    if not _activation_intact(home, state):
        raise ForgeError(
            f"Homebrew activation has changed: {state['activation']}",
            hint="jforge env repair",
        )
    for row in state["packages"]:
        receipt = Path(row["receipt"])
        if not receipt.is_file() or digest(receipt) != row["receipt_sha256"]:
            raise ForgeError(
                f"Homebrew keg has changed: {row['prefix']}", hint="jforge env repair"
            )
    return state["activation"]


def _writable(config):
    if os.geteuid() == 0:
        raise ForgeError(
            "Run Homebrew package operations as the ordinary Homebrew owner."
        )
    prefix = Path(config["prefix"])
    for directory in (prefix, Path(config["cellar"]), prefix / "bin", prefix / "opt"):
        if not os.access(
            directory if directory.exists() else directory.parent, os.W_OK | os.X_OK
        ):
            raise ForgeError(
                f"Homebrew directory is not writable by this user: {directory}"
            )


@contextmanager
def _prefix_lock(config):
    import fcntl

    directory = Path(config["prefix"]) / "var/jarvis-forge"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = directory / "metadata.json"
    if marker.is_file() and read_json(marker).get("prefix") != config["prefix"]:
        raise ForgeError("Homebrew lock metadata belongs to another prefix.")
    if not marker.is_file():
        write_json(
            marker, {"schema_version": 1, "prefix": config["prefix"], "owned": []}
        )
    with (directory / "operation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ForgeError(
                f"Another Forge operation is using Homebrew at {config['prefix']}.",
                code=1,
            ) from exc
        yield


from .package_operations import profile_write_lock


@profile_write_lock
def transact(
    home,
    action,
    requests,
    *,
    dry_run=False,
    timeout=3600,
    jobs=None,
    cache="auto",
    progress=None,
    confirm=None,
):
    from .parallelism import policy

    parallel = policy()
    jobs = (
        parallel["homebrew_make_jobs"]
        if jobs is None
        else min(jobs, parallel["homebrew_make_jobs"])
    )
    if jobs < 1:
        raise ForgeError("The internal build budget must be positive.")
    parallel["homebrew_make_jobs"] = jobs
    if action in {"upgrade", "reinstall"}:
        return _upgrade_or_rebuild(
            home,
            action,
            requests,
            dry_run=dry_run,
            timeout=timeout,
            jobs=jobs,
            cache=cache,
            progress=progress,
            confirm=confirm,
        )
    if action not in {"install", "remove", "check"}:
        raise ForgeError(f"apt {action} is unavailable for a shared Homebrew profile.")
    if cache != "auto":
        raise ForgeError("Spack cache policies do not apply to Homebrew.")
    names = list(dict.fromkeys(_formula(value) for value in requests))
    preview = dry_run or action == "check"
    config = settings(home, allow_unregistered=preview)

    def prepare():
        if progress:
            progress("[1/4] Audit Homebrew formulae and dependencies")
        previous = current(home)["roots"] if configured(home) else []
        removals = []
        if action == "remove":
            from .package_lifecycle import reverse_dependents, protect_apps

            active = current(home)["packages"]
            if any(
                name not in ({row["name"] for row in active} | set(previous))
                for name in names
            ):
                raise ForgeError("Formula is not selected in this profile.")
            removed_names = reverse_dependents(active, set(names))
            removals = [row for row in active if row["name"] in removed_names]
            _, ownership = _ownership(config)
            seen = {row["prefix"] for row in removals}
            removals += [
                row
                for prefix, row in ownership["owned"].items()
                if row["name"] in removed_names and prefix not in seen
            ]
            protect_apps(home, removals)
            roots = [name for name in previous if name not in removed_names]
        else:
            roots = list(dict.fromkeys([*previous, *names]))
        installed = _core_installations(config)
        deferred = {} if action == "remove" else None
        missing = (
            []
            if action == "remove"
            else [name for name in roots if name not in installed]
        )
        packages = _selected_kegs(
            config,
            installed,
            [name for name in roots if name not in missing],
            roots=roots,
            deferred=deferred,
        )
        audited = _audit_install(config, missing)
        resolved = _resolved_packages(config, missing) if missing else []
        native = (
            _invoke(
                config["brew"],
                [
                    "install",
                    "--dry-run",
                    "--formula",
                    *["homebrew/core/" + name for name in missing],
                ],
            )
            if missing
            else ""
        )
        return {
            "backend": "homebrew",
            "scope": "shared",
            "roots": roots,
            "packages": packages,
            "would_install": missing,
            "audited_dependencies": audited,
            "deferred_roots": deferred or {},
            "dry_run": dry_run or action == "check",
            "parallelism": parallel,
            "resolved_packages": resolved,
            "native_plan": native,
            "changes": {"install": resolved, "rebuild": [], "remove": removals},
        }

    if preview:
        return prepare()
    with (
        locked(home),
        operation(home, action, {"formulae": names, "backend": "homebrew"}) as (
            directory,
            result,
        ),
    ):
        with _prefix_lock(config):
            plan = prepare()
            write_json(directory / "plan.json", plan)
            if progress:
                progress("[2/4] Prepare shared Homebrew installation")
            before_install = _core_installations(config)
            if (
                plan["changes"]["install"]
                and confirm
                and (
                    any(row["name"] not in names for row in plan["changes"]["install"])
                )
            ):
                confirm(plan["changes"])
            if plan["changes"]["remove"]:
                if confirm is None:
                    raise ForgeError(
                        "Removal requires confirmation.",
                        hint="jforge apt remove NAME --yes",
                    )
                confirm(plan["changes"])
                # Update profile references before attempting shared Cellar cleanup.
            if plan["would_install"]:
                _writable(config)
                if progress:
                    progress("[3/4] Install " + ", ".join(plan["would_install"]))
                try:
                    _invoke(
                        config["brew"],
                        [
                            "install",
                            "--formula",
                            *[
                                "homebrew/core/" + name
                                for name in plan["would_install"]
                            ],
                        ],
                        timeout=timeout,
                        logs=directory,
                        jobs=jobs,
                    )
                finally:
                    # Native Brew may install dependencies before reporting failure.
                    # Those kegs still belong to Forge and can later be reclaimed.
                    _remember_owned(config, before_install, _core_installations(config))
            packages = plan["packages"]
            if plan["would_install"]:
                after_install = _core_installations(config)
                _remember_owned(config, before_install, after_install)
                packages = _selected_kegs(
                    config, after_install, plan["roots"], roots=plan["roots"]
                )
            if progress:
                progress("[4/4] Publish Homebrew activation")
            previous_files = _profile_files(home)
            ownership, previous_ownership = _ownership(config)
            try:
                state = _publish(home, plan["roots"], packages, directory.name)
                _remember_reference(home, config, packages)
                from .unified_profiles import refresh_activation, sync_definition

                refresh_activation(home)
                sync_definition(home)
            except BaseException:
                _restore_files(home, previous_files)
                write_json(ownership, previous_ownership)
                raise
            if plan["changes"]["remove"]:
                try:
                    result["physically_removed"] = _uninstall(
                        home, config, plan["changes"]["remove"], directory
                    )
                except ForgeError as exc:
                    result["cleanup_error"] = str(exc)
                    result["note"] = (
                        "Profile references removed; retry unreferenced Cellar cleanup with jforge apt autoremove."
                    )
            from .unified_profiles import refresh_activation, sync_definition

            refresh_activation(home)
            sync_definition(home)
            result.update({**plan, **state}, state_committed=True)
            return result


def update(home):
    config = settings(home)
    _writable(config)
    with (
        locked(home),
        operation(home, "update", {"backend": "homebrew"}) as (directory, result),
    ):
        with _prefix_lock(config):
            _invoke(config["brew"], ["update"], timeout=3600, logs=directory)
        result.update(backend="homebrew", scope="shared")
        return result


def repair(home, *, dry_run=False):
    config = settings(home)

    def inspect():
        state = current(home)
        roots = state["roots"]
        deferred = {}
        packages = _selected_kegs(
            config, _core_installations(config), roots, roots=roots, deferred=deferred
        )
        prefixes = {pkg["prefix"] for pkg in packages}
        removed = [row for row in state["packages"] if row["prefix"] not in prefixes]
        entry = Path(state["activation"])
        changed = packages != state["packages"] or not _activation_intact(home, state)
        report = {
            "activation": str(entry),
            "changed": changed,
            "dry_run": dry_run,
            "unchecked": 0,
            "deferred": len(deferred),
            "deferred_roots": deferred,
            "removed": len(removed),
            "settings": [
                {
                    "script": str(entry),
                    "entry": i,
                    "kind": "keg",
                    "value": row["prefix"],
                    "status": "removed",
                    "reason": "Homebrew keg is no longer selected",
                }
                for i, row in enumerate(removed, 1)
            ],
        }
        if changed and not dry_run:
            revision = "repair"
            published = _publish(home, roots, packages, revision)
            report["activation"] = published["activation"]
            report["report"] = str(Path(published["activation"]).parent / "report.json")
            write_json(Path(report["report"]), report)
        return report

    if dry_run:
        return inspect()
    with locked(home), _prefix_lock(config):
        return inspect()


def _ownership(config):
    file = Path(config["prefix"]) / "var/jarvis-forge/metadata.json"
    value = (
        read_json(file)
        if file.is_file()
        else {
            "schema_version": 1,
            "prefix": config["prefix"],
            "owned": {},
            "references": {},
        }
    )
    if not isinstance(value.get("owned"), dict):
        value["owned"] = {}
    value.setdefault("references", {})
    return file, value


def _remember_owned(config, before, after):
    file, value = _ownership(config)
    old = {
        str(Path(config["cellar"]) / name / item["version"])
        for name, row in before.items()
        for item in row.get("installed", [])
    }
    for name, row in after.items():
        for item in row.get("installed", []):
            prefix = str(Path(config["cellar"]) / name / item["version"])
            if prefix not in old:
                value["owned"][prefix] = {
                    "name": name,
                    "version": item["version"],
                    "prefix": prefix,
                    "hash": f"{name}@{item['version']}",
                }
    write_json(file, value)


def _remember_reference(home, config, packages):
    file, value = _ownership(config)
    value["references"][str(home.root)] = [row["prefix"] for row in packages]
    write_json(file, value)


def _reclaimable(home, config, candidates):
    _, value = _ownership(config)
    references = set()
    for scope, prefixes in value["references"].items():
        if scope != str(home.root) and (Path(scope) / "packages.json").is_file():
            references.update(prefixes)
    selected = {row["prefix"] for row in candidates}
    inventory = _core_installations(config)
    removable = []
    for row in candidates:
        name = row["name"]
        kegs = {
            str(Path(config["cellar"]) / name / item["version"])
            for item in inventory.get(name, {}).get("installed", [])
        }
        # Brew uninstalls a formula, so an unowned or referenced alternate keg
        # prevents physical deletion of all versions of that formula.
        if (
            kegs
            and kegs.issubset(value["owned"])
            and not kegs.intersection(references)
            and kegs.issubset(selected)
        ):
            removable.append(row)
    return removable


def _retired_kegs(home, config, candidates):
    """A retained older keg is collectible only with no native or profile user."""
    _, ownership = _ownership(config)
    references = {
        prefix
        for scope, prefixes in ownership["references"].items()
        if (Path(scope) / "packages.json").is_file()
        for prefix in prefixes
    }
    inventory = _core_installations(config)
    native_references = {
        (name_of(dep["full_name"]), dep.get("pkg_version"))
        for formula in _installed(config)
        for item in formula.get("installed", [])
        for dep in item.get("runtime_dependencies", [])
    }
    result = []
    for row in candidates:
        prefix = Path(row["prefix"])
        name = row["name"]
        version = row["version"]
        current = Path(config["prefix"]) / "opt" / name
        if str(prefix) not in ownership["owned"] or str(prefix) in references:
            continue
        if not current.exists() or current.resolve() == prefix.resolve():
            continue
        if (name, version) in native_references:
            continue
        kegs = {
            str(Path(config["cellar"]) / name / item["version"])
            for item in inventory.get(name, {}).get("installed", [])
        }
        if str(prefix) not in kegs:
            continue
        _inside(type(home)(Path(config["cellar"])), prefix)
        result.append(row)
    return result


def _uninstall(home, config, candidates, logs):
    removable = _reclaimable(home, config, candidates)
    retired = _retired_kegs(home, config, candidates)
    # Native uninstall checks formula dependents and may remove only the newest keg.
    for name in dict.fromkeys(row["name"] for row in removable):
        owned = [row for row in removable if row["name"] == name]
        while any(Path(row["prefix"]).exists() for row in owned):
            before = {row["prefix"] for row in owned if Path(row["prefix"]).exists()}
            _invoke(
                config["brew"],
                ["uninstall", "--formula", "homebrew/core/" + name],
                logs=logs,
            )
            if before == {
                row["prefix"] for row in owned if Path(row["prefix"]).exists()
            }:
                raise ForgeError(
                    f"Homebrew did not remove the approved kegs of {name}.", code=1
                )
    # Homebrew has no version-specific uninstall verb. These prefixes are owned,
    # unlinked and unreferenced; their receipts are the native installation index.
    for row in retired:
        prefix = Path(row["prefix"])
        if prefix.exists():
            shutil.rmtree(prefix)
    removed = [row for row in candidates if not Path(row["prefix"]).exists()]
    file, value = _ownership(config)
    for prefix in list(value["owned"]):
        if not Path(prefix).exists():
            value["owned"].pop(prefix)
    write_json(file, value)
    write_json(logs / "cellar-cleanup.json", {"removed": removed, "retired": retired})
    return removed


@profile_write_lock
def autoremove(home, *, dry_run=False, progress=None, confirm=None):
    from .package_lifecycle import protect_apps

    config = settings(home)

    def inspect():
        _, value = _ownership(config)
        references = {
            prefix
            for scope, prefixes in value["references"].items()
            if (Path(scope) / "packages.json").is_file()
            for prefix in prefixes
        }
        candidates = [
            row
            for prefix, row in value["owned"].items()
            if prefix not in references and Path(prefix).exists()
        ]
        protect_apps(home, candidates)
        approved = list(
            {
                row["prefix"]: row
                for row in [
                    *_reclaimable(home, config, candidates),
                    *_retired_kegs(home, config, candidates),
                ]
            }.values()
        )
        return candidates, {"install": [], "rebuild": [], "remove": approved}

    if dry_run:
        candidates, changes = inspect()
        return {"backend": "homebrew", "dry_run": True, "changes": changes}
    with (
        locked(home),
        operation(home, "autoremove", {}) as (op, result),
        _prefix_lock(config),
    ):
        candidates, changes = inspect()
        if candidates and confirm is None:
            raise ForgeError(
                "Autoremove requires confirmation.", hint="jforge apt autoremove --yes"
            )
        if confirm:
            confirm(changes)
        removed = _uninstall(home, config, changes["remove"], op)
        result.update(
            backend="homebrew",
            removed=removed,
            changes=changes,
            state_committed=True,
            activation=current(home)["activation"],
        )
        return result


def search(home, term):
    if not term or term.startswith("-") or any(char in term for char in ("\n", "\x00")):
        raise ForgeError("Supply a package search term.")
    config = settings(home, allow_unregistered=True)
    output = _invoke(config["brew"], ["search", "--desc", "--formula", term])
    rows = []
    for line in output.splitlines():
        name, separator, description = line.partition(":")
        name = name.strip().removesuffix(" ✔")
        if FORMULA.fullmatch(name):
            rows.append(
                {
                    "name": name,
                    "description": description.strip() if separator else "",
                    "backend": "homebrew",
                }
            )
    return rows


def _resolved_packages(config, roots):
    """Core's published versions and runtime dependency graph for missing kegs."""
    found = {}

    def visit(name, root):
        name = _formula(name)
        _forbid_python(name, root)
        if name in found:
            return
        rows = _info(config, ["--formula", "homebrew/core/" + name])
        if (
            len(rows) != 1
            or rows[0].get("tap") != "homebrew/core"
            or rows[0]["name"] != name
        ):
            raise ForgeError(f"Cannot audit official formula {name}.")
        row = rows[0]
        version = row.get("versions", {}).get("stable")
        if not isinstance(version, str) or not version:
            raise ForgeError(
                f"Homebrew did not report an available version for {name}."
            )
        if revision := row.get("revision", 0):
            version += "_" + str(revision)
        dependencies = list(
            dict.fromkeys(
                [*row.get("dependencies", []), *row.get("recommended_dependencies", [])]
            )
        )
        found[name] = {
            "name": name,
            "version": version,
            "external": False,
            "installed": False,
            "automatic": name not in roots,
            "node": {
                "parameters": {},
                "dependencies": [{"name": dep} for dep in dependencies],
            },
        }
        for dep in dependencies:
            visit(dep, root)

    for name in roots:
        visit(name, name)
    return list(found.values())


def _profile_files(home):
    files = {}
    for path in (
        home.root / "packages.json",
        home.root / "activate.sh",
        home.root / "metadata.json",
        home.root / "profile.yaml",
    ):
        files[path] = path.read_bytes() if path.is_file() else None
    for path in (home.root / "runtime").rglob("*"):
        if path.is_file():
            files[path] = path.read_bytes()
    return files


def _restore_files(home, files):
    for file in (home.root / "runtime").rglob("*"):
        if file.is_file() and file not in files:
            file.unlink()
    for file, data in files.items():
        if data is None:
            file.unlink(missing_ok=True)
        else:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)


def _upgrade_or_rebuild(
    home,
    action,
    requests,
    *,
    dry_run=False,
    timeout=3600,
    jobs=None,
    cache="auto",
    progress=None,
    confirm=None,
):
    if cache != "auto":
        raise ForgeError("Spack cache policies do not apply to Homebrew.")
    config = settings(home)
    names = list(dict.fromkeys(_formula(value) for value in requests))

    def prepare():
        state = current(home)
        selected = {row["name"]: row for row in state["packages"]}
        targets = names or state["roots"]
        if missing := set(targets) - selected.keys():
            raise ForgeError("Packages are not selected: " + ", ".join(sorted(missing)))
        audited = _audit_install(config, targets)
        resolved = _resolved_packages(config, targets)
        native = (
            _invoke(
                config["brew"],
                [
                    "upgrade" if action == "upgrade" else "install",
                    "--dry-run",
                    "--formula",
                    *["homebrew/core/" + name for name in targets],
                ],
            )
            if targets
            else ""
        )
        available = {row["name"]: row for row in resolved}
        changes = [
            {**available[name], "previous_version": selected[name]["version"]}
            for name in targets
            if action == "reinstall"
            or available[name]["version"] != selected[name]["version"]
        ]
        if action == "reinstall" and any(
            row["version"] != row["previous_version"] for row in changes
        ):
            raise ForgeError(
                "Homebrew cannot rebuild the selected version from the current formula.",
                hint="Use apt upgrade, or select a versioned native formula.",
            )
        if action == "reinstall":
            _, ownership = _ownership(config)
            references = {
                prefix
                for scope, prefixes in ownership["references"].items()
                if scope != str(home.root) and (Path(scope) / "packages.json").is_file()
                for prefix in prefixes
            }
            if any(
                selected[name]["prefix"] not in ownership["owned"]
                or selected[name]["prefix"] in references
                for name in targets
            ):
                raise ForgeError(
                    "Homebrew rebuilding requires Forge-owned kegs with no other profile references."
                )
            from .package_lifecycle import protect_apps

            protect_apps(
                home,
                [
                    {**selected[name], "hash": selected[name]["install_id"]}
                    for name in targets
                ],
            )
        return {
            "backend": "homebrew",
            "roots": state["roots"],
            "packages": state["packages"],
            "targets": targets,
            "resolved_packages": resolved,
            "native_plan": native,
            "audited_dependencies": audited,
            "dry_run": dry_run,
            "changes": {
                "install": [],
                "upgrade": changes if action == "upgrade" else [],
                "rebuild": changes if action == "reinstall" else [],
                "remove": [],
            },
        }

    if dry_run:
        return prepare()
    with (
        locked(home),
        operation(home, action, {"formulae": names, "backend": "homebrew"}) as (
            op,
            result,
        ),
        _prefix_lock(config),
    ):
        plan = prepare()
        write_json(op / "plan.json", plan)
        if any(plan["changes"].values()):
            if confirm is None:
                raise ForgeError(
                    "Homebrew upgrade/rebuilding requires confirmation.",
                    hint="Repeat with --yes after reviewing the plan.",
                )
            confirm(plan["changes"])
        files = _profile_files(home)
        backups = {}
        before = _core_installations(config)
        ownership, previous_ownership = _ownership(config)
        try:
            if action == "reinstall":
                from .paths import sequence

                for row in plan["packages"]:
                    if row["name"] not in plan["targets"]:
                        continue
                    prefix = Path(row["prefix"])
                    parent = Path(config["prefix"]) / "var/jarvis-forge/rollback"
                    backup = parent / sequence(parent, "rebuild-" + row["name"])
                    shutil.copytree(prefix, backup, symlinks=True)
                    backups[prefix] = backup
            if plan["targets"]:
                if progress:
                    progress(
                        "Upgrade native Homebrew packages"
                        if action == "upgrade"
                        else "Rebuild native Homebrew packages"
                    )
                try:
                    _invoke(
                        config["brew"],
                        [
                            "upgrade" if action == "upgrade" else "reinstall",
                            "--formula",
                            *["homebrew/core/" + name for name in plan["targets"]],
                        ],
                        timeout=timeout,
                        jobs=jobs,
                        logs=op,
                    )
                finally:
                    _remember_owned(config, before, _core_installations(config))
            packages = _selected_kegs(
                config, _core_installations(config), plan["roots"], roots=plan["roots"]
            )
            state = _publish(home, plan["roots"], packages, op.name)
            _remember_reference(home, config, packages)
            from .unified_profiles import refresh_activation, sync_definition

            refresh_activation(home)
            sync_definition(home)
            result.update({**plan, **state}, state_committed=True)
        except BaseException:
            for prefix, backup in backups.items():
                if prefix.exists():
                    shutil.rmtree(prefix)
                backup.rename(prefix)
            _restore_files(home, files)
            _, latest = _ownership(config)
            latest["references"] = previous_ownership["references"]
            write_json(ownership, latest)
            raise
        for backup in backups.values():
            shutil.rmtree(backup)
        return result
