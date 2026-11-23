"""Profile-owned source archives shared by application presets."""

from pathlib import Path

from .downloads import unpack
from .paths import directory_lock, semantic_name, temporary_directory
from .storage import ForgeError, now, read_json, write_json


def location(scope, source):
    shared = source["shared"]
    return scope.root / "sources" / semantic_name(shared["name"], shared["version"])


def acquire(scope, source, download):
    """Publish one complete extraction; incomplete downloads leave no source tree."""
    target = location(scope, source)
    identity = {key: source.get(key, "") for key in ("url", "sha256", "subdir", "shared")}
    with directory_lock(scope.root / "locks/sources", target.name + ".lock"):
        if target.parent.is_symlink() or target.is_symlink():
            raise ForgeError(f"Shared source must not be a symbolic link: {target}")
        if target.exists():
            marker = target / "metadata.json"
            if not marker.is_file() or marker.is_symlink():
                raise ForgeError(f"Unregistered shared source directory is preserved: {target}")
            saved = read_json(marker)
            if (saved.get("kind") != "shared-source" or saved.get("status") != "ready"
                    or saved.get("profile") != str(scope.root) or saved.get("source") != identity):
                raise ForgeError(f"Shared source identity differs from the recipe: {target}")
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        archive = download({**source, **source["shared"]})
        with temporary_directory(target.parent / "work", "extract") as work:
            payload = work / "source"
            unpack(archive, payload, strip_prefix=source.get("subdir", ""))
            if not any(payload.iterdir()):
                raise ForgeError(f"Source archive has no files at {source.get('subdir', '')}")
            if (payload / "metadata.json").exists() or (payload / "metadata.json").is_symlink():
                raise ForgeError("Shared source archive contains reserved metadata.json")
            write_json(payload / "metadata.json", {
                "schema_version": 1, "kind": "shared-source", "status": "ready",
                **source["shared"], "profile": str(scope.root), "source": identity, "created_at": now(),
            })
            payload.rename(target)
        return target
