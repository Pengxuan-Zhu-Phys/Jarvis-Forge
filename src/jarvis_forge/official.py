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


def _selector(source):
    if source.get("origin") not in REPOSITORIES:
        raise ForgeError("--origin must be debian or ubuntu.")
    for field in ("suite", "component"):
        if not isinstance(source.get(field), str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,127}", source[field]):
            raise ForgeError(f"Invalid official source {field}: {source.get(field)!r}")
    return source


def default_source(home):
    config = _load_profiles(home)
    source = config.get("source")
    if source is None and config["imports"]:
        source = {key: config["imports"][0].get(key) for key in DEFAULT_SOURCE}
    if source is not None and not isinstance(source, dict):
        raise ForgeError("Invalid official source configuration.")
    return _selector({key: (source or {}).get(key, value) for key, value in DEFAULT_SOURCE.items()})


def _index_rows(path):
    opener = lzma.open if path.name.endswith(".xz") else gzip.open
    try:
        with opener(path, "rt", encoding="utf-8") as stream:
            yield from paragraphs(stream)
    except (lzma.LZMAError, gzip.BadGzipFile, UnicodeError, EOFError) as exc:
        raise ForgeError(f"Cannot read official source index: {exc}", code=1) from exc


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
    index = _inside(home, Path(saved["snapshot"]["index"]))
    if not index.is_file() or digest(index) != saved["snapshot"]["index_sha256"]:
        raise ForgeError("Official source index is missing or modified.", hint="jforge apt update")
    return saved


def cached_packages(home, query="", *, exact=False, details=False):
    saved = _cached_repository(home)
    if saved is None:
        return []
    result = []
    for row in _index_rows(Path(saved["snapshot"]["index"])):
        names = {row["Package"], *(n.strip() for n in row.get("Binary", "").replace("\n", " ").split(",") if n.strip())}
        for name in sorted(names):
            if (name == query if exact else query.lower() in (name + " " + row.get("Description", "")).lower()):
                entry = {"name": name, "version": row["Version"], "source_package": row["Package"],
                               "description": row.get("Description", f"Official source package {row['Package']}"),
                               "origin": f"official:{saved['source']['origin']}:{saved['source']['suite']}",
                               "status": "official-index"}
                if details:
                    entry["source_record"] = row
                result.append(entry)
    return result


def _save_repository(home, source, snapshot):
    previous = home.root / "cache/apt/official-repository.json"
    old = read_json(previous) if previous.exists() else {}
    if (old.get("snapshot", {}).get("index_sha256") == snapshot["index_sha256"] and old.get("source") == source
            and isinstance(old.get("source_packages"), int)):
        count = old["source_packages"]
    else:
        count = sum(1 for _ in _index_rows(Path(snapshot["index"])))
    record = {"schema_version": 1, "source": source, "snapshot": snapshot,
              "source_packages": count, "updated_at": now()}
    write_json(_inside(home, home.root / "cache/apt/official-repository.json"), record)
    return record


