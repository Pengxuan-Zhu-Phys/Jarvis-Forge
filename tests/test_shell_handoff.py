"""The command owns shell changes; the hook only replays successful handoffs."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from jarvis_forge import shell_handoff
from jarvis_forge.cli import main
from jarvis_forge.environment_registry import add
from jarvis_forge.storage import ForgeHome


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_real_cli_switch_and_detach_use_fd_handoff_and_preserve_python(tmp_path, monkeypatch, capsys, shell):
    if not shutil.which(shell):
        pytest.skip("shell is not installed")
    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "preferences"))
    script = tmp_path / "user's environment.sh"
    script.write_text("export FIXTURE_HANDOFF=enabled\n")
    add(home, "example", script)
    assert main(["--home", str(home.root), "shell-init"]) == 0
    hook = capsys.readouterr().out
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "jforge"
    launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -m jarvis_forge \"$@\"\n")
    launcher.chmod(0o700)
    options = ["--noprofile", "--norc"] if shell == "bash" else ["-f"]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src"), "TMPDIR": str(tmp_path)}
    code = r'''
export PATH="$1:$PATH"
eval "$3"
before=$(command -v python3)
jforge env switch example --home "$2"
printf 'switch %s\n' "${FIXTURE_HANDOFF-unset}"
jforge --home="$2" env detach
printf 'detach %s\n' "${FIXTURE_HANDOFF-unset}"
test "$before" = "$(command -v python3)" || exit 31
jforge --home "$2" --json env switch example
printf 'json %s\n' "${FIXTURE_HANDOFF-unset}"
'''
    result = subprocess.run([shell, *options, "-ic", code, "test", str(bin_dir), str(home.root), hook],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "switch enabled\n" in result.stdout and "detach unset\n" in result.stdout
    assert result.stdout.endswith("json unset\n")
    start = result.stdout.index('{\n  "ok"')
    payload = json.loads(result.stdout[start:result.stdout.rindex("\njson unset")])
    assert payload["ok"] and payload["selected"] == "example"
    assert not list(tmp_path.glob("jforge-shell.*"))


@pytest.mark.parametrize("mode", ["json", "dry-run", "read", "targeted-repair", "app-list"])
def test_non_mutating_result_never_resolves_home_or_hands_off(monkeypatch, mode):
    monkeypatch.setenv("_JFORGE_SHELL_HANDOFF", "1")
    class Settings:
        as_json = mode == "json"
        @property
        def home(self):
            pytest.fail("a read/preview resolved the Forge home for shell activation")
    ctx = SimpleNamespace(obj=Settings(), params={"dry_run": mode == "dry-run",
                          "shell": "/external/activate.sh" if mode == "targeted-repair" else None})
    command = {"read": "version", "targeted-repair": "env repair", "app-list": "apt profile apps"}.get(mode, "env detach")
    shell_handoff.for_result(ctx, command, {})


def test_hook_has_no_argument_interpretation():
    # Replay language is separate from the native command argv. A new command
    # needs no corresponding shell branch or package-name special case.
    assert 'for _jf_arg in "$@"' not in shell_handoff.HOOK
    assert "--dry-run" not in shell_handoff.HOOK and "--json" not in shell_handoff.HOOK
    assert "build:run" not in shell_handoff.HOOK and "apt install" not in shell_handoff.HOOK
