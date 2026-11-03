from __future__ import annotations
import gzip,hashlib,json,subprocess
from pathlib import Path
import pytest
import yaml
from jarvis_forge import official
from jarvis_forge.apt_profiles import active_home, switch, profiles
from jarvis_forge.cli import main
from jarvis_forge.packages import install, environment_path
from jarvis_forge.storage import ForgeHome, write_json, ForgeError
from test_apt import recipe


@pytest.fixture
def profile_base(tmp_path, monkeypatch):
    base=ForgeHome(tmp_path/'user home');base.initialize()
    def imported(home, *, origin,suite,component='main',keyring=None):
        source=official._selector({'origin':origin,'suite':suite,'component':component,'keyring':None})
        path=home.root/'cache/index.gz';path.write_bytes(gzip.compress(b''))
        official._save_repository(home,source,{'index':str(path),'index_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        write_json(home.root/'apt/official.json',{'schema_version':1,'source':source,'imports':[]})
        return {'status':'passed'}
    monkeypatch.setattr(official,'import_repository',imported)
    return base


def test_switch_isolates_instances_and_preserves_user_directory(profile_base,tmp_path,capsys):
    base=profile_base
    recipe(base,tmp_path,'old')
    install(base,['old'],official=False)
    old_activation=environment_path(base)
    assert main(['--home',str(base.root),'apt','switch','ubuntu','--json'])==0
    switched=json.loads(capsys.readouterr().out)
    target=active_home(base)
    assert target!=base and target.root.is_relative_to(base.root)
    assert switched['source']['components']==['main','universe']
    assert switched['source']['pockets']==['updates','security']
    assert main(['--home',str(base.root),'list','--json'])==0
    assert json.loads(capsys.readouterr().out)['packages']==[]
    assert main(['--home',str(base.root),'env','list','--json'])==0
    assert [v['name'] for v in json.loads(capsys.readouterr().out)['environments']]==['apt']
    assert main(['--home',str(base.root),'paths','--json'])==0
    assert json.loads(capsys.readouterr().out)['packages']==str(target.root/'packages')
    assert Path(old_activation).is_file() and base.packages()[0]['name']=='old'
    assert switch(base,'legacy')['apt_home']==str(base.root)
    assert active_home(base)==base


def test_switch_and_repeated_source_clean_paths_and_restore_scalars(profile_base,tmp_path):
    base=profile_base
    recipe(base,tmp_path,'old')
    first=install(base,['old'],official=False)
    switch(profile_base,'ubuntu')
    target=active_home(base)
    recipe(target,tmp_path,'new')
    second=install(target,['new'],official=False)
    for shell in ('bash','zsh'):
        import shutil
        if not shutil.which(shell):continue
        proc=subprocess.run([shell,'-c', '''
export PATH=/usr/bin:/bin
source "$1"
export PATH="$PATH:/user/extra"
source "$2"
source "$2"
printf '%s\\n%s\\n%s\\n' "$PATH" "${OLD_ROOT-unset}" "$NEW_ROOT"
''','check',first['activation'],second['activation']],capture_output=True,text=True)
        assert proc.returncode==0,proc.stderr
        paths,old,new=proc.stdout.splitlines()
        assert first['packages'][0]['prefix'] not in paths
        assert paths.count(second['packages'][0]['prefix']+'/bin')==1
        assert '/user/extra' in paths and old=='unset' and new==second['packages'][0]['prefix']


def test_failed_switch_preserves_selection(profile_base,monkeypatch):
    switch(profile_base,'ubuntu')
    before=active_home(profile_base)
    def fail(*args,**kwargs):raise ForgeError('index unavailable',code=1)
    monkeypatch.setattr(official,'import_repository',fail)
    with pytest.raises(ForgeError,match='index unavailable'):
        switch(profile_base,'debian')
    assert active_home(profile_base)==before


def test_switch_empty_environment_removes_previous_exports(profile_base,tmp_path):
    recipe(profile_base,tmp_path,'old')
    result=install(profile_base,['old'],official=False)
    switched=switch(profile_base,'ubuntu')
    p=subprocess.run(['bash','-c','source "$1"; source "$2"; printf "%s\\n%s" "$PATH" "${OLD_ROOT-unset}"','check',result['activation'],switched['activation']],text=True,capture_output=True)
    assert p.returncode==0,p.stderr
    assert result['packages'][0]['prefix'] not in p.stdout and p.stdout.endswith('unset')


def test_existing_scope_retains_keyring_and_refuses_import_identity_change(profile_base,monkeypatch):
    switch(profile_base,'ubuntu')
    target=active_home(profile_base)
    saved=json.loads((target.root/'apt/official.json').read_text())
    saved['source']['keyring']='/trusted/keyring.gpg'
    write_json(target.root/'apt/official.json',saved)
    def fail_import(*args,**kwargs):raise AssertionError('Unchanged scope should reuse its verified indexes')
    monkeypatch.setattr(official,'import_repository',fail_import)
    assert switch(profile_base,'ubuntu')['source']['keyring']=='/trusted/keyring.gpg'
    # Direct imports, including into an empty scope, cannot repurpose its identity.
    monkeypatch.undo()
    from jarvis_forge.official import import_repository
    with pytest.raises(ForgeError,match='another source selection'):
        import_repository(target,origin='debian',suite='bookworm')
    assert active_home(profile_base)==target


@pytest.mark.parametrize('arguments,expected', [('apt search install',False),('apt install toy --dry-run',False),('apt switch --help',False),('apt switch',False),('apt switch ubuntu',True),('apt install toy',True)])
def test_shell_hook_only_refreshes_after_changes(tmp_path,capsys,arguments,expected):
    assert main(['--home',str(tmp_path/'home'),'apt','shell-init'])==0
    hook=capsys.readouterr().out
    fake=tmp_path/'jforge'
    fake.write_text('#!/bin/bash\nif [ "$1 $2 $3" = "apt env --path" ]; then printf "%s\\n" "$ACTIVATION"; fi\n')
    fake.chmod(0o755)
    activation=tmp_path/'activate.sh';activation.write_text('printf "activated\\n"\n')
    script='export PATH="$1:$PATH" ACTIVATION="$2"; eval "$3" >/dev/null; jforge '+arguments
    proc=subprocess.run(['bash','-c',script,'test',str(tmp_path),str(activation),hook],capture_output=True,text=True)
    assert proc.returncode==0,proc.stderr
    assert ('activated' in proc.stdout)==expected


def test_switch_can_recover_a_missing_selected_scope(profile_base,capsys):
    write_json(profile_base.root/'apt/selection.json',{'schema_version':1,'id':'missing'})
    assert main(['--home',str(profile_base.root),'apt','switch','legacy','--json'])==0
    assert json.loads(capsys.readouterr().out)['profile']=='legacy'
    assert active_home(profile_base)==profile_base
