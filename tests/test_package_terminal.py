"""Exercise the interactive SSH launch path missed by pipe-only acceptance."""

import os
from pathlib import Path
import pty
import subprocess
import sys

import pytest

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


@pytest.mark.parametrize('command',['profile','apt'])
def test_install_progress_survives_plan_printing_in_a_real_terminal(tmp_path,command):
    import errno
    import re
    import select
    import time
    script=tmp_path/'terminal-install.py'
    script.write_text('''import sys
from pathlib import Path
from jarvis_forge import apt_cli,spack_backend,unified_profiles
from jarvis_forge.cli import main
from jarvis_forge.package_operations import run_command
work=Path(sys.argv[1]); mode=sys.argv[2]
context=work/'environment.sh'; context.write_text('true\\n')
def perform(progress,confirm):
    assert progress.live is not None
    progress('[1/4] Resolve dependencies')
    progress('[2/4] Check installation plan')
    confirm({'install':[{'name':'alpha','version':'1.0'}]})
    assert not progress.closed and progress.live.live.is_started
    progress('[3/4] Install packages')
    progress.installation_plan([dict(name='alpha',version='1.0',hash='abcdefg123',root=True,external=False,installed=False)])
    code="import time; print('[ ] abcdefg alpha@1.0 build (0s)',flush=True); time.sleep(0.6); print('[+] abcdefg alpha@1.0 /prefix (1s)',flush=True)"
    run_command([sys.executable,'-c',code],work,context,work,'install',5,progress=progress)
    assert progress.live.live.is_started and not progress.closed
    progress('[4/4] Save environment')
if mode=='profile':
    def create(home,name,**kw):
        perform(kw['progress'],kw['confirm'])
        return dict(id=name,status='ready',activation=str(work/'activate.sh'))
    unified_profiles.create=create
    args=['profile','create','ubuntu24']
else:
    apt_cli.ensure_active=lambda home,**kw:home
    def install(home,action,specs,**kw):
        perform(kw['progress'],kw['confirm'])
        return dict(status='passed',packages=[])
    spack_backend.transact=install
    args=['apt','install','alpha']
raise SystemExit(main(['--home',str(work/'forge'),*args]))
''')
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'),TERM='xterm-256color',COLUMNS='90',LINES='30')
    env.pop('NO_COLOR',None)
    master,slave=pty.openpty();output=bytearray();process=None
    try:
        process=subprocess.Popen([sys.executable,str(script),str(tmp_path),command],stdin=slave,stdout=slave,stderr=slave,env=env,cwd=tmp_path)
        os.close(slave);slave=None
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if select.select([master],[],[],0.2)[0]:
                try:data=os.read(master,65536)
                except OSError as exc:
                    if exc.errno==errno.EIO:break
                    raise
                if not data:break
                output.extend(data)
        else:
            pytest.fail('terminal progress child timed out')
        assert process.wait(timeout=2)==0,output.decode(errors='replace')
    finally:
        if process is not None and process.poll() is None:
            process.kill();process.wait()
        if slave is not None:os.close(slave)
        os.close(master)
    text=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',output.decode(errors='replace'))
    assert '1 running' in text and 'alpha@1.0' in text and 'Activity' in text
    assert 'Install: alpha@1.0' in text and '━' in text and 'Traceback' not in text
