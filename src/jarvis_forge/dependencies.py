"""Dependency groups and provider-aware, version-domain-specific matching."""
from __future__ import annotations

from packaging.version import Version
from debian.debian_support import Version as DebianVersion

from .storage import ForgeError


def native(text):
    from .catalog import requirement
    req = requirement(text)
    return {"name": req.name, "specifier": str(req.specifier)}


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
        aliases = recipe.get("provenance", {}).get("binary_versions", {})
        return (name == recipe["name"] or name in aliases) and Version(recipe["version"]) in SpecifierSet(request["specifier"])
    versions = recipe.get("provenance", {}).get("binary_versions", {})
    if name not in versions:
        return False
    actual = versions[name]
    if request.get("relation") == "=" and not recipe.get("provenance", {}).get("allow_debian_equality", False):
        return False
    return not request.get("relation") or (actual is not None and version_matches(actual, request["relation"], request["version"]))


def identity(recipe):
    p = recipe.get("provenance", {})
    if p.get("kind") == "official-apt-source":
        return (p.get("origin"), p.get("suite"), p.get("component"), p.get("source_package"))
    return ("recipe", recipe["name"])


def resolve(recipes, specs):
    """Backtrack complete groups, including alternatives and shared source providers."""
    ordered_candidates = sorted(recipes, key=lambda r: Version(r["version"]), reverse=True)
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

    result = solve([(None, [native(s)], []) for s in specs], {}, {})
    if result is None:
        raise ForgeError("Unresolved dependencies or conflicting versions: " + "; ".join(sorted(failures)[:12]),
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
