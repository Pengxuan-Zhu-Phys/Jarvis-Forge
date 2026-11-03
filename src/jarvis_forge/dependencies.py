"""Dependency groups and provider-aware, version-domain-specific matching."""
from __future__ import annotations

from packaging.version import Version
from functools import cmp_to_key
from debian.debian_support import Version as DebianVersion

from .storage import ForgeError


def native(text, *, root=False):
    from .catalog import requirement, DebianSpecifier
    req = requirement(text)
    if isinstance(req.specifier, DebianSpecifier):
        result = {"name": req.name, "relation": req.specifier.relation, "version": req.specifier.version}
        if root:
            result["root_debian"] = True
        return result
    result = {"name": req.name, "specifier": str(req.specifier)}
    if getattr(req, "forge_debian_constraint", None):
        result["official_constraint"] = list(req.forge_debian_constraint)
        if root:
            result["root_debian"] = True
    if root and getattr(req, "forge_exact_version", None):
        result["exact_version"] = req.forge_exact_version
    return result


def groups(recipe):
    result = []
    for field in ("depends", "build_depends", "run_depends"):
        result.extend([[native(text)] for text in recipe.get(field, [])])
    result.extend(group["alternatives"] for group in recipe.get("debian_dependencies", []))
    return result


def version_matches(actual, relation, expected):
    if not relation:
        return True
    a, b = DebianVersion(actual), DebianVersion(expected)
    return {"<<": a < b, "<=": a <= b, "=": a == b, ">=": a >= b, ">>": a > b}[relation]


def matches(recipe, request):
    name = request["name"]
    if "specifier" in request:
        from packaging.specifiers import SpecifierSet
        provenance = recipe.get("provenance", {})
        aliases = provenance.get("binary_versions", {})
        if name != recipe["name"] and name not in aliases:
            return False
        if request.get("official_constraint") and provenance.get("kind") == "official-apt-source":
            actual = aliases.get(name) or provenance.get("debian_version")
            relation, version = request["official_constraint"]
            if relation == "=" and not request.get("root_debian") and not provenance.get("allow_debian_equality", False):
                return False
            return actual is not None and version_matches(actual, relation, version)
        exact = request.get("exact_version")
        if exact and provenance.get("kind") == "official-apt-source":
            actual = aliases.get(name) or provenance.get("debian_version")
            if actual and version_matches(actual, "=", exact):
                return True
            if any(character in exact for character in ("-", ":", "~")):
                return False
        return Version(recipe["version"]) in SpecifierSet(request["specifier"])
    versions = recipe.get("provenance", {}).get("binary_versions", {})
    if name in versions:
        actual = versions[name]
    elif name == recipe["name"] and recipe.get("provenance", {}).get("kind") == "official-apt-source":
        actual = recipe["provenance"].get("debian_version")
    else:
        return False
    if request.get("relation") == "=" and not request.get("root_debian") and not recipe.get("provenance", {}).get("allow_debian_equality", False):
        return False
    return not request.get("relation") or (actual is not None and version_matches(actual, request["relation"], request["version"]))


def identity(recipe):
    p = recipe.get("provenance", {})
    if p.get("kind") == "official-apt-source":
        return (p.get("origin"), p.get("release_suite", p.get("suite")), p.get("component"), p.get("source_package"))
    return ("recipe", recipe["name"])


def newer(candidate, installed):
    """Compare full official revisions; source changes alone are not upgrades."""
    cp, ip = candidate.get("provenance", {}), installed.get("provenance", {})
    if cp.get("kind") == ip.get("kind") == "official-apt-source" and cp.get("debian_version") and ip.get("debian_version"):
        return DebianVersion(cp["debian_version"]) > DebianVersion(ip["debian_version"])
    return Version(candidate["version"]) > Version(installed["version"])


def resolve(recipes, specs, *, preferred=(), root_requirements=()):
    """Backtrack complete groups, including alternatives and shared source providers."""
    def compare(left, right):
        lp, rp = left.get("provenance", {}), right.get("provenance", {})
        if identity(left) == identity(right) and lp.get("debian_version") and rp.get("debian_version"):
            a, b = DebianVersion(lp["debian_version"]), DebianVersion(rp["debian_version"])
        else:
            a, b = Version(left["version"]), Version(right["version"])
        return (a > b) - (a < b)
    ordered_candidates = sorted(recipes, key=cmp_to_key(compare), reverse=True)
    ordered_candidates.sort(key=lambda r: r not in preferred)
    failures = set()
    decisions = 0

    def solve(pending, selected, edges, depth=0):
        nonlocal decisions
        decisions += 1
        if decisions > 20000 or len(selected) > 200 or depth > 600:
            raise ForgeError("Dependency resolution limit exceeded; split the request or pin versions.")
        if not pending:
            return selected, edges
        owner, alternatives, chain = pending[0]
        rest = pending[1:]
        for req in alternatives:
            existing = [r for r in selected.values() if matches(r, req)]
            choices = existing + [r for r in ordered_candidates if matches(r, req) and r not in existing]
            for recipe in choices:
                name = recipe["name"]
                if name in chain:
                    failures.add(" -> ".join(chain + [name]) + " (cycle)")
                    continue
                if name in selected and selected[name] != recipe:
                    continue
                if any(identity(r) == identity(recipe) and r != recipe for r in selected.values()):
                    continue
                new_edges = {k: list(v) for k, v in edges.items()}
                if owner is not None and name != owner:
                    new_edges.setdefault(owner, [])
                    if name not in new_edges[owner]:
                        new_edges[owner].append(name)
                elif owner == name:
                    # Internal binary dependencies are handled when source metadata is mapped.
                    failures.add(" -> ".join(chain + [name]) + " (cycle)")
                    continue
                if name in selected:
                    result = solve(rest, selected, new_edges, depth + 1)
                else:
                    new = {**selected, name: recipe}
                    dependencies = [(name, group, chain + [name]) for group in groups(recipe)]
                    result = solve(dependencies + rest, new, new_edges, depth + 1)
                if result is not None:
                    return result
            failures.add(" -> ".join(chain + [req["name"]]))
        return None

    result = solve([(None, [native(s, root=True)], []) for s in specs] + [(None, [r], []) for r in root_requirements], {}, {})
    if result is None:
        raise ForgeError("No compatible recipes satisfy the request; unresolved dependencies or conflicting versions: " + "; ".join(sorted(failures)[:12]),
                         hint="Inspect recipes with jforge apt show <package>; import/update the official source or provide a compatible recipe.")
    selected, edges = result
    ordered, visiting, done = [], [], set()

    def visit(name):
        if name in visiting:
            raise ForgeError("Dependency cycle: " + " -> ".join(visiting + [name]))
        if name in done:
            return
        visiting.append(name)
        for dep in edges.get(name, []):
            visit(dep)
        visiting.pop()
        done.add(name)
        recipe = dict(selected[name])
        recipe["provenance"] = {**recipe.get("provenance", {}), "dependency_requests": {
            field: recipe.get(field, []) for field in ("depends", "build_depends", "run_depends", "debian_dependencies")}}
        recipe["depends"] = list(edges.get(name, []))
        ordered.append(recipe)
    for name in selected:
        visit(name)
    return ordered
