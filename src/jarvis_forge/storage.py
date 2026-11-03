"""User-owned directories and plain-file records; reads never initialize state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


class ForgeError(Exception):
    def __init__(self, message: str, *, code: int = 2, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:10]}"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ForgeError(f"Cannot read record {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ForgeError(f"Expected a JSON object in {path}.")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", value):
        raise ForgeError(f"Invalid identifier: {value!r}.")
    return value


@dataclass(frozen=True)
class ForgeHome:
    root: Path

    @classmethod
    def resolve(cls, explicit: str | None = None) -> ForgeHome:
        return cls(Path(explicit or os.environ.get("JFORGE_HOME") or
                        "~/.jarvis-forge").expanduser().resolve())

    def paths(self) -> dict:
        names = ("recipes", "packages", "environments", "suites", "operations",
                 "runs", "comparisons", "services", "cache", "work", "locks")
        return {"home": str(self.root), **{n: str(self.root / n) for n in names}}

    def initialize(self) -> dict:
        config = self.root / "config.json"
        if config.exists():
            self.require_initialized()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, path in self.paths().items():
            if name != "home":
                Path(path).mkdir(exist_ok=True, mode=0o700)
        if not config.exists():
            # Exclusive creation also preserves identity under concurrent init.
            try:
                with config.open("x", encoding="utf-8") as stream:
                    os.chmod(config, 0o600)
                    json.dump({"schema_version": 1, "host_id": uuid.uuid4().hex,
                               "created_at": now()}, stream, indent=2)
                    stream.write("\n")
            except FileExistsError:
                pass
        return self.require_initialized()

    def require_initialized(self) -> dict:
        path = self.root / "config.json"
        if not path.exists():
            raise ForgeError(f"Forge home is not initialized: {self.root}",
                             hint=f"jforge --home {self.root} init")
        config = read_json(path)
        if config.get("schema_version") != 1 or not isinstance(config.get("host_id"), str):
            raise ForgeError(f"Invalid Forge home configuration: {path}")
        return config

    def packages(self, name: str | None = None) -> list[dict]:
        if name is not None:
            identifier(name)
        base = self.root / "packages"
        records = []
        for path in sorted(base.glob("*/*/*/manifest.json")):
            item = read_json(path)
            required = ("name", "version", "install_id", "prefix")
            if not all(isinstance(item.get(key), str) for key in required):
                raise ForgeError(f"Incomplete package manifest: {path}")
            if name is None or item["name"] == name:
                records.append({**item, "manifest_path": str(path),
                                "prefix_exists": Path(item["prefix"]).is_dir()})
        return records

    def run_path(self, run_id: str) -> Path:
        identifier(run_id)
        path = self.root / "runs" / run_id
        if not path.is_dir() or not (path / "request.json").is_file():
            raise ForgeError(f"Run not found: {run_id}", hint="jforge history")
        return path

    def show(self, run_id: str) -> dict:
        path = self.run_path(run_id)
        request = read_json(path / "request.json")
        final = path / "result.json"
        return {**request, **(read_json(final) if final.exists() else
                              {"status": "unknown", "reason": "No final result recorded."}),
                "path": str(path)}

    def history(self, suite: str | None = None) -> list[dict]:
        records = []
        for path in sorted((self.root / "runs").glob("*/request.json"), reverse=True):
            item = self.show(path.parent.name)
            if suite is None or item.get("suite_id") == suite:
                records.append(item)
        return records

