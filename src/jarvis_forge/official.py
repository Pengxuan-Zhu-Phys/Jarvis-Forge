"""Official repository indexes and user-prefix source installation adapters."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
from importlib.resources import files
import json
import lzma
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
from urllib.parse import quote

from packaging.version import InvalidVersion, Version
import yaml

from .catalog import Catalog, requirement, validate_recipe
from .downloads import fetch
from .packages import _inside, locked, operation, verified_archive, quilt_patches
from .storage import ForgeError, digest, now, package_name, read_json, write_json


REPOSITORIES = {"debian": "https://deb.debian.org/debian", "ubuntu": "https://archive.ubuntu.com/ubuntu"}
ADAPTERS = {"eigen3": "eigen", "redis": "redis"}
DEFAULT_SOURCE = {"origin": "debian", "suite": "bookworm", "component": "main", "keyring": None}


def source_components(source):
    values = source.get("components") or str(source.get("component", "main")).split(",")
    return list(dict.fromkeys(v.strip() for v in values))


def _source_key(source):
    return source["origin"], source["suite"], tuple(source_components(source)), tuple(source.get("pockets", [])), source.get("keyring")


def source_selection(profile):
    result = {key: profile.get(key) for key in DEFAULT_SOURCE}
    components = source_components(profile)
    result["component"] = components[0]
    if len(components) > 1:
        result["components"] = components
    if "pockets" in profile:
        result["pockets"] = list(profile["pockets"])
    return result


def _indexes(snapshot):
    return snapshot.get("indexes") or [snapshot]


def _selector(source):
    if source.get("origin") not in REPOSITORIES:
        raise ForgeError("--origin must be debian or ubuntu.")
    components = source_components(source)
    for field, value in [("suite", source.get("suite"))] + [("component", v) for v in components]:
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,127}", value):
            raise ForgeError(f"Invalid official source {field}: {value!r}")
    defaults = ["updates", "security"] if ((source["origin"] == "ubuntu" and source["suite"] in {"focal", "jammy", "noble"}) or
                                                  (source["origin"] == "debian" and source["suite"] in {"bullseye", "bookworm", "trixie"})) else []
    pockets = source.get("pockets", defaults)
    if not isinstance(pockets, list) or any(p not in {"updates", "security"} for p in pockets):
        raise ForgeError("Supported repository pockets are updates and security.")
    source = dict(source, component=components[0])
    if pockets or "pockets" in source:
        source["pockets"] = list(dict.fromkeys(pockets))
    if len(components) > 1:
        source["components"] = components
    else:
        source.pop("components", None)
    return source


def default_source(home):
    config = _load_profiles(home)
    source = config.get("source")
    if source is None and config["imports"]:
        source = source_selection(config["imports"][0])
    if source is not None and not isinstance(source, dict):
        raise ForgeError("Invalid official source configuration.")
    return _selector({**{key: source.get(key, value) for key, value in DEFAULT_SOURCE.items()}, **{key: source[key] for key in ("components", "pockets") if key in source}}) if source else None


def _index_rows(path):
    opener = lzma.open if path.name.endswith(".xz") else gzip.open
    try:
        with opener(path, "rt", encoding="utf-8") as stream:
            yield from paragraphs(stream)
    except (lzma.LZMAError, gzip.BadGzipFile, UnicodeError, EOFError) as exc:
        raise ForgeError(f"Cannot read official source index: {exc}", code=1) from exc



def _index_stamp(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]

def _cached_repository(home):
    path = home.root / "cache/apt/official-repository.json"
    if not path.exists():
        return None
    saved = read_json(path)
    if saved.get("schema_version") != 1 or not isinstance(saved.get("snapshot"), dict):
        raise ForgeError(f"Invalid official repository cache: {path}", hint="jforge apt update")
    if not all(isinstance(saved["snapshot"].get(key), str) for key in ("index", "index_sha256")):
        raise ForgeError(f"Incomplete official repository cache: {path}", hint="jforge apt update")
    if saved.get("source") != default_source(home):
        return None
    verified = saved.get("verified_indexes", {})
    for index_record in _indexes(saved["snapshot"]):
        index = _inside(home, Path(index_record["index"]))
        if not index.is_file():
            raise ForgeError("Official source index is missing or modified.", hint="jforge apt update")
        expected = {"sha256": index_record["index_sha256"], "stamp": _index_stamp(index)}
        if verified.get(str(index)) != expected and digest(index) != index_record["index_sha256"]:
            raise ForgeError("Official source index is missing or modified.", hint="jforge apt update")
    return saved


def _index_list_rows(path):
    """Read only fields needed for listing; ignore large checksum and file lists."""
    opener = lzma.open if path.name.endswith(".xz") else gzip.open
    wanted = {"Package", "Binary", "Version", "Description"}
    row, field = {}, None
    try:
        with opener(path, "rt", encoding="utf-8") as stream:
            for raw in stream:
                line = raw.rstrip("\r\n")
                if not line:
                    if row:
                        yield row
                    row, field = {}, None
                elif line.startswith((" ", "\t")):
                    if field in wanted:
                        row[field] += "\n" + line[1:]
                else:
                    key, sep, value = line.partition(":")
                    if not sep:
                        raise ForgeError("Malformed Debian control field.")
                    field = key
                    if key in wanted:
                        if key in row:
                            raise ForgeError(f"Duplicate Debian control field: {key}")
                        row[key] = value.lstrip()
            if row:
                yield row
    except (lzma.LZMAError, gzip.BadGzipFile, UnicodeError, EOFError) as exc:
        raise ForgeError(f"Cannot read official source index: {exc}", code=1) from exc


def cached_packages(home, query="", *, exact=False, details=False):
    saved = _cached_repository(home)
    if saved is None:
        if default_source(home) and (home.root / "cache/apt/official-repository.json").exists():
            raise ForgeError("The selected source configuration has changed; refresh its package indexes.", hint="jforge apt update")
        return []
    index = Path(saved["snapshot"]["index"])
    cache = _inside(home, home.root / "cache/apt/official-list.json")
    rows = None
    if not details and cache.is_file():
        try:
            data = read_json(cache)
            if data.get("schema_version") == 1 and data.get("index_keys") == [[r.get("suite"), r.get("component", saved["source"]["component"]), r["index_sha256"]] for r in _indexes(saved["snapshot"])] and isinstance(data.get("rows"), list) and all(
                isinstance(row, dict) and all(isinstance(row.get(k), str) for k in ("Package", "Version"))
                and all(isinstance(row.get(k, ""), str) for k in ("Binary", "Description", "Forge-Component", "Forge-Suite"))
                for row in data["rows"]):
                rows = data["rows"]
        except ForgeError:
            pass  # Rebuild an incomplete or damaged derived cache.
    if rows is None:
        rows = [dict(row, **{"Forge-Component": index_record.get("component", saved["source"]["component"]), "Forge-Suite": index_record.get("suite", saved["source"]["suite"])})
                for index_record in _indexes(saved["snapshot"])
                for row in (_index_rows(Path(index_record["index"])) if details else _index_list_rows(Path(index_record["index"])))]
        if not details:
            try:
                write_json(cache, {"schema_version": 1, "index_keys": [[r.get("suite"), r.get("component", saved["source"]["component"]), r["index_sha256"]] for r in _indexes(saved["snapshot"])], "rows": rows})
            except OSError:
                pass  # A read-only cache must not prevent listing packages.
    from debian.debian_support import Version as DebianVersion
    candidates = {}
    for row in rows:
        names = {row["Package"], *(n.strip() for n in row.get("Binary", "").replace("\n", " ").split(",") if n.strip())}
        for name in sorted(names):
            if (name == query if exact else query.lower() in (name + " " + row.get("Description", "")).lower()):
                entry = {"name": name, "version": row["Version"], "source_package": row["Package"],
                         "description": row.get("Description", f"Official source package {row['Package']}"),
                         "origin": f"official:{saved['source']['origin']}:{row.get('Forge-Suite', saved['source']['suite'])}/{row.get('Forge-Component', saved['source']['component'])}",
                         "status": "official-index"}
                if details:
                    entry["source_record"] = row
                if name not in candidates or DebianVersion(entry["version"]) > DebianVersion(candidates[name]["version"]):
                    candidates[name] = entry
    return [candidates[name] for name in sorted(candidates)]


def _save_repository(home, source, snapshot):
    previous = home.root / "cache/apt/official-repository.json"
    old = read_json(previous) if previous.exists() else {}
    if (old.get("snapshot") and [(r["index_sha256"]) for r in _indexes(old["snapshot"])] == [r["index_sha256"] for r in _indexes(snapshot)] and old.get("source") == source
            and isinstance(old.get("source_packages"), int)):
        count = old["source_packages"]
    else:
        count = len({row["Package"] for index_record in _indexes(snapshot) for row in _index_rows(Path(index_record["index"]))})
    verified = {r["index"]: {"sha256": r["index_sha256"], "stamp": _index_stamp(Path(r["index"]))} for r in _indexes(snapshot)}
    record = {"schema_version": 1, "source": source, "snapshot": snapshot,
              "source_packages": count, "updated_at": now(), "verified_indexes": verified}
    write_json(_inside(home, home.root / "cache/apt/official-repository.json"), record)
    return record


def source_family(source):
    """Identity of an isolated scope, independent of its trust-keyring path."""
    source = _selector(source)
    return (source["origin"], source["suite"], tuple(source_components(source)), tuple(source.get("pockets", [])))


def import_repository(home, *, origin, suite, component="main", keyring=None):
    source = _selector({"origin": origin, "suite": suite, "component": component,
                        "keyring": str(Path(keyring).expanduser().resolve()) if keyring else None})
    with locked(home), operation(home, "import-repository", source) as (directory, result):
        previous = default_source(home)
        different = previous and source_family(previous) != source_family(source)
        scoped = read_json(home.root / "config.json").get("apt_base_home") if (home.root / "config.json").is_file() else None
        if different and (scoped or _load_profiles(home)["imports"] or home.packages()):
            raise ForgeError("This environment already tracks packages from another source selection.",
                             hint=f"jforge apt switch {origin} --suite {suite} --component {','.join(source_components(source))}")
        snapshot = _snapshot(home, source, directory / "repository", {})
        saved = _save_repository(home, source, snapshot)
        config = _load_profiles(home)
        config["source"] = source
        write_json(_inside(home, home.root / "apt/official.json"), config)
        result.update(source=source, source_packages=saved["source_packages"], index=snapshot["index"],
                      apt_signature_verified=snapshot["apt_signature_verified"], trust=snapshot["trust"])
        return result


def paragraphs(stream):
    record, last = {}, None
    for raw in stream:
        line = raw.rstrip("\r\n")
        if not line:
            if record:
                yield record
            record, last = {}, None
        elif line.startswith((" ", "\t")):
            if last is None:
                raise ForgeError("Malformed Debian control continuation.")
            record[last] += "\n" + line[1:]
        else:
            key, separator, value = line.partition(":")
            if not separator or key in record:
                raise ForgeError(f"Malformed or duplicate Debian control field: {key}")
            record[key], last = value.lstrip(), key
    if record:
        yield record


def release_body(text):
    if text.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
        if "-----BEGIN PGP SIGNATURE-----" not in text:
            raise ForgeError("InRelease is missing its signature section.")
        try:
            body = text.split("\n\n", 1)[1].split("-----BEGIN PGP SIGNATURE-----", 1)[0]
        except IndexError as exc:
            raise ForgeError("Malformed InRelease clear-signed message.") from exc
        return "\n".join(line[2:] if line.startswith("- ") else line for line in body.splitlines())
    raise ForgeError("The official repository did not return an InRelease document.")


def checksums(text):
    result = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3 or not re.fullmatch(r"[a-fA-F0-9]{64}", parts[0]) or not parts[1].isdigit():
            raise ForgeError("Malformed SHA-256 repository record.")
        name = safe_relative(parts[2])
        if name in result:
            raise ForgeError(f"Duplicate repository filename: {name}")
        result[name] = {"sha256": parts[0].lower(), "size": int(parts[1])}
    return result


def safe_relative(name):
    path = PurePosixPath(name)
    if (path.is_absolute() or ".." in path.parts or not path.parts
            or any(char in name for char in ("\\", "%", "?", "#", ":"))):
        raise ForgeError(f"Unsafe repository path: {name!r}")
    return name


def source_row(index, package, version=None):
    opener = lzma.open if index.name.endswith(".xz") else gzip.open
    matches = []
    try:
        with opener(index, "rt", encoding="utf-8") as stream:
            for row in paragraphs(stream):
                binaries = {name.strip() for name in row.get("Binary", "").replace("\n", " ").split(",")}
                if package == row.get("Package") or package in binaries:
                    if version is None or row.get("Version") == version:
                        matches.append(row)
    except (lzma.LZMAError, gzip.BadGzipFile, UnicodeError, EOFError) as exc:
        raise ForgeError(f"Cannot read official source index: {exc}", code=1) from exc
    if len(matches) != 1:
        if not matches:
            raise ForgeError(f"Package {package!r} was not found in the selected official source index.",
                             hint="Check the origin, suite and component configured by jforge apt import.")
        versions = ", ".join(r.get("Version", "?") for r in matches)
        raise ForgeError(f"Expected one source record for {package}; found {len(matches)} ({versions}).",
                         hint="Choose a source version with --source-version.")
    return matches[0]


def _snapshot(home, profile, directory, session):
    origin, suite, component = profile["origin"], profile["suite"], profile["component"]
    key = _source_key(profile)
    if key in session:
        return session[key]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if profile.get("pockets"):
        snapshots = []
        for selected_suite in [suite] + [suite + "-" + pocket for pocket in profile["pockets"]]:
            child = dict(profile, suite=selected_suite, pockets=[])
            snapshots.append(_snapshot(home, child, directory / selected_suite, session))
        snapshot = dict(snapshots[0], indexes=[record for saved in snapshots for record in _indexes(saved)],
                        pockets=list(profile["pockets"]),
                        release_checksums=[{"suite": saved["suite"], "sha256": saved["release_sha256"]} for saved in snapshots])
        expiries = [saved["valid_until"] for saved in snapshots if saved.get("valid_until")]
        if expiries:
            snapshot["valid_until"] = min(expiries, key=parsedate_to_datetime)
        write_json(directory / "snapshot.json", snapshot)
        session[key] = snapshot
        return snapshot
    repo = ("https://security.debian.org/debian-security" if origin == "debian" and suite.endswith("-security") else REPOSITORIES[origin])
    release = directory / "InRelease"
    fetch(f"{repo}/dists/{suite}/InRelease", release, directory / "release-download")
    signature_verified = False
    if profile.get("keyring"):
        gpgv = shutil.which("gpgv")
        if not gpgv:
            raise ForgeError("--keyring verification requires gpgv on PATH.")
        ring = Path(profile["keyring"])
        if not ring.is_file():
            raise ForgeError(f"Trusted archive keyring not found: {ring}")
        with (directory / "signature.stdout.log").open("wb") as out, (directory / "signature.stderr.log").open("wb") as err:
            try:
                process = subprocess.run([gpgv, "--homedir", str(directory), "--keyring", str(ring), str(release)],
                                         stdout=out, stderr=err, timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise ForgeError("InRelease signature verification timed out.", code=1) from exc
        if process.returncode:
            raise ForgeError("Official InRelease signature verification failed.", code=1)
        signature_verified = True
    records = list(paragraphs(release_body(release.read_text(encoding="utf-8")).splitlines()))
    if len(records) != 1:
        raise ForgeError("Invalid repository Release metadata.")
    metadata = records[0]
    expected_origin = origin
    if metadata.get("Origin", "").lower() != expected_origin:
        raise ForgeError("Repository Origin does not match the selected distribution.")
    if suite not in {metadata.get("Codename"), metadata.get("Suite")}:
        raise ForgeError("Repository suite/codename does not match the requested distribution.")
    if metadata.get("Valid-Until"):
        expires = parsedate_to_datetime(metadata["Valid-Until"])
        if expires <= datetime.now(timezone.utc):
            raise ForgeError("The repository's Release metadata has expired.", code=1)
    hashes = checksums(metadata.get("SHA256", ""))
    from .official_dependencies import architecture
    try:
        arch = architecture()
    except ForgeError:
        arch = None  # Metadata and explicit source templates remain portable.
    indexes = []
    for selected_component in source_components(profile):
        index_record = {"component": selected_component, "suite": suite, "repo": repo}
        kinds = [("source", "Sources", "index", "index_sha256")]
        if arch:
            kinds.append(("binary-" + arch, "Packages", "binary_index", "binary_sha256"))
        for folder, basename, path_key, sha_key in kinds:
            candidates = [f"{selected_component}/{folder}/{basename}.xz", f"{selected_component}/{folder}/{basename}.gz"]
            selected = next((name for name in candidates if name in hashes), None)
            if not selected:
                raise ForgeError(f"No {basename} index for {selected_component}/{folder} in {origin} {suite}.")
            expected = hashes[selected]
            target = _inside(home, home.root / "cache/apt/official-indexes" / (expected["sha256"] + Path(selected).suffix))
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not target.is_file() or digest(target) != expected["sha256"] or target.stat().st_size != expected["size"]:
                downloaded = directory / f"{selected_component}-{basename}{Path(selected).suffix}"
                download_url = f"{repo}/dists/{suite}/{selected}"
                if metadata.get("Acquire-By-Hash", "").lower() in {"yes", "force"}:
                    hashed_url = f"{repo}/dists/{suite}/{PurePosixPath(selected).parent}/by-hash/SHA256/{expected['sha256']}"
                    try:
                        fetch(hashed_url, downloaded, directory / f"{selected_component}-{basename}-by-hash-download")
                    except ForgeError:
                        # A mirror can lack an older by-hash object. Fallback is
                        # still checked against the same signed release digest.
                        fetch(download_url, downloaded, directory / f"{selected_component}-{basename}-download")
                else:
                    fetch(download_url, downloaded, directory / f"{selected_component}-{basename}-download")
                if digest(downloaded) != expected["sha256"] or downloaded.stat().st_size != expected["size"]:
                    raise ForgeError(f"{basename} index does not match Release SHA-256/size; the mirror may have changed during refresh.", code=1, hint="Retry jforge apt update; no unverified index was published.")
                shutil.copyfile(downloaded, target)
            index_record.update({path_key: str(target), sha_key: expected["sha256"]})
        indexes.append(index_record)
    snapshot = {**indexes[0], "indexes": indexes, "architecture": arch,
                "repo": repo, "release_sha256": digest(release), "release_date": metadata.get("Date"),
                "valid_until": metadata.get("Valid-Until"), "codename": metadata.get("Codename"),
                "apt_signature_verified": signature_verified,
                "trust": "trusted-keyring" if signature_verified else "official-HTTPS-and-checksums"}
    write_json(directory / "snapshot.json", snapshot)
    session[key] = snapshot
    return snapshot


def _archive_layout(path):
    try:
        with tarfile.open(path, "r:*") as package:
            names = [m.name.removeprefix("./") for m in package.getmembers() if m.name not in {".", "./"}]
    except tarfile.TarError as exc:
        raise ForgeError(f"Cannot inspect source archive: {exc}", code=1) from exc
    # Archive members are filesystem paths, not URLs. Characters such as #
    # are valid source filenames (Boost and MariaDB ship them).
    for name in names:
        member = PurePosixPath(name)
        if member.is_absolute() or ".." in member.parts or "\\" in name or "\x00" in name:
            raise ForgeError(f"Unsafe archive path: {name!r}")
    roots = {PurePosixPath(name).parts[0] for name in names}
    if len(roots) != 1:
        raise ForgeError("Source archive needs an adapter for its directory layout.")
    if not roots:
        raise ForgeError("Source archive contains no files or directories.")
    root = next(iter(roots))
    relative = {str(PurePosixPath(name).relative_to(root)) for name in names}
    backend = "custom"
    for directory in ("", "build/cmake", "cmake", "expat", "src", "unix"):
        prefix = directory + "/" if directory else ""
        for marker, kind in (("CMakeLists.txt", "cmake"), ("configure", "autotools"), ("meson.build", "meson")):
            if prefix + marker in relative:
                backend = kind + (":" + directory if directory else "")
                return root, backend
    return root, backend


def _upstream_version(version):
    upstream = re.sub(r"^\d+:", "", version).rsplit("-", 1)[0] if "-" in version else re.sub(r"^\d+:", "", version)
    numeric_letter = re.fullmatch(r"(\d+(?:\.\d+)*)([a-zA-Z])", upstream)
    version_input = numeric_letter[1] + "+" + numeric_letter[2].lower() if numeric_letter else upstream
    try:
        normalized = str(Version(version_input))
    except InvalidVersion as exc:
        repacked = re.sub(r"[._-]((?:dfsg|ds)[A-Za-z0-9.]*)$", r"+\1", upstream, flags=re.I)
        if repacked == upstream:
            match = re.fullmatch(r"(\d+(?:\.\d+)*)([A-Za-z][A-Za-z0-9.]*)", upstream)
            repacked = match[1] + "+" + match[2].lower() if match else upstream
        try:
            normalized = str(Version(repacked))
        except InvalidVersion:
            raise ForgeError(f"Upstream version {upstream!r} needs an explicit version adapter.") from exc
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", normalized):
        raise ForgeError("Normalized upstream version cannot be represented in a Forge recipe.")
    return normalized


def _generic_draft(profile, row, source, backend):
    kind, _, subdirectory = backend.partition(":")
    if kind not in {"cmake", "autotools", "meson"}:
        return None
    source_directory = "${source}" + ("/" + subdirectory if subdirectory else "")
    if kind == "cmake":
        build = [["cmake", "-S", source_directory, "-B", "${build}", "-DCMAKE_INSTALL_PREFIX=${prefix}", "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_TESTING=OFF"],
                 ["cmake", "--build", "${build}", "--parallel", "${jobs}"]]
        install, tools = [["cmake", "--install", "${build}"]], ["cmake", "cc", "c++"]
    elif kind == "meson":
        build = [["meson", "setup", "${build}", source_directory, "--prefix=${prefix}", "--libdir=lib", "--buildtype=release", "--wrap-mode=nodownload"],
                 ["meson", "compile", "-C", "${build}", "-j", "${jobs}"]]
        install, tools = [["meson", "install", "-C", "${build}"]], ["meson", "ninja", "cc", "c++"]
    else:
        build = [[source_directory + "/configure", "--prefix=${prefix}"], ["make", "-j${jobs}"]]
        install, tools = [["make", "install"]], ["make", "cc", "c++"]
    return {"schema_version": 2 if source.get("components") else 1, "name": profile["name"], "version": _upstream_version(row["Version"]),
            "description": f"Draft build adapter for {row['Package']}", "source": source,
            "requires_tools": tools + (["patch"] if source.get("patch_archive") else []),
            "depends": [], "build": build, "install": install,
            "check": [["${python}", "-c", "from pathlib import Path; import sys; assert any(Path(sys.argv[1]).iterdir())", "${prefix}"]],
            "environment": {"prepend": {"PATH": ["${prefix}/bin"], "CMAKE_PREFIX_PATH": ["${prefix}"]}},
            "provenance": {"kind": "official-apt-source", "status": "draft", "debian_version": row["Version"],
                           "unresolved_build_dependencies": {k: v for k, v in row.items() if k.startswith("Build-Depends")},
                           "review_required": "Map dependencies, build options and package-specific checks before use."}}


def _member_text(archive, root, name):
    with tarfile.open(archive, "r:*") as package:
        member = next((m for m in package.getmembers() if m.name.removeprefix("./") == f"{root}/{name}"), None)
        if member is None or not member.isfile() or member.size > 2 * 1024 * 1024:
            return None
        with package.extractfile(member) as stream:
            return stream.read().decode("utf-8", errors="replace")


def _automatic_recipe(profile, row, source, backend, archive):
    # Metadata-only packages are dependency nodes, not empty native builds.
    if row["Package"] in {"boost-defaults", "libjpeg8-empty", "mpi-defaults", "blas-defaults"}:
        return {"schema_version": 2, "name": profile["name"], "version": _upstream_version(row["Version"]),
                "description": "Dependency metapackage from official source metadata", "source": source,
                "install": [["${python}", "-c", "from pathlib import Path; import sys; (Path(sys.argv[1])/'.jforge-metapackage').write_text(sys.argv[2])", "${prefix}", row["Package"]]],
                "check": [["${python}", "-c", "from pathlib import Path; import sys; assert (Path(sys.argv[1])/'.jforge-metapackage').is_file()", "${prefix}"]],
                "artifacts": [".jforge-metapackage"], "provenance": {"build_adapter": "metapackage"}, "environment": {}}
    if row["Package"].startswith("boost1."):
        if not re.fullmatch(r"libboost(?:\d+\.\d+)?-dev", profile["package"] if "package" in profile else profile["name"]):
            return None  # Compiled Boost components require their own feature contract.
        with tarfile.open(archive, "r:*") as package:
            names = {member.name.removeprefix("./") for member in package.getmembers() if member.isfile()}
        root = source["subdir"]
        if f"{root}/boost/version.hpp" not in names and not any(
                name.startswith(root + "/libs/") and name.endswith("/include/boost/version.hpp") for name in names):
            return None
        copy_headers = ("from pathlib import Path\nimport shutil,sys\n"
                        "s=Path(sys.argv[1]); dst=Path(sys.argv[2])/'include/boost'\n"
                        "parts=([s/'boost'] if (s/'boost').is_dir() else sorted((s/'libs').glob('**/include/boost')))\n"
                        "for part in parts:\n"
                        " for f in part.rglob('*'):\n"
                        "  if f.is_file():\n"
                        "   target=dst/f.relative_to(part); target.parent.mkdir(parents=True,exist_ok=True)\n"
                        "   if target.exists() and target.read_bytes()!=f.read_bytes(): raise SystemExit('Conflicting Boost header: '+str(target))\n"
                        "   shutil.copy2(f,target)\n")
        return {"schema_version": 2, "name": profile["name"], "version": _upstream_version(row["Version"]),
                "description": "Boost development headers", "source": source,
                "install": [["${python}", "-c", copy_headers, "${source}", "${prefix}"]],
                "check": [["${python}", "-c", "from pathlib import Path; import sys; assert (Path(sys.argv[1])/'include/boost/version.hpp').is_file()", "${prefix}"]],
                "artifacts": ["include/boost/version.hpp"], "provenance": {"build_adapter": "boost-headers"},
                "environment": {"prepend": {"CMAKE_PREFIX_PATH": ["${prefix}"], "CPATH": ["${prefix}/include"]}, "set": {"BOOST_ROOT": "${prefix}", "BOOST_INCLUDEDIR": "${prefix}/include"}}}
    if row["Package"] == "openssl" and _member_text(archive, source["subdir"], "Configure") is not None:
        return {"schema_version": 2, "name": profile["name"], "version": _upstream_version(row["Version"]),
                "description": "OpenSSL libraries and command line tools", "source": source,
                "requires_tools": ["perl", "make", "cc"], "build": [["perl", "${source}/Configure", "--prefix=${prefix}", "--openssldir=${prefix}/ssl", "--libdir=lib"], ["make", "-j${jobs}"]],
                "install": [["make", "install_sw"]], "check": [["${prefix}/bin/openssl", "version"]],
                "artifacts": ["bin/openssl", "include/openssl/ssl.h", "lib/pkgconfig/openssl.pc"], "provenance": {"build_adapter": "openssl"},
                "environment": {"prepend": {"PATH": ["${prefix}/bin"], "CMAKE_PREFIX_PATH": ["${prefix}"], "PKG_CONFIG_PATH": ["${prefix}/lib/pkgconfig"], "CPATH": ["${prefix}/include"], "LIBRARY_PATH": ["${prefix}/lib"], "LD_LIBRARY_PATH": ["${prefix}/lib"]}}}
    recipe = _generic_draft(profile, row, source, backend)
    if recipe:
        recipe["provenance"] = {}
        if row["Package"] == "eigen3":
            recipe["build"][0].append("-DEIGEN_BUILD_DOC=OFF")
            recipe["provenance"]["build_adapter"] = "eigen-headers"
        recipe["description"] = f"{row['Package']} from official {profile['origin']} sources ({backend})"
        recipe["environment"]["prepend"].update({"PKG_CONFIG_PATH": ["${prefix}/lib/pkgconfig", "${prefix}/lib64/pkgconfig", "${prefix}/share/pkgconfig"],
                                                 "LD_LIBRARY_PATH": ["${prefix}/lib", "${prefix}/lib64"], "LIBRARY_PATH": ["${prefix}/lib", "${prefix}/lib64"], "CPATH": ["${prefix}/include"]})
        recipe["check"] = [["${python}", "-c", "from pathlib import Path; import sys; assert any(p.is_file() for p in Path(sys.argv[1]).rglob('*'))", "${prefix}"]]
        if (row["Package"] == "gsl" and backend == "autotools" and "+dfsg" in row["Version"]
                and _member_text(archive, source["subdir"], "doc/autoconf.rst") is None):
            # Debian's repacked GSL source can omit documentation required by
            # upstream's recursive all/install targets. Retain the library build.
            skip_docs = ("from pathlib import Path\nimport re,sys\n"
                         "p=Path(sys.argv[1])/'Makefile'\ns=p.read_text()\n"
                         "pattern=r'(?m)^SUBDIRS[ \\t]*=[^\\n]*(?:\\\\\\n[^\\n]*)*'\n"
                         "m=re.search(pattern,s)\n"
                         "if not m or 'doc' not in m.group().split(): raise SystemExit('GSL SUBDIRS layout changed; cannot skip documentation safely')\n"
                         "line=re.sub(r'(?<!\\S)doc(?!\\S)', '', m.group())\n"
                         "p.write_text(s[:m.start()]+line+s[m.end():])\n")
            recipe["build"].insert(1, ["${python}", "-c", skip_docs, "${source}"])
            recipe["schema_version"] = 2
            recipe["artifacts"] = ["include/gsl/gsl_version.h", "bin/gsl-config", "lib/pkgconfig/gsl.pc", "lib/libgsl.a"]
            verify = ("from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                      "assert (p/'include/gsl/gsl_version.h').is_file(); "
                      "assert any(x.is_file() for d in ('lib','lib64') for x in (p/d).glob('libgsl.*')); "
                      "assert any((p/d/'pkgconfig/gsl.pc').is_file() for d in ('lib','lib64'))")
            recipe["check"] = [["${python}", "-c", verify, "${prefix}"], ["${prefix}/bin/gsl-config", "--version"]]
        return recipe
    # Neofetch is an architecture-independent Bash command, not a CMake project.
    if row["Package"] == "neofetch":
        script = _member_text(archive, source["subdir"], "neofetch")
        if script and re.match(r"^#![^\n]*\bbash\b", script):
            copy = ("from pathlib import Path; import shutil,sys; s=Path(sys.argv[1]); p=Path(sys.argv[2]); "
                    "(p/'bin').mkdir(parents=True,exist_ok=True); shutil.copyfile(s/'neofetch',p/'bin/neofetch'); "
                    "(p/'bin/neofetch').chmod(0o755); "
                    "m=p/'share/man/man1'; m.mkdir(parents=True,exist_ok=True); "
                    "shutil.copyfile(s/'neofetch.1',m/'neofetch.1') if (s/'neofetch.1').is_file() else None")
            check = ("import subprocess,sys; "
                     "r=subprocess.run([sys.argv[1],'--no_config','--version'],capture_output=True,text=True); "
                     "sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); "
                     "sys.exit(0 if r.returncode in (0,1) and r.stdout.strip() == 'Neofetch ' + sys.argv[2] else 1)")
            return {"schema_version": 1, "name": profile["name"], "version": _upstream_version(row["Version"]),
                    "description": "Neofetch system information command from official sources", "source": source,
                    "requires_tools": ["bash"], "install": [["${python}", "-c", copy, "${source}", "${prefix}"]],
                    "check": [["bash", "-n", "${prefix}/bin/neofetch"],
                              ["${python}", "-c", check, "${prefix}/bin/neofetch", _upstream_version(row["Version"])]],
                    "environment": {"prepend": {"PATH": ["${prefix}/bin"]}}}
    # Only infer Make installs that explicitly expose a prefix variable.
    makefile = _member_text(archive, source["subdir"], "Makefile")
    if makefile and re.search(r"(?m)^install\s*:", makefile) and not re.search(r"(?m)^override\s+(?:PREFIX|prefix)\b", makefile):
        variable = next((v for v in ("PREFIX", "prefix") if re.search(rf"(?m)^{v}\s*[?:+]?=", makefile)
                         and (f"$({v})" in makefile or "${" + v + "}" in makefile)), None)
        if variable:
            return {"schema_version": 1, "name": profile["name"], "version": _upstream_version(row["Version"]),
                    "description": f"{row['Package']} from official sources (Makefile prefix)", "source": source,
                    "requires_tools": ["make", "cc", "c++"], "build": [["make", "-j${jobs}"]],
                    "install": [["make", "install", variable + "=${prefix}"]],
                    "check": [["${python}", "-c", "from pathlib import Path; import sys; assert any(p.is_file() for p in Path(sys.argv[1]).rglob('*'))", "${prefix}"]],
                    "environment": {"prepend": {"PATH": ["${prefix}/bin"], "CMAKE_PREFIX_PATH": ["${prefix}"]}}}
    return None



def source_archives(row):
    """Select the main archive and named components without discarding inputs."""
    hashes = checksums(row.get("Checksums-Sha256", ""))
    extension = r"(?:gz|bz2|xz|lzma)"
    mains = [name for name in hashes if re.search(r"\.orig\.tar\." + extension + r"$", name)]
    addons = [name for name in hashes if ".orig-" in name and not name.endswith(".asc")]
    if not mains:
        mains = [name for name in hashes if re.search(r"\.tar\." + extension + r"$", name) and ".debian." not in name and ".orig-" not in name]
    if len(mains) != 1:
        raise ForgeError(f"Expected one main source archive for {row['Package']}; found {mains or 'none'}. Components: {addons}")
    main = mains[0]
    prefix = main.split(".orig.tar.", 1)[0]
    components, seen = [], set()
    for filename in sorted(addons):
        match = re.fullmatch(re.escape(prefix) + r"\.orig-([A-Za-z0-9][A-Za-z0-9-]*)\.tar\." + extension, filename)
        if not match or match[1].lower() == "debian" or match[1].lower() in seen:
            raise ForgeError(f"Invalid or duplicate source component archive: {filename}")
        seen.add(match[1].lower())
        components.append((match[1], filename))
    return main, components

def _candidate(home, profile, row, snapshot, directory):
    hashes = checksums(row.get("Checksums-Sha256", ""))
    original, components = source_archives(row)
    base = safe_relative(row.get("Directory", ""))
    url = lambda n: snapshot["repo"] + "/" + quote(base + "/" + n, safe="/+~._-")
    source = {"url": url(original), "sha256": hashes[original]["sha256"]}
    archive = verified_archive(home, source, directory / "source-download")
    source["subdir"], backend = _archive_layout(archive)
    if components:
        source["components"] = [{"name": name, "url": url(filename), "sha256": hashes[filename]["sha256"]}
                                for name, filename in components]
    packaging = [n for n in hashes if re.search(r"\.debian\.tar\.(gz|bz2|xz)$", n)]
    if len(packaging) > 1 or any(n.endswith(".diff.gz") for n in hashes):
        raise ForgeError("This Debian source format needs a packaging adapter.")
    patch_names = []
    if packaging:
        name = packaging[0]
        source["patch_archive"] = {"url": url(name), "sha256": hashes[name]["sha256"]}
        patch_archive = verified_archive(home, source["patch_archive"], directory / "packaging-download")
        from .downloads import unpack
        unpack(patch_archive, directory / "packaging")
        patch_names = [p.name for p, _ in quilt_patches(directory / "packaging")]
    template = profile.get("template")
    if template is None and profile.get("automatic"):
        template = _automatic_recipe(profile, row, source, backend, archive)
    if template is None:
        return None, {"reason": f"Detected {backend}; no compatible build adapter is available for this source layout.",
                      "source": source, "detected_backend": backend, "patches": patch_names,
                      "draft_recipe": _generic_draft(profile, row, source, backend)}
    recipe = copy.deepcopy(template)
    expected_backend = "cmake" if "cmake" in recipe.get("requires_tools", []) else None
    if expected_backend and backend.split(":", 1)[0] != expected_backend:
        raise ForgeError(f"Source build system changed from {expected_backend} to {backend}.")
    recipe.update(name=profile["name"], version=_upstream_version(row["Version"]), source=source)
    if components:
        recipe["schema_version"] = 2
    recipe["description"] = template.get("description", row["Package"])
    if source.get("patch_archive"):
        recipe["requires_tools"] = list(dict.fromkeys([*recipe.get("requires_tools", []), "patch"]))
    recipe["provenance"] = {"kind": "official-apt-source", "status": "generated",
                            "origin": profile["origin"], "suite": profile["suite"], "component": profile["component"],
                            "source_package": row["Package"], "debian_version": row["Version"],
                            "source_components": [name for name, _ in components], "release_suite": profile.get("release_suite", profile["suite"]),
                            "requested_package": profile["package"], "source_variant": "upstream-with-quilt-patches" if ".orig." in original else "native-source",
                            "dependency_policy": "Forge template; Debian Build-Depends retained in import report"}
    if template.get("provenance", {}).get("build_adapter"):
        recipe["provenance"]["build_adapter"] = template["provenance"]["build_adapter"]
    elif row["Package"] == "eigen3" and {"-DBUILD_TESTING=OFF", "-DEIGEN_BUILD_DOC=OFF"} <= {arg for command in recipe.get("build", []) for arg in command}:
        recipe["provenance"]["build_adapter"] = "eigen-headers"
    if profile.get("automatic"):
        recipe["provenance"]["automatic_adapter"] = True
    validate_recipe(recipe, "official source import")
    return recipe, {"detected_backend": backend, "patches": patch_names, "components": source.get("components", []),
                    "debian_build_dependencies": {k: v for k, v in row.items() if k.startswith("Build-Depends")},
                    "scope": "Builds the upstream project with quilt patches; does not execute debian/rules or produce .deb packages."}


def _binary_rows(home, snapshot, session):
    key = ("binary-rows", tuple(r.get("binary_sha256") for r in _indexes(snapshot)))
    if key not in session:
        wanted = {"Package", "Version", "Source", "Depends", "Pre-Depends", "Provides", "Architecture"}
        rows = []
        for index_record in _indexes(snapshot):
            if not index_record.get("binary_index"):
                raise ForgeError("The official cache has no native binary dependency metadata.", hint="Run jforge apt update on the target Linux machine.")
            path = _inside(home, Path(index_record["binary_index"]))
            if not path.is_file() or digest(path) != index_record["binary_sha256"]:
                raise ForgeError("Binary dependency index is missing or modified.", hint="jforge apt update")
            rows.extend({**{k: v for k, v in row.items() if k in wanted}, "Forge-Component": index_record.get("component")}
                        for row in _index_rows(path))
        session[key] = rows
    return session[key]


def _derive(home, profile, directory, session):
    profile = _selector(profile)
    snapshot = _snapshot(home, profile, directory / "repository", session)
    binary_rows = _binary_rows(home, snapshot, session) if snapshot.get("binary_index") else []
    from .official_dependencies import source_identity, parse, attach
    if profile.get("automatic"):
        from .official_dependencies import architecture
        architecture()  # Only real automatic builds require a native Linux target.
    requested = profile["package"]
    from debian.debian_support import Version as DebianVersion
    binaries = [r for r in binary_rows if r["Package"] == requested]
    if profile.get("source_version"):
        binaries = [r for r in binaries if source_identity(r)[1] == profile["source_version"]]
    binary = max(binaries, key=lambda r: DebianVersion(r["Version"])) if binaries else None
    if binary is None and profile.get("automatic"):
        providers = [r for r in binary_rows if any(a["name"] == requested for group in parse(r.get("Provides", ""), snapshot["architecture"]) for a in group)]
        unique = {}
        for provider in providers:
            previous = unique.get(provider["Package"])
            if previous is None or DebianVersion(provider["Version"]) > DebianVersion(previous["Version"]):
                unique[provider["Package"]] = provider
        providers = list(unique.values())
        if len(providers) == 1:
            binary = providers[0]
        elif len(providers) > 1:
            raise ForgeError(f"Virtual dependency {requested} needs an explicit provider: " + ", ".join(r["Package"] for r in providers[:12]))
    source_name, source_version = source_identity(binary) if binary else (requested, profile.get("source_version"))
    source_key = ("source-rows", tuple(r["index_sha256"] for r in _indexes(snapshot)))
    if source_key not in session:
        table = {}
        for index_record in _indexes(snapshot):
            for source_record in _index_rows(Path(index_record["index"])):
                record = dict(source_record, **{"Forge-Component": index_record.get("component", profile["component"]), "Forge-Suite": index_record.get("suite", profile["suite"]), "Forge-Repo": index_record.get("repo", snapshot["repo"])})
                table.setdefault(record["Package"], []).append(record)
                for name in record.get("Binary", "").replace("\n", " ").split(","):
                    if name.strip() and name.strip() != record["Package"]:
                        table.setdefault(name.strip(), []).append(record)
        session[source_key] = table
    records = session[source_key].get(source_name, [])
    selected_version = profile.get("source_version") or source_version
    records = [r for r in records if not selected_version or r["Version"] == selected_version]
    if not records:
        raise ForgeError(f"No source record for {requested} at the selected version.", hint="jforge apt update")
    records.sort(key=lambda r: DebianVersion(r["Version"]))
    row = records[-1]
    identical_version = [r for r in records if r["Version"] == row["Version"]]
    if len({r.get("Checksums-Sha256") for r in identical_version}) > 1:
        raise ForgeError(f"Inconsistent source checksums for {requested}={row['Version']}")
    write_json(directory / "source-record.json", row)
    try:
        recipe, details = _candidate(home, dict(profile, component=row["Forge-Component"], suite=row["Forge-Suite"], release_suite=profile["suite"]), row, dict(snapshot, repo=row["Forge-Repo"]), directory)
        if recipe and profile.get("automatic"):
            recipe = attach(recipe, row, binary["Package"] if binary else requested, binary_rows, snapshot["architecture"])
            validate_recipe(recipe, "official dependency adapter")
    except ForgeError as exc:
        if exc.code != 2:
            raise
        recipe, details = None, {"reason": str(exc), "hint": exc.hint}
    report = {"schema_version": 1, "profile": {k: v for k, v in profile.items() if k != "template"},
              "snapshot": snapshot, "source_record": row, "recipe": recipe,
              "import_status": "generated" if recipe else "needs_adapter", **details}
    write_json(directory / "import-report.json", report)
    if recipe:
        (directory / "recipe.yaml").write_text(yaml.safe_dump(recipe, sort_keys=False), encoding="utf-8")
    elif report.get("draft_recipe"):
        (directory / "recipe.draft.yaml").write_text(yaml.safe_dump(report["draft_recipe"], sort_keys=False), encoding="utf-8")
    return report


def _load_profiles(home):
    path = home.root / "apt/official.json"
    value = read_json(path) if path.exists() else {"schema_version": 1, "imports": []}
    if value.get("schema_version") != 1 or not isinstance(value.get("imports"), list):
        raise ForgeError(f"Invalid official import configuration: {path}")
    return value


def _cached(home):
    path = home.root / "cache/apt/official.json"
    return read_json(path) if path.exists() else {"schema_version": 1, "packages": [], "pending": []}


def refresh_official(home, operation_dir, *, refresh_repository=True):
    profiles = _load_profiles(home)["imports"]
    current = _cached(home)
    by_name = {entry["recipe"]["name"]: entry for entry in current["packages"]}
    pending, summaries, session = [], [], {}
    source = default_source(home)
    snapshot = _snapshot(home, source, operation_dir / "official-repository", session) if refresh_repository and source else None
    for index, profile in enumerate(profiles):
        if source and source_family(profile) == source_family(source):
            # A tightened source keyring also applies to previously tracked recipes.
            profile = {**profile, **source_selection(source)}
        directory = _inside(home, operation_dir / f"official-{index}")
        directory.mkdir(mode=0o700)
        report = _derive(home, profile, directory, session)
        if report["recipe"]:
            by_name[profile["name"]] = {"recipe": report["recipe"], "origin": f"official:{profile['origin']}:{profile['suite']}"}
        else:
            pending.append({"name": profile["name"], "reason": report["reason"],
                            "report": str(directory / "import-report.json")})
        summaries.append({"name": profile["name"], "debian_version": report["source_record"]["Version"],
                          "import_status": report["import_status"], "report": str(directory / "import-report.json"),
                          "apt_signature_verified": report["snapshot"]["apt_signature_verified"]})
    write_json(_inside(home, home.root / "cache/apt/official.json"), {
        "schema_version": 1, "packages": list(by_name.values()), "pending": pending, "updated_at": now()})
    result = {"official_imports": summaries, "pending_imports": pending}
    if snapshot:
        repository = _save_repository(home, source, snapshot)
        result.update(official_source=source, source_packages=repository["source_packages"], index=snapshot["index"])
    return result


def _profile(home, package, *, origin, suite, component="main", name=None,
                   template_name=None, source_version=None, keyring=None, automatic=False, components=None, pockets=None):
    if origin not in REPOSITORIES:
        raise ForgeError("--origin must be debian or ubuntu.")
    selection = _selector({"origin": origin, "suite": suite, "component": component, **({"components": components} if components else {}), **({"pockets": pockets} if pockets is not None else {})})
    component = selection["component"]
    for value in (package, suite, component):
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,127}", value):
            raise ForgeError(f"Invalid official source selector: {value}")
    template_name = template_name or ADAPTERS.get(package)
    # Binary name aliases map to the existing source-specific adapter.
    if template_name is None and package == "libeigen3-dev":
        template_name = "eigen"
    template = None
    if template_name:
        candidates = [r for r in Catalog(home).list() if r["name"] == template_name]
        if not candidates:
            raise ForgeError(f"Forge template not found: {template_name}")
        chosen = max(candidates, key=lambda r: Version(r["version"]))
        template = {k: v for k, v in chosen.items() if k != "origin"}
    target = name or template_name or package
    package_name(target)
    return {"package": package, "origin": origin, "suite": suite, "component": component,
               "name": target, "template": template, "source_version": source_version,
               "keyring": str(Path(keyring).expanduser().resolve()) if keyring else None, "automatic": automatic,
               **({"components": selection["components"]} if selection.get("components") else {}),
               **({"pockets": selection["pockets"]} if "pockets" in selection else {})}


def _store_import(home, profile, report, directory):
    target = profile["name"]
    profiles = _load_profiles(home)
    profiles["imports"] = [p for p in profiles["imports"] if p["name"] != target] + [profile]
    if "source" not in profiles:
        profiles["source"] = source_selection(profile)
    current = _cached(home)
    current["pending"] = [p for p in current["pending"] if p["name"] != target]
    current["packages"] = [p for p in current["packages"] if p["recipe"]["name"] != target]
    if report["recipe"]:
        current["packages"].append({"recipe": report["recipe"], "origin": f"official:{profile['origin']}:{profile['suite']}"})
    else:
        current["pending"].append({"name": target, "reason": report["reason"],
                                   "report": str(directory / "import-report.json")})
    write_json(_inside(home, home.root / "apt/official.json"), profiles)
    write_json(_inside(home, home.root / "cache/apt/official.json"), current)
    source = source_selection(profile)
    if source == default_source(home):
        _save_repository(home, source, report["snapshot"])


def import_package(home, package, *, origin, suite, component="main", name=None,
                   template_name=None, source_version=None, keyring=None):
    profile = _profile(home, package, origin=origin, suite=suite, component=component,
                       name=name, template_name=template_name, source_version=source_version, keyring=keyring)
    target = profile["name"]
    with locked(home), operation(home, "import", {k: v for k, v in profile.items() if k != "template"}) as (directory, result):
        report = _derive(home, profile, directory, {})
        _store_import(home, profile, report, directory)
        result.update(name=target, source_package=report["source_record"]["Package"],
                      debian_version=report["source_record"]["Version"], import_status=report["import_status"],
                      recipe_path=str(directory / "recipe.yaml") if report["recipe"] else
                                  str(directory / "recipe.draft.yaml") if report.get("draft_recipe") else None,
                      report=str(directory / "import-report.json"),
                      apt_signature_verified=report["snapshot"]["apt_signature_verified"],
                      trust=report["snapshot"]["trust"])
        return result


def missing_packages(home, specs):
    from .dependencies import native, matches
    recipes = Catalog(home).list()
    return sorted({native(spec, root=True)["name"] for spec in specs if not any(matches(p, native(spec, root=True)) for p in recipes)})


def ensure_recipes(home, specs, directory, progress=None):
    """Discover the complete candidate closure before starting any compilation."""
    from .dependencies import native, matches, groups
    pending = [(native(spec, root=True), [native(spec, root=True)["name"]]) for spec in specs]
    visited, reports, session, errors = set(), [], {}, []
    source = default_source(home)
    cached = _cached_repository(home) if source else None
    if cached and cached["snapshot"].get("binary_index"):
        expiry = cached["snapshot"].get("valid_until")
        if not expiry or parsedate_to_datetime(expiry) > datetime.now(timezone.utc):
            session[_source_key(source)] = cached["snapshot"]
    while pending:
        req, chain = pending.pop(0)
        marker = tuple(sorted(req.items()))
        if marker in visited:
            continue
        visited.add(marker)
        if len(visited) > 200:
            raise ForgeError("Official dependency discovery limit exceeded; split the request or supply dedicated recipes.")
        choices = [p for p in Catalog(home).list() if matches(p, req)]
        if not choices:
            from .packages import installed_recipe
            for record in home.packages():
                if record.get("managed_by") == "jforge-apt":
                    snapshot_recipe = installed_recipe(home, record)
                    if snapshot_recipe and matches(snapshot_recipe, req):
                        choices.append(snapshot_recipe)
        legacy = any(p.get("provenance", {}).get("automatic_adapter") and p["schema_version"] == 1 for p in choices)
        if not choices or legacy:
            previous = next((p for p in _load_profiles(home)["imports"] if p["name"] == req["name"]), None)
            if not source and not previous:
                errors.append({"chain": chain, "reason": "No compatible recipe or imported official source"})
                continue
            profile = dict(previous, automatic=True) if previous else _profile(home, req["name"], **source, name=req["name"], automatic=True)
            if progress:
                progress("Resolving " + " -> ".join(chain) + f" in {profile['origin']} {profile['suite']}")
            destination = _inside(home, directory / f"official-lookup-{len(reports)}")
            destination.mkdir(mode=0o700)
            try:
                report = _derive(home, profile, destination, session)
                _store_import(home, profile, report, destination)
                reports.append({"name": req["name"], "import_status": report["import_status"], "report": str(destination / "import-report.json")})
                if not report["recipe"]:
                    errors.append({"chain": chain, "reason": report["reason"]})
                    if progress:
                        progress("Unavailable dependency candidate " + " -> ".join(chain) + ": " + report["reason"])
                    continue
                choices = [report["recipe"]] if matches(report["recipe"], req) else []
            except ForgeError as exc:
                if exc.code != 2:
                    raise
                reports.append({"name": req["name"], "import_status": "unresolved", "reason": str(exc)})
                errors.append({"chain": chain, "reason": str(exc)})
                if progress:
                    progress("Unavailable dependency candidate " + " -> ".join(chain) + ": " + str(exc))
                continue
        # Import all viable version/alternative branches; the solver selects a
        # globally consistent graph. Failed unused alternatives do not block it.
        for choice in choices:
            for group in groups(choice):
                pending.extend((dep, chain + [dep["name"]]) for dep in group)
    write_json(directory / "dependency-discovery.json", {"schema_version": 1, "imports": reports, "unresolved_candidates": errors})
    return reports