def import_repository(home, *, origin, suite, component="main", keyring=None):
    source = _selector({"origin": origin, "suite": suite, "component": component,
                        "keyring": str(Path(keyring).expanduser().resolve()) if keyring else None})
    with locked(home), operation(home, "import-repository", source) as (directory, result):
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
    key = (origin, suite, component, profile.get("keyring"))
    if key in session:
        return session[key]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    repo = REPOSITORIES[origin]
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
    if metadata.get("Origin", "").lower() != origin:
        raise ForgeError("Repository Origin does not match the selected distribution.")
    if suite not in {metadata.get("Codename"), metadata.get("Suite")}:
        raise ForgeError("Repository suite/codename does not match the requested distribution.")
    if metadata.get("Valid-Until"):
        expires = parsedate_to_datetime(metadata["Valid-Until"])
        if expires <= datetime.now(timezone.utc):
            raise ForgeError("The repository's Release metadata has expired.", code=1)
    hashes = checksums(metadata.get("SHA256", ""))
    candidates = [f"{component}/source/Sources.xz", f"{component}/source/Sources.gz"]
    selected = next((name for name in candidates if name in hashes), None)
    if not selected:
        raise ForgeError(f"No supported source index for component {component}.")
    expected = hashes[selected]
    target = _inside(home, home.root / "cache/apt/official-indexes" / (expected["sha256"] + Path(selected).suffix))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not target.is_file() or digest(target) != expected["sha256"] or target.stat().st_size != expected["size"]:
        downloaded = directory / Path(selected).name
        fetch(f"{repo}/dists/{suite}/{selected}", downloaded, directory / "index-download")
        if digest(downloaded) != expected["sha256"] or downloaded.stat().st_size != expected["size"]:
            raise ForgeError("Official source index does not match Release SHA-256/size.", code=1)
        shutil.copyfile(downloaded, target)
    snapshot = {"repo": repo, "index": str(target), "index_sha256": expected["sha256"],
                "release_sha256": digest(release), "release_date": metadata.get("Date"),
                "valid_until": metadata.get("Valid-Until"),
                "codename": metadata.get("Codename"), "apt_signature_verified": signature_verified,
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
    roots = {PurePosixPath(safe_relative(name)).parts[0] for name in names}
    if len(roots) != 1:
        raise ForgeError("Source archive needs an adapter for its directory layout.")
    root = next(iter(roots))
    relative = {str(PurePosixPath(name).relative_to(root)) for name in names}
    backend = "cmake" if "CMakeLists.txt" in relative else "autotools" if "configure" in relative else "custom"
    return root, backend


def _upstream_version(version):
    upstream = re.sub(r"^\d+:", "", version).rsplit("-", 1)[0] if "-" in version else re.sub(r"^\d+:", "", version)
    try:
        normalized = str(Version(upstream))
    except InvalidVersion as exc:
        raise ForgeError(f"Upstream version {upstream!r} needs an explicit version adapter.") from exc
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", normalized):
        raise ForgeError("Normalized upstream version cannot be represented in a Forge recipe.")
    return normalized


def _generic_draft(profile, row, source, backend):
    if backend not in {"cmake", "autotools"}:
        return None
    if backend == "cmake":
        build = [["cmake", "-S", "${source}", "-B", "${build}", "-DCMAKE_INSTALL_PREFIX=${prefix}", "-DBUILD_TESTING=OFF"],
                 ["cmake", "--build", "${build}", "--parallel", "${jobs}"]]
        install = [["cmake", "--install", "${build}"]]
        tools = ["cmake", "cc", "c++"]
    else:
        build = [["${source}/configure", "--prefix=${prefix}"], ["make", "-j${jobs}"]]
        install, tools = [["make", "install"]], ["make", "cc", "c++"]
    return {"schema_version": 1, "name": profile["name"], "version": _upstream_version(row["Version"]),
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
    recipe = _generic_draft(profile, row, source, backend)
    if recipe:
        recipe["provenance"] = {}
        recipe["description"] = f"{row['Package']} from official {profile['origin']} sources ({backend})"
        recipe["environment"]["prepend"].update({"PKG_CONFIG_PATH": ["${prefix}/lib/pkgconfig", "${prefix}/lib64/pkgconfig", "${prefix}/share/pkgconfig"],
                                                 "LD_LIBRARY_PATH": ["${prefix}/lib", "${prefix}/lib64"]})
        recipe["check"] = [["${python}", "-c", "from pathlib import Path; import sys; assert any(p.is_file() for p in Path(sys.argv[1]).rglob('*'))", "${prefix}"]]
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
            return {"schema_version": 1, "name": profile["name"], "version": _upstream_version(row["Version"]),
                    "description": "Neofetch system information command from official sources", "source": source,
                    "requires_tools": ["bash"], "install": [["${python}", "-c", copy, "${source}", "${prefix}"]],
                    "check": [["bash", "-n", "${prefix}/bin/neofetch"], ["${prefix}/bin/neofetch", "--version"]],
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


def _candidate(home, profile, row, snapshot, directory):
    hashes = checksums(row.get("Checksums-Sha256", ""))
    original = [n for n in hashes if re.search(r"\.orig\.tar\.(gz|bz2|xz)$", n)]
    if not original:
        original = [n for n in hashes if re.search(r"\.tar\.(gz|bz2|xz)$", n) and ".debian." not in n]
    if len(original) != 1 or any(".orig-" in n for n in hashes):
        raise ForgeError("Native/multiple-original source packages require an adapter.")
    original = original[0]
    base = safe_relative(row.get("Directory", ""))
    url = lambda n: snapshot["repo"] + "/" + quote(base + "/" + n, safe="/+~._-")
    source = {"url": url(original), "sha256": hashes[original]["sha256"]}
    archive = verified_archive(home, source, directory / "source-download")
    source["subdir"], backend = _archive_layout(archive)
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
    if expected_backend and backend != expected_backend:
        raise ForgeError(f"Source build system changed from {expected_backend} to {backend}.")
    recipe.update(name=profile["name"], version=_upstream_version(row["Version"]), source=source)
    recipe["description"] = template.get("description", row["Package"])
    if source.get("patch_archive"):
        recipe["requires_tools"] = list(dict.fromkeys([*recipe.get("requires_tools", []), "patch"]))
    recipe["provenance"] = {"kind": "official-apt-source", "status": "generated",
                            "origin": profile["origin"], "suite": profile["suite"], "component": profile["component"],
                            "source_package": row["Package"], "debian_version": row["Version"],
                            "requested_package": profile["package"], "source_variant": "upstream-with-quilt-patches" if ".orig." in original else "native-source",
                            "dependency_policy": "Forge template; Debian Build-Depends retained in import report"}
    if profile.get("automatic"):
        recipe["provenance"]["automatic_adapter"] = True
    validate_recipe(recipe, "official source import")
    return recipe, {"detected_backend": backend, "patches": patch_names,
                    "debian_build_dependencies": {k: v for k, v in row.items() if k.startswith("Build-Depends")},
                    "scope": "Builds the upstream project with quilt patches; does not execute debian/rules or produce .deb packages."}


def _derive(home, profile, directory, session):
    snapshot = _snapshot(home, profile, directory / "repository", session)
    row = source_row(Path(snapshot["index"]), profile["package"], profile.get("source_version"))
    write_json(directory / "source-record.json", row)
    try:
        recipe, details = _candidate(home, profile, row, snapshot, directory)
    except ForgeError as exc:
        if exc.code != 2:
            raise
        recipe, details = None, {"reason": str(exc)}
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
    snapshot = _snapshot(home, source, operation_dir / "official-repository", session) if refresh_repository else None
    for index, profile in enumerate(profiles):
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
                   template_name=None, source_version=None, keyring=None, automatic=False):
    if origin not in REPOSITORIES:
        raise ForgeError("--origin must be debian or ubuntu.")
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
               "keyring": str(Path(keyring).expanduser().resolve()) if keyring else None, "automatic": automatic}


def _store_import(home, profile, report, directory):
    target = profile["name"]
    profiles = _load_profiles(home)
    profiles["imports"] = [p for p in profiles["imports"] if p["name"] != target] + [profile]
    if "source" not in profiles:
        profiles["source"] = {key: profile.get(key) for key in DEFAULT_SOURCE}
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
    source = {key: profile.get(key) for key in DEFAULT_SOURCE}
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
    names = {p["name"] for p in Catalog(home).list()}
    return sorted({requirement(spec).name for spec in specs} - names)


def ensure_recipes(home, specs, directory, progress=None):
    """Called inside install's existing operation lock; no nested package lock."""
    pending = [requirement(spec) for spec in specs]
    visited, reports, session = set(), [], {}
    source = default_source(home)
    if missing_packages(home, specs):
        cached = _cached_repository(home)
        if cached and cached.get("source") == source:
            snapshot = cached["snapshot"]
            expiry = snapshot.get("valid_until")
            if not expiry or parsedate_to_datetime(expiry) > datetime.now(timezone.utc):
                session[(source["origin"], source["suite"], source["component"], source.get("keyring"))] = snapshot
    while pending:
        req = pending.pop(0)
        if req.name in visited:
            continue
        visited.add(req.name)
        choices = [p for p in Catalog(home).list() if p["name"] == req.name]
        if not choices:
            previous = next((p for p in _load_profiles(home)["imports"] if p["name"] == req.name), None)
            profile = dict(previous, automatic=True) if previous else _profile(home, req.name, **source, name=req.name, automatic=True)
            if progress:
                progress(f"Looking up {req.name} in official {profile['origin']} {profile['suite']}/{profile['component']}")
            destination = _inside(home, directory / f"official-lookup-{len(reports)}")
            destination.mkdir(mode=0o700)
            report = _derive(home, profile, destination, session)
            _store_import(home, profile, report, destination)
            reports.append({"name": req.name, "source_package": report["source_record"]["Package"],
                            "version": report["source_record"]["Version"], "import_status": report["import_status"],
                            "report": str(destination / "import-report.json")})
            if not report["recipe"]:
                raise ForgeError(f"Official source found for {req.name}, but an installation adapter is required: {report['reason']}",
                                 hint=f"Source and dependency details: {destination / 'import-report.json'}")
            choices = [report["recipe"]]
        matching = [p for p in choices if Version(p["version"]) in req.specifier]
        if matching:
            chosen = max(matching, key=lambda p: Version(p["version"]))
            pending.extend(requirement(dep) for dep in chosen.get("depends", []))
    return reports
