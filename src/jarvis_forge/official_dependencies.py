"""Translate repository relationships into explicit source-build requirements.

Unsupported Debian semantics fail closed instead of disappearing from a recipe.
"""
from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from debian.deb822 import PkgRelation

from .dependencies import version_matches
from .storage import ForgeError, now, write_json


TOOLS = {"cmake": "cmake", "make": "make", "gcc": "cc", "g++": "c++", "gfortran": "gfortran",
         "pkg-config": "pkg-config", "pkgconf": "pkgconf", "bash": "bash", "python3": "python3",
         "autoconf": "autoconf", "automake": "automake", "m4": "m4", "texinfo": "makeinfo",
         "gettext": "gettext", "patch": "patch", "bison": "bison", "flex": "flex",
         "gawk": "gawk", "awk": "awk", "binutils": "ld", "perl": "perl", "libtool": "libtoolize", "meson": "meson", "ninja-build": "ninja", "bc": "bc"}
# These drive debian/rules, which the upstream CMake/configure adapters do not execute.
PACKAGING = {"debhelper", "debhelper-compat", "dh-autoreconf", "dh-strip-nondeterminism", "dpkg-dev", "dh-exec"}


def architecture():
    if platform.system() != "Linux":
        raise ForgeError("Automatic Debian dependency resolution currently requires a native Linux target.",
                         hint="Use an explicit Forge recipe for this platform.")
    arch = {"x86_64": "amd64", "aarch64": "arm64", "ppc64le": "ppc64el", "s390x": "s390x", "riscv64": "riscv64"}.get(platform.machine())
    if not arch:
        raise ForgeError(f"Unsupported native dependency architecture: {platform.machine()}")
    return arch


def _arch_match(value, arch):
    if value in {arch, "linux-" + arch}:
        return True
    parts = value.split("-")
    if "any" not in parts:
        return False
    if len(parts) > 4:
        raise ForgeError(f"Unsupported architecture wildcard: {value}")
    # Debian native GNU/Linux architecture tuples are ABI-libc-OS-CPU.
    # Right-aligned wildcards such as kfreebsd-any are valid and simply
    # do not match this Linux host; negative restrictions must not fail.
    pattern = ["any"] * (4 - len(parts)) + parts
    target = ["base", "gnu", "linux", arch]
    return all(want == "any" or want == actual for want, actual in zip(pattern, target))


def parse(text, arch, *, active_profiles=()):
    if not text.strip():
        return []
    result = []
    for group in PkgRelation.parse_relations(text):
        alternatives = []
        for item in group:
            name = item["name"]
            if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name):
                raise ForgeError(f"Unresolved dependency expression: {text}")
            qualifiers = item.get("archqual")
            if qualifiers not in {None, "any", "native", arch}:
                raise ForgeError(f"Cross-architecture dependency is unsupported: {name}:{qualifiers}")
            restrictions = item.get("arch") or []
            positive = [r.arch for r in restrictions if r.enabled]
            negative = [r.arch for r in restrictions if not r.enabled]
            if positive and negative:
                raise ForgeError(f"Mixed architecture restrictions in dependency: {name}")
            if positive and not any(_arch_match(a, arch) for a in positive):
                continue
            if any(_arch_match(a, arch) for a in negative):
                continue
            profiles = item.get("restrictions") or []
            # Only an explicit native adapter may enable nobiarch. Other Debian
            # test/documentation profiles remain inactive.
            if profiles and not any(all(term.enabled == (term.profile in active_profiles) for term in formula) for formula in profiles):
                continue
            atom = {"name": name}
            if item.get("version"):
                relation, version = item["version"]
                if relation not in {"<<", "<=", "=", ">=", ">>"}:
                    raise ForgeError(f"Unsupported dependency comparison: {relation}")
                atom.update(relation=relation, version=version)
            alternatives.append(atom)
        if alternatives:
            result.append(alternatives)
    return result


