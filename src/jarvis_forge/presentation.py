"""Text presentation for SSH terminals; machine-readable results stay in cli.emit."""
from __future__ import annotations

import shlex
from pathlib import Path

from rich.panel import Panel
from rich.text import Text

from .build_names import app_name


COMPACT_COMMANDS = {"apt list", "apt search", "apt instances", "apt prefix", "history", "env list", "apt sources", "doctor"}
DETAILS = ("install_id", "prefix", "path", "activation", "revision", "python", "python_version", "suite_id",
           "automatic", "description", "reason", "error", "report", "url", "suite", "component",
           "selected_suite", "selected_component")
PATH_FIELDS = {"path", "prefix", "activation", "report", "index", "home", "python", "executable", "source_command"}
COMMAND_FIELDS = {"argv", "command", "build", "install", "check", "probe", "dependency_check"}


def line(console, value="", *, style=None):
    # soft_wrap disables Rich's width-based folding/cropping. The terminal can
    # visually wrap a long path, but the output retains its original line bytes.
    console.print(Text(str(value), style=style), soft_wrap=True)


def package_setup_message(console, message, *, indent=""):
    """Render known setup notices without exposing backend wording."""
    if message.startswith("GNU patch ") and message.endswith(" ready"):
        version = message[len("GNU patch "):-len(" ready")]
        text = Text(indent)
        text.append("✓ ", style="green")
        text.append("GNU patch", style="bold")
        text.append(f"  {version}", style="cyan")
        text.append(" · ready", style="dim")
        console.print(text, soft_wrap=True)
        return True
    prefix = "Use pinned tool-suite versions: "
    if message.startswith(prefix):
        from rich.table import Table

        line(console, indent + "Tool suite · pinned versions", style="bold")
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim")
        table.add_column()
        table.add_column(style="cyan", no_wrap=True)
        names = {"gcc": "GCC", "cmake": "CMake", "gmake": "GNU Make",
                 "autoconf": "Autoconf", "automake": "Automake"}
        for spec in message[len(prefix):].split(", "):
            name, _, version = spec.partition("@")
            table.add_row(indent, Text(names.get(name, name)), Text(version, style="cyan"))
        console.print(table)
        return True
    return False


def label(key):
    return {"operation_id": "Operation", "run_id": "Run", "suite_id": "Suite", "install_id": "Instance",
            "official_source": "Official source", "source_packages": "Source packages", "updated_at": "Updated",
            "debian_version": "Debian version", "import_status": "Import status"}.get(key, key.replace("_", " ").capitalize())


def scalar(value):
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def field(console, key, value, *, indent=0, compact=False):
    prefix = " " * indent
    title = label(key)
    if isinstance(value, dict):
        if not value:
            line(console, f"{prefix}{title}: none")
        elif {"origin", "suite", "component"} <= value.keys():
            line(console, f"{prefix}{title}: {value['origin']} {value['suite']}/{','.join(value.get('components', [value['component']]))}")
            for child, data in value.items():
                if child not in {"origin", "suite", "component", "components"} and data is not None:
                    field(console, child, data, indent=indent + 2, compact=compact)
        else:
            line(console, f"{prefix}{title}:", style="bold")
            for child, data in value.items():
                field(console, child, data, indent=indent + 2, compact=compact)
    elif isinstance(value, list):
        if not value:
            line(console, f"{prefix}{title}: none")
        elif key in COMMAND_FIELDS and all(isinstance(v, str) for v in value):
            line(console, f"{prefix}{title}:")
            line(console, prefix + "  " + shlex.join(value))
        else:
            line(console, f"{prefix}{title} ({len(value)}):", style="bold")
            for item in value:
                if isinstance(item, dict):
                    record(console, item, indent=indent + 2, compact=compact)
                elif isinstance(item, list):
                    if all(isinstance(v, str) for v in item):
                        line(console, prefix + "  " + shlex.join(item))
                    else:
                        for child in item:
                            field(console, "item", child, indent=indent + 2)
                else:
                    line(console, prefix + "  " + scalar(item))
    else:
        text = scalar(value)
        if key in PATH_FIELDS or text.startswith(("/", "~/")) or "\n" in text:
            line(console, f"{prefix}{title}:")
            for part in text.splitlines() or [""]:
                line(console, prefix + "  " + part)
        else:
            line(console, f"{prefix}{title}: {text}")


def record(console, item, *, indent=0, compact=False):
    identity = next((k for k in ("name", "run_id", "id", "suite_id") if item.get(k) is not None), None)
    used = set()
    if identity is not None:
        parts = [scalar(item[identity])]
        used.add(identity)
        for key in ("version", "debian_version"):
            if item.get(key) is not None:
                parts.append(scalar(item[key]))
                used.add(key)
                break
        for key in ("action", "import_status", "status"):
            if item.get(key) is not None:
                parts.append("[" + scalar(item[key]) + "]")
                used.add(key)
        if item.get("active"):
            parts.append("[active]")
        if "active" in item:
            used.add("active")
        line(console, " " * indent + " ".join(parts), style="bold")
    keys = [key for key in DETAILS if key in item] if compact and identity is not None else list(item)
    for key in keys:
        if key not in used:
            field(console, key, item[key], indent=indent + (2 if identity else 0), compact=compact)


def package_kind(item):
    if item.get("scope") == "shared" and not item.get("selected"):
        return "shared"
    if "automatic" in item:
        return "dependency" if item["automatic"] else "requested"
    if "root" in item:
        return "requested" if item["root"] else "dependency"
    return ""


def _column_widths(headers, rows):
    return [max(Text(row[i]).cell_len for row in [headers, *rows]) for i in range(len(headers))]


def _columns(console, headers, rows, *, widths=None):
    widths = widths or _column_widths(headers, rows)
    if sum(widths) + 2 * len(headers) > console.width:
        return False
    for index, row in enumerate([headers, *rows]):
        text = Text("  ")
        for column, value in enumerate(row):
            text.append(value, style="bold" if index == 0 else "cyan" if column == 0 else None)
            if column < len(headers) - 1:
                text.append(" " * (widths[column] - Text(value).cell_len + 2))
        console.print(text, soft_wrap=True)
    return True


