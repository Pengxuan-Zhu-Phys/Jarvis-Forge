"""Restore Forge-owned shell hook loaders."""

import os
from pathlib import Path
import shutil
import stat
import subprocess

from .package_operations import _inside
from .shell_repair import activation_lock
from .storage import ForgeError, new_id, write_json, write_text


_START = "# >>> jforge shell hook >>>"
_END = "# <<< jforge shell hook <<<"
_LOADER = r'''case $- in
    *i*)
        if [ "${_JFORGE_HOOK_LOADED-}" != 1 ] &&
            _jforge_hook=$(command jforge shell-init 2>/dev/null); then
            eval "$_jforge_hook" || :
        fi
        unset _jforge_hook
        ;;
esac
'''


def startup_files():
    shell = Path(os.environ.get("SHELL", "")).name
    home = Path.home()
    if shell == "zsh":
        return [(Path(os.environ.get("ZDOTDIR") or home) / ".zshrc", shell)]
    if shell == "bash":
        login = next((home / name for name in (".bash_profile", ".bash_login", ".profile")
                      if (home / name).is_file()), home / ".bash_profile")
        return [(home / ".bashrc", shell), (login, shell)]
    return []


def _edit(home, path, shell, transform, *, dry_run):
    path = path.resolve()

    def inspect():
        original = path.read_bytes().decode("utf-8") if path.exists() else None
        updated = transform(original or "")
        if updated is None:
            return None
        changed = updated != original
        report = {"activation": str(path), "dry_run": dry_run, "changed": changed,
                  "settings": [], "removed": 0, "unchecked": 0, "deferred": 0}
        if not changed:
            return report
        parser = shutil.which(shell)
        if not parser:
            raise ForgeError(f"{shell} is required to check {path}.")
        options = ["--noprofile", "--norc"] if shell == "bash" else ["-f"]
        env = {key: value for key, value in os.environ.items()
               if key not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"} and not key.startswith("BASH_FUNC_")}
        checked = subprocess.run([parser, *options, "-n"], input=updated, text=True, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
        if checked.returncode:
            raise ForgeError(f"Cannot repair shell hook in {path}:\n{checked.stderr.strip()}")
        if not dry_run:
            directory = _inside(home, home.root / "environments/hook-repairs" / new_id("hook", root=home.root / "environments/hook-repairs"))
            if original is not None:
                backup = directory / "original.sh"
                write_text(backup, original)
                report["backup"] = str(backup)
            report["report"] = str(directory / "report.json")
            write_json(directory / "report.json", report)
            mode = stat.S_IMODE(path.stat().st_mode) if original is not None else 0o600
            write_text(path, updated, mode=mode)
        return report

    if dry_run:
        return inspect()
    path.parent.mkdir(parents=True, exist_ok=True)
    with activation_lock(path):
        return inspect()


def repair_startup(home, path, shell, *, dry_run=False, install=False):
    """Replace the managed block, or register it when explicitly requested."""
    block = f"{_START}\n{_LOADER}{_END}\n"

    def update(original):
        lines = original.splitlines(keepends=True)
        for start, end in ((_START, _END),):
            starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == start]
            ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == end]
            if not starts and not ends:
                continue
            if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
                raise ForgeError(f"Incomplete or duplicate managed shell hook block: {path}")
            first, last = starts[0], ends[0]
            return "".join(lines[:first]) + block + "".join(lines[last + 1:])
        if install:
            return original + ("\n" if original and not original.endswith("\n") else "") + block
        return None

    # Leave an unregistered startup file alone during automatic discovery.
    if not install and (not path.is_file() or not any(
            marker.encode() in path.read_bytes() for marker in (_START, _END))):
        return None
    return _edit(home, path, shell, update, dry_run=dry_run)
