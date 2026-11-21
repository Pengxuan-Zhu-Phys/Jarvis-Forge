"""Native package replacement and rollback in the original absolute prefixes."""

from pathlib import Path
import shutil

from .paths import semantic_name, sequence
from .storage import ForgeError, digest, read_json, write_json


def reverse_dependents(packages, names):
    selected = set(names)
    changed = True
    while changed:
        changed = False
        for row in packages:
            dependencies = row.get("node", {}).get("dependencies", [])
            if row["name"] not in selected and any(
                dep["name"] in selected for dep in dependencies
            ):
                selected.add(row["name"])
                changed = True
    return selected


def protect_apps(home, packages):
    marker = home.root / "metadata.json"
    data = read_json(marker) if marker.is_file() else {}
    hashes = {row["hash"] for row in packages}
    names = {row["name"] for row in packages}
    blocked = []
    for app in data.get("apps", []):
        if not app.get("enabled", True):
            continue
        file = Path(app.get("metadata", Path(app.get("prefix", "")) / "metadata.json"))
        record = read_json(file) if file.is_file() else app
        dependencies = record.get(
            "runtime_packages", record.get("packages", record.get("dependencies", []))
        )
        if isinstance(dependencies, dict):
            dependencies = list(dependencies.values())
        if any(
            (isinstance(dep, str) and dep in names)
            or (
                isinstance(dep, dict)
                and (
                    dep.get("hash") in hashes
                    if dep.get("hash")
                    else dep.get("name") in names
                )
            )
            for dep in dependencies
        ):
            blocked.append(app["name"])
    if blocked:
        raise ForgeError(
            "Packages are required by enabled apps: " + ", ".join(sorted(set(blocked))),
            hint="Remove or rebuild these apps before removing their packages.",
        )


def history(home, action, candidate, result):
    folder = home.root / "history" / sequence(home.root / "history", action)
    folder.mkdir()
    for name in ("spack.yaml", "spack.lock"):
        if (candidate / name).is_file():
            shutil.copy2(candidate / name, folder / name)
    result.update(history=folder.name.split("-")[0], history_path=str(folder))
    write_json(
        folder / "result.json",
        {
            **result,
            "manifest_sha256": digest(folder / "spack.yaml"),
            "lock_sha256": digest(folder / "spack.lock")
            if (folder / "spack.lock").is_file()
            else None,
        },
    )
    return folder


def historical(home, number):
    if not str(number).isdigit():
        raise ForgeError("History selection must be a numeric sequence.")
    choices = list((home.root / "history").glob(f"{int(number):04d}-*"))
    if len(choices) != 1:
        raise ForgeError(f"No unique profile history matches {number}.")
    folder = choices[0]
    record = read_json(folder / "result.json")
    for name, key in [("spack.yaml", "manifest_sha256"), ("spack.lock", "lock_sha256")]:
        if not (folder / name).is_file() or digest(folder / name) != record.get(key):
            raise ForgeError(f"History is incomplete or changed: {folder / name}")
    return folder, record


