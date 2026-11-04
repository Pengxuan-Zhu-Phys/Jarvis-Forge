"""Text presentation for SSH terminals; machine-readable results stay in cli.emit."""
from __future__ import annotations

import shlex
import textwrap

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


def apt_update(console, values):
    pending = values.get("pending_imports", [])
    status = values.get("status", "completed")
    suffix = f"; {len(pending)} pending" if pending else ""
    line(console, f"APT update: {status}{suffix}", style="bold")
    for key in ("official_source", "available", "source_packages", "updated_at"):
        if key in values:
            if key == "available":
                line(console, f"Recipes available: {values[key]}")
            elif key == "source_packages" and isinstance(values[key], int):
                line(console, f"Source packages: {values[key]:,}")
            else:
                field(console, key, values[key])
    imports = values.get("official_imports", [])
    line(console)
    line(console, f"Official imports ({len(imports)}):", style="bold")
    for item in imports:
        # Successful update rows need an identity/version/status, while failure
        # details are printed in the dedicated pending section below.
        summary = {key: item[key] for key in ("name", "debian_version", "version", "import_status", "status") if key in item}
        record(console, summary, indent=2)
    if not imports:
        line(console, "  none")
    if pending:
        line(console)
        field(console, "pending_imports", pending)
    inactive = values.get("inactive_imports", [])
    if inactive:
        line(console)
        line(console, f"Inactive automatic imports ({len(inactive)}):", style="bold")
        line(console, "  " + ", ".join(item["name"] for item in inactive))
        line(console, "  Unused by resolved root packages; saved for later reactivation.")
    line(console)
    if "operation_id" in values:
        field(console, "operation_id", values["operation_id"])
    if "path" in values:
        field(console, "logs", values["path"])
    if "index" in values:
        field(console, "index", values["index"])
    consumed = {"official_source", "available", "source_packages", "updated_at", "official_imports", "pending_imports", "inactive_imports", "operation_id", "path", "index", "status"}
    for key, value in values.items():
        if key not in consumed:
            field(console, key, value, compact=True)


def render(console, command, values):
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
    summary = textwrap.shorten(message.replace("\n", " "), width=width, placeholder="...")
    console.print(Panel(Text(summary), title=Text(prog), border_style="red"))
    line(console, "Details:", style="bold")
    line(console, message)
    if hint:
        line(console, "Try:", style="bold")
        line(console, hint)
