"""Download and unpack source archives inside a private build directory."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import shutil
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
    """Extract regular files/directories only; never follow archive links."""
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    total, count = 0, 0

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
                    if (mode & 0o170000) not in {0, 0o040000, 0o100000}:
                        raise ForgeError(f"Archive links/special files are unsupported: {member.filename}", code=1)
                    path = target(member.filename, member.file_size)
                    if member.is_dir():
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with package.open(member) as src, path.open("xb") as dst:
                            shutil.copyfileobj(src, dst)
                        path.chmod((mode & 0o777) or 0o644)
        else:
            with tarfile.open(archive, "r:*") as package:
                for member in package:
                    if not member.isdir() and not member.isfile():
                        raise ForgeError(f"Archive links/special files are unsupported: {member.name}", code=1)
                    path = target(member.name, member.size)
                    if member.isdir():
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with package.extractfile(member) as src, path.open("xb") as dst:
                            shutil.copyfileobj(src, dst)
                        path.chmod((member.mode & 0o777) or 0o644)
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
        raise ForgeError(f"Cannot unpack source archive: {exc}", code=1) from exc