def source_identity(row):
    source = row.get("Source", row["Package"])
    match = re.fullmatch(r"([^\s]+)(?:\s+\(([^)]+)\))?", source)
    if not match:
        raise ForgeError(f"Invalid binary Source field: {source}")
    return match[1], match[2] or row["Version"]


def attach(recipe, row, requested, binary_rows, arch):
    recipe.setdefault("provenance", {})
    if requested.endswith(("-dbg", "-dbgsym", "-doc", "-data", "-common")):
        raise ForgeError(f"The binary output {requested} needs a dedicated artifact adapter.")
    from debian.debian_support import Version as DebianVersion
    own = {}
    for provider in binary_rows:
        if source_identity(provider) != (row["Package"], row["Version"]):
            continue
        previous = own.get(provider["Package"])
        if previous is None or DebianVersion(provider["Version"]) > DebianVersion(previous["Version"]):
            own[provider["Package"]] = provider
    versions = {name: p["Version"] for name, p in own.items()}
    for p in own.values():
        for group in parse(p.get("Provides", ""), arch):
            for atom in group:
                if atom.get("relation") not in {None, "="}:
                    raise ForgeError("Invalid versioned Provides in repository metadata.")
                versions.setdefault(atom["name"], atom.get("version"))
    if requested not in own:
        # A source name can describe several incompatible output configurations.
        # Ask for a concrete output rather than guessing its runtime closure.
        raise ForgeError(f"No binary output {requested!r} matches source {row['Package']}={row['Version']}.",
                         hint="Install one of these binary package names: " + ", ".join(sorted(own)[:12]))
    from .upstream_adapters import ADAPTERS, dependency as adapt_dependency
    adapter = recipe["provenance"].get("build_adapter")
    active_profiles = ("nobiarch",) if adapter in ADAPTERS.values() else ()
    relationships, decisions = [], []
    def relationship(kind, group):
        changed, reason = adapt_dependency(adapter, kind, group)
        if reason:
            decisions.append({"kind": kind, "alternatives": group,
                              "disposition": "native-build-contract", "replacement": changed, "reason": reason})
        return changed
    for field in ("Build-Depends", "Build-Depends-Arch", "Build-Depends-Indep"):
        for group in parse(row.get(field, ""), arch, active_profiles=active_profiles):
            changed = relationship("build", group)
            if changed:
                relationships.append(("build", changed))
    # Follow all internal output edges before discarding self edges at source level.
    seen, pending = set(), [requested]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for field in ("Pre-Depends", "Depends"):
            for group in parse(own[name].get(field, ""), arch):
                group = relationship("run", group)
                if not group:
                    continue
                internal = [a for a in group if a["name"] in versions and
                            (not a.get("relation") or (versions[a["name"]] is not None and version_matches(versions[a["name"]], a["relation"], a["version"])))]
                if internal:
                    pending.extend(a["name"] for a in internal if a["name"] in own)
                else:
                    relationships.append(("run", group))
    provided = {name: own[name]["Version"] for name in seen}
    for name in seen:
        for group in parse(own[name].get("Provides", ""), arch):
            for atom in group:
                provided.setdefault(atom["name"], atom.get("version"))
    mapped, external = [], []
    for kind, group in relationships:
        adapter = recipe.get("provenance", {}).get("build_adapter")
        if kind == "build" and (adapter in {"metapackage", "boost-headers"} or (adapter == "eigen-headers" and not any(a["name"] == "cmake" for a in group))):
            decisions.append({"alternatives": group, "disposition": "not-required-by-adapter", "reason": adapter + " copies no compiled Debian payload and does not run Debian packaging/tests"})
            continue
        if adapter == "boost-headers" and any(re.fullmatch(r"libstdc\+\+-[0-9]+-dev", a["name"]) for a in group):
            external.append({"name": "build-essential", "kind": kind})
            decisions.append({"alternatives": group, "disposition": "native-compiler-headers", "reason": "Rebuilt headers use the user-selected C++ standard library rather than Debian's prebuilt compiler ABI"})
            continue
        if kind == "build" and any(a["name"] in PACKAGING for a in group):
            decisions.append({"alternatives": group, "disposition": "not-required-by-upstream-adapter",
                              "reason": "The selected adapter does not execute debian/rules or produce Debian packages."})
            continue
        if kind == "build" and any(a["name"] == "build-essential" for a in group):
            external.append({**next(a for a in group if a["name"] == "build-essential"), "kind": "build"})
            continue
        tool = next((a for a in group if a["name"] in TOOLS and shutil.which(TOOLS[a["name"]])), None)
        system = next((a for a in group if a["name"] in {"libc6", "libc6-dev", "libc-dev", "libgcc-s1", "libstdc++6"}), None)
        chosen = tool or system
        if chosen:
            if kind == "run" and chosen["name"] in {"libc6", "libgcc-s1", "libstdc++6"}:
                # These minima describe Ubuntu/Debian's prebuilt ELF artifacts.
                # Source builds link to the user's selected native toolchain.
                external.append({"name": chosen["name"], "kind": kind, "binary_requirement": chosen,
                                 "version_policy": "rebuilt-native-abi"})
                decisions.append({"alternatives": group, "disposition": "rebuilt-native-abi",
                                  "reason": "Probe the native runtime; prebuilt distribution ABI minima do not constrain a source rebuild"})
            else:
                external.append({**chosen, "kind": kind})
            continue
        if any(a["name"] in versions for a in group):
            # A genuine build dependency on its own source requires bootstrap support.
            raise ForgeError(f"Source bootstrap dependency requires an adapter: {row['Package']} -> {group}")
        mapped.append({"kind": kind, "alternatives": group})
    recipe["schema_version"] = 2
    recipe["debian_dependencies"] = mapped
    recipe.setdefault("probe", list(recipe["check"]))
    for atom in external:
        if atom["name"] in TOOLS:
            recipe["requires_tools"] = list(dict.fromkeys(recipe.get("requires_tools", []) + [TOOLS[atom["name"]]]))
    if requested.startswith("lib") and recipe.get("provenance", {}).get("build_adapter") != "metapackage":
        if requested.endswith("-dev"):
            command = "from pathlib import Path; import sys; p=Path(sys.argv[1]); assert any(x.is_file() for x in (p/'include').rglob('*')), 'No development headers installed'"
        else:
            command = "from pathlib import Path; import sys; p=Path(sys.argv[1]); assert any(x.is_file() and ('.so' in x.name or x.suffix in ('.a','.dylib')) for d in ('lib','lib64') for x in (p/d).rglob('*')), 'No library installed'"
        recipe["probe"] = list(recipe["probe"]) + [["${python}", "-c", command, "${prefix}"]]
    recipe["provenance"].update(binary_versions=provided, binary_package=requested,
                                 external_dependencies=external, dependency_decisions=decisions,
                                 dependency_policy="repository relationships with explicit upstream adapter decisions",
                                 dependency_architecture=arch, dependency_build_profiles=list(active_profiles))
    return recipe


