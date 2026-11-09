"""Use the user's Homebrew installation for audited native formulae."""

from contextlib import contextmanager
import fnmatch
import hashlib
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
from .storage import ForgeError, digest, new_id, read_json, write_json, write_text


ALIASES = {"libeigen3-dev": "eigen", "libgsl-dev": "gsl", "sqlite3": "sqlite", "pkg-config": "pkgconf"}
FORMULA = re.compile(r"[a-z0-9][a-z0-9+._-]*(?:@[0-9][a-z0-9.+_-]*)?")
PYTHON = re.compile(r"(?:python|cpython|pypy|micropython)(?:$|[@0-9-])")
PYTHON_TOOLS = {"pyenv", "pyenv-virtualenv", "pip", "pipx", "pipenv", "uv", "virtualenv",
                "conda", "miniconda", "anaconda", "miniforge", "mamba", "micromamba"}
BREW_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_NO_INSTALL_UPGRADE": "1",
    "HOMEBREW_NO_INSTALL_CLEANUP": "1",
    "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
    "HOMEBREW_NO_SUDO": "1",
    "HOMEBREW_NO_ANALYTICS": "1",
    "NONINTERACTIVE": "1",
}


class UnusableKeg(ForgeError):
    """A root or its installed dependencies cannot be activated."""


def configured(home):
    return (home.root / "apt/homebrew.json").is_file()


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
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, start_new_session=True)
    except OSError as exc:
        raise ForgeError(f"Cannot run the Homebrew launcher {brew}: {exc}", code=1) from exc
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
            raise ForgeError(f"Homebrew {args[0]} timed out after {timeout:g} seconds.", code=1) from exc
    if logs:
        (logs / "brew.stdout.log").write_bytes(out)
        (logs / "brew.stderr.log").write_bytes(err)
    if process.returncode:
        detail = err.decode(errors="replace").strip() or out.decode(errors="replace").strip()
        raise ForgeError(f"Homebrew {args[0]} failed (exit {process.returncode})."
                         + (f"\n{detail}" if detail else ""), code=1)
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
        raise ForgeError("Homebrew was not found on PATH.", hint="Make the existing brew executable available in your shell.")
    # Preserve the invoked launcher path. Its target may change with a Brew update.
    brew = str(Path(brew).absolute())
    prefix = _brew_path(brew, "--prefix")
    cellar = _brew_path(brew, "--cellar")
    if not Path(prefix).is_dir() or not Path(cellar).is_relative_to(Path(prefix)):
        raise ForgeError(f"Unsupported Homebrew prefix or Cellar: {prefix}, {cellar}")
    return {"schema_version": 1, "brew": brew, "prefix": prefix, "cellar": cellar}


def settings(home, *, allow_unregistered=False):
    _darwin()
    path = _inside(home, home.root / "apt/homebrew.json")
    if not path.is_file():
        if allow_unregistered:
            return discover()
        raise ForgeError("The Homebrew profile is pending.", hint="Make Homebrew available, then run jforge init.")
    data = read_json(path)
    if data.get("schema_version") != 1 or not all(
            isinstance(data.get(key), str) and Path(data[key]).is_absolute() for key in ("brew", "prefix", "cellar")):
        raise ForgeError(f"Invalid Homebrew profile: {path}")
    if (_brew_path(data["brew"], "--prefix") != data["prefix"]
            or _brew_path(data["brew"], "--cellar") != data["cellar"]):
        raise ForgeError(f"The Homebrew prefix or Cellar has changed for {home.root}.")
    return data


def current(home):
    path = _inside(home, home.root / "apt/state.json")
    state = read_json(path).get("homebrew") if path.is_file() else None
    if (not isinstance(state, dict) or not isinstance(state.get("roots"), list)
            or not isinstance(state.get("packages"), list) or not isinstance(state.get("activation"), str)):
        raise ForgeError(f"No Homebrew environment has been published: {home.root}", hint="jforge init")
    return state


def name_of(value):
    value = value.removeprefix("homebrew/core/")
    return ALIASES.get(value, value)


