"""Two-layer profiles use one platform and independent current environments."""

from pathlib import Path
import json
import os
import sys

import pytest
import yaml

from jarvis_forge import unified_profiles as profiles, apt_profiles, spack_backend
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeHome, ForgeError, write_json, read_json


@pytest.fixture
def model(tmp_path,monkeypatch):
    base=ForgeHome(tmp_path/'forge');base.initialize()
    calls=[]
    monkeypatch.setenv('TMPDIR',str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path/'preferences'))
    from jarvis_forge import toolchains
    monkeypatch.setattr(toolchains,'_verify',lambda value:value)
    def prepare(base,name,**kwargs):
        path=base.root/'platforms'/name
        if (path/'metadata.json').is_file():return read_json(path/'metadata.json')
        calls.append(name)
        path.mkdir(parents=True)
        script=path/'activate.sh';script.write_text(':\n')
        record={'schema_version':3,'kind':'platform','id':name,'preset':kwargs.get('preset') or name,
                'revision':1,'backend':'spack','host':profiles.host(),'target':'x86_64' if profiles.host()['machine']=='x86_64' else 'aarch64',
                'status':'ready','activation':str(script),'toolchain':{'id':name,'drivers':{'CC':'/usr/bin/cc','CXX':'/usr/bin/c++'},
                    'tools':{},'version':'13.3.0','prefix':str(path/'pkg/gcc-13.3.0'),'target':'x86_64'}}
        write_json(path/'metadata.json',record)
        return record
    monkeypatch.setattr(profiles,'prepare_platform',prepare)
    def bootstrap(home,**kwargs):
        runtime=home.root/'spack/runtime';runtime.mkdir(parents=True)
        (runtime/'activate.sh').write_text(':\n')
        (home.root/'spack/spack.yaml').write_text('spack: {specs: []}\n')
        write_json(home.root/'spack/backend.json',{'schema_version':1,'backend':'spack','python':profiles._python()})
        write_json(home.root/'packages.json',{'activation':str(runtime/'activate.sh'),'backend':'spack',
                    'spack':{'roots':[],'packages':[],'activation':str(runtime/'activate.sh'),'environment':str(home.root/'spack')}})
    monkeypatch.setattr(spack_backend,'bootstrap',bootstrap)
    monkeypatch.setattr(spack_backend,'_executable',lambda home:Path(sys.executable))
    return base,calls


def test_profiles_share_platform_but_have_independent_state_and_scripts(model):
    base,calls=model
    first=profiles.create(base,'cbs',platform='ubuntu24')
    second=profiles.create(base,'gambit',platform='ubuntu24')
    assert calls==['ubuntu24']
    assert first['platform']==second['platform']=='ubuntu24'
    for name in ('cbs','gambit'):
        scope=base.root/'profiles'/name
        assert (scope/'metadata.json').is_file() and (scope/'activate.sh').is_file()
        assert (scope/'spack/spack.yaml').is_file()
        assert not (scope/'apt').exists()
        assert str(base.root/'platforms/ubuntu24/activate.sh') in (scope/'activate.sh').read_text()
    assert not profiles.selection_path(base).exists()
    profiles.set_default(base,'cbs')
    assert apt_profiles.active_home(base).root==base.root/'profiles/cbs'
    assert list(read_json(base.root/'profiles/selection.json')['hosts'].values())[0]['id']=='cbs'
    assert not (base.root/'apt/selection.json').exists()


def test_namespace_collision_and_duplicate_profile_are_rejected(model):
    base,_=model
    profiles.create(base,'cbs',platform='ubuntu24')
    with pytest.raises(ForgeError,match='already exists'):profiles.create(base,'cbs',platform='ubuntu24')
    with pytest.raises(ForgeError,match='namespace'):profiles.create(base,'ubuntu24',platform='other')
    with pytest.raises(ForgeError,match='namespace'):profiles._namespace(base,'cbs','platform')


def test_host_compatibility_uses_arch_glibc_and_target_not_hostname(monkeypatch):
    host={'platform':'linux','system':'ubuntu','version':'24.04','machine':'x86_64','libc':'glibc 2.35'}
    record={'id':'ubuntu24','preset':'ubuntu24','host':host,'target':'x86_64'}
    profiles.compatible(record,{**host,'system':'rocky','version':'9','libc':'glibc 2.36'})
    with pytest.raises(ForgeError,match='glibc'):profiles.compatible(record,{**host,'libc':'glibc 2.34'})
    with pytest.raises(ForgeError,match='requires'):profiles.compatible(record,{**host,'machine':'aarch64'})
    monkeypatch.setattr(profiles,'_target',lambda current,target:(_ for _ in ()).throw(ForgeError('CPU unavailable')))
    with pytest.raises(ForgeError,match='CPU unavailable'):profiles.compatible(record,host)


def test_broken_backend_never_falls_back_and_default_is_preserved(model):
    base,_=model
    profiles.create(base,'cbs',platform='ubuntu24');profiles.set_default(base,'cbs')
    pointer=(base.root/'profiles/selection.json').read_bytes()
    scope=apt_profiles.active_home(base)
    (scope.root/'spack/backend.json').unlink()
    with pytest.raises(ForgeError,match='incomplete'):apt_profiles.ensure_active(base)
    assert (base.root/'profiles/selection.json').read_bytes()==pointer
    assert not (base.root/'profiles/spack-default').exists()


def test_old_layout_is_refused_without_modification(tmp_path):
    base=ForgeHome(tmp_path/'old')
    write_json(base.root/'apt/selection.json',{'id':'spack-default'})
    before=list(base.root.rglob('*'))
    with pytest.raises(ForgeError,match='Unsupported profile layout'):apt_profiles.active_home(base)
    assert list(base.root.rglob('*'))==before


def test_check_and_list_are_read_only_without_automatic_gcc_bootstrap(tmp_path,monkeypatch,capsys):
    base=tmp_path/'absent'
    monkeypatch.setattr(profiles,'_prerequisites',lambda *args,**kwargs:('x86_64',{}))
    assert main(['--home',str(base),'--json','profile'])==0
    assert json.loads(capsys.readouterr().out)['instances']==[]
    assert main(['--home',str(base),'--json','profile','check','ubuntu24'])==0
    assert json.loads(capsys.readouterr().out)['profiles'][0]['status']=='buildable'
    assert not base.exists()
    monkeypatch.setattr(profiles,'host',lambda:{'platform':'linux','system':'almalinux','version':'9','machine':'x86_64','libc':'glibc 2.34'})
    with pytest.raises(ForgeError,match='No project profile'):apt_profiles.ensure_active(ForgeHome(base))
    assert not base.exists()
