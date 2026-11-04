"""Exercise the interactive SSH launch path missed by pipe-only acceptance."""

import os
from pathlib import Path
import pty
import subprocess
import sys

from jarvis_forge.storage import read_json


def test_detached_package_command_does_not_inherit_interactive_stdin(tmp_path):
    env_script = tmp_path / "context.sh"
    env_script.write_text("export FIXTURE_CONTEXT=active\n")
    child_code = """import os,sys
assert os.getsid(0)==os.getpid(), "package command must retain its isolated session"
assert not sys.stdin.isatty(), "package command inherited a terminal from a different session"
assert sys.stdin.read()=="", "noninteractive command must receive EOF"
assert os.environ["FIXTURE_CONTEXT"]=="active"
print("stdout-ok",flush=True)
print("stderr-ok",file=sys.stderr,flush=True)
"""
    parent_code = """import sys
from pathlib import Path
from jarvis_forge.package_operations import run_command
assert sys.stdin.isatty(), "the wrapper must be launched from a terminal"
run_command([sys.executable,"-c",sys.argv[2]],Path(sys.argv[1]),Path(sys.argv[1])/"context.sh",Path(sys.argv[1]),"terminal",5,progress=lambda message:None)
print("wrapper-ok")
"""
    master, slave = pty.openpty()
    try:
        inherited = dict(os.environ)
        inherited["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [sys.executable, "-c", parent_code, str(tmp_path), child_code],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=inherited,
            timeout=15,
            text=True,
        )
    finally:
        os.close(slave)
        os.close(master)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "wrapper-ok\n"
    assert "stdout-ok" in result.stderr and "stderr-ok" in result.stderr
    assert (tmp_path / "command-terminal.stdout.log").read_text() == "stdout-ok\n"
    assert (tmp_path / "command-terminal.stderr.log").read_text() == "stderr-ok\n"
    record = read_json(tmp_path / "command-terminal.json")
    assert record["status"] == "passed"
    assert record["stdin"] == "devnull"