def _formula(value):
    name = name_of(value)
    if not FORMULA.fullmatch(name):
        raise ForgeError(f"Expected an official Homebrew formula name: {value!r}")
    return name


def _forbid_python(name, root):
    if PYTHON.match(name) or name in PYTHON_TOOLS:
        raise UnusableKeg(f"Homebrew formula {root!r} requires {name!r}. Python belongs to pyenv.",
                          hint="Choose native dependencies that work with your pyenv Python.")


def _info(config, args):
    raw = _invoke(config["brew"], ["info", "--json=v2", *args])
    try:
        document = json.loads(raw)
        rows = document["formulae"]
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in rows):
            raise ValueError("invalid formula records")
    except (TypeError, KeyError, ValueError) as exc:
        raise ForgeError("Homebrew returned invalid formula metadata.", code=1) from exc
    return rows


def _installed(config):
    return _info(config, ["--installed"])


def _keg(config, formula, installed):
    version = installed.get("version")
    if not isinstance(version, str) or not version or Path(version).name != version or version in {".", ".."}:
        raise ForgeError(f"Invalid Homebrew installation version for {formula['name']}.")
    name = _formula(formula["name"])
    prefix = Path(config["cellar"]) / name / version
    receipt = prefix / "INSTALL_RECEIPT.json"
    return {"name": name, "formula": formula.get("full_name", name), "version": version,
            "prefix": str(prefix), "opt_prefix": str(Path(config["prefix"]) / "opt" / name),
            "install_id": f"{name}@{version}", "backend": "homebrew", "status": "installed",
            "receipt": str(receipt), "receipt_sha256": digest(receipt) if receipt.is_file() else None}


def inventory(home, *, config=None):
    config = config or settings(home, allow_unregistered=True)
    state = current(home) if configured(home) else {"packages": []}
    selected = {row["install_id"]: row for row in state["packages"]}
    rows = []
    for formula in _installed(config):
        for installation in formula.get("installed", []):
            row = _keg(config, formula, installation)
            if Path(row["prefix"]).is_dir():
                active = formula.get("tap") == "homebrew/core" and row["install_id"] in selected
                rows.append({**row, "selected": active, "scope": "shared",
                             "automatic": selected[row["install_id"]]["automatic"] if active else None})
    return rows


def catalog(home, pattern="*", query=None):
    config = settings(home, allow_unregistered=True)
    installed = {row["name"]: row for row in inventory(home, config=config)}
    names = _invoke(config["brew"], ["formulae"]).splitlines()
    return [installed.get(name, {"name": name, "backend": "homebrew", "status": "available", "scope": "shared"})
            for name in names if fnmatch.fnmatchcase(name, pattern) and (query is None or query.lower() in name.lower())]


def details(home, name):
    config = settings(home, allow_unregistered=True)
    name = _formula(name)
    rows = _info(config, ["--formula", "homebrew/core/" + name])
    if len(rows) != 1 or rows[0].get("tap") != "homebrew/core":
        raise ForgeError(f"No official Homebrew formula: {name}")
    return rows[0]


def _core_installations(config):
    return {row["name"]: row for row in _installed(config) if row.get("tap") == "homebrew/core"}


