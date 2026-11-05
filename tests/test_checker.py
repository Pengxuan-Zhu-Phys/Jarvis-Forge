from pathlib import Path
import sys
import yaml
import pytest

from jarvis_forge import checker
from jarvis_forge.storage import ForgeHome, ForgeError
from jarvis_forge.cli import main


def config(tmp_path, checks):
    path = tmp_path / 'checks.yaml'
    path.write_text(yaml.safe_dump({'schema_version':1,'kind':'checker','id':'regression','checks':checks}))
    return path


def command(name, inputs, needs=(), code="print('passed')"):
    return {'id':name,'inputs':inputs,'needs':list(needs),'argv':[sys.executable,'-c',code]}


@pytest.fixture
def home(tmp_path):
    value=ForgeHome(tmp_path/'home'); value.initialize(); return value


def test_changed_inputs_select_dependents_and_full_forces_run(tmp_path,home):
    (tmp_path/'a.py').write_text('a')
    (tmp_path/'b.py').write_text('b')
    path=config(tmp_path,[command('a',['a.py']),command('consumer',['b.py'],['a']),command('independent',['b.py'])])
    first=checker.run(home,path)
    assert first['status']=='passed' and first['executed']==3
    second=checker.run(home,path)
    assert second['reused']==3 and second['executed']==0
    (tmp_path/'a.py').write_text('changed')
    third=checker.run(home,path)
    assert {s['id']:s['status'] for s in third['steps']}=={'a':'passed','independent':'reused','consumer':'passed'}
    full=checker.run(home,path,full=True)
    assert full['executed']==3
    selected=checker.run(home,path,selected=['consumer'])
    assert {s['id'] for s in selected['steps']}=={'a','consumer'}


def test_deleted_glob_inputs_are_changes(tmp_path,home):
    (tmp_path/'x.txt').write_text('x')
    path=config(tmp_path,[command('one',['*.txt'])])
    assert checker.run(home,path)['executed']==1
    (tmp_path/'x.txt').unlink()
    assert checker.run(home,path)['executed']==1


def test_standard_checks_share_reports_and_run_again_by_default(tmp_path, home):
    import json
    path = config(tmp_path, [{'id': 'python', 'check': {'python': {'version': '>=3.10', 'modules': ['json']}}}])
    result = checker.run(home, path)
    assert result['status'] == 'passed', result
    report = json.loads((Path(result['path']) / 'steps/python/checks.json').read_text())
    assert report['modules']['json']['status'] == 'found'
    assert checker.run(home, path)['executed'] == 1
    path = config(tmp_path, [{'id': 'python', 'check': {'python': {'modules': ['forge_missing_28461']}}}])
    failed = checker.run(home, path)
    assert failed['status'] == 'failed'
    assert 'forge_missing_28461' in failed['steps'][0]['error']


def test_failure_blocks_dependents_and_invalidates_success(tmp_path,home):
    marker=tmp_path/'state.txt'; marker.write_text('good')
    good="from pathlib import Path; assert Path('state.txt').read_text()=='good'"
    path=config(tmp_path,[command('a',['state.txt'],code=good),command('child',['state.txt'],['a']),command('other',['state.txt'])])
    assert checker.run(home,path)['status']=='passed'
    marker.write_text('bad')
    result=checker.run(home,path)
    assert result['status']=='failed'
    assert {s['id']:s['status'] for s in result['steps']}=={'a':'failed','other':'passed','child':'blocked'}
    marker.write_text('good')
    assert checker.run(home,path)['executed']==3


def test_always_run_and_missing_inputs_rejected(tmp_path,home):
    path=config(tmp_path,[{'id':'external','cache':False,'argv':['true']}])
    assert checker.run(home,path)['executed']==1
    assert checker.run(home,path)['executed']==1
    path=config(tmp_path,[{'id':'oops','argv':['true']}])
    with pytest.raises(ForgeError,match='inputs'):
        checker.load(path)


def test_changed_command_and_tampered_evidence_rerun(tmp_path,home):
    (tmp_path/'input').write_text('x')
    path=config(tmp_path,[command('a',['input'])])
    first=checker.run(home,path)
    log=Path(first['path'])/'steps/a/stdout.log'; log.write_text('tampered')
    assert checker.run(home,path)['executed']==1
    path=config(tmp_path,[command('a',['input'],code="print('new command')")])
    assert checker.run(home,path)['executed']==1


def test_checker_cli_is_read_only_for_listing(tmp_path,capsys):
    path=config(tmp_path,[command('a',['*.py'])])
    missing=tmp_path/'absent'
    assert main(['--home',str(missing),'checker','list',str(path),'--json'])==0
    assert not missing.exists()


def test_always_run_prerequisite_invalidates_cached_dependents(tmp_path,home):
    (tmp_path/'input').write_text('stable')
    path=config(tmp_path,[{'id':'external','cache':False,'argv':['true']},command('child',['input'],['external'])])
    assert checker.run(home,path)['executed']==2
    assert checker.run(home,path)['executed']==2


def test_checker_executes_workflow_and_invalidates_changed_recipe(tmp_path,home):
    workflow=tmp_path/'flow.yaml'
    def write_flow(argv):
        workflow.write_text(yaml.safe_dump({'schema_version':1,'kind':'build','id':'flow','steps':[{'id':'smoke','uses':'command','with':{'argv':argv}}]}))
    write_flow(['true'])
    path=config(tmp_path,[{'id':'workflow-check','inputs':['flow.yaml'],'build':'flow.yaml'}])
    first=checker.run(home,path)
    assert first['status']=='passed'
    assert first['steps'][0]['build_run_id']
    assert checker.run(home,path)['reused']==1
    write_flow(['false'])
    last=checker.run(home,path)
    assert last['status']=='failed' and last['executed']==1
