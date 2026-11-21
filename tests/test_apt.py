"""Package CLI vocabulary, read-only queries and explicit profile boundaries."""
import json
import pytest
from jarvis_forge.cli import main

pytestmark=pytest.mark.usefixtures('linux_apt')

@pytest.mark.parametrize('command',['','list','search','show','install','remove','autoremove','update','upgrade','sources'])
def test_apt_help(command,capsys,tmp_path):
    home=tmp_path/'absent'
    assert main(['--home',str(home),'apt',*([command] if command else []),'-h'])==0
    out=capsys.readouterr().out
    assert 'Usage: jforge apt' in out and '\x1b[' not in out
    assert not home.exists()

@pytest.mark.parametrize('command,hint',[('reinstall','install'),('full-upgrade','upgrade'),('edit-sources','sources'),
    ('satisfy','install'),('env','profile show'),('import','profile create'),('switch','profile use'),
    ('profile','profile'),('check','install'),('compilers','profile show'),('shell-init','shell-init')])
def test_legacy_commands_error_with_replacement(command,hint,tmp_path,capsys):
    home=tmp_path/'absent'
    assert main(['--home',str(home),'--json','apt',command])==2
    value=json.loads(capsys.readouterr().out)
    assert hint in value['error']['hint']
    assert not home.exists()

def test_exact_package_verbs_and_offline_queries(tmp_path,capsys):
    home=tmp_path/'absent';args=['--home',str(home),'--json','apt']
    assert main(args)==0
    assert set(json.loads(capsys.readouterr().out)['commands'])=={'list','search','show','install','remove','autoremove','update','upgrade','sources'}
    for query in (['list'],['list','--available'],['search','linear']):
        assert main([*args,*query])==0
        assert json.loads(capsys.readouterr().out)['ok']
    assert not home.exists()

def test_list_defaults_to_installed_packages(tmp_path,capsys,monkeypatch):
    from jarvis_forge import spack_backend
    installed=[{'name':'eigen','version':'5.0.1'},{'name':'gsl','version':'2.8'}]
    available=[{'name':'eigen','status':'available'},{'name':'root','status':'available'}]
    monkeypatch.setattr('jarvis_forge.storage.ForgeHome.packages',lambda home:installed)
    monkeypatch.setattr(spack_backend,'catalog',lambda home,pattern:available)
    args=['--home',str(tmp_path/'absent'),'--json','apt','list','e*']
    assert main(args)==0
    result=json.loads(capsys.readouterr().out)
    assert result['view']=='installed' and result['packages']==installed[:1]
    assert main([*args,'--available'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['view']=='available' and result['packages']==available[:1]
    assert not (tmp_path/'absent').exists()

@pytest.mark.parametrize('args',[
    ['install','gsl','--no-official'],['update','--recipes-only'],['install','gsl','--cache','bad'],
    ['list','--upgradable'],['install','gsl','--jobs','8']])
def test_invalid_options_do_not_initialize(tmp_path,capsys,args):
    home=tmp_path/'absent'
    assert main(['--home',str(home),'--json','apt',*args])==2
    assert not json.loads(capsys.readouterr().out)['ok'] and not home.exists()

def test_real_dry_run_requires_selected_profile(tmp_path,capsys):
    home=tmp_path/'absent'
    assert main(['--home',str(home),'--json','apt','install','gsl','--dry-run'])==2
    assert 'profile' in json.loads(capsys.readouterr().out)['error']['message']
    assert not home.exists()