def package_rows(console, packages, *, planned=False):
    """One row per instance; never fold or crop a package name or version."""
    categories = {"base-tool": "base tool", "bootstrap-dependency": "bootstrap dep", "application": "application"}
    categorized = any(item.get("category") for item in packages)
    headers = ["Package", "Version", *(["Source"] if categorized else []), "Kind", "State"]
    rows = []
    for item in sorted(packages, key=lambda p: (package_kind(p) == "dependency", p["name"], str(p.get("version", "")), str(p.get("prefix", "")))):
        state = ("archived" if item.get("archived") else "active" if item.get("active")
                 else "installed" if item.get("installed") else "planned" if planned
                 else item.get("status", "available"))
        if item.get("external") or item.get("provenance", {}).get("external"):
            state += ", external"
        row = [str(item["name"]), str(item.get("version") or "—")]
        if categorized:
            row.append(categories.get(item.get("category"), "—"))
        rows.append([*row, package_kind(item) or "—", state])
    if not rows:
        line(console, "  No matching packages.")
        return
    if _columns(console, headers, rows):
        return

    def compact_table(items, widths):
        basic_headers = ["Package", "Version", "Kind", "State"]
        if _columns(console, basic_headers, items, widths=widths):
            return
        # Keep the aligned identity columns and move metadata below them.
        # soft_wrap preserves full names/versions even on very small terminals.
        identity_widths = _column_widths(basic_headers[:2], items)
        line(console, "  " + "Package".ljust(identity_widths[0]) + " Version", style="bold")
        for name, version, kind, state in items:
            text = Text("  ")
            text.append(name, style="cyan")
            text.append(" " * (identity_widths[0] - Text(name).cell_len + 1) + version)
            console.print(text, soft_wrap=True)
            line(console, "    " + " · ".join(v for v in (kind, state) if v != "—"), style="dim")

    if not categorized:
        compact_table(rows, _column_widths(headers, rows))
        return
    # An extra source column must not downgrade an entire SSH package list.
    # Group by that source and retain the original four aligned columns.
    basic = [[name, version, kind, state] for name, version, source, kind, state in rows]
    widths = _column_widths(["Package", "Version", "Kind", "State"], basic)
    titles = {"base tool": "Base tools", "bootstrap dep": "Bootstrap dependencies",
              "application": "Application packages", "—": "Other packages"}
    visible = 0
    for source, title in titles.items():
        group = [[name, version, kind, state] for name, version, category, kind, state in rows if category == source]
        if not group:
            continue
        if visible:
            line(console)
        line(console, f"{title} · {len(group)}", style="bold")
        compact_table(group, widths)
        visible += 1


def package_summary(console, packages):
    kinds = [package_kind(p) for p in packages]
    parts = [f"{len(packages)} packages"]
    for kind in ("requested", "dependency"):
        count = kinds.count(kind)
        if count:
            noun = "dependency" if count == 1 else "dependencies"
            parts.append(f"{count} {noun if kind == 'dependency' else kind}")
    line(console, " · ".join(parts))


def apt_packages(console, values):
    view = values.get("view")
    title = {"installed": "Installed packages", "available": "Available packages", "archived": "Archived packages"}
    line(console, title.get(view, "APT packages") + " · " + str(values.get("backend", "spack")), style="bold")
    packages = values.get("packages", [])
    package_summary(console, packages)
    line(console)
    package_rows(console, packages)
    if packages:
        line(console)
        line(console, "Details: jforge apt show <package>", style="dim")


def package_change_plan(console, changes, *, verbose=False, indent=""):
    for action in ("install", "upgrade", "rebuild", "remove"):
        rows = changes.get(action, [])
        if not rows:
            continue
        packages = ", ".join(row["name"] + "@" + str(row.get("version", "")) for row in rows)
        message = action.capitalize() + ": " + packages
        if not verbose and Text(indent + message).cell_len > console.width:
            message = action.capitalize() + f": {len(rows)} packages"
        line(console, indent + message)


def apt_transaction(console, command, values):
    preview = values.get("dry_run")
    line(console, f"APT {command.removeprefix('apt ')}: " + ("preview" if preview else str(values.get("status", "completed"))), style="bold")
    if "roots" in values:
        line(console, "Requests: " + (", ".join(values["roots"]) or "none"))
    if preview and values.get("backend") == "homebrew":
        line(console, "To install with Homebrew: " + (", ".join(values["would_install"]) or "none"))
    for reason in values.get("deferred_roots", {}).values():
        line(console, "  " + reason)
    if "packages" in values:
        packages = values["packages"]
        package_summary(console, packages)
        line(console)
        # A solver check shows the complete plan; installation summaries show
        # roots, rather than repeating every dependency after its live logs.
        selected = packages if command == "apt check" else [p for p in packages if package_kind(p) != "dependency"]
        package_rows(console, selected, planned=command == "apt check")
        line(console)
        if command == "apt check":
            line(console, "Plan only; no packages built or activated.", style="dim")
        else:
            line(console, "Preview only." if preview else "Installed packages: jforge apt list", style="dim")
    if values.get("activation"):
        field(console, "activation", values.get("environment_activation", values["activation"]))
    if values.get("source_command"):
        field(console, "activate", values["source_command"])
    for key in ("note", "plan", "path"):
        if values.get(key):
            field(console, "logs" if key == "path" else key, values[key])
    if values.get("store_retained"):
        line(console, "Package store retained for saved environment revisions.")
    if command == "apt remove" and values.get("backend") == "homebrew" and not preview:
        line(console, "Shared Homebrew kegs retained.")


def _join(values):
    return ", ".join(str(value) for value in values)


def _cmake_flag(name, value):
    if isinstance(value, bool):
        return f"-D{name}={'ON' if value else 'OFF'}"
    if isinstance(value, dict) and "value" in value:
        shown = value["value"]
        if value.get("type") == "BOOL" and isinstance(shown, bool):
            return f"-D{name}={'ON' if shown else 'OFF'}"
        return f"-D{name}={shown}"
    return f"-D{name}={value}"


def _check_text(value):
    if not isinstance(value, dict):
        if isinstance(value, list):
            return _join(shlex.join(item) if isinstance(item, list) else str(item) for item in value) or "none"
        return scalar(value)
    parts = []
    python = value.get("python") or {}
    if python:
        bits = []
        if python.get("version"):
            bits.append("Python " + str(python["version"]))
        if python.get("development"):
            bits.append("development headers")
        modules = python.get("modules") or []
        if modules:
            bits.append("imports " + _join(modules))
        if bits:
            parts.append(", ".join(bits))
    tools = value.get("tools") or []
    if tools:
        parts.append("tools " + _join(tools))
    for name in _script_paths(value):
        parts.append(name)
    return "; ".join(parts) or "none"


def _script_paths(value):
    if not isinstance(value, dict):
        return []
    scripts = value.get("scripts") or []
    if isinstance(scripts, str):
        scripts = [scripts]
    names = []
    for item in scripts:
        names.append(item.get("path", str(item)) if isinstance(item, dict) else str(item))
    return names