def _selected_kegs(config, installed, names, *, roots, deferred=None):
    """Follow the recorded runtime dependency versions of installed Core kegs."""
    packages = {}

    def visit(selection, name, root, version=None):
        name = _formula(name)
        _forbid_python(name, root)
        row = installed.get(name)
        if not row:
            raise UnusableKeg(f"Required Homebrew formula {name!r} is missing for {root!r}.")
        versions = row.get("installed", [])
        if version is None:
            opt = Path(config["prefix"]) / "opt" / name
            try:
                version = opt.resolve(strict=True).name
            except FileNotFoundError:
                if len(versions) != 1:
                    raise UnusableKeg(f"Cannot select the active Homebrew keg for {name!r}.") from None
                version = versions[0]["version"]
        match = next((item for item in versions if item.get("version") == version), None)
        if match is None or not isinstance(match.get("runtime_dependencies"), list):
            raise UnusableKeg(f"Cannot audit the Homebrew installation of {name}@{version}.")
        if name in selection:
            if selection[name]["version"] != version:
                raise UnusableKeg(f"Homebrew profile requires more than one keg of {name!r}.")
            return
        record = _keg(config, row, match)
        prefix = Path(record["prefix"])
        if not prefix.is_dir() or not prefix.resolve().is_relative_to(Path(config["cellar"])) or not record["receipt_sha256"]:
            raise UnusableKeg(f"Missing Homebrew installation evidence for {name}@{version}.")
        dependencies = []
        for dep in match["runtime_dependencies"]:
            if not isinstance(dep, dict) or not isinstance(dep.get("full_name"), str):
                raise ForgeError(f"Invalid Homebrew dependency record for {name!r}.")
            dependency = _formula(dep["full_name"])
            _forbid_python(dependency, root)
            dep_version = dep.get("pkg_version") or dep.get("version")
            if not isinstance(dep_version, str) or not dep_version:
                raise ForgeError(f"Missing Homebrew dependency version for {dependency!r}.")
            dependencies.append((dependency, dep_version))
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
        raw = _invoke(config["brew"], ["deps", "--formula", "--full-name", "--include-build",
                                       "--include-implicit", target])
        dependencies = list(dict.fromkeys(_formula(dependency) for dependency in raw.splitlines()))
        for dependency in dependencies:
            _forbid_python(dependency, name)
        # Explicitly check formula identity, including when brew resolved an alias.
        metadata = _info(config, ["--formula", target])
        if len(metadata) != 1 or metadata[0].get("tap") != "homebrew/core" or metadata[0]["name"] != name:
            raise ForgeError(f"Cannot audit official Homebrew formula {name!r}. Use its canonical formula name.")
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
        paths["PKG_CONFIG_PATH"].extend(path for path in
                                        (prefix / "lib/pkgconfig", prefix / "share/pkgconfig") if path.is_dir())
    return {key: [str(path) for path in values] for key, values in paths.items()}


def _publish(home, roots, packages, revision):
    directory = _inside(home, home.root / "environments/apt/revisions" / revision)
    paths = _activation_paths(packages)
    adapter = cmake.write_environment(directory / "homebrew-environment.sh", paths=paths)
    entry = directory / "activate.sh"
    write_text(entry, activation(home, [adapter]))
    state = {"roots": roots, "packages": packages, "activation": str(entry), "paths": paths,
             "script_sha256": {path.name: digest(path) for path in (entry, adapter)}}
    write_json(_inside(home, home.root / "apt/state.json"),
               {"schema_version": 1, "backend": "homebrew", "activation": str(entry),
                "last_operation_id": revision, "homebrew": state})
    return state


def initialize(home, base, config):
    home.initialize()
    file = _inside(home, home.root / "apt/homebrew.json")
    if file.exists() and read_json(file) != config:
        raise ForgeError(f"Homebrew profile is already bound to a different installation: {home.root}")
    settings_file = _inside(home, home.root / "config.json")
    write_json(settings_file, {**read_json(settings_file), "apt_base_home": str(base.root)})
    write_json(file, config)
    return _publish(home, [], [], new_id("brew"))


def _activation_intact(home, state):
    try:
        entry = _inside(home, Path(state["activation"]))
    except ForgeError:
        return False
    expected = state.get("script_sha256", {})
    for name in ("activate.sh", "homebrew-environment.sh"):
        path = entry.with_name(name)
        if path.is_symlink() or not path.is_file() or digest(path) != expected.get(name):
            return False
    return entry.name == "activate.sh" and state.get("paths") == _activation_paths(state["packages"])


def environment_path(home):
    _darwin()
    state = current(home)
    if not _activation_intact(home, state):
        raise ForgeError(f"Homebrew activation has changed: {state['activation']}", hint="jforge env repair")
    for row in state["packages"]:
        receipt = Path(row["receipt"])
        if not receipt.is_file() or digest(receipt) != row["receipt_sha256"]:
            raise ForgeError(f"Homebrew keg has changed: {row['prefix']}", hint="jforge env repair")
    return state["activation"]


