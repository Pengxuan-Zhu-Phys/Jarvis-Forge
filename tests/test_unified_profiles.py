"""Build platform suites once, bind external Python, and isolate project profiles."""
from pathlib import Path
import json
import os
import shlex
import shutil
import subprocess
import sys
import pytest
from jarvis_forge import unified_profiles as profiles,toolchains,spack_backend,apt_profiles
from jarvis_forge.storage import ForgeHome,ForgeError,read_json,write_json,write_text
from profile_fixtures import project_profile

LINUX={'platform':'linux','system':'almalinux','version':'9.9','machine':'x86_64','libc':'glibc 2.34'}


@pytest.fixture
def suite(tmp_path,monkeypatch):
    base=ForgeHome(tmp_path/'forge');base.initialize();calls=[]
    monkeypatch.setattr(profiles,'host',lambda:LINUX)
    monkeypatch.setenv('TMPDIR',str(tmp_path))
    monkeypatch.setattr(spack_backend,'bootstrap',lambda *args,**kwargs:None)
    def install(scope,action,specs,**kwargs):
        calls.append((scope,specs,kwargs));packages=[]
        for spec in specs:
            name,version=spec.split()[0].split('@=');prefix=scope.root/'pkg'/f'{name}-{version}'
            write_json(prefix/'.spack/spec.json',{'spec':spec})
            for binary in toolchains.DRIVERS.values() if name=='gcc' else toolchains.TOOL_COMMANDS[name]:
                write_text(prefix/'bin'/binary, '#!/bin/sh\nprintf "%s\\n" '+shlex.quote(binary+' '+version)+'\n',mode=0o700)
            packages.append({'name':name,'version':version,'root':True,'external':False,'prefix':str(prefix)})
        return {'packages':packages,'path':str(scope.root)}
    monkeypatch.setattr(spack_backend,'transact',install)
    def inspect(record):
        library=Path(record['prefix'])/'lib64/libgcc_s.so.1';library.parent.mkdir(exist_ok=True);library.touch()
        return dict(os.environ),{library.name:library}
    monkeypatch.setattr(toolchains,'_inspect',inspect)
    return base,calls


def test_named_platform_builds_pinned_suite_in_own_store_and_reuses_it(suite):
    base,calls=suite
    platform=profiles.prepare_platform(base,'ubuntu24-alma',preset='ubuntu24')
    assert platform['preset']=='ubuntu24' and platform['host']==LINUX
    assert Path(platform['stage']).is_relative_to(base.root.parent/f'jforge-stages-{os.getuid()}'/'platforms')
    assert platform['toolchain']['deprecated_versions']==profiles.presets()['ubuntu24']['versions']
    assert Path(platform['toolchain']['prefix']).name=='gcc-13.3.0'
    assert calls[0][2]['deprecated_versions']==profiles.presets()['ubuntu24']['versions']
    assert calls[0][2]['target']=='x86_64'
    assert profiles.prepare_platform(base,'ubuntu24-alma')==platform and len(calls)==1
    assert (base.root/'platforms/ubuntu24-alma/bin/gfortran').is_symlink()
    assert not list((base.root/'profiles').glob('*/metadata.json'))


def test_changed_platform_target_or_compiler_is_rejected(suite):
    base,_=suite
    data=profiles.prepare_platform(base,'ubuntu24')
    with pytest.raises(ForgeError,match='uses target'):profiles.prepare_platform(base,'ubuntu24',target='x86_64_v3')
    Path(data['toolchain']['prefix'],'.spack/spec.json').write_text('changed')
    with pytest.raises(ForgeError,match='changed'):profiles.prepare_platform(base,'ubuntu24')


def test_invalid_stage_and_unregistered_platform_are_not_adopted(suite,tmp_path):
    base,_=suite
    with pytest.raises(ForgeError,match='stage is not writable'):profiles.prepare_platform(base,'ubuntu24',stage=tmp_path/'absent')
    assert not (base.root/'platforms/ubuntu24').exists()
    occupied=base.root/'platforms/ubuntu24';occupied.mkdir();(occupied/'user-file').write_text('keep')
    with pytest.raises(ForgeError,match='Unregistered'):profiles.prepare_platform(base,'ubuntu24')
    assert (occupied/'user-file').read_text()=='keep'


