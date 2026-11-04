"""Download and unpack source archives inside a private build directory."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import shutil
import os
import subprocess
import tarfile
from urllib.parse import unquote, urlparse
import zipfile

from .storage import ForgeError


def fetch(url: str, target: Path, logs: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
        source = Path(unquote(parsed.path))
        if not source.is_file():
            raise ForgeError(f"Source archive is not a file: {source}", code=1)
        shutil.copyfile(source, target)
        return
    if parsed.scheme != "https":
        raise ForgeError(f"Sources must use HTTPS or a local file URI: {url}")
    curl, wget = shutil.which("curl"), shutil.which("wget")
    if curl:
        argv = [curl, "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
                "--connect-timeout", "20", "--max-time", "300", "--output", str(target), url]
    elif wget:
        argv = [wget, "--https-only", "--timeout=60", "--tries=1", "-O", str(target), url]
    else:
        raise ForgeError("HTTPS downloads require curl or wget on PATH.")
    with logs.with_suffix(".stdout.log").open("wb") as out, logs.with_suffix(".stderr.log").open("wb") as err:
        try:
            result = subprocess.run(argv, stdout=out, stderr=err, timeout=330)
        except subprocess.TimeoutExpired as exc:
            raise ForgeError(f"Download timed out: {url}", code=1) from exc
    if result.returncode:
        raise ForgeError(f"Download failed for {url}; see {logs.with_suffix('.stderr.log')}", code=1)


def unpack(archive: Path, destination: Path) -> None:
    """Extract files first, then validate internal links without following archive writes."""
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    total, count = 0, 0
    links = []
    created_links = []

    def target(name, size):
        nonlocal total, count
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ForgeError(f"Unsafe archive path: {name!r}", code=1)
        total += size
        count += 1
        if size < 0 or total > 4 * 1024**3 or count > 200000:
            raise ForgeError("Archive exceeds the extraction limits.", code=1)
        return destination.joinpath(*path.parts)

    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as package:
                for member in package.infolist():
                    mode = member.external_attr >> 16
                    kind = mode & 0o170000
                    if kind not in {0, 0o040000, 0o100000, 0o120000}:
                        raise ForgeError(f"Archive special files are unsupported: {member.filename}", code=1)
                    path = target(member.filename, member.file_size)
                    if kind == 0o120000:
                        if member.file_size > 65536:
                            raise ForgeError("Archive link target is too long.", code=1)
                        links.append((path, package.read(member).decode("utf-8"), "symlink"))
                    elif member.is_dir():
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with package.open(member) as src, path.open("xb") as dst:
                            shutil.copyfileobj(src, dst)
                        path.chmod((mode & 0o777) or 0o644)
        else:
            with tarfile.open(archive, "r:*") as package:
                for member in package:
                    if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                        raise ForgeError(f"Archive special files are unsupported: {member.name}", code=1)
                    path = target(member.name, member.size)
                    if member.issym() or member.islnk():
                        links.append((path, member.linkname, "symlink" if member.issym() else "hardlink"))
                    elif member.isdir():
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with package.extractfile(member) as src, path.open("xb") as dst:
                            shutil.copyfileobj(src, dst)
                        path.chmod((member.mode & 0o777) or 0o644)
        root = destination.resolve()
        for path, linkname, kind in links:
            link = PurePosixPath(linkname)
            if link.is_absolute() or "\\" in linkname or not linkname:
                raise ForgeError(f"Unsafe archive link: {path.name} -> {linkname}", code=1)
            candidate = path.parent / linkname if kind == "symlink" else destination / linkname
            if not candidate.resolve().is_relative_to(root):
                raise ForgeError(f"Archive link escapes extraction root: {path.name} -> {linkname}", code=1)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() or path.is_symlink():
                raise ForgeError(f"Archive link collides with another member: {path}", code=1)
        pending = [entry for entry in links if entry[2] == "hardlink"]
        while pending:
            later = []
            for path, linkname, kind in pending:
                source = destination / linkname
                if source.is_file() and not source.is_symlink():
                    os.link(source, path)
                    created_links.append(path)
                else:
                    later.append((path, linkname, kind))
            if len(later) == len(pending):
                raise ForgeError("Archive has unresolved or cyclic hardlinks.", code=1)
            pending = later
        for path, linkname, kind in links:
            if kind == "symlink":
                path.symlink_to(linkname)
                created_links.append(path)
        for path, _, _ in links:
            if not path.resolve().is_relative_to(root):
                raise ForgeError(f"Archive link chain escapes extraction root: {path}", code=1)
    except (ForgeError, tarfile.TarError, zipfile.BadZipFile, OSError, UnicodeError, RuntimeError) as exc:
        for path in reversed(created_links):
            path.unlink(missing_ok=True)
        if isinstance(exc, ForgeError):
            raise
        raise ForgeError(f"Cannot unpack source archive: {exc}", code=1) from exc