def _step_detail(step):
    options = step.get("with") or {}
    if step.get("uses") == "apt.install":
        specs = options.get("specs") or []
        return f"{len(specs)} packages into the active apt environment"
    if step.get("uses") == "cmake.project":
        bits = []
        phases = options.get("phases") or []
        if phases:
            bits.append("phases " + _join(phases))
        cache = options.get("cache") or {}
        for key in ("CMAKE_BUILD_TYPE", "CMAKE_CXX_STANDARD"):
            if key in cache:
                bits.append(_cmake_flag(key, cache[key]))
        return "; ".join(bits)
    if step.get("uses") == "check":
        return _check_text(options.get("spec") or {})
    if step.get("uses") == "command":
        argv = [str(part) for part in options.get("argv") or []]
        script = next((part for part in argv if part.endswith(".py")), None)
        return script or shlex.join(argv)
    return ""


def _show_host_requirements(console, requirements):
    if requirements.get("platforms"):
        line(console, "Platform: " + _join(requirements["platforms"]))
    if requirements.get("profiles"):
        line(console, "Profiles: " + _join(requirements["profiles"]))
    if requirements.get("apt_backend") == "spack":
        line(console, "APT profile: Spack")


def _show_native_build(console, card, requirements):
    line(console, app_name(str(card.get("name", "build"))), style="bold")
    _show_host_requirements(console, requirements)
    build = card.get("build") or {}
    if build:
        title = " ".join(part for part in (str(build.get("name", "")), str(build.get("version", ""))) if part)
        if title:
            line(console, title)
        source = build.get("source")
        if isinstance(source, dict):
            if source.get("url"):
                line(console, "Source: " + str(source["url"]))
            if source.get("subdir"):
                line(console, "Archive directory: " + str(source["subdir"]))
        elif source:
            line(console, "Source: " + str(source))
    dependencies = card.get("dependencies") or []
    if dependencies:
        line(console)
        line(console, f"Apt packages ({len(dependencies)}):", style="bold")
        line(console, "  " + _join(dependencies))
    cmake = (build.get("cmake") or {}) if build else {}
    options = cmake.get("options") or {}
    if cmake:
        line(console)
        line(console, "Build:", style="bold")
        enabled = [name for name, value in options.items() if value is True]
        disabled = [name for name, value in options.items() if value is False]
        if "all" in enabled:
            line(console, "  Configures with -Dall=ON")
        for name, value in options.items():
            if not isinstance(value, bool):
                line(console, "  " + _cmake_flag(name, value))
        extras = [name for name in enabled if name != "all"]
        if extras:
            line(console, "  Also on: " + _join(extras))
        if disabled:
            line(console, "  Off: " + _join(disabled))
        for label_name, key in (("Headers", "includes"), ("Libraries", "libraries"), ("Runtime", "runtime")):
            values = cmake.get(key) or []
            if values:
                line(console, f"  {label_name}: " + _join(values))
    features = build.get("features") or []
    checks = card.get("checks") or {}
    if checks or features or build.get("required_files"):
        line(console)
        line(console, "Acceptance:", style="bold")
        if checks.get("before") is not None:
            line(console, "  Before: " + _check_text(checks.get("before")))
        if checks.get("after") is not None:
            line(console, "  After: " + _check_text(checks.get("after")))
        if features:
            line(console, "  Required features: " + _join(features))
        required = build.get("required_files") or []
        if required:
            line(console, "  Required files: " + _join(required))


def _show_legacy_build(console, card, requirements):
    workflow = card.get("Workflow") or {}
    line(console, app_name(str(workflow.get("name", "build"))), style="bold")
    _show_host_requirements(console, requirements)
    modules = []
    for group in ("LibDeps", "Calculators"):
        entries = (card.get(group) or {}).get("Modules") or []
        if isinstance(entries, list):
            modules.extend(entries)
    if modules:
        line(console)
        line(console, f"Modules ({len(modules)}):", style="bold")
        for module in modules:
            if not isinstance(module, dict):
                line(console, "  " + str(module))
                continue
            title = " ".join(part for part in (str(module.get("name", "module")), str(module.get("version", ""))) if part)
            line(console, "  " + title)
            if module.get("recipe"):
                line(console, "    recipe: " + str(module["recipe"]))
            if module.get("workflow"):
                line(console, "    requires: " + str(module["workflow"]))


def show_build(console, card, *, path, sha256, requirements, ordered=None):
    """Summarize a build card. JSON output keeps the original mapping."""
    if isinstance(card, dict) and "name" in card and "Workflow" not in card:
        _show_native_build(console, card, requirements)
    elif isinstance(card, dict) and "Workflow" in card:
        _show_legacy_build(console, card, requirements)
    else:
        line(console, "Build card", style="bold")
        _show_host_requirements(console, requirements)
    if ordered:
        line(console)
        line(console, f"Steps ({len(ordered)}):", style="bold")
        scripts = []
        checks = card.get("checks") if isinstance(card, dict) else None
        if isinstance(checks, dict):
            for stage in ("before", "after"):
                scripts.extend(_script_paths(checks.get(stage)))
        for index, step in enumerate(ordered, 1):
            line(console, f"  {index}. {step.get('id', 'step')}  {step.get('uses', '')}")
            if step.get("uses") == "command" and scripts:
                detail = scripts.pop(0)
            else:
                detail = _step_detail(step)
            standard = (((card.get("build") or {}).get("cmake") or {}).get("options") or {}).get("CMAKE_CXX_STANDARD") if isinstance(card, dict) else None
            if detail and standard is not None:
                detail = detail.replace("${parameters.cxx_standard}", str(standard))
            if detail:
                line(console, "     " + detail)
    line(console)
    line(console, "Card:")
    line(console, "  " + str(path))
    line(console, "SHA-256: " + str(sha256))
    if ordered is None:
        line(console, "Details: jforge build show <build> --expanded", style="dim")


def apt_update(console, values):
    source = "Homebrew metadata" if values.get("backend") == "homebrew" else "Spack recipes"
    line(console, source + ": " + str(values.get("status", "completed")), style="bold")
    catalog = values.get("build_catalog")
    if catalog:
        line(console, f"Build recipes for {catalog['platform']} · {len(catalog['builds'])} available"
             f" · {len(catalog['excluded_builds'])} for other platforms")
        if catalog.get("path"):
            field(console, "build_catalog_logs", catalog["path"])
    for key in ("available", "repository_commit", "updated_at", "note", "operation_id", "path"):
        if key in values:
            field(console, "logs" if key == "path" else key, values[key])
    for key, value in values.items():
        if key not in {"available", "repository_commit", "updated_at", "note", "operation_id", "path", "status",
                       "build_catalog", "package_update"}:
            field(console, key, value, compact=True)