class Transaction:
    def __init__(self, home, op, inventory, bridge, checkpoint):
        self.home, self.op, self.inventory, self.bridge = home, op, inventory, bridge
        self.checkpoint = checkpoint
        self.current_inventory = {
            row["prefix"]: row for row in inventory if not row["external"]
        }
        for row in inventory:
            self._validate(row)
        self.moved = {}
        self.detached = set()
        self.started = False
        self.original = {}
        for folder in (home.root / "spack/runtime", home.root / "spack/.jforge-config"):
            if folder.is_dir():
                for file in folder.rglob("*"):
                    if file.is_file():
                        self.original[file] = file.read_bytes()
        for name in (
            "spack/spack.yaml",
            "spack/spack.lock",
            "packages.json",
            "activate.sh",
            "profile.yaml",
            "metadata.json",
        ):
            file = home.root / name
            self.original[file] = file.read_bytes() if file.is_file() else None
        for row in inventory:
            if not row["external"]:
                file = Path(row["prefix"]) / "metadata.json"
                self.original[file] = file.read_bytes() if file.is_file() else None
        self.before_prefixes = {
            row["prefix"] for row in inventory if not row["external"]
        }
        self.new_prefixes = set()
        self.backup = (
            home.root
            / "pkg/rollback"
            / sequence(home.root / "pkg/rollback", "packages")
        )
        self.backup.mkdir()
        write_json(
            self.backup / "metadata.json",
            {
                "schema_version": 1,
                "kind": "rollback",
                "operation": str(op),
                "status": "prepared",
                "database_checkpoint": str(checkpoint),
                "prefixes": [],
            },
        )

    def _validate(self, row):
        if row["external"]:
            return
        from .package_operations import _inside

        prefix = _inside(self.home, Path(row["prefix"]))
        if prefix.parent != self.home.root / "pkg" or prefix.name != semantic_name(
            row["name"], row["version"]
        ):
            raise ForgeError(f"Unexpected package prefix: {prefix}")

    def changes(self, plan, overwrite=()):
        if "inventory" in plan:
            observed = {}
            for row in plan["inventory"]:
                self._validate(row)
                if not row["external"]:
                    observed[row["prefix"]] = row
            self.current_inventory = observed
        replacements = []
        for row in plan["packages"]:
            self._validate(row)
            if row["external"]:
                continue
            prefix = Path(row["prefix"])
            previous = self.current_inventory.get(str(prefix))
            if previous and (
                previous["hash"] != row["hash"] or row["name"] in overwrite
            ):
                replacements.append(previous)
            elif prefix.exists() and previous is None:
                raise ForgeError(
                    f"Unregistered package directory occupies {prefix}; its files are preserved."
                )
        return replacements

    def move(self, rows):
        rows = [row for row in rows if not row["external"]]
        for row in rows:
            self._validate(row)
        if not rows:
            return
        self.started = True
        hashes = list(dict.fromkeys(row["hash"] for row in rows))
        # A retry can register the same hash again; detach each observed native
        # record, while retaining the single original filesystem backup.
        self.bridge({"action": "detach", "hashes": hashes})
        self.detached.update(hashes)
        for row in rows:
            prefix = Path(row["prefix"])
            if prefix.exists():
                if str(prefix) in self.moved or str(prefix) in self.new_prefixes:
                    shutil.rmtree(prefix)
                else:
                    destination = self.backup / prefix.name
                    prefix.rename(destination)
                    self.moved[str(prefix)] = str(destination)
            self.current_inventory.pop(str(prefix), None)
        write_json(
            self.backup / "metadata.json",
            {
                "schema_version": 1,
                "kind": "rollback",
                "operation": str(self.op),
                "status": "installing",
                "database_checkpoint": str(self.checkpoint),
                "prefixes": self.moved,
            },
        )

    def prepare(self, plan, overwrite=()):
        replacements = self.changes(plan, overwrite)
        self.started = True
        self.move(replacements)
        self.new_prefixes.update(
            row["prefix"]
            for row in plan["packages"]
            if not row["external"] and row["prefix"] not in self.before_prefixes
        )

    def finish(self):
        shutil.rmtree(self.backup)

    def rollback(self):
        for prefix in set(self.moved) | self.new_prefixes:
            path = Path(prefix)
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        for prefix, backup in self.moved.items():
            if Path(backup).exists():
                Path(backup).rename(prefix)
        for folder in (
            self.home.root / "spack/runtime",
            self.home.root / "spack/.jforge-config",
        ):
            if folder.is_dir():
                shutil.rmtree(folder)
        database_error = None
        if self.started and self.checkpoint.is_file():
            try:
                self.bridge({"action": "restore", "checkpoint": str(self.checkpoint)})
            except BaseException as exc:
                database_error = exc
        for file, data in self.original.items():
            if data is None:
                file.unlink(missing_ok=True)
            else:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(data)
        if database_error is not None:
            raise ForgeError(
                "Package files and activation were restored, but the native database restore failed.",
                code=1,
                hint=f"Preserved database checkpoint: {self.checkpoint}\nRollback journal: {self.backup}",
            ) from database_error
        shutil.rmtree(self.backup)
