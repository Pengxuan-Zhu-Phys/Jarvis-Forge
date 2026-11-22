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


def test_profile_inventory_unifies_stores_without_changing_native_state(tmp_path, monkeypatch, capsys):
    import io
    from rich.console import Console
    from jarvis_forge import spack_backend, toolchains
    from jarvis_forge.presentation import package_rows
    from jarvis_forge.storage import ForgeHome, read_json, write_json
    from profile_fixtures import project_profile

    base = ForgeHome(tmp_path / 'forge')
    profile = project_profile(base, 'ubuntu24')
    tools = profile.root / 'tools'
    def package(root, name, version, hash, manual=False, external=False):
        prefix = root / 'pkg' / f'{name}-{version}'
        prefix.mkdir(parents=True, exist_ok=True)
        return dict(name=name, version=version, hash=hash, prefix=str(prefix),
                    root=manual, automatic=not manual, external=external, installed=True, active=True)
    gcc = package(tools, 'gcc', '13.3.0', 'tool-gcc', manual=True)
    cmake = package(tools, 'cmake', '3.28.6', 'tool-cmake', manual=True)
    bootstrap_gmp = package(tools, 'gmp', '6.3.0', 'host-gmp')
    old_runtime = package(tools, 'gcc-runtime', '11.5.0', 'host-runtime')
    write_json(tools / 'packages.json', {'spack': {'packages': [gcc, cmake, bootstrap_gmp, old_runtime]}})
    application_gmp = package(profile.root, 'gmp', '6.3.0', 'app-gmp')
    new_runtime = package(profile.root, 'gcc-runtime', '13.3.0', 'app-runtime')
    gsl = package(profile.root, 'gsl', '2.8', 'app-gsl', manual=True)
    state = read_json(profile.root / 'packages.json')
    state['spack']['packages'] = [{**gcc, 'hash': 'external-gcc', 'external': True, 'root': False},
                                  application_gmp, new_runtime, gsl]
    write_json(profile.root / 'packages.json', state)
    project_profile(base, 'ubuntu22', select=False)
    def snapshot():
        return {str(p.relative_to(base.root)): p.read_bytes() for p in base.root.rglob('*') if p.is_file()}
    before = snapshot()
    monkeypatch.setattr(spack_backend, 'run', lambda *a, **k: pytest.fail('inventory must be offline'))
    monkeypatch.setattr(toolchains, 'install_suite', lambda *a, **k: pytest.fail('inventory must not install tools'))
    args = ['--home', str(base.root), '--json', 'apt', 'list']
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    rows = result['packages']
    assert result['backend'] == 'spack' and len(rows) == 7
    compiler, = [p for p in rows if p['name'] == 'gcc']
    assert compiler['category'] == 'base-tool' and compiler['sources'] == ['tools', 'apt']
    assert not compiler['external']
    assert {r['hash'] for r in compiler['references']} == {'tool-gcc', 'external-gcc'}
    gmp = [p for p in rows if p['name'] == 'gmp']
    assert len(gmp) == 2 and len({p['prefix'] for p in gmp}) == 2
    assert {p['category'] for p in gmp} == {'bootstrap-dependency', 'application'}
    assert main([*args, 'gcc*']) == 0
    assert len(json.loads(capsys.readouterr().out)['packages']) == 3
    # Newly visible base tools also support their existing details/path commands.
    assert main(['--home', str(base.root), 'apt', 'show', 'cmake', '--path']) == 0
    assert capsys.readouterr().out.strip() == cmake['prefix']
    assert main(['--home', str(base.root), '--json', 'apt', 'show', 'cmake']) == 0
    assert json.loads(capsys.readouterr().out)['installed_instances'][0]['category'] == 'base-tool'
    monkeypatch.setattr(spack_backend, 'catalog', lambda *a, **k: [{'name': 'available-only'}])
    assert main([*args, '--available']) == 0
    assert json.loads(capsys.readouterr().out)['packages'] == [{'name': 'available-only'}]
    assert main(['--home', str(base.root), '--profile', 'ubuntu22', '--json', 'apt', 'list']) == 0
    assert json.loads(capsys.readouterr().out)['packages'] == []
    assert snapshot() == before
    for width in (48, 160):
        screen = io.StringIO()
        package_rows(Console(file=screen, width=width), rows)
        text = screen.getvalue()
        assert all(label in text.lower() for label in ('base tool', 'bootstrap dep', 'application'))
        assert text.count('gcc-runtime') == 2 and text.count('gmp') == 2


@pytest.mark.parametrize('width', [40, 80, 120])
def test_inventory_table_adapts_without_inline_metadata_dump(width):
    import copy
    import io
    from rich.console import Console
    from jarvis_forge.presentation import package_rows
    rows=[
        dict(name='gcc',version='13.3.0',category='base-tool',root=True,active=True),
        dict(name='ca-certificates-mozilla',version='2026-03-19',category='bootstrap-dependency',root=False,installed=True),
        dict(name='glibc',version='2.34',category='bootstrap-dependency',root=False,active=True,external=True),
        dict(name='rapidjson',version='1.2.0-2024-08-16',category='application',root=False,active=True),
        dict(name='bzip2',version='1.0.8',category='bootstrap-dependency',root=False,active=True),
        dict(name='bzip2',version='1.0.8',category='application',root=False,active=True),
    ]
    before=copy.deepcopy(rows)
    screen=io.StringIO();package_rows(Console(file=screen,width=width),rows)
    text=screen.getvalue()
    assert rows==before
    assert 'ca-certificates-mozilla' in text and '1.2.0-2024-08-16' in text
    assert text.count('bzip2')==2 and 'active, external' in text
    assert '[application;' not in text and '[bootstrap dep;' not in text
    if width==120:
        assert 'Source' in text and 'Bootstrap dependencies' not in text
    else:
        assert all(label in text for label in ('Base tools','Bootstrap dependencies','Application packages'))
        headers=[line for line in text.splitlines() if line.strip().startswith('Package')]
        assert len(headers)==3
        if width==80:
            assert all(line.split()==['Package','Version','Kind','State'] for line in headers)
            assert len({tuple(line.index(key) for key in ('Package','Version','Kind','State')) for line in headers})==1
            state_column=headers[0].index('State')
            assert next(line for line in text.splitlines() if line.strip().startswith('glibc ')).index('active, external')==state_column
        else:
            assert all(line.split()==['Package','Version'] for line in headers)
            assert any(line.strip()=='dependency · active, external' for line in text.splitlines())