def homebrew_formula(console, values):
    formula = values["package"]
    line(console, f"{formula['name']} · Homebrew", style="bold")
    for key in ("desc", "homepage", "license", "dependencies"):
        if formula.get(key):
            field(console, "description" if key == "desc" else key, formula[key])
    line(console)
    package_rows(console, values["installed_instances"])
    for item in values["installed_instances"]:
        line(console, "  " + item["prefix"], style="dim")


def _build_catalog_rows(console, entries, *, libraries=False):
    headers = ["Library" if libraries else "Recipe", "Revision"]
    if not libraries:
        headers.append("Scripts")
    rows = []
    descriptions = []
    for entry in entries:
        row = [entry["id"] if libraries else app_name(entry["id"]), str(entry.get("revision") or "—")]
        if not libraries:
            row.append(str(len(entry.get("files") or [])))
        rows.append(row)
        descriptions.append(" ".join(str(entry.get("description") or "").split()))
    if any(descriptions):
        headers.append("Description")
        rows = [row + [description or "—"] for row, description in zip(rows, descriptions)]
    if _columns(console, headers, rows):
        return
    # Descriptions are optional details, not a reason to discard the identity
    # table for every recipe in a narrow terminal.
    basic_headers = headers[:-1] if any(descriptions) else headers
    basic_rows = [row[:len(basic_headers)] for row in rows]
    if _columns(console, basic_headers, basic_rows):
        if any(descriptions):
            line(console)
            line(console, "Descriptions", style="bold")
            for row, description in zip(rows, descriptions):
                if description:
                    line(console, "  " + row[0], style="cyan")
                    line(console, "    " + description)
        return
    for row, description in zip(rows, descriptions):
        parts = row[:2]
        if not libraries:
            count = row[2]
            parts.append(count + (" script" if count == "1" else " scripts"))
        line(console, "  " + " · ".join(parts))
        if description:
            line(console, "    " + description)


def build_catalog(console, command, values):
    builds = values["builds"]
    libraries = values.get("libraries", [])
    if command == "build update":
        noun = "recipe" if len(builds) == 1 else "recipes"
        line(console, f"Build catalog updated · {len(builds)} {noun}", style="bold")
    elif builds:
        line(console, f"Build recipes ({len(builds)})", style="bold")
    else:
        line(console, "No cached build recipes.")
    if builds:
        _build_catalog_rows(console, builds)
    if libraries:
        line(console)
        line(console, f"Build libraries ({len(libraries)})", style="bold")
        _build_catalog_rows(console, libraries, libraries=True)
    if builds:
        line(console)
        line(console, "Inspect with jforge build show <app>, or fetch with jforge build fetch <app>.", style="dim")
    elif command == "build list":
        line(console)
        line(console, "Download recipes with jforge build update.", style="dim")


def build_plan(console, values, *, verbose=False):
    from .builds import _step_title

    line(console, "Build plan · " + app_name(values["build_id"]), style="bold")
    if values.get("workspace"):
        field(console, "workspace", values["workspace"])
    if values.get("environment_name"):
        field(console, "environment", values["environment_name"])
    for node in values["nodes"]:
        role = "app" if node["recipe"] == values["root"] else "dependency"
        line(console, f"  {app_name(node['build'])} · {role}", style="bold")
        _show_host_requirements(console, node["host_requirements"])
        if excluded := node.get("outputs_off"):
            line(console, "    Exclude while building: " + ", ".join(dict.fromkeys(map(app_name, excluded))))
        for index, step in enumerate(node["steps"], 1):
            line(console, f"    {index}. {_step_title(step)}")
    if verbose:
        line(console)
        for entry in values["cmake_commands"]:
            line(console, f"  {app_name(entry['build'])} / {entry['step']}")
            for phase in entry["phases"]:
                line(console, f"    {phase}: " + shlex.join(entry["argv"][phase]))
    line(console)
    line(console, values["note"], style="dim")


def _app_table(console, headers, rows):
    if _columns(console, headers, rows):
        return
    # Keep long NAS prefixes out of the summary columns. Paths remain complete
    # on their own logical lines for copying and natural terminal wrapping.
    identities = [row[:-1] for row in rows]
    if not _columns(console, headers[:-1], identities):
        for row in identities:
            line(console, f"  {row[0]} {row[1]}", style="cyan")
            line(console, "    " + " · ".join(f"{key}: {value}" for key, value in zip(headers[2:-1], row[2:])))
    line(console)
    line(console, "Locations", style="bold")
    for row in rows:
        line(console, f"  {row[0]} {row[1]}", style="cyan")
        line(console, "    " + str(row[-1]))


def build_artifacts(console, artifacts, *, installed=False):
    if not artifacts:
        line(console, "No apps installed in this profile." if installed else "No recorded build artifacts.")
        if installed:
            line(console, "Browse recipes with jforge build list --available.", style="dim")
        return
    title = "Installed apps" if installed else "Build artifacts"
    line(console, f"{title} ({len(artifacts)})", style="bold")
    if installed:
        _app_table(console,['App','Version','State','Status','Location'],
                   [[item.get('name') or app_name(item['build_id']), str(item['version']),
                     'ON' if item.get('enabled') else 'OFF', item['status'], item['prefix']] for item in artifacts])
        line(console, "Inspect an app with jforge build show <app>.", style="dim")
        return
    for item in artifacts:
        line(console)
        line(console, f"  {app_name(item['build_id'])} {item['version']} · {item['output']} · {item['status']}", style="cyan")
        line(console, "    ID  " + item["artifact_id"])
        line(console, "    " + item["prefix"])
    line(console)
    line(console, "Find an installation with jforge build show <app> --path.", style="dim")


def apt_profile_list(console, profiles, *, show_hint=True):
    if not profiles:
        line(console, "No configured package profiles.")
        return
    line(console, f"Package profiles ({len(profiles)})", style="bold")
    for profile in profiles:
        state = ("missing" if profile.get("status") == "missing"
                 else "pending setup" if profile.get("configured") is False else "active" if profile["active"]
                 else "archived" if profile["backend"] == "archived" else "inactive")
        line(console, f"  {profile['id']} · {state}")
        line(console, f"    {profile['path']}", style="dim")
    if show_hint:
        line(console)
        line(console, "Select a package profile with jforge profile use <name>.", style="dim")


