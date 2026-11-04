from __future__ import annotations

import pytest

from jarvis_forge import catalog
from jarvis_forge.dependencies import native, matches


@pytest.mark.parametrize('constraint', ['libtinfo6>=6', 'sqlite3==3.45.1', 'pkg>=1.0-1', 'pkg=1:2.0-1', 'g++>=11', 'pkg>=1,<3'])
def test_forge_requirements_do_not_mutate_third_party_parser(monkeypatch,constraint):
    parser=catalog.Requirement
    # Model both a slotted and read-only upstream parser. Only public read
    # access to specifier/extras/marker/url is available to Forge.
    class ReadOnlyRequirement:
        __slots__=('_parsed',)
        def __init__(self,text):object.__setattr__(self,'_parsed',parser(text))
        def __getattr__(self,name):return getattr(self._parsed,name)
        def __setattr__(self,name,value):raise AttributeError('Third-party parser is read-only')
    monkeypatch.setattr(catalog,'Requirement',ReadOnlyRequirement)
    parsed=catalog.requirement(constraint)
    assert isinstance(parsed,catalog.PackageRequirement)
    assert parsed.name==constraint.split('=')[0].split('>')[0]
    request=native(constraint,root=True)
    assert request['name']==parsed.name


def test_debian_revision_keeps_its_domain_with_forge_owned_metadata():
    parsed=catalog.requirement('libtinfo6>=6.4-1')
    assert parsed.forge_debian_constraint==('>=','6.4-1')
    candidate={'name':'libtinfo6','version':'6.4','provenance':{'kind':'official-apt-source','debian_version':'6.4-2'}}
    assert matches(candidate,native('libtinfo6>=6.4-1',root=True))


@pytest.mark.parametrize('timezone',[False,True])
def test_generated_tcl_probe_sends_real_lines_to_the_interpreter(tmp_path,timezone):
    import json,subprocess,sys
    from jarvis_forge.upstream_adapters import _tcl_check
    # Execute the generated Python check; the interpreter stub validates the
    # bytes on stdin, as Tcl would, instead of only inspecting recipe strings.
    program='puts [info patchlevel]\n'
    reply='8.6.14\n'
    if timezone:
        program+='puts [clock format 0 -timezone UTC -format %Y]\n';reply+='1970\n'
    interpreter=tmp_path/'tclsh8.6'
    interpreter.write_text('#!'+sys.executable+'\nimport sys\nassert sys.stdin.read()=='+repr(program)+'\nsys.stdout.write('+repr(reply)+')\n')
    interpreter.chmod(0o755)
    command=_tcl_check(str(interpreter),timezone=timezone)
    result=subprocess.run([sys.executable,*command[1:]],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
