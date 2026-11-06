"""Text presentation for SSH terminals; machine-readable results stay in cli.emit."""
from __future__ import annotations

import shlex

from rich.panel import Panel
from rich.text import Text


COMPACT_COMMANDS = {"apt list", "apt search", "list", "history", "env list", "apt sources", "doctor"}
DETAILS = ("install_id", "prefix", "path", "activation", "revision", "python", "python_version", "suite_id",
           "automatic", "description", "reason", "error", "report", "url", "suite", "component",
           "selected_suite", "selected_component")
PATH_FIELDS = {"path", "prefix", "activation", "report", "index", "home", "python", "executable", "source_command"}
COMMAND_FIELDS = {"argv", "command", "build", "install", "check", "probe", "dependency_check"}


def line(console, value="", *, style=None):
    # soft_wrap disables Rich's width-based folding/cropping. The terminal can
    # visually wrap a long path, but the output retains its original line bytes.
    console.print(Text(str(value), style=style), soft_wrap=True)


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
    if "automatic" in item:
        return "dependency" if item["automatic"] else "requested"
    if "root" in item:
        return "requested" if item["root"] else "dependency"
    return ""


def package_rows(console, packages, *, planned=False):
    """One row per instance; never fold or crop a package name or version."""
    rows = []
    for item in sorted(packages, key=lambda p: (package_kind(p) == "dependency", p["name"], str(p.get("version", "")))):
        state = ("archived" if item.get("archived") else "active" if item.get("active")
                 else "installed" if item.get("installed") else "planned" if planned
                 else item.get("status", "available"))
        if item.get("external") or item.get("provenance", {}).get("external"):
            state += ", external"
        rows.append([str(item["name"]), str(item.get("version") or "—"), package_kind(item) or "—", state])
    if not rows:
        line(console, "  No matching packages.")
        return
    headers = ["Package", "Version", "Kind", "State"]
    widths = [max(Text(row[i]).cell_len for row in [headers, *rows]) for i in range(4)]
    if sum(widths) + 8 > console.width:
        # Narrow SSH terminals retain complete identifiers on a single logical
        # line; terminal wrapping stays visual, so copying still works.
        for name, version, kind, state in rows:
            tags = "; ".join(v for v in (kind, state) if v != "—")
            line(console, f"  {name} {version} [{tags}]")
        return
    for index, row in enumerate([headers, *rows]):
        text = Text("  ")
        for column, value in enumerate(row):
            text.append(value, style="bold" if index == 0 else "cyan" if column == 0 else None)
            if column < 3:
                text.append(" " * (widths[column] - Text(value).cell_len + 2))
        console.print(text, soft_wrap=True)


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
    line(console, "APT packages · " + str(values.get("backend", "spack")), style="bold")
    packages = values.get("packages", [])
    package_summary(console, packages)
    line(console)
    package_rows(console, packages)
    if packages:
        line(console)
        line(console, "Details: jforge apt show <package>", style="dim")


def apt_transaction(console, command, values):
    preview = values.get("dry_run")
    line(console, f"APT {command.removeprefix('apt ')}: " + ("preview" if preview else str(values.get("status", "completed"))), style="bold")
    if "roots" in values:
        line(console, "Requests: " + (", ".join(values["roots"]) or "none"))
    if "packages" in values:
        packages = values["packages"]
        package_summary(console, packages)
        line(console)
        # A solver check shows the complete plan; installation summaries show
        # roots, rather than repeating every dependency after its live logs.
        selected = packages if command == "apt check" else [p for p in packages if package_kind(p) != "dependency"]
        package_rows(console, selected, planned=command == "apt check")
        line(console)
        line(console, "All packages: jforge apt list --installed" if command != "apt check" else "Plan only; no packages built or activated.", style="dim")
    for key in ("note", "activation", "plan", "path"):
        if values.get(key):
            field(console, "logs" if key == "path" else key, values[key])
    if values.get("store_retained"):
        line(console, "Package store retained for saved environment revisions.")


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


def _show_native_build(console, card):
    line(console, str(card.get("name", "build")), style="bold")
    platforms = card.get("platforms") or []
    if platforms:
        line(console, "Platform: " + _join(platforms))
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


def _show_legacy_build(console, card):
    workflow = card.get("Workflow") or {}
    line(console, str(workflow.get("name", "build")), style="bold")
    if workflow.get("platforms"):
        line(console, "Platform: " + _join(workflow["platforms"]))
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


def show_build(console, card, *, path, sha256, ordered=None):
    """Summarize a build card. JSON output keeps the original mapping."""
    if isinstance(card, dict) and "name" in card and "Workflow" not in card:
        _show_native_build(console, card)
    elif isinstance(card, dict) and "Workflow" in card:
        _show_legacy_build(console, card)
    else:
        line(console, "Build card", style="bold")
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
    line(console, "Spack recipes: " + str(values.get("status", "completed")), style="bold")
    for key in ("available", "repository_commit", "updated_at", "note", "operation_id", "path"):
        if key in values:
            field(console, "logs" if key == "path" else key, values[key])
    for key, value in values.items():
        if key not in {"available", "repository_commit", "updated_at", "note", "operation_id", "path", "status"}:
            field(console, key, value, compact=True)


def render(console, command, values):
    if command in {"apt list", "apt search"}:
        apt_packages(console, values)
        return
    if command in {"apt install", "apt reinstall", "apt remove", "apt upgrade", "apt full-upgrade", "apt satisfy", "apt check"}:
        apt_transaction(console, command, values)
        return
    if command == "apt update":
        apt_update(console, values)
        return
    if command == "apt switch" and "profile" in values:
        line(console, "APT environment: " + str(values["profile"]), style="bold")
        for key in ("source", "user_home", "apt_home", "packages", "activation", "status"):
            if key in values:
                field(console, key, values[key])
        if "path" in values:
            field(console, "logs", values["path"])
        return
    for key, value in values.items():
        field(console, key, value, compact=command in COMPACT_COMMANDS)


def error(console, prog, message, hint=None):
    text = message + ("\nTry: " + hint if hint else "")
    width = max(20, console.width - 8)
    if all(len(part) <= width for part in text.splitlines()):
        console.print(Panel(Text(text), title=Text(prog), border_style="red"))
        return
    console.print(Panel(Text("Command failed."), title=Text(prog), border_style="red"))
    line(console, message)
    if hint:
        line(console, "Try:", style="bold")
        line(console, hint)