def build_result(console, values, prog, resume_command, *, handed_off=False):
    status, suite = values["status"], app_name(values["suite_id"])
    failed_step = values.get("failed_step")
    if status == "passed":
        line(console, f"{suite} · Passed", style="bold green")
    else:
        heading = f"{suite} · {status.capitalize()}"
        if failed_step:
            heading += f" at step {failed_step['index']}/{failed_step['total']}"
            if app_name(failed_step["build"]) != suite:
                heading += f" in {app_name(failed_step['build'])}"
            heading += f" · {failed_step['title']}"
        error(console, prog, heading + "\n" + values["error"])
        if values.get("hint"):
            line(console, values["hint"])
    field(console, "run_id", values["run_id"])
    outputs = values["outputs"] if status == "passed" else []
    workspace_activation = (values.get("workspace") and
                            (len(outputs) == 1 or any(row["name"] == "installation" for row in outputs)))
    for row in outputs:
        name = "Installed" if row["name"] == "installation" else f"Installed {row['name']}"
        line(console, f"{name} · {row['version']}", style="bold")
        line(console, "  " + row["prefix"])
        selected = row["name"] == "installation" or len(outputs) == 1
        if row.get("activation") and not workspace_activation and not (handed_off and selected):
            line(console, "  " + shlex.join(["source", row["activation"]]))
    if workspace_activation and not handed_off:
        line(console, shlex.join(["source", str(Path(values["workspace"]) / "activate.sh")]))
    if status == "passed" and values.get("environment_entry"):
        line(console, "Added to the apt environment:")
        line(console, "  " + values["environment_entry"])
    elif status == "passed" and values.get("environment_entry_error"):
        line(console, "Could not add this installation to the apt environment.")
        line(console, "  " + values["environment_entry_error"])
    field(console, "build_log", str(Path(values["attempt"]) / "build.log"))
    if failed_step:
        line(console, "Resume this run:")
        line(console, "  " + resume_command)


def shell_repair(console, values):
    preview = values["dry_run"]
    label = "Shell repair preview" if preview else "Shell repair"
    line(console, label, style="bold")
    if values.get("scope") == "managed":
        changed = "would change" if preview else "changed"
        line(console, f"  {len(values['targets'])} checked · {values['changed']} {changed} · {values['removed']} invalid paths · "
             f"{values['deferred']} deferred · {len(values['errors'])} failed")
        for target in values["targets"]:
            state = "would repair" if preview else "repaired"
            if not target["changed"]:
                state = "deferred" if target["deferred"] else "checked"
            line(console, f"  {target['kind']} {target['name']} · {state}")
            for reason in target.get("deferred_roots", {}).values():
                line(console, "    " + reason, style="dim")
            if target.get("report"):
                line(console, "    " + target["report"], style="dim")
        return
    line(console, values["activation"])
    removed = "would remove" if preview else "removed"
    line(console, f"  {len(values['settings'])} recorded · {values['removed']} {removed} · {values['unchecked']} unchecked")
    for reason in values.get("deferred_roots", {}).values():
        line(console, "  " + reason)
    for item in values["settings"]:
        if item["status"] == "removed":
            name = item.get("name", item["kind"])
            target = f" {item['value']}" if "value" in item else ""
            line(console, f"  {name}{target} · {item['reason']}")
            location = f" entry {item['entry']}" if "entry" in item else f":{item['line']}"
            line(console, f"    {item['script']}{location}", style="dim")
    if values.get("backup"):
        line(console, "Backup " + values["backup"])
    if values.get("report"):
        line(console, "Report " + values["report"])


def environment_list(console, environments, profiles):
    if not environments:
        line(console, "No shell environments.")
    else:
        line(console, f"Environments ({len(environments)})", style="bold")
        for item in environments:
            line(console, f"  {item['name']} [{item.get('status') or 'ready'}]")
            if item.get("description"):
                line(console, "    " + item["description"])
            if item.get("path"):
                line(console, "    " + item["path"], style="dim")
    line(console)
    line(console, "Load one with jforge env switch <name>. Use apt for packages.", style="dim")
    line(console, "Show metadata with jforge env metadata <name>.", style="dim")
    line(console)
    apt_profile_list(console, profiles)


def environment_metadata(console, record, path):
    kind = record.get("kind") or "shell"
    line(console, f"{record.get('name', 'environment')} [{kind}]", style="bold")
    if record.get("description"):
        line(console, "  " + record["description"])
    line(console)
    field(console, "metadata", path)
    if record.get("activation"):
        field(console, "activation", record["activation"])
    if record.get("profile"):
        field(console, "profile", record["profile"])
    field(console, "revision", record.get("revision"))
    backend = record.get("backend_activation")
    if backend:
        field(console, "packages" if kind == "apt" else "revision_script", backend)
    builds = record.get("builds") if isinstance(record.get("builds"), list) else []
    line(console, f"Builds ({len(builds)}):", style="bold")
    if not builds:
        line(console, "  none")
    for item in builds:
        if not isinstance(item, dict):
            line(console, "  " + scalar(item))
            continue
        title = app_name(item.get("build_id") or "build")
        output = item.get("output")
        if output and output != title:
            title += " · " + str(output)
        line(console, "  " + title)
        if item.get("artifact_id"):
            line(console, "    artifact " + str(item["artifact_id"]), style="dim")
        if item.get("activation"):
            line(console, "    " + item["activation"], style="dim")
    if kind == "apt":
        omitted = record.get("omitted_builds")
        names = [item for item in omitted if isinstance(item, str)] if isinstance(omitted, list) else []
        line(console, f"Omitted ({len(names)}):", style="bold")
        if not names:
            line(console, "  none")
        for item in names:
            line(console, "  " + app_name(item))


def environment_selection(console, command, values):
    if command == "env detach":
        line(console, "Detached.", style="bold")
        if values.get("previous"):
            field(console, "previous", values["previous"])
    elif command == "env drop":
        line(console, "Removed from the apt environment: " + app_name(values["build_id"]), style="bold")
        field(console, "path", values["path"])
        if values.get("description"):
            field(console, "description", values["description"])
    else:
        line(console, "Selected environment: " + str(values.get("selected")), style="bold")
        if values.get("description"):
            field(console, "description", values["description"])
        field(console, "path", values["path"])
    if values.get("shell") == "pending":
        line(console, "This shell applies the change when the command returns.")
    elif values.get("shell") == "unchanged" and command == "env drop":
        line(console, "The apt file no longer sources it. This shell is unchanged.")
    elif values.get("shell") == "unchanged":
        line(console, "Saved for new shells. This shell is unchanged.")
        line(console, 'Load the hook with: eval "$(command jforge shell-init)"', style="dim")


