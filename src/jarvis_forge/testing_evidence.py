"""Live evidence used by full test gates and by read-only status queries."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile
from urllib.parse import unquote, urlsplit

from . import builds, checker, testing
from .storage import ForgeError, digest


def _archive_identity(subject, project):
    archive = Path(subject["archive"])
    actual = digest(archive)
    if actual != subject["sha256"]:
        raise ForgeError(f"Archive SHA-256 differs from the pinned digest: {archive.name}", code=1)
    expected = {}
    prefix = None
    try:
        with tarfile.open(archive, "r|*") as stream:
            for member in stream:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise ForgeError(f"Unsafe archive member: {member.name}", code=1)
                prefix = prefix or path.parts[0]
                if path.parts[0] != prefix:
                    raise ForgeError("The acceptance archive must contain one project directory.", code=1)
                if member.isdir():
                    continue
                name = PurePosixPath(*path.parts[1:]).as_posix()
                if not member.isfile() or name == "." or name in expected:
                    raise ForgeError(f"Unsupported or duplicate archive member: {member.name}", code=1)
                with stream.extractfile(member) as payload:
                    sha = hashlib.sha256()
                    for chunk in iter(lambda: payload.read(1024 * 1024), b""):
                        sha.update(chunk)
                expected[name] = {"sha256": sha.hexdigest(), "executable": bool(member.mode & 0o111)}
    except tarfile.TarError as exc:
        raise ForgeError(f"Cannot read acceptance archive {archive.name}: {exc}", code=1) from exc
    if not expected:
        raise ForgeError("The acceptance archive has no project files.", code=1)
    for path in sorted(project.rglob("*")):
        name = path.relative_to(project).as_posix()
        if path.is_symlink() or (not path.is_dir() and name not in expected):
            raise ForgeError(f"Unexpected file in extracted project: {name}", code=1)
    for name, record in sorted(expected.items()):
        file = project / name
        if (not file.is_file() or digest(file) != record["sha256"]
                or bool(file.stat().st_mode & 0o111) != record["executable"]):
            raise ForgeError(f"Extracted project input differs from the archive: {name}", code=1)
    manifest = builds.fingerprint(expected)
    return {"kind": "archive", "sha256": actual, "manifest": manifest, "files": len(expected),
            "source_key": builds.fingerprint([actual, manifest]), "dirty": False}


def _editable_sources(rows, identity):
    result = {}
    for row in rows:
        try:
            direct = json.loads(row["direct_url"] or "{}")
        except ValueError:
            continue
        if not isinstance(direct, dict) or not isinstance(direct.get("url"), str):
            continue
        url = urlsplit(direct["url"])
        if url.scheme != "file" or url.netloc not in ("", "localhost"):
            continue
        info = direct.get("dir_info")
        if not isinstance(info, dict) or info.get("editable") is not True:
            continue
        path = Path(unquote(url.path))
        source = identity(path) if path.is_dir() else None
        result.setdefault(row["name"], []).append({"path": str(path), "source_key": (source or {}).get("source_key")})
    return result


def snapshot(home, card, selection, environment_name, *, prepared=None, source=None):
    from .testing_run import Source, probe_packages

    project = Path(card["project"])
    problems = []
    try:
        subject = _archive_identity(card["subject"], project) if card.get("subject") else (
            source if source is not None else Source(project).identity())
        if subject is None:
            problems.append("Unbound project. Use a tracked Git project or supply a pinned archive.")
        elif subject.get("dirty"):
            problems.append("The project has uncommitted changes.")
    except (ForgeError, OSError) as exc:
        subject = None
        problems.append(str(exc))

    evidence = {"version": 1, "subject": (subject or {}).get("source_key"),
                "card": hashlib.sha256(card["text"].encode()).hexdigest(),
                "profile": selection["profile"], "steps": selection["ids"]}
    card = {**card, "_files": {}}
    try:
        if Path(card["path"]).read_text(encoding="utf-8") != card["text"]:
            problems.append("The test card changed during the run.")
    except OSError as exc:
        problems.append(str(exc))
    inputs = {}
    for name in selection["ids"]:
        group = card["groups"].get(name)
        step = group or card["checks"][name]
        try:
            manifest = checker.input_manifest(project, step.get("inputs", []))
            if group is not None:
                manifest["pytest-files"] = [(file, digest(project / file))
                                            for file in testing.group_files(card, group)]
            inputs[name] = builds.fingerprint(manifest)
        except (ForgeError, OSError) as exc:
            problems.append(str(exc))
    evidence["inputs"] = inputs
    try:
        evidence["card_inputs"] = builds.fingerprint(checker.input_manifest(
            Path(card["path"]).parent, card.get("card_inputs", [])))
    except (ForgeError, OSError) as exc:
        problems.append(str(exc))
    try:
        environment, observed, origin = prepared or checker.prepare_environment(
            home, None, card["environment"], Path(card["path"]).parent, environment_name)[:3]
        python = observed.get("python", {})
        if not python.get("path"):
            raise ForgeError("The selected environment has no python3 on PATH.", code=1)

        def packages(env, cwd):
            key, rows = probe_packages(python["path"], env, cwd)
            return key, _editable_sources(rows, lambda path: Source(path).identity())

        installed, editable = packages(environment, project)
        groups, group_editables = {}, {}
        for item in selection["ids"]:
            group = card["groups"].get(item)
            if group is None or not (group["pythonpath"] or group["env"] or group["isolate"] == "installed"):
                continue
            env = testing.python_environment(environment, project, group)
            groups[item], group_editable = packages(env, home.root if group["isolate"] == "installed" else project)
            if group_editable != editable:
                group_editables[item] = group_editable
        evidence.update(environment={key: value for key, value in origin.items() if key != "files"},
                        variables=builds.fingerprint({key: value for key, value in environment.items()
                                                      if key in _RUNTIME_VARIABLES
                                                      or key in card["environment"].get("variables", {})}),
                        tools=observed, packages={"base": installed, "groups": groups},
                        editable={"base": editable, "groups": group_editables})
    except (ForgeError, OSError, ValueError) as exc:
        problems.append(str(exc))
    benchmarks = {}
    for item in selection["ids"]:
        uses = (card["checks"].get(item) or {}).get("uses")
        if uses is None:
            continue
        from . import bench

        path = Path(card["path"]).parent / uses["bench"]
        try:
            value = bench.card_status(home, path, profile=uses["profile"])
        except (ForgeError, OSError) as exc:
            problems.append(f"{item}: {exc}")
            continue
        benchmarks[item] = {key: value[key] for key in ("run_id", "evidence", "reason", "next_argv")}
        if not value["accepted"]:
            problems.append(f"{item}: {value['reason']}")
    if benchmarks:
        evidence["bench"] = benchmarks
    return subject, evidence, problems


_RUNTIME_VARIABLES = {
    "PATH", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    "CMAKE_PREFIX_PATH", "PKG_CONFIG_PATH", "SPACK_ENV", "JFORGE_APT_PROFILE",
}


def changes(recorded, current):
    if not recorded or recorded.get("version") != current["version"]:
        return ["The report predates the current evidence format."]
    labels = {"subject": "The project source changed after the recorded run.", "card": "The test card changed.",
              "card_inputs": "Files used by the test card changed.",
              "steps": "The selected checks changed.", "inputs": "Declared test inputs changed.",
              "environment": "The selected environment or its revision changed.",
              "variables": "Runtime paths or declared environment variables changed.",
              "tools": "The selected Python or native tools changed.",
              "packages": "Installed Python distributions changed.",
              "editable": "An editable Python dependency changed.",
              "bench": "Required benchmark evidence changed."}
    return [label for key, label in labels.items() if recorded.get(key) != current.get(key)]