def _writable(config):
    if os.geteuid() == 0:
        raise ForgeError("Run Homebrew package operations as the ordinary Homebrew owner.")
    prefix = Path(config["prefix"])
    for directory in (prefix, Path(config["cellar"]), prefix / "bin", prefix / "opt"):
        if not os.access(directory if directory.exists() else directory.parent, os.W_OK | os.X_OK):
            raise ForgeError(f"Homebrew directory is not writable by this user: {directory}")


@contextmanager
def _prefix_lock(config):
    import fcntl

    key = hashlib.sha256(os.fsencode(config["prefix"])).hexdigest()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache").expanduser()
    directory = cache / "jarvis-forge/homebrew-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / key).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ForgeError(f"Another Forge operation is using Homebrew at {config['prefix']}.", code=1) from exc
        yield


def transact(home, action, requests, *, dry_run=False, timeout=3600, jobs=1, cache="auto", progress=None):
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
        if action == "remove":
            if any(name not in previous for name in names):
                raise ForgeError("Only explicitly selected Homebrew roots can be removed from this profile.")
            roots = [name for name in previous if name not in names]
        else:
            roots = list(dict.fromkeys([*previous, *names]))
        installed = _core_installations(config)
        deferred = {} if action == "remove" else None
        missing = [] if action == "remove" else [name for name in roots if name not in installed]
        packages = _selected_kegs(config, installed, [name for name in roots if name not in missing],
                                  roots=roots, deferred=deferred)
        audited = _audit_install(config, missing)
        return {"backend": "homebrew", "scope": "shared", "roots": roots, "packages": packages,
                "would_install": missing, "audited_dependencies": audited, "deferred_roots": deferred or {},
                "dry_run": dry_run or action == "check"}

    if preview:
        return prepare()
    with locked(home), operation(home, action, {"formulae": names, "backend": "homebrew"}) as (directory, result):
        with _prefix_lock(config):
            plan = prepare()
            write_json(directory / "plan.json", plan)
            if progress:
                progress("[2/4] Prepare shared Homebrew installation")
            if plan["would_install"]:
                _writable(config)
                if progress:
                    progress("[3/4] Install " + ", ".join(plan["would_install"]))
                _invoke(config["brew"], ["install", "--formula", *["homebrew/core/" + name for name in plan["would_install"]]],
                        timeout=timeout, logs=directory, jobs=jobs)
            packages = plan["packages"]
            if plan["would_install"]:
                packages = _selected_kegs(config, _core_installations(config), plan["roots"], roots=plan["roots"])
            if progress:
                progress("[4/4] Publish Homebrew activation")
            state = _publish(home, plan["roots"], packages, directory.name)
            result.update({**plan, **state}, state_committed=True)
            return result


def update(home):
    config = settings(home)
    _writable(config)
    with locked(home), operation(home, "update", {"backend": "homebrew"}) as (directory, result):
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
        packages = _selected_kegs(config, _core_installations(config), roots, roots=roots, deferred=deferred)
        prefixes = {pkg["prefix"] for pkg in packages}
        removed = [row for row in state["packages"] if row["prefix"] not in prefixes]
        entry = Path(state["activation"])
        changed = packages != state["packages"] or not _activation_intact(home, state)
        report = {"activation": str(entry), "changed": changed, "dry_run": dry_run, "unchecked": 0,
                  "deferred": len(deferred), "deferred_roots": deferred, "removed": len(removed),
                  "settings": [{"script": str(entry), "entry": i, "kind": "keg", "value": row["prefix"],
                                "status": "removed", "reason": "Homebrew keg is no longer selected"}
                               for i, row in enumerate(removed, 1)]}
        if changed and not dry_run:
            revision = new_id("brew-repair")
            published = _publish(home, roots, packages, revision)
            report["activation"] = published["activation"]
            report["report"] = str(Path(published["activation"]).parent / "report.json")
            write_json(Path(report["report"]), report)
        return report

    if dry_run:
        return inspect()
    with locked(home), _prefix_lock(config):
        return inspect()