def _card_title(card):
    return card["suite_id"] + (f" (catalog card {card['project_id']})" if card.get("project_id") else "")


def test_validate(console, values):
    for card in values["cards"]:
        line(console, f"Card {_card_title(card)}: valid", style="bold")
        line(console, f"  {card['path']}", style="dim")
        line(console, f"  Groups: {card['groups']}   Checks: {card['checks']}   "
                      f"Coverage: {'declared' if card['coverage'] else 'not declared'}")
        for name, ids in card["profiles"].items():
            default = " (default)" if name == card["default_profile"] else ""
            line(console, f"  Profile {name}{default}: {', '.join(ids)}")
        for warning in card["warnings"]:
            line(console, f"  Warning: {warning}", style="yellow")


def test_discovery(console, values):
    line(console, "Test discovery: " + ("complete" if values['confirmed'] else "failed"), style="bold")
    counts={adapter:sum(row['adapter']==adapter for row in values['items']) for adapter in ('pytest','command')}
    line(console, f"  {counts['pytest']} pytest items · {counts['command']} checks · no test bodies executed")
    field(console, "python", values['python'])
    field(console, "inventory", values['inventory_path'])
    for message in values.get('metadata_diagnostics',[]):
        line(console, "  Metadata: " + message, style="yellow")
    for error in values.get('errors',[]):
        for entry in error.get('collection',{}).get('errors',[]):
            line(console, f"  {error['step_id']}: {entry['message']}", style="red")


def test_case_report(console, report):
    line(console, report['title'], style="bold")
    line(console, f"  {report.get('outcome') or report['state']} · {report['execution_mode']} · report {report['status']}")
    for name in ('run_id','instance_id','native_id','metadata_status','purpose','reason','generation_error'):
        if report.get(name):field(console, name, report[name])
    if report.get('reused_from'):field(console, "reused_from", report['reused_from'])
    for criterion in report.get('criteria',[]):
        line(console, f"  [{criterion['status']}] {criterion['expected']} ({criterion['verification']})")
    line(console, f"  {len(report.get('artifacts',[]))} registered evidence items")


