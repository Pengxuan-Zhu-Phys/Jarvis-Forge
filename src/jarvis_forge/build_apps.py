"""Profile-owned app workspaces, installation, reuse and physical reclamation."""

from copy import deepcopy
from contextlib import contextmanager, nullcontext, ExitStack
from contextvars import ContextVar
import hashlib
from pathlib import Path
import shutil
import os
import subprocess

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from . import builds, build_catalog, build_workspace, unified_profiles as profiles
from .build_names import app_name
from .package_operations import _inside, operation
from .paths import semantic_name, sequence
from .storage import ForgeError, ForgeHome, read_json, write_json, write_text


_LOCKED = ContextVar("forge_app_operation", default=frozenset())


@contextmanager
def locked(scope):
    if str(scope.root) in _LOCKED.get():
        yield
        return
    with builds.file_lock(
        scope.root / "locks/apps.lock",
        message="Another app operation is using this profile.",
    ):
        token = _LOCKED.set(_LOCKED.get() | {str(scope.root)})
        try:
            yield
        finally:
            _LOCKED.reset(token)


@contextmanager
def reading(scope):
    """Export a coherent snapshot without creating lock files in a read command."""
    import fcntl

    if str(scope.root) in _LOCKED.get():
        yield
        return
    try:
        descriptor = os.open(
            scope.root / "locks/apps.lock", os.O_RDONLY | os.O_NOFOLLOW
        )
    except OSError as exc:
        raise ForgeError(f"Profile read lock is unavailable: {scope.root}") from exc
    with os.fdopen(descriptor, "r") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ForgeError(
                "Another operation is changing this profile; retry the export when it finishes."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def split(value):
    name, separator, version = str(value).partition("@")
    return app_name(name), version if separator else None


def selected(base, *, write=False, progress=None, confirm=None):
    from .apt_profiles import active_home, ensure_active

    scope = (
        ensure_active(base, progress=progress, confirm=confirm)
        if write
        else active_home(base)
    )
    data = profiles.record(scope)
    if not data:
        raise ForgeError(
            "Select a project profile first.",
            hint="jforge profile create default --platform ubuntu24",
        )
    profiles.verify(scope, data)
    profiles.validate_python(data, os.environ)
    return scope


def installed(base, scope=None):
    scope = scope or selected(base)
    data = profiles.record(scope)
    rows = []
    for app in data.get("apps", []):
        row = deepcopy(app)
        row["status"] = (
            "ready"
            if row.get("prefix")
            and Path(row["prefix"]).is_dir()
            and Path(row.get("activation", "")).is_file()
            else "missing"
        )
        row["app"] = row["name"]
        row["build_id"] = row.get("build_id", row["name"])
        row["output"] = row.get("output", "installation")
        rows.append(row)
    return sorted(rows, key=lambda row: (row["name"], row.get("version", "")))


def _matches(base, scope, reference):
    name, version = split(reference)
    return [
        row
        for row in installed(base, scope)
        if row["name"] == name and (version is None or row["version"] == version)
    ]


def _local(reference):
    path = Path(reference).expanduser()
    if path.is_dir() and (path / "build.yaml").is_file():
        return path / "build.yaml"
    return path if path.is_file() else None


def _source(base, scope, reference, *, fresh=False):
    if file := _local(reference):
        return file.resolve()
    name, version = split(reference)
    if not fresh:
        cwd = build_workspace.locate(Path.cwd())
        if (
            cwd
            and app_name(builds.load(cwd / "build.yaml", home=base)["config"]["id"])
            == name
            and (
                version is None
                or builds._declared_version(
                    builds.load(cwd / "build.yaml", home=base)["config"]
                )
                == version
            )
        ):
            return cwd / "build.yaml"
        for row in sorted(
            _matches(base, scope, reference),
            key=lambda row: row.get("enabled", False),
            reverse=True,
        ):
            workspace = row.get("workspace")
            if workspace and (Path(workspace) / "build.yaml").is_file():
                return Path(workspace) / "build.yaml"
        for candidate in sorted((scope.root / "src").glob(name + "-*/build.yaml")):
            loaded = builds.load(candidate, home=base)
            if version is None or builds._declared_version(loaded["config"]) == version:
                return candidate
    source = build_catalog.acquire(base, name, local_workspaces=False)
    loaded = builds.load(source, home=base)
    if version and builds._declared_version(loaded["config"]) != version:
        raise ForgeError(
            f"No recipe or installed version matches {reference}.",
            hint="jforge build list --available",
        )
    return source


def fetch(base, reference, directory=None, *, scope=None, fresh=True):
    scope = scope or selected(base, write=True)
    source = _source(base, scope, reference, fresh=fresh)
    loaded = builds.load(source, home=base)
    name = app_name(loaded["config"]["id"])
    version = builds._declared_version(loaded["config"])
    external = directory is not None
    target = (
        Path(directory).expanduser().resolve()
        if external
        else scope.root / "src" / semantic_name(name, version)
    )
    if source == target / "build.yaml":
        owner = read_json(target / "metadata.json")
        if owner.get("home") != str(base.root) or owner.get("profile") not in (
            None,
            scope.root.name,
        ):
            raise ForgeError(
                f"Workspace belongs to another Forge home or profile: {target}"
            )
        return target
    if (target / "metadata.json").is_file():
        existing = read_json(target / "metadata.json")
        if (
            existing.get("kind") != "workspace"
            or existing.get("home") != str(base.root)
            or existing.get("profile")
            not in (
                None,
                scope.root.name,
            )
        ):
            raise ForgeError(f"Workspace belongs to another profile or owner: {target}")
        if (target / "build.yaml").read_bytes() != source.read_bytes():
            raise ForgeError(
                f"Different recipe contents occupy {target}; the editable workspace is preserved."
            )
        return target
    build_workspace.create(base, source, target)
    data = read_json(target / "metadata.json")
    data.update(
        name=name,
        version=version,
        profile=scope.root.name,
        platform=profiles.record(scope)["platform"],
        external=external,
        source_root=str(source.parent),
        status="fetched",
    )
    write_json(target / "metadata.json", data)
    return target


def import_bundle(base, scope, app):
    target = scope.root / "src" / semantic_name(app["name"], app["version"])
    if target.exists():
        raise ForgeError(f"Import would overwrite an existing workspace: {target}")
    target.mkdir(parents=True)
    bundle = app["recipe"]
    write_text(target / "build.yaml", bundle["text"])
    for row in bundle["files"]:
        file = _inside(ForgeHome(target), target / row["path"])
        write_text(file, row["content"])
    builds.load(target / "build.yaml", home=base)
    write_json(
        target / "metadata.json",
        {
            "schema_version": 1,
            "kind": "workspace",
            "build": "build.yaml",
            "home": str(base.root),
            "name": app["name"],
            "version": app["version"],
            "profile": scope.root.name,
            "platform": profiles.record(scope)["platform"],
            "external": False,
            "origin": "profile-export",
            "resource_hashes": {row["path"]: row["sha256"] for row in bundle["files"]},
            "sha256": bundle["sha256"],
            "status": "fetched",
        },
    )
    return target


def _preflight(base, scope, workspace):
    """Probe independent requirements before acquiring dependency workspaces."""
    from . import standard_checks

    loaded = builds.load(workspace / "build.yaml", home=base)
    if not loaded["config"].get("requires"):
        return
    checks = [
        step
        for step in loaded["ordered"]
        if step["uses"] == "check" and not step.get("needs")
    ]
    if not checks:
        return
    data = profiles.record(scope)
    environment = profiles.build_environment(scope, data, os.environ)
    with operation(
        scope, "app-preflight", {"app": app_name(loaded["config"]["id"])}
    ) as (directory, result):
        for step in checks:
            logs = directory / step["id"]
            logs.mkdir()

            def invoke(argv):
                process = subprocess.run(
                    argv,
                    cwd=workspace,
                    env=environment,
                    capture_output=True,
                    timeout=step.get("timeout_seconds", 300),
                )
                (logs / "stdout.log").write_bytes(process.stdout)
                (logs / "stderr.log").write_bytes(process.stderr)
                if process.returncode:
                    raise ForgeError(
                        "Application preflight probe failed: "
                        + process.stderr.decode(errors="replace"),
                        code=1,
                    )

            standard_checks.execute(
                step["with"]["spec"], data["python"]["executable"], invoke, logs
            )
        result["status"] = "passed"


def _dependencies(base, scope, workspace, visiting, progress, confirm, parameters=None):
    loaded = builds.load(workspace / "build.yaml", home=base)
    name = app_name(loaded["config"]["id"])
    if name in visiting:
        raise ForgeError(
            "Cross-build dependency cycle: " + " -> ".join([*visiting, name])
        )
    for alias, requirement in loaded["config"].get("requires", {}).items():
        if requirement.get("artifact"):
            row = builds.artifact(base, requirement["artifact"])
            if row.get("profile_instance") != scope.root.name:
                raise ForgeError("A pinned app dependency belongs to another profile.")
            continue
        reference = requirement.get("build", requirement.get("workflow"))
        path = Path(reference)
        if not path.is_absolute() and (workspace / path).exists():
            reference = str(workspace / path)
        candidates = _matches(base, scope, reference)
        compatible = [
            row
            for row in candidates
            if row["status"] == "ready"
            and Version(row["version"]) in SpecifierSet(requirement.get("version", ""))
        ]
        requested_standard = (parameters or {}).get("cxx_standard")
        if requested_standard is not None:
            compatible = [
                row
                for row in compatible
                if builds.artifact(base, row["artifact_id"])["context"]["cxx_standard"]
                == str(requested_standard)
            ]
        if any(row.get("enabled") for row in compatible):
            continue
        if compatible:
            chosen = max(compatible, key=lambda row: Version(row["version"]))
            _enable(base, scope, chosen)
            continue
        child = fetch(base, reference, scope=scope, fresh=False)
        child_config = builds.load(child / "build.yaml", home=base)["config"]
        version = builds._declared_version(child_config)
        if confirm:
            confirm(
                {
                    "install": [
                        {"name": app_name(child_config["id"]), "version": version}
                    ],
                    "rebuild": [],
                    "remove": [],
                }
            )
        if Version(version) not in SpecifierSet(requirement.get("version", "")):
            raise ForgeError(
                f"Dependency {alias} requires {requirement['version']}; its recipe provides {version}."
            )
        result = install(
            base,
            str(child),
            scope=scope,
            progress=progress,
            confirm=confirm,
            _visiting=(*visiting, name),
            parameters={
                key: value
                for key, value in (parameters or {}).items()
                if key in child_config.get("parameters", {})
            }
            or None,
        )
        if result["status"] != "passed":
            run = result["runs"][-1]
            raise ForgeError(
                f"Dependency {alias} failed: " + result.get("error", "build failed"),
                code=1,
                hint="Build log: " + str(Path(run["attempt"]) / "build.log"),
            )


def _input(base, scope, workspace, parameters):
    graph = builds.graph(base, str(workspace), parameters=parameters)
    rows = {}
    for key, node in graph["nodes"].items():
        rows[key] = {
            "parameters": node["parameters"],
            "sources": {
                name: builds.source_identity(source, Path(node["path"]).parent)
                for name, source in node["config"].get("sources", {}).items()
            },
        }
    state = read_json(scope.root / "packages.json")[profiles.record(scope)["backend"]]
    return builds.fingerprint(
        {
            "nodes": rows,
            "packages": [
                (row["name"], row.get("hash", row.get("install_id")), row["prefix"])
                for row in state["packages"]
            ],
            "profile": profiles.record(scope)["python"],
        }
    )


class Rebuild:
    """Move managed prefixes on their filesystem; keep original absolute paths."""

    def __init__(
        self,
        base,
        scope,
        workspace,
        *,
        in_place=False,
        data=None,
        publication_only=False,
    ):
        self.base, self.scope, self.workspace = base, scope, workspace
        self.publication_only = publication_only
        self.files = {}
        self.moves = {}
        self.started = set()
        self.new_receipts = set()
        self.runtime_files = set(workspace.glob("*-activate*.sh"))
        for file in (
            scope.root / "metadata.json",
            scope.root / "activate.sh",
            scope.root / "profile.yaml",
            workspace / "metadata.json",
            workspace / "activate.sh",
            workspace / ".jforge-instance.json",
        ):
            self.files[file] = file.read_bytes() if file.is_file() else None
        for file in workspace.glob("*-activate*.sh"):
            if file.is_file():
                self.files[file] = file.read_bytes()
        original = read_json(workspace / "metadata.json")
        self.owners = {
            name for name in (scope.root.name, original.get("profile")) if name
        }
        self.receipts = {
            file: file.read_bytes()
            for file in (base.root / "builds/artifacts").glob("*/manifest.json")
            if read_json(file).get("profile_instance") in self.owners
        }
        data = data or original
        name = data["name"]
        version = data["version"]
        paths = (
            []
            if in_place
            else [
                workspace / "install"
                if data["external"]
                else scope.root / "apps" / semantic_name(name, version)
            ]
        )
        paths.append(
            workspace / "build"
            if data["external"]
            else scope.root / "build" / semantic_name(name, version)
        )
        # Downloaded source trees are owned by the workspace; local checkouts are not.
        for name, source in (
            builds.load(workspace / "build.yaml", home=base)["config"]
            .get("sources", {})
            .items()
        ):
            if "url" in source:
                paths.append(workspace / "src" / name)
        self.paths = [] if publication_only else paths

    def start(self):
        for path in self.paths:
            if path.is_symlink():
                raise ForgeError(f"Refusing to rebuild through a symlink: {path}")
            self.started.add(path)
            if path.exists():
                backup = (
                    path.parent
                    / "rollback"
                    / sequence(path.parent / "rollback", "rebuild-" + path.name)
                )
                backup.parent.mkdir(parents=True, exist_ok=True)
                path.rename(backup)
                self.moves[path] = backup
        for file in self.receipts:
            row = read_json(file)
            if Path(row["prefix"]) in self.paths or row.get("workspace") == str(
                self.workspace
            ):
                file.unlink()
        data = read_json(self.workspace / "metadata.json")
        data.pop("instance", None)
        data.pop("install_state", None)
        write_json(self.workspace / "metadata.json", data)

    def finish(self):
        pending = []
        for backup in self.moves.values():
            try:
                shutil.rmtree(backup)
            except OSError as exc:
                pending.append({"path": str(backup), "error": str(exc)})
        return pending

    def rollback(self):
        for path in self.started:
            if path.exists():
                shutil.rmtree(path)
            if path in self.moves:
                self.moves[path].rename(path)
        for file in (self.base.root / "builds/artifacts").glob("*/manifest.json"):
            if not self.publication_only and (
                file not in self.receipts
                and read_json(file).get("profile_instance") in self.owners
            ):
                file.unlink()
        if not self.publication_only:
            for file in set(self.workspace.glob("*-activate*.sh")) - self.runtime_files:
                file.unlink()
        for file, data in {
            **({} if self.publication_only else self.receipts),
            **self.files,
        }.items():
            if data is None:
                file.unlink(missing_ok=True)
            else:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(data)


def _enable(base, scope, app):
    manifest = builds.artifact(base, app["artifact_id"])
    if manifest.get("profile_instance") != scope.root.name:
        raise ForgeError("App belongs to a different profile.")
    if manifest.get("apt") and manifest["apt"] != builds._apt_snapshot(
        {"backend": "spack", "path": str(scope.root)}
    ):
        raise ForgeError("The app dependency plan changed; rebuild it before enabling.")
    _dependent_apps(
        base,
        scope,
        [
            row
            for row in installed(base, scope)
            if row["name"] == app["name"]
            and row.get("enabled")
            and row["artifact_id"] != app["artifact_id"]
        ],
    )
    saved = {
        file: file.read_bytes() if file.is_file() else None
        for file in (
            scope.root / "metadata.json",
            scope.root / "activate.sh",
            scope.root / "profile.yaml",
        )
    }
    data = profiles.record(scope)
    try:
        for row in data.get("apps", []):
            if row["name"] == app["name"]:
                row["enabled"] = row["artifact_id"] == app["artifact_id"]
        write_json(scope.root / "metadata.json", data)
        profiles.refresh_activation(scope)
        profiles.sync_definition(scope)
    except BaseException:
        for file, content in saved.items():
            if content is None:
                file.unlink(missing_ok=True)
            else:
                file.write_bytes(content)
        raise
    return manifest


def _install(
    base,
    reference,
    *,
    scope=None,
    parameters=None,
    rebuild=False,
    dry_run=False,
    progress=None,
    confirm=None,
    _visiting=(),
    verbose=False,
    as_json=False,
):
    scope = scope or selected(
        base, write=not dry_run, progress=progress, confirm=confirm
    )
    source = _source(base, scope, reference)
    located = build_workspace.locate(source)
    if located:
        data = read_json(located / "metadata.json")
        if data.get("home") != str(base.root):
            raise ForgeError(
                f"Workspace belongs to another Forge home: {located}",
                hint="Fetch the recipe into a new workspace in the current Forge home.",
            )
        if (
            data.get("profile") not in (None, scope.root.name)
            and data.get("external") is False
        ):
            raise ForgeError(
                "Internal app workspaces belong to their original profile.",
                hint="Fetch this app into the selected profile instead of moving its workspace.",
            )
        if data.get("profile") not in (None, scope.root.name) and not rebuild:
            raise ForgeError(
                f"Workspace belongs to profile {data['profile']}.",
                hint=f"Use --profile {data['profile']}, or --rebuild to select the current profile.",
            )
        workspace = located
    elif dry_run:
        workspace = source.parent
    else:
        workspace = fetch(base, str(source), scope=scope, fresh=False)
    if dry_run:
        return preview(base, scope, source, parameters=parameters)
    data = read_json(workspace / "metadata.json")
    if not data.get("name"):
        loaded = builds.load(workspace / "build.yaml", home=base)
        data.update(
            name=app_name(loaded["config"]["id"]),
            version=builds._declared_version(loaded["config"]),
            external=True,
        )
    declared = builds._declared_version(
        builds.load(workspace / "build.yaml", home=base)["config"]
    )
    if data["version"] != declared:
        raise ForgeError(
            "Workspace version differs from its recorded installation.",
            hint="Fetch the new version into a new workspace directory; each workspace holds one app version.",
        )
    original_metadata = (workspace / "metadata.json").read_bytes()
    old_scope = (
        profiles._scoped_home(base, "profiles", data["profile"])
        if data.get("profile") not in (None, scope.root.name)
        else None
    )
    if old_scope and not profiles.record(old_scope):
        old_scope = None
    if old_scope:
        _dependent_apps(
            base,
            old_scope,
            [
                row
                for row in installed(base, old_scope)
                if row.get("workspace") == str(workspace)
            ],
        )
    data.update(profile=scope.root.name, platform=profiles.record(scope)["platform"])
    _preflight(base, scope, workspace)
    _dependencies(base, scope, workspace, _visiting, progress, confirm, parameters)
    fingerprint = _input(base, scope, workspace, parameters)
    previous = read_json(workspace / "metadata.json").get("install_state", {})
    existing = _matches(base, scope, data["name"] + "@" + data["version"])
    ready = next((row for row in existing if row["status"] == "ready"), None)
    if (
        ready
        and not rebuild
        and previous.get("fingerprint") == fingerprint
        and previous.get("status") == "passed"
    ):
        manifest = _enable(base, scope, ready)
        return {
            "status": "passed",
            "action": "reused" if ready.get("enabled") else "enabled",
            "apps": _matches(base, scope, data["name"] + "@" + data["version"]),
            "activation": str(workspace / "activate.sh")
            if data["external"]
            else str(scope.root / "activate.sh"),
            "runs": [],
        }
    graph = builds.graph(base, str(workspace), parameters=parameters)
    in_place = any(
        step["with"].get("in_place")
        for step in graph["nodes"][graph["root"]]["ordered"]
        if step["uses"] == "cmake.project"
    )
    if (
        not in_place
        and not ready
        and not previous.get("run")
        and not data.get("instance")
    ):
        prefix = (
            workspace / "install"
            if data["external"]
            else scope.root / "apps" / semantic_name(data["name"], data["version"])
        )
        binary = (
            workspace / "build"
            if data["external"]
            else scope.root / "build" / semantic_name(data["name"], data["version"])
        )
        for path in (prefix, binary):
            if path.exists() or path.is_symlink():
                raise ForgeError(
                    f"Unregistered app directory is preserved: {path}",
                    hint="Choose a fresh app workspace or remove the conflicting directory yourself.",
                )
    rebuild = bool(
        rebuild
        or ready
        or data.get("instance")
        and previous.get("fingerprint") != fingerprint
    )
    plan = {"install": [], "rebuild": existing if rebuild else [], "remove": []}
    _dependent_apps(
        base,
        scope,
        [
            row
            for row in _matches(base, scope, data["name"])
            if row.get("enabled") and (rebuild or row["version"] != data["version"])
        ],
    )
    if rebuild:
        if confirm is None:
            raise ForgeError(
                "App rebuilding requires confirmation.",
                hint="jforge build install APP --rebuild --yes",
            )
        confirm(plan)
        if in_place and progress:
            progress(
                "In-place source build: existing binaries can change and cannot be rolled back."
            )
    tx = Rebuild(
        base,
        scope,
        workspace,
        in_place=in_place,
        data=data,
        publication_only=not rebuild,
    )
    if tx:
        tx.files[workspace / "metadata.json"] = original_metadata
        if old_scope:
            for file in (
                old_scope.root / "metadata.json",
                old_scope.root / "activate.sh",
                old_scope.root / "profile.yaml",
            ):
                tx.files[file] = file.read_bytes() if file.is_file() else None
    resume = (
        previous.get("run")
        if not rebuild
        and previous.get("fingerprint") == fingerprint
        and previous.get("status") in {"failed", "interrupted"}
        else None
    )
    try:
        write_json(workspace / "metadata.json", data)
        if rebuild:
            tx.start()
        result = builds.execute(
            base,
            None if resume else str(workspace),
            parameters=parameters,
            resume=resume,
            progress=progress,
            confirm=confirm,
            verbose=verbose,
            as_json=as_json,
        )
        if result["status"] != "passed":
            restored = rebuild
            if tx:
                tx.rollback()
                tx = None
            if restored:
                return {
                    "status": result["status"],
                    "action": "failed",
                    "apps": [],
                    "runs": [result],
                    "error": result.get("error"),
                }
            current = read_json(workspace / "metadata.json")
            current["install_state"] = {
                "fingerprint": _input(base, scope, workspace, parameters),
                "status": result["status"],
                "run": result["run_id"],
            }
            write_json(workspace / "metadata.json", current)
            return {
                "status": result["status"],
                "action": "failed",
                "apps": [],
                "runs": [result],
                "error": result.get("error"),
            }
        if old_scope:
            old = profiles.record(old_scope)
            old["apps"] = [
                row
                for row in old.get("apps", [])
                if row.get("workspace") != str(workspace)
            ]
            write_json(old_scope.root / "metadata.json", old)
            profiles.refresh_activation(old_scope)
            profiles.sync_definition(old_scope)
        current = read_json(workspace / "metadata.json")
        current["install_state"] = {
            "fingerprint": _input(base, scope, workspace, parameters),
            "status": "passed",
            "run": result["run_id"],
        }
        write_json(workspace / "metadata.json", current)
        profiles.sync_definition(scope)
        cleanup_pending = tx.finish() if tx else []
        tx = None
        if cleanup_pending and progress:
            progress(
                "App installed; some transaction backups could not be removed. See cleanup_pending in the result."
            )
        return {
            "status": "passed",
            "action": "installed",
            "cleanup_pending": cleanup_pending,
            "apps": _matches(base, scope, data["name"] + "@" + data["version"]),
            "activation": str(workspace / "activate.sh")
            if data["external"]
            else str(scope.root / "activate.sh"),
            "runs": [result],
        }
    except BaseException:
        if tx:
            tx.rollback()
            if not rebuild and "result" in locals():
                current = read_json(workspace / "metadata.json")
                current["install_state"] = {
                    "fingerprint": _input(base, scope, workspace, parameters),
                    "status": "failed",
                    "run": result["run_id"],
                }
                write_json(workspace / "metadata.json", current)
        raise


def install(
    base,
    reference,
    *,
    scope=None,
    parameters=None,
    rebuild=False,
    dry_run=False,
    progress=None,
    confirm=None,
    _visiting=(),
    verbose=False,
    as_json=False,
):
    from .apt_profiles import using_profile, validate_active

    scope = scope or selected(
        base, write=not dry_run, progress=progress, confirm=confirm
    )
    validate_active(scope)
    scopes = {scope.root: scope}
    if (
        not dry_run
        and (source := _local(reference))
        and (workspace := build_workspace.locate(source))
    ):
        metadata = read_json(workspace / "metadata.json")
        if metadata.get("profile") not in (None, scope.root.name):
            old = profiles._scoped_home(base, "profiles", metadata["profile"])
            if profiles.record(old):
                scopes[old.root] = old
    with using_profile(base, scope), ExitStack() as locks:
        if not dry_run:
            for root in sorted(scopes):
                locks.enter_context(locked(scopes[root]))
        return _install(
            base,
            reference,
            scope=scope,
            parameters=parameters,
            rebuild=rebuild,
            dry_run=dry_run,
            progress=progress,
            confirm=confirm,
            _visiting=_visiting,
            verbose=verbose,
            as_json=as_json,
        )


def preview(base, scope, source, *, parameters=None):
    from . import spack_backend, homebrew_backend

    graph = builds.graph(base, str(source), parameters=parameters)
    builds._check_host(base, graph)
    specs = []
    apps = []
    for node in graph["nodes"].values():
        apps.append(
            {
                "name": app_name(node["config"]["id"]),
                "version": builds._declared_version(node["config"]),
            }
        )
        for step in node["ordered"]:
            if step["uses"] == "apt.install":
                specs.extend(step["with"]["specs"])
    driver = (
        spack_backend
        if profiles.record(scope)["backend"] == "spack"
        else homebrew_backend
    )
    packages = (
        driver.transact(scope, "install", list(dict.fromkeys(specs)), dry_run=True)
        if specs
        else {"changes": {"install": [], "rebuild": [], "remove": []}}
    )
    return {
        "status": "planned",
        "dry_run": True,
        "apps": apps,
        "changes": packages["changes"],
        "packages": packages,
        "runs": [],
    }


def _dependent_apps(base, scope, removed):
    ids = {row["artifact_id"] for row in removed}
    blocked = []
    for row in installed(base, scope):
        if row["artifact_id"] in ids or not row.get("enabled"):
            continue
        record = read_json(Path(row["metadata"]))
        if ids.intersection(record.get("dependencies", [])):
            blocked.append(row["name"])
    if blocked:
        raise ForgeError(
            "Apps are required by enabled apps: " + ", ".join(sorted(set(blocked)))
        )


def remove(base, references, *, dry_run=False, confirm=None, scope=None):
    scope = scope or selected(base)
    with nullcontext() if dry_run else locked(scope):
        return _remove(base, references, dry_run=dry_run, confirm=confirm, scope=scope)


def _remove(base, references, *, dry_run=False, confirm=None, scope=None):
    scope = scope or selected(base)
    rows = []
    for reference in references:
        matches = _matches(base, scope, reference)
        if not matches:
            raise ForgeError(f"No installed app matches {reference}.")
        rows.extend(matches)
    rows = list({row["artifact_id"]: row for row in rows}.values())
    _dependent_apps(base, scope, rows)
    result = {
        "status": "planned" if dry_run else "passed",
        "dry_run": dry_run,
        "apps": rows,
        "action": "removed",
        "activation": str(scope.root / "activate.sh"),
    }
    if dry_run:
        return result
    if confirm is None:
        raise ForgeError(
            "App removal requires confirmation.", hint="jforge build remove APP --yes"
        )
    confirm({"install": [], "rebuild": [], "remove": rows})
    kept = [
        row
        for row in profiles.record(scope).get("apps", [])
        if row["artifact_id"] not in {item["artifact_id"] for item in rows}
    ]
    _delete(base, scope, rows, kept)
    return result


def _delete(base, scope, rows, kept):
    # All external workspace files remain user-owned, including install/.
    live = {row.get("prefix") for row in kept}
    paths = set()
    for row in rows:
        if row.get("external"):
            continue
        for parent in ("apps", "src", "build"):
            path = scope.root / parent / semantic_name(row["name"], row["version"])
            if str(scope.root / "apps" / path.name) not in live and path.exists():
                paths.add(_inside(scope, path))
    moved = {}
    saved = {
        file: file.read_bytes() if file.is_file() else None
        for file in (
            scope.root / "metadata.json",
            scope.root / "activate.sh",
            scope.root / "profile.yaml",
        )
    }
    try:
        for path in paths:
            backup = (
                path.parent
                / "rollback"
                / sequence(path.parent / "rollback", "remove-" + path.name)
            )
            backup.parent.mkdir(parents=True, exist_ok=True)
            path.rename(backup)
            moved[path] = backup
        data = profiles.record(scope)
        data["apps"] = kept
        write_json(scope.root / "metadata.json", data)
        profiles.refresh_activation(scope)
        profiles.sync_definition(scope)
    except BaseException:
        for path, backup in moved.items():
            backup.rename(path)
        for file, content in saved.items():
            if content is None:
                file.unlink(missing_ok=True)
            else:
                file.write_bytes(content)
        raise
    for backup in moved.values():
        shutil.rmtree(backup)


def autoremove(base, *, dry_run=False, confirm=None, scope=None):
    scope = scope or selected(base)
    with nullcontext() if dry_run else locked(scope):
        return _autoremove(base, dry_run=dry_run, confirm=confirm, scope=scope)


def _autoremove(base, *, dry_run=False, confirm=None, scope=None):
    scope = scope or selected(base)
    rows = [
        row
        for row in installed(base, scope)
        if not row.get("enabled") and not row.get("external")
    ]
    if not rows:
        return {
            "status": "passed",
            "action": "removed",
            "apps": [],
            "dry_run": dry_run,
            "activation": str(scope.root / "activate.sh"),
        }
    # Exact artifact selectors avoid removing the active version of the same app.
    _dependent_apps(base, scope, rows)
    if not dry_run:
        if confirm is None:
            raise ForgeError(
                "App autoremove requires confirmation.",
                hint="jforge build autoremove --yes",
            )
        confirm({"install": [], "rebuild": [], "remove": rows})
        ids = {row["artifact_id"] for row in rows}
        kept = [
            row
            for row in profiles.record(scope)["apps"]
            if row["artifact_id"] not in ids
        ]
        _delete(base, scope, rows, kept)
    return {
        "status": "planned" if dry_run else "passed",
        "action": "removed",
        "apps": rows,
        "dry_run": dry_run,
        "activation": str(scope.root / "activate.sh"),
    }


def _bundle(source, base):
    loaded = builds.load(source, home=base)
    copies = {"build.yaml": source.read_bytes()}
    for row in loaded["card_sources"]:
        if {"path", "content", "sha256"}.issubset(row):
            copies[row["path"]] = row["content"].encode()
    return copies


def _bundle_changed(workspace, copies):
    return any(
        not (workspace / name).is_file() or (workspace / name).read_bytes() != content
        for name, content in copies.items()
    )


def _check_editable(workspace, metadata, copies):
    tracked = {"build.yaml": metadata["sha256"], **metadata.get("resource_hashes", {})}
    for name, expected in tracked.items():
        file = _inside(ForgeHome(workspace), workspace / name)
        if (
            not file.is_file()
            or hashlib.sha256(file.read_bytes()).hexdigest() != expected
        ):
            raise ForgeError(
                f"Workspace has local edits: {file}",
                hint="Use build install DIR for your edits, or fetch the updated recipe into another directory.",
            )
    for name, content in copies.items():
        file = _inside(ForgeHome(workspace), workspace / name)
        if name not in tracked and file.exists() and file.read_bytes() != content:
            raise ForgeError(f"New recipe resource would overwrite a user file: {file}")


def upgrade(base, references=(), *, dry_run=False, confirm=None, progress=None):
    """Upgrade from the refreshed catalog, preserving edited workspace inputs."""
    from .apt_profiles import using_profile

    scope = selected(base, write=not dry_run, progress=progress, confirm=confirm)
    with using_profile(base, scope), nullcontext() if dry_run else locked(scope):
        active = [row for row in installed(base, scope) if row.get("enabled")]
        names = list(references) or [row["name"] for row in active]
        results = []
        plans = []
        for reference in names:
            name, version = split(reference)
            if version:
                raise ForgeError(
                    "Use app names with upgrade; select a version with build install APP@VERSION."
                )
            current = next((row for row in active if row["name"] == name), None)
            if not current:
                raise ForgeError(f"No enabled installed app matches {reference}.")
            if current.get("external"):
                workspace = Path(current["workspace"])
                source = workspace / "build.yaml"
                # An external editable recipe is authoritative; never replace it.
                wanted_version = builds._declared_version(
                    builds.load(source, home=base)["config"]
                )
            else:
                source = _source(base, scope, reference, fresh=True)
                wanted_version = builds._declared_version(
                    builds.load(source, home=base)["config"]
                )
                workspace = scope.root / "src" / semantic_name(name, wanted_version)
            copies = _bundle(source, base)
            changed = (
                workspace.is_dir()
                and source != workspace / "build.yaml"
                and _bundle_changed(workspace, copies)
            )
            if changed:
                _check_editable(
                    workspace, read_json(workspace / "metadata.json"), copies
                )
            if dry_run:
                planned = preview(base, scope, source)
                installed_input = (
                    read_json(workspace / "metadata.json").get("install_state", {})
                    if (workspace / "metadata.json").is_file()
                    else {}
                )
                unchanged = (
                    wanted_version == current["version"]
                    and workspace.is_dir()
                    and not _bundle_changed(workspace, copies)
                    and installed_input.get("fingerprint")
                    == _input(base, scope, workspace, None)
                )
                if not unchanged:
                    plans.append(planned)
                continue
            original = {}
            try:
                if changed:
                    metadata = read_json(workspace / "metadata.json")
                    copies = {
                        workspace / name: content for name, content in copies.items()
                    }
                    original = {
                        file: file.read_bytes() if file.is_file() else None
                        for file in [*copies, workspace / "metadata.json"]
                    }
                    for file, content in copies.items():
                        if not file.resolve().is_relative_to(workspace.resolve()):
                            raise ForgeError("Recipe update escapes its workspace.")
                        file.parent.mkdir(parents=True, exist_ok=True)
                        file.write_bytes(content)
                    metadata["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
                    metadata["resource_hashes"] = {
                        str(file.relative_to(workspace)): hashlib.sha256(
                            content
                        ).hexdigest()
                        for file, content in copies.items()
                        if file.name != "build.yaml"
                    }
                    write_json(workspace / "metadata.json", metadata)
                elif not workspace.is_dir():
                    workspace = fetch(base, str(source), scope=scope, fresh=True)
                result = install(
                    base,
                    str(workspace),
                    scope=scope,
                    rebuild=bool(original),
                    progress=progress,
                    confirm=confirm,
                )
                if result["status"] != "passed":
                    for file, content in original.items():
                        if content is None:
                            file.unlink(missing_ok=True)
                        else:
                            file.write_bytes(content)
                    return result
                results.append(result)
            except BaseException:
                for file, content in original.items():
                    if content is None:
                        file.unlink(missing_ok=True)
                    else:
                        file.write_bytes(content)
                raise
        return {
            "status": "planned" if dry_run else "passed",
            "dry_run": dry_run,
            "apps": [app for result in results for app in result["apps"]]
            if not dry_run
            else [app for plan in plans for app in plan["apps"]],
            "runs": [run for result in results for run in result["runs"]],
            "plans": plans,
            "activation": str(scope.root / "activate.sh"),
        }