def test_failed_platform_records_failure_without_changing_selection(suite,monkeypatch):
    base,_=suite;pointer=base.root/'profiles/selection.json'
    write_json(pointer,{'schema_version':3,'hosts':{}});before=pointer.read_bytes()
    def fail(*args,**kwargs):raise ForgeError('compiler build failed')
    monkeypatch.setattr(toolchains,'install_platform',fail)
    with pytest.raises(ForgeError,match='compiler build failed'):profiles.prepare_platform(base,'ubuntu24')
    assert read_json(base.root/'platforms/ubuntu24/metadata.json')['status']=='failed'
    assert pointer.read_bytes()==before
    with pytest.raises(ForgeError,match='incomplete'):profiles.prepare_platform(base,'ubuntu24')


@pytest.mark.parametrize('shell',['bash','zsh'])
def test_profile_activation_switch_preserves_pyenv_and_user_added_paths(tmp_path,shell):
    if not shutil.which(shell):pytest.skip('shell unavailable')
    base=ForgeHome(tmp_path/'forge');first=project_profile(base,'cbs');second=project_profile(base,'gambit')
    for scope in (first,second):
        write_text(scope.root/'spack/runtime/activate.sh',f'export PATH={shlex.quote(str(scope.root/"pkg/toy-1/bin"))}${{PATH:+:$PATH}}\nexport SAMPLE={scope.root.name}\n')
        profiles.refresh_activation(scope)
    code='''set -e
before=$(python3 -c 'import sys;print(sys.executable,sys.prefix,sys.base_prefix)')
source "$1"
export PATH="/user/extra:$PATH"
source "$2"
source "$2"
test "$before" = "$(python3 -c 'import sys;print(sys.executable,sys.prefix,sys.base_prefix)')"
printf '%s\\n%s\\n' "$PATH" "$SAMPLE"
_jforge_env_restore
printf '%s\\n' "$PATH"
'''
    result=subprocess.run([shell,'-c',code,'test',str(first.root/'activate.sh'),str(second.root/'activate.sh')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    path,name,restored=result.stdout.splitlines()
    assert 'profiles/cbs/pkg' not in path and path.count('profiles/gambit/pkg')==1 and '/user/extra' in path
    assert name=='gambit' and '/user/extra' in restored and 'profiles/gambit/pkg' not in restored


def test_external_apps_are_not_loaded_and_queries_do_not_refresh_files(tmp_path,capsys):
    from jarvis_forge.cli import main
    base=ForgeHome(tmp_path/'forge');scope=project_profile(base)
    external=tmp_path/'external.sh';external.write_text('exit 91\n')
    data=profiles.record(scope);data['apps']=[{'name':'external','activation':str(external),'enabled':True,'external':True}]
    write_json(scope.root/'metadata.json',data);profiles.refresh_activation(scope)
    assert str(external) not in (scope.root/'activate.sh').read_text()
    before={file:(file.read_bytes(),file.stat().st_mtime_ns) for file in base.root.rglob('*') if file.is_file()}
    for command in (['profile'],['profile','show','default'],['profile','show','--path'],['env','current','--path']):
        assert main(['--home',str(base.root),*command])==0,capsys.readouterr()
        capsys.readouterr()
    assert all(file.read_bytes()==value[0] and file.stat().st_mtime_ns==value[1] for file,value in before.items())
    assert {file for file in base.root.rglob('*') if file.is_file()}==set(before)


def test_selected_python_is_checked_before_apt_mutation(tmp_path,monkeypatch):
    base=ForgeHome(tmp_path/'forge');scope=project_profile(base)
    data=profiles.record(scope);data['python']['version']='0.0.0';write_json(scope.root/'metadata.json',data)
    before=(base.root/'profiles/selection.json').read_bytes()
    with pytest.raises(ForgeError,match='External Python changed'):apt_profiles.ensure_active(base)
    assert (base.root/'profiles/selection.json').read_bytes()==before


def test_select_missing_profile_does_not_create_a_profile_directory(tmp_path):
    base=ForgeHome(tmp_path/'missing')
    with pytest.raises(ForgeError,match='No profile named'):profiles.set_default(base,'absent')
    assert not base.root.exists()


def test_profile_selection_revalidates_after_taking_namespace_lock(tmp_path,monkeypatch):
    from contextlib import contextmanager
    import jarvis_forge.paths as paths
    base=ForgeHome(tmp_path/'forge');scope=project_profile(base)
    real=paths.directory_lock
    @contextmanager
    def concurrent_remove(parent,name='.sequence.lock'):
        with real(parent,name):
            if name=='.profiles.lock':shutil.rmtree(scope.root)
            yield
    monkeypatch.setattr(paths,'directory_lock',concurrent_remove)
    with pytest.raises(ForgeError,match='No profile named'):profiles.set_default(base,'default')
    assert not scope.root.exists()