def test_plan(console, values):
    batch = values["batch"]
    for number, card in enumerate(values["cards"]):
        if number:
            line(console)
        heading = f"suite: {card['suite_id']}"
        if card.get("project_id"):
            heading += f"   project: {card['project_id']}"
        heading += f"   profile: {card['profile'] or 'none'}   batch: {batch['index']}/{batch['count']}"
        line(console, heading, style="bold")
        rows = [("step", "kind", "isolate", "markers", "files")]
        for step in card["steps"]:
            if step["kind"] == "pytest":
                rows.append((step["id"], "pytest", step["isolate"], step["markers"] or "-",
                             f"{len(step['paths'])}/{step['files']}"))
            else:
                rows.append((step["id"], step["kind"], "", "", ""))
        widths = [max(len(row[column]) for row in rows) for column in range(5)]
        for row in rows:
            line(console, "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
        for step in card["steps"]:
            if step["kind"] != "pytest":
                continue
            line(console, f"{step['id']}:" + (" empty batch" if not step["paths"] else ""), style="dim")
            for path in step["paths"]:
                line(console, f"  {path}", style="dim")
        for warning in card["warnings"]:
            line(console, f"Warning: {warning}", style="yellow")


def _table(console, rows, *, right=()):
    widths = [max(len(str(row[column])) for row in rows) for column in range(len(rows[0]))]
    for row in rows:
        cells = [str(cell).rjust(width) if column in right else str(cell).ljust(width)
                 for column, (cell, width) in enumerate(zip(row, widths))]
        line(console, "  ".join(cells).rstrip())


def _count(step, key):
    counts = step.get("counts")
    return counts.get(key, 0) if counts else "-"


def test_run(console, values):
    if "runs" not in values:
        for key, value in values.items():
            field(console, key, value)
        return
    for number, run in enumerate(values["runs"]):
        if number:
            line(console)
        heading = f"suite: {run['suite_id']}"
        if run.get("project_id"):
            heading += f"   project: {run['project_id']}"
        heading += f"   profile: {run.get('profile') or 'groups'}   acceptance_id: {run.get('acceptance_id')}"
        line(console, heading, style="bold")
        source = run.get("source") or {}
        environment = run.get("environment") or {}
        state = "not in Git" if not source else ("dirty" if source.get("dirty") else "clean")
        line(console, f"source: {(source.get('commit') or '-')[:8]} ({state})   "
                      f"env: {environment.get('name') or environment.get('shell') or 'inherited'}   "
                      f"packages: {(run.get('packages_digest') or '-')[:12]}")
        rows = [("step", "batch", "status", "passed", "failed", "skipped", "duration", "reused")]
        for step in run["steps"]:
            step_batch = step.get("batch")
            duration = step.get("duration_seconds")
            rows.append((step["id"], f"{step_batch['index']}/{step_batch['count']}" if step_batch else "-",
                         step["status"], _count(step, "passed"), _count(step, "failed") if not step.get("counts")
                         else step["counts"].get("failed", 0) + step["counts"].get("errors", 0),
                         _count(step, "skipped"), f"{duration:.1f}s" if duration is not None else "-",
                         "yes" if step["status"] == "reused" else "no"))
        _table(console, rows, right={3, 4, 5, 6})
        for step in run["steps"]:
            if step.get("reason") and step["status"] != "reused":
                line(console, f"{step['id']}: {step['reason']}", style="red" if step["status"] == "failed" else "dim")
            for skip in step.get("unexpected_skips", [])[:10]:
                line(console, f"  unexpected skip {skip['node']}: {skip['message']}", style="red")
        for warning in run.get("warnings", []):
            line(console, f"Warning: {warning}", style="yellow")
        if run.get("error"):
            line(console, f"Error: {run['error']}", style="red")
        line(console, f"Run {run['run_id']}: {run['status']}", style="bold")
    summary = values.get("summary")
    if summary:
        line(console)
        test_summary(console, summary)


def test_summary(console, report):
    line(console, f"Acceptance {report['acceptance_id']}: {report['status']}, "
                  f"accepted: {'yes' if report['accepted'] else 'no'}", style="bold")
    for entry in report["entries"]:
        verdict = "accepted" if entry["accepted"] else entry["status"]
        line(console, f"  {entry['entry']} ({entry.get('profile') or 'groups'}): {verdict}")
        for reason in entry["reasons"]:
            line(console, f"    {reason}", style="yellow")
    if report.get("path"):
        line(console, f"Report: {report['path']}", style="dim")


def test_status(console, values):
    import shlex

    if values.get("status") == "interrupted":
        line(console, "Acceptance interrupted.", style="yellow")
        return
    for card in values["cards"]:
        title = card["suite_id"] + (f" (catalog card {card['project_id']})" if card.get("project_id") else "")
        verdict = card["status"].replace("_", " ")
        line(console, f"{title} · {verdict}", style="green" if card["accepted"] else "yellow")
        for reason in card["reasons"]:
            line(console, f"  {reason}")
        if card.get("acceptance_id"):
            line(console, f"  Report  {card['acceptance_id']}", style="dim")
        if card["next_argv"]:
            line(console, f"  Next    {shlex.join(card['next_argv'])}", style="dim")


def test_history(console, values):
    runs = values["runs"]
    if not runs:
        line(console, "No jforge test runs.")
        return
    rows = [("run", "suite", "profile", "batch", "status", "commit", "acceptance")]
    for run in runs:
        batch = run.get("batch") or {}
        rows.append((run["run_id"], run.get("project_id") or run["suite_id"], run.get("profile") or "groups",
                     f"{batch.get('index', 1)}/{batch.get('count', 1)}", run["status"],
                     (run.get("commit") or "-")[:8] + ("*" if run.get("dirty") else ""), run.get("acceptance_id") or "-"))
    _table(console, rows)
    if values.get("case") is not None:
        for run in runs:
            for case in run.get("cases", []):
                line(console, f"{run['run_id']}  {case['outcome']:<8} {case['node']}")


def playlist(console, command, values):
    library = values["library"]
    if command == "playlist project list":
        projects = values["projects"]
        line(console, f"{library} · {len(projects)} projects", style="bold")
        for row in projects:
            status = row["root_source"] if row["available"] else "unavailable"
            line(console, f"  {row['id']}  {row['title']} · {status}")
            if row["root"]:
                line(console, f"    {row['root']}")
    elif command == "playlist project path":
        line(console, f"Registered {values['id']} in {library}")
        line(console, values["root"])
    elif command in {"playlist cases", "playlist sync", "playlist verify"}:
        cases = values["cases"]
        line(console, f"{library} · {len(cases)} cases", style="bold")
        if command == "playlist cases":
            for case in cases:
                profiles = ", ".join(case["profiles"])
                detail = f" · {profiles}" if profiles else ""
                line(console, f"  {case['id']}  {case['kind']}{detail}")
        else:
            line(console, "Inventory updated." if command == "playlist sync" else "Library verified.")
    elif command == "playlist list":
        for item in values["playlists"]:
            kind = "saved" if item["stored"] else "profile"
            detail = item.get("problem") or f"{len(item['members'])} cases"
            line(console, f"{item['id']}  {kind} · {detail}")
        if not values["playlists"]:
            line(console, "No playlists.")
    elif "playlist" in values:
        item = values["playlist"]
        line(console, item["title"], style="bold")
        line(console, item["id"])
        if item.get("problem"):
            line(console, item["problem"])
        for case in item["members"]:
            line(console, f"  {case}")
    elif "jobs" in values:
        for job in values["jobs"]:
            line(console, f"{job['id']}  {job['status']}  {job['label']}")
        if not values["jobs"]:
            line(console, "No playlist jobs.")
    elif "job" in values:
        job = values["job"]
        line(console, f"{job['label']} · {job['status']}", style="bold")
        line(console, job["id"])
        for row in job["cases"]:
            line(console, f"  {row['id']}  {row['status']}")
            if row.get("reason"):
                line(console, f"    {row['reason']}")
    else:
        action = "Created" if command == "playlist init" else "Selected"
        line(console, f"{action} playlist library {library}")
        line(console, values["path"])


def profile_check(console, values):
    host = values["host"]
    line(console, f"{host['system']} {host['version']} · {host['machine']}", style="bold")
    for profile in values["profiles"]:
        line(console, f"  {profile.get('id', profile['name'])} · {profile['status']}")
        if profile.get("reason"):
            line(console, "    " + profile["reason"])
    if values.get("default"):
        field(console, "default profile", values["default"])


def app_operation(console,command,values):
    status=values.get('status','passed')
    for run in values.get('runs',[]):
        target=run.get('workspace') or run.get('suite_id','APP')
        shown={**run,'outputs':[dict(row,name=name) for name,row in run.get('outputs',{}).items()]}
        if run['status'] in {'failed','interrupted'}:
            from .cli import _console
            build_result(_console(stderr=True),shown,'jforge','jforge build install '+shlex.quote(str(target)))
        else:build_result(console,shown,'jforge','jforge build install '+shlex.quote(str(target)))
    if status in {'failed','interrupted'} and values.get('runs'):return
    line(console,f"{command.capitalize()} · {status}",style='bold')
    rows=values.get('apps',[])
    if rows:
        _app_table(console,['App','Version','State','Location'],
                   [[row.get('name',row.get('app','')),str(row.get('version','')),
                     'ON' if row.get('enabled') else 'OFF' if 'enabled' in row else 'planned',
                     str(row.get('prefix',''))] for row in rows])
    elif status=='passed':line(console,'  No app changes.')
    if values.get('error'):
        error=values['error'];line(console,error.get('message','') if isinstance(error,dict) else str(error),style='red')
    if values.get('activation'):field(console,'activation',values['activation'])
    for run in values.get('runs',[]):
        if run.get('attempt'):field(console,'log',str(Path(run['attempt'])/'build.log'))
    for plan in values.get('plans',[]):
        changes=plan.get('changes',{})
        for action,packages in changes.items():
            if packages:line(console,'  '+action+': '+', '.join(row['name']+'@'+str(row.get('version','')) for row in packages))


def render(console, command, values):
    if command=='profile export':
        field(console,'profile export',values.get('path',values.get('output','stdout')))
        return
    if command in {'build install','build upgrade','build remove','build autoremove'}:
        app_operation(console,command,values);return
    if command=='build show' and 'app' in values:
        row=values['app'];line(console,f"{row['name']} {row['version']} · {'ON' if row.get('enabled') else 'OFF'}",style='bold')
        field(console,'prefix',row['prefix'])
        if row.get('workspace'):field(console,'workspace',row['workspace'])
        return
    if command=='apt show':
        package=values['package'];line(console,package['name'],style='bold')
        for key in ('description','desc','homepage','license'):
            if package.get(key):field(console,key,package[key])
        if package.get('latest_version'):field(console,'latest version',package['latest_version'])
        if package.get('versions'):field(console,'versions',', '.join(package['versions']))
        package_rows(console,values['installed_instances'])
        for row in values['installed_instances']:field(console,'prefix',row['prefix'])
        return
    if command=='profile' or command=='profile show' and values.get('view')=='list':
        line(console,'Profiles',style='bold')
        for row in values['instances']:
            selected=' · selected' if row['id']==values.get('default') else ''
            line(console,f"  {row['id']} · {row['status']}{selected}")
        return
    if command in {'profile create','profile use','profile show'}:
        line(console,f"{values['id']} · {values['status']}",style='bold')
        if command=='profile show':
            line(console,f"  {values['packages']} packages · {values['apps']} apps · Python {values['python']['version']}")
            for row in values['history']:
                line(console,f"  {row.get('history','')} · {row.get('action','')} · {row.get('status','')}")
        field(console,'activation',values['activation'])
        return
    if command=='profile remove':
        line(console,f"{values['name']} · {'would remove' if values['dry_run'] else 'removed'}")
        return
    if command in {'apt profile check','profile check'}:
        profile_check(console, values)
        return
    if command == "apt profile apps":
        line(console, values["profile"], style="bold")
        if "changed" in values:
            line(console, f"  {values.get('app') or app_name(values['changed'])} · {'ON' if values['enabled'] else 'OFF'}")
        elif not values["outputs"]:
            line(console, "  No apps installed in this profile.")
        else:
            for item in values["outputs"]:
                line(console, f"  {'ON ' if item['enabled'] else 'OFF'}  {app_name(item['build_id'])} {item['version']} · {item['output']}")
                line(console, "       " + item["artifact_id"], style="dim")
        return
    if command in {"apt profile build", "apt profile global"}:
        line(console, f"{values['id']} · {values['status']}", style="bold")
        field(console, "activation", values["activation"])
        return
    if command.startswith("playlist "):
        playlist(console, command, values)
        return
    if command == "test validate":
        test_validate(console, values)
        return
    if command == "test discover":
        test_discovery(console, values)
        return
    if command == "test metadata validate":
        line(console, f"Metadata: valid · {values['definitions']} definitions · {values['reviewed']} reviewed", style="bold")
        line(console, "  Bindings checked" if values['binding_checked'] else "  Format checked; bindings not checked")
        for message in values.get('diagnostics',[]):line(console, "  " + message, style="yellow")
        return
    if command == "test metadata scaffold":
        line(console, f"Metadata drafts: {values['definitions']} definitions · review required", style="bold")
        field(console, "output", values['output'])
        return
    if command == "test plan":
        test_plan(console, values)
        return
    if command == "test run":
        test_run(console, values)
        return
    if command == "test summarize":
        test_summary(console, values)
        return
    if command in {"test status", "test accept"}:
        test_status(console, values)
        return
    if command == "test history":
        test_history(console, values)
        return
    if command == "test report":
        if values.get('kind') == 'test-case':
            test_case_report(console, values['report'])
            return
        if values.get("markdown"):
            console.print(values["markdown"], markup=False, highlight=False, end="")
            return
    if command == "toolchain list":
        toolchains = values["toolchains"]
        line(console, f"Installed toolchains ({len(toolchains)})", style="bold")
        for toolchain in toolchains:
            line(console, f"  {toolchain['id']}")
            line(console, "    " + toolchain["prefix"], style="dim")
        return
    if command == "env repair":
        shell_repair(console, values)
        return
    if command == "build list" and values.get("view") == "installed":
        build_artifacts(console, values["builds"], installed=True)
        return
    if command in {"build list", "build update", "build search"}:
        build_catalog(console, command, values)
        return
    if command == "build validate":
        line(console, f"{app_name(values['build_id'])} · Recipe valid", style="bold")
        if values["nodes"] > 1:
            field(console, "builds in plan", values["nodes"])
        return
    if command == "build fetch":
        line(console, "Recipe fetched.", style="bold")
        field(console, "recipe", values.get("path",values.get("workspace")))
        target = values.get("workspace") or values.get("path", "APP")
        line(console, "Build with jforge build install " + shlex.quote(target), style="dim")
        return
    if command == "build artifacts":
        build_artifacts(console, values["artifacts"])
        return
    if command in {"apt list", "apt search"}:
        apt_packages(console, values)
        return
    if command == "apt show" and values.get("backend") == "homebrew":
        homebrew_formula(console, values)
        return
    if command in {"apt install", "apt reinstall", "apt remove", "apt upgrade", "apt full-upgrade", "apt satisfy", "apt check"}:
        apt_transaction(console, command, values)
        return
    if command == "apt update":
        apt_update(console, values)
        return
    if command == "env list" and "profiles" in values:
        environment_list(console, values["environments"], values["profiles"])
        return
    if command in {"env switch", "env detach", "env drop"}:
        environment_selection(console, command, values)
        return
    if command == "env metadata" and "metadata" in values:
        environment_metadata(console, values["metadata"], values.get("path"))
        return
    if command in {"apt profile", "apt switch"} and "environments" in values:
        presets = values.get("presets") or []
        if presets:
            line(console, "Built-in profiles", style="bold")
            for preset in presets:
                line(console, f"  {preset['name']} · {preset['backend']} · {preset['reference']}")
            if values.get("default"):
                field(console, "default profile", values["default"])
        apt_profile_list(console, values["environments"], show_hint=not presets)
        if presets:
            line(console)
            line(console, "Run jforge profile check to see which presets this host supports.", style="dim")
        return
    if command in {"apt profile", "apt switch"} and "profile" in values:
        line(console, "APT environment: " + str(values["profile"]), style="bold")
        for key in ("source", "user_home", "apt_home", "packages", "activation", "status"):
            if key in values:
                field(console, key, values[key])
        if "path" in values:
            field(console, "logs", values["path"])
        return
    if command == "apt env":
        for key, label in (("activation", "activation"), ("source_command", "activate")):
            if values.get(key):
                field(console, label, values[key])
        return
    for key, value in values.items():
        field(console, key, value, compact=command in COMPACT_COMMANDS)


def error(console, prog, message, hint=None):
    text = message + ("\nTry: " + hint if hint else "")
    width = max(0, console.width - 8)
    if all(Text(part).cell_len <= width for part in text.splitlines()):
        console.print(Panel(Text(text), title=Text(prog), border_style="red"))
        return
    console.print(Panel(Text("Command failed."), title=Text(prog), border_style="red"))
    line(console, message)
    if hint:
        line(console, "Try:", style="bold")
        line(console, hint)
