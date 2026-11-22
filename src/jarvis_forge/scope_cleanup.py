"""Delete registered scopes without unlinking open NFS lock files."""

import fcntl
import os
from pathlib import Path
import re
import shutil
import errno
import time

from .storage import ForgeError, read_json, write_json


def receipt(base, scope):
    from .package_operations import _inside
    return _inside(base, base.root / "removals" / (scope.root.parent.name + "-" + scope.root.name + ".json"))


def owned(base, scope):
    file = scope.root / "config.json"
    if not file.is_file() or file.is_symlink():
        return False
    data = read_json(file)
    return (data.get("schema_version") == 1 and data.get("layout_version") == 4
            and data.get("forge_home") == str(base.root))


def lock_residue(scope):
    if not scope.root.is_dir() or scope.root.is_symlink():
        return False
    for file in scope.root.rglob("*"):
        if file.is_symlink():
            return False
        if file.is_dir():
            if file.relative_to(scope.root).parts != ("locks",):
                return False
        elif file.parent != scope.root / "locks" or not (
                file.name in {"apps.lock", "apt.lock"} or re.fullmatch(r"\.nfs[0-9a-fA-F]+", file.name)):
            return False
    return True


def registered_removal(base, scope):
    file = receipt(base, scope)
    if not file.exists():
        return False
    if file.is_symlink():
        raise ForgeError(f"Removal record must not be a symbolic link: {file}")
    data = read_json(file)
    if data.get("schema_version") != 1 or data.get("scope") != str(scope.root):
        raise ForgeError(f"Invalid removal record: {file}")
    return True


def delete(base, scope):
    """Caller holds the external exclusive lease and has closed inner locks."""
    marker = scope.root / "metadata.json"
    file = receipt(base, scope)
    # A former NFS silly-rename may outlive an interrupted remover. Check and
    # close each descriptor before unlinking any lock directory.
    for lock in (scope.root / "locks").glob("*"):
        if lock.is_file() and not lock.is_symlink():
            descriptor = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "r+") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ForgeError(f"A running operation still holds {lock}.") from exc
    if not registered_removal(base, scope):
        write_json(file, {"schema_version": 1, "kind": "scope-removal", "scope": str(scope.root),
                          "metadata": read_json(marker) if marker.is_file() else None})
    if marker.is_file():
        write_json(marker, {**read_json(marker), "status": "removing"})
    for attempt in range(3):
        try:
            if scope.root.exists():
                for child in scope.root.iterdir():
                    if child.name == "metadata.json":
                        continue
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
                scope.root.rmdir()
            break
        except OSError as exc:
            if attempt < 2 and exc.errno in {errno.ENOTEMPTY, errno.EBUSY}:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise ForgeError(f"Cleanup of {scope.root.name} is incomplete: {exc}",
                             hint="Repeat the same remove or create command to continue cleanup.") from exc
    file.unlink(missing_ok=True)