def check_external(recipes, evidence_path=None):
    """Probe selected external providers before any source compilation begins."""
    evidence = []
    for recipe in recipes:
        for atom in recipe.get("provenance", {}).get("external_dependencies", []):
            entry = {"consumer": recipe["name"], "dependency": atom, "status": "not-ready", "commands": []}
            evidence.append(entry)
            def save():
                if evidence_path is not None:
                    write_json(evidence_path, {"providers": evidence})
            def run(argv, **kwargs):
                command_record = {"argv": argv, "cwd": os.getcwd(), "started_at": now()}
                entry["commands"].append(command_record)
                save()
                try:
                    if kwargs.get("text"):
                        kwargs.setdefault("encoding", "utf-8")
                        kwargs.setdefault("errors", "replace")
                    result = subprocess.run(argv, **kwargs)
                    command_record.update(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode, finished_at=now())
                    save()
                    return result
                except BaseException as exc:
                    command_record.update(error=str(exc), finished_at=now())
                    save()
                    raise
            name = atom["name"]
            tool = TOOLS.get(name)
            variable = {"cc": "CC", "c++": "CXX", "gfortran": "FC"}.get(tool)
            if tool:
                argv = shlex.split(os.environ.get(variable, tool) if variable else tool) + (["-W", "version"] if name == "awk" else ["--version"])
                result = run(argv, capture_output=True, text=True, timeout=15)
                output = result.stdout + result.stderr
                found = re.search(r"\d+(?:\.\d+)+", output)
                version = found[0] if found else None
                if result.returncode:
                    raise ForgeError(f"External tool probe failed for {recipe['name']} -> {name}: {output[-1000:]}")
            elif name in {"build-essential", "libc6-dev", "libc-dev"}:
                # Check real compiler/header availability rather than pretending
                # the Debian build-essential metapackage is installed on an HPC.
                if atom.get("relation"):
                    raise ForgeError(f"A versioned external development metapackage needs an adapter: {name}")
                argv = shlex.split(os.environ.get("CC", "cc")) + ["-x", "c", "-fsyntax-only", "-"]
                result = run(argv, input="#include <stdlib.h>\nint main(void){return 0;}\n", capture_output=True, text=True, timeout=15)
                version = None
                if result.returncode:
                    raise ForgeError(f"Native development environment is unavailable for {recipe['name']}: {result.stderr[-1000:]}")
                if name == "build-essential":
                    cxx = shlex.split(os.environ.get("CXX", "c++")) + ["-x", "c++", "-fsyntax-only", "-"]
                    cxx_probe = run(cxx, input="#include <vector>\nint main(){std::vector<int> v;}\n", capture_output=True, text=True, timeout=15)
                    if cxx_probe.returncode:
                        raise ForgeError(f"Native C++ headers are unavailable for {recipe['name']}: {cxx_probe.stderr[-1000:]}")
            elif name == "libc6":
                value = os.confstr("CS_GNU_LIBC_VERSION")
                version = value.split()[-1] if value else None
                argv = ["platform", "glibc"]
            else:
                compiler = "CXX" if name == "libstdc++6" else "CC"
                default = "c++" if compiler == "CXX" else "cc"
                command = shlex.split(os.environ.get(compiler, default))
                argv = command + ["-dumpfullversion", "-dumpversion"]
                result = run(argv, capture_output=True, text=True, timeout=15)
                version = result.stdout.strip() if result.returncode == 0 else None
                library = "libstdc++.so.6" if compiler == "CXX" else "libgcc_s.so.1"
                path = run(command + ["-print-file-name=" + library], capture_output=True, text=True, timeout=15)
                if path.returncode or not Path(path.stdout.strip()).is_file():
                    raise ForgeError(f"Compiler runtime cannot be located: {recipe['name']} -> {name}")
            compared = version
            # Explicit native-tool adapter: compare upstream version semantics,
            # retaining an epoch where Debian uses one for this tool family.
            # Exact binary package equality cannot be proven by a native tool.
            if version and atom.get("relation"):
                if atom["relation"] == "=":
                    raise ForgeError(f"Exact Debian package equality needs an explicit provider: {recipe['name']} -> {atom}")
                if ":" in atom["version"]:
                    compared = atom["version"].split(":", 1)[0] + ":" + version
            if atom.get("relation") and (not compared or not version_matches(compared, atom["relation"], atom["version"])):
                raise ForgeError(f"External dependency version is not satisfied: {recipe['name']} -> {atom}; found {version}",
                                 hint="Load a compatible user-selected environment or supply a dedicated build adapter.")
            entry.update(argv=argv, version=version, compared_version=compared, version_policy=atom.get("version_policy", "native-provider-upstream-version"), status="ready")
            save()
    return evidence
