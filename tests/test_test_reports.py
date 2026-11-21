"""Real pytest processes and HTTP consumers for the shared per-test contract."""
import json
from pathlib import Path
import sys

import pytest
import yaml

from jarvis_forge import testing_run, test_reports, test_discovery, test_metadata, test_report_api as api
from jarvis_forge import test_report_runtime as store
from jarvis_forge.storage import ForgeError, ForgeHome, write_json
from test_test_run import project

@pytest.fixture
def home(tmp_path):
    value=ForgeHome(tmp_path/'home');value.initialize();return value


def run(tmp_path,home,source,**kwargs):
    root,card=project(tmp_path,{'test_a.py':source},**kwargs)
    value=testing_run.run(home,card,full=True)
    record=value['runs'][0];report_root=test_reports.directory(home,record['run_id'],'unit')
    inventory,items=store.snapshot(report_root)
    return root,card,record,report_root,inventory,items


def bind_job(home,record):
    selected={'id':'reports','path':'/unused/library.yaml'}
    job={'id':'job-reports','status':record['status'],'submitted_at':record['started_at'],
         'cases':[{'id':'demo/tests/'+step['id'],'project':'demo','kind':'pytest','status':step['status'],'unit':0,'run_id':record['run_id']} for step in record['steps']]}
    from jarvis_forge.playlist_jobs import _base
    write_json(_base(home,selected)/job['id']/'state.json',job)
    return selected,job


def test_real_pytest_phases_logs_and_no_output_html(tmp_path,home):
    source='''import pytest
@pytest.fixture
def broken_setup(): raise RuntimeError("setup failed")
@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown failed")
def test_pass(): pass
def test_fail():
    print("only this case · μ 🧪")
    assert False, "precise failure"
def test_setup(broken_setup): pass
def test_teardown(broken_teardown): pass
@pytest.mark.skip(reason="controlled skip")
def test_skip(): pass
@pytest.mark.xfail(reason="known bug")
def test_xfail(): assert False
@pytest.mark.xfail(reason="non-strict")
def test_xpass(): pass
@pytest.mark.parametrize("x",[1,2],ids=["one","two"])
def test_parameter(x): assert x>0
'''
    _,_,record,root,inventory,rows=run(tmp_path,home,source)
    assert len(rows)==9 and inventory['confirmed']
    assert len({row['instance_id'] for row in rows})==9
    outcomes={row['native_id'].split('::')[-1]:row['outcome'] for row in rows}
    assert outcomes['test_teardown']=='error' and outcomes['test_setup']=='error'
    assert outcomes['test_xfail']=='xfailed' and outcomes['test_xpass']=='xpassed'
    assert outcomes['test_skip']=='skipped' and outcomes['test_fail']=='failed'
    final=[event for event in store.events(root) if event['event_type']=='case_finished']
    assert len(final)==9 and len({event['instance_id'] for event in final})==9
    passed=next(row for row in rows if row['native_id'].endswith('test_pass'))
    html=test_reports.run_case(home,record['run_id'],passed['instance_id'],'html')
    assert 'No additional output.' in html and passed['instance_id'] in html
    failed=next(row for row in rows if row['outcome']=='failed')
    data=read_case(root,failed)
    assert 'only this case' in data['logs']['stdout'] and 'precise failure' in data['logs']['traceback']
    assert not read_case(root,passed)['logs']['stdout']
    selected,job=bind_job(home,record)
    progress=api.progress(home,selected,job['id'])['summaries'][0]
    assert progress['completed']==9 and progress['total']==9
    events=api.job_events(home,selected,job['id'],{})
    assert api.job_events(home,selected,job['id'],{'after':[events['next_cursor']]})['items']==[]
    # Interrupted cursor-index write is recovered from already persisted events.
    from jarvis_forge.playlist_jobs import _directory
    (_directory(home,selected,job['id'])/'test-events/sources.json').unlink()
    assert api.job_events(home,selected,job['id'],{'after':[events['next_cursor']]})['items']==[]


def read_case(root,row):return store.read(root/'cases'/row['storage_key']/'report.json')


def test_collection_failure_has_no_fabricated_total_and_has_step_report(tmp_path,home):
    _,_,record,root,inventory,rows=run(tmp_path,home,'raise RuntimeError("controlled collection failure")\n')
    assert rows==[] and not inventory['confirmed']
    assert store.read(root/'progress.json')['summaries'][0]['total'] is None
    assert 'controlled collection failure' in (root/'step-report.html').read_text()
    assert record['status']=='failed'


def test_fail_fast_leaves_known_items_not_run(tmp_path,home):
    groups=[{'id':'unit','match':['tests/test_*.py'],'inputs':['tests/**/*.py'],'skip_allow':[],'pytest_args':['--maxfail=1']}]
    _,_,_,root,inventory,rows=run(tmp_path,home,'def test_a(): assert False\ndef test_b(): pass\ndef test_c(): pass\n',groups=groups)
    assert len(rows)==3 and sum(row['state']=='finished' for row in rows)==1
    assert sum(row['state']=='not_run' for row in rows)==2
    progress=store.read(root/'progress.json')['summaries'][0]
    assert progress['completed']==1 and progress['not_run']==2 and progress['total']==3


def test_discovery_runs_no_bodies_and_is_not_implicit(tmp_path,home):
    marker=tmp_path/'body-ran'
    root,card=project(tmp_path,{'test_a.py':f'from pathlib import Path\ndef test_a(): Path({str(marker)!r}).touch()\n'})
    value=test_discovery.discover_card(home,card)
    assert len(value['items'])==1 and not marker.exists()
    assert value['python']==sys.executable and value['items'][0]['instance_id'] is None
    assert not list((home.root/'runs').glob('*/result.json'))


def test_metadata_validation_is_static_strict_and_contained(tmp_path):
    directory=tmp_path/'metadata';directory.mkdir()
    index=directory/'index.yaml';document=directory/'a.yaml'
    index.write_text(yaml.safe_dump({'schema_version':1,'kind':'test-metadata-index','project_id':'demo','documents':['a.yaml']}))
    definition={'id':'demo.purpose','title':'No-output assertion','purpose':'Detect changed arithmetic result.',
                'binding':{'adapter':'pytest','selector':'tests/test_a.py::test_a'},'scenario':{'inputs':{'a':1},'action':'Compare the sum.'},
                'criteria':[{'id':'sum','expected':'1 + 1 equals 2.','verification':'automated'}],
                'failure_meaning':'The sum changed. Inspect the assertion.','limitations':['Does not cover floating-point arithmetic.'],'review':{'status':'draft'}}
    data={'schema_version':1,'kind':'test-metadata','project_id':'demo','scope':{'owner':'team','component':'math'},'definitions':[definition]}
    document.write_text(yaml.safe_dump(data))
    assert test_metadata.validate(index)['definitions']==1
    with pytest.raises(ForgeError,match='requires an explicit'):test_metadata.validate(index,strict=True)
    inventory={'confirmed':True,'project_id':'demo','metadata_digest':test_metadata.load(index)['digest'],
               'items':[{'native_id':'tests/test_a.py::test_a[x]','adapter':'pytest'}]}
    assert test_metadata.validate(index,strict=True,inventory=inventory)['binding_checked']
    with pytest.raises(ForgeError,match='does not match'):
        test_metadata.validate(index,strict=True,inventory={**inventory,'metadata_digest':'old'})
    definition['variants']={'x':{'criteria':[{'id':'sum','expected':'TODO','verification':'automated'}]}}
    document.write_text(yaml.safe_dump(data))
    with pytest.raises(ForgeError,match='Placeholder'):test_metadata.load(index)
    definition['variants']['x']['criteria']=[definition['criteria'][0]]*2
    document.write_text(yaml.safe_dump(data))
    with pytest.raises(ForgeError,match='Duplicate criterion'):test_metadata.load(index)
    del definition['variants']
    data['definitions'].append(definition);document.write_text(yaml.safe_dump(data))
    with pytest.raises(ForgeError,match='Duplicate'):test_metadata.load(index)
    index.write_text(yaml.safe_dump({'schema_version':1,'kind':'test-metadata-index','project_id':'demo','documents':['../outside.yaml']}))
    with pytest.raises(ForgeError,match='leaves'):test_metadata.load(index)


def test_acceptance_status_binds_the_same_metadata_inputs_as_execution(tmp_path, home):
    from test_test_run import commit

    root, card = project(tmp_path, profiles={"acceptance": ["unit"]},
                         coverage={"paths": ["tests/test_*.py"]})
    directory = root / "tests/metadata"
    directory.mkdir()
    index = directory / "index.yaml"
    document = directory / "cases.yaml"
    index.write_text(yaml.safe_dump({"schema_version": 1, "kind": "test-metadata-index",
                                    "project_id": "demo", "documents": ["cases.yaml"]}))
    metadata = {"schema_version": 1, "kind": "test-metadata", "project_id": "demo",
                "scope": {"component": "arithmetic"}, "definitions": [{
                    "id": "demo.no-output", "title": "No-output test",
                    "purpose": "Verify a passing test can have no captured output.",
                    "binding": {"adapter": "pytest", "selector": "tests/test_a.py::test_ok"},
                    "scenario": {"inputs": {}, "action": "Execute the no-output test."},
                    "criteria": [{"id": "pass", "expected": "The test passes.",
                                  "verification": "automated"}],
                    "failure_meaning": "The no-output execution regressed.",
                    "limitations": ["Does not verify arithmetic operations."],
                    "review": {"status": "draft"}}]}
    document.write_text(yaml.safe_dump(metadata))
    commit(root, "add metadata")
    first = testing_run.run(home, card, profile="acceptance")
    assert first["summary"]["accepted"]
    assert testing_run.status(home, card)["accepted"]

    metadata["definitions"][0]["purpose"] = "Verify the persisted no-output result."
    document.write_text(yaml.safe_dump(metadata))
    commit(root, "change metadata")
    stale = testing_run.status(home, card)
    assert not stale["accepted"]
    assert "Declared test inputs changed." in stale["cards"][0]["reasons"]
    refreshed = testing_run.run(home, card, profile="acceptance")
    assert refreshed["runs"][0]["steps"][0]["status"] == "passed"
    assert testing_run.status(home, card)["accepted"]


def test_query_cursor_revision_filters_and_utf8_boundaries(tmp_path):
    rows=[{'instance_id':str(i),'native_id':f'test_{i}','title':'needle' if i==600 else 'other','state':'finished','outcome':'passed'} for i in range(635)]
    first=api.page(rows,'v1',{})
    assert len(first['items'])==100
    second=api.page(rows,'v1',{'cursor':[first['next_cursor']]})
    assert second['items'][0]['instance_id']=='100'
    with pytest.raises(ForgeError) as error:api.page(rows,'v2',{'cursor':[first['next_cursor']]})
    assert error.value.code==409
    assert api.page(rows,'v1',{'q':['needle']})['total']==1
    assert api.page([], 'v1',{},confirmed=False)['total'] is None
    parameterized=[{'native_id':f'tests/a.py::test_value[{value}]','step_id':step,'adapter':'pytest'}
                   for step,value in [('unit','one'),('unit','two'),('other','three')]]
    match=api.page(parameterized,'parameters',{'q':['[one]']})
    assert len(match['items'])==1 and match['items'][0]['variant_count']==2
    row={'attempt':1,'attempts':[1],'state':'finished','execution_mode':'executed'}
    text='诊断 μ 🧪\n'*20000;(tmp_path/'stdout.log').write_text(text)
    offset=0;parts=[]
    while True:
        part=api.log(row,tmp_path,{'offset':[str(offset)],'limit':['101']});parts.append(part['text']);offset=part['next_offset']
        if part['eof']:break
    assert ''.join(parts)==text and offset==len(text.encode())
    with pytest.raises(ForgeError):api.log(row,tmp_path,{'offset':['1']})
    with pytest.raises(ForgeError):api.log(row,tmp_path,{'limit':['262145']})


def test_missing_metadata_and_renderer_failure_do_not_rewrite_outcomes(tmp_path,monkeypatch):
    context={'project_id':'demo','run_id':'run-A1','step_id':'unit','target_id':'local'}
    row=store.make_row('tests/a.py::test_ok',context,{'definitions':[]},0)
    row.update(state='finished',outcome='passed')
    monkeypatch.setattr(store,'render',lambda report:(_ for _ in ()).throw(RuntimeError('renderer failed')))
    store.save_case(tmp_path,row)
    assert store.read(tmp_path/'cases'/row['storage_key']/'result.json')['outcome']=='passed'
    assert read_case(tmp_path,row)['status']=='failed' and row['metadata_status']=='missing'
    assert store.read(tmp_path/'state-index.json')['items'][row['storage_key']]['report_status']=='failed'


def test_http_reports_isolate_relative_assets_and_serve_complete_frontend(tmp_path,home):
    from http.client import HTTPConnection
    from threading import Thread
    from jarvis_forge.dashboard.server import make_server
    source='''import os
from pathlib import Path
def test_picture(jforge_evidence):
    bundle=Path(os.environ['JFORGE_STEP_DIR'])/'bundle'
    bundle.mkdir()
    (bundle/'index.html').write_text('<img src="plot.svg"><script>window.bad=true</script>')
    (bundle/'plot.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect width="20" height="20"/></svg>')
    jforge_evidence(bundle)
'''
    _,_,record,root,_,rows=run(tmp_path,home,source)
    assert rows[0]['artifacts'] and record['status']=='passed'
    selected,job=bind_job(home,record)
    server=make_server(home,tmp_path,0,'test-session-token')
    server.dashboard.selected=lambda:selected
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    def get(path,authorized=True):
        connection=HTTPConnection('127.0.0.1',server.server_port)
        connection.request('GET',path,headers={'X-Jforge-Token':'test-session-token'} if authorized else {})
        response=connection.getresponse();data=response.read();headers=dict(response.getheaders());connection.close()
        return response.status,headers,data
    try:
        assert get('/api/capabilities',False)[0]==401
        assert json.loads(get('/api/capabilities')[2])['api_version']==1
        for path in ('/test-views.js','/test-model.js','/demo-scenarios.js','/fixtures/v2/states-1-attempt-1.html'):
            assert get(path)[0]==200
        base=f'/api/jobs/{job["id"]}/test-cases/{rows[0]["instance_id"]}'
        import jsonschema
        schema=json.loads((Path(__file__).parents[1]/'src/jarvis_forge/schemas/test-report.schema.json').read_text())
        for path,definition in [('/api/capabilities','capabilities'),(base,'detail'),(base+'/report','report_response'),
                                (base+'/log','log'),(f'/api/jobs/{job["id"]}/progress','progress'),
                                (f'/api/jobs/{job["id"]}/events','events'),(f'/api/jobs/{job["id"]}/test-cases','list')]:
            status,_,data=get(path)
            assert status==200
            jsonschema.Draft202012Validator({**schema,'$ref':'#/$defs/'+definition}).validate(json.loads(data))
        response=json.loads(get(base+'/report')[2]);href=response['report']['html_href']
        status,headers,body=get(href,False)
        assert status==200 and b'Expected criteria' in body
        assert "frame-ancestors 'self'" in headers['Content-Security-Policy']
        assert 'allow-scripts' not in headers['Content-Security-Policy']
        artifact=rows[0]['artifacts'][0];prefix=href.removesuffix('report.html')
        status,headers,body=get(prefix+artifact['relative_href'],False)
        assert status==200 and 'sandbox allow-scripts' in headers['Content-Security-Policy']
        assert 'allow-same-origin' not in headers['Content-Security-Policy']
        assert get(prefix+artifact['relative_href'].replace('index.html','plot.svg'),False)[0]==200
        assert get(prefix+'../../metadata.json',False)[0]==404
        assert get(prefix+'result.json',False)[0]==404
        assert get(href.replace('/report-view/','/report-view/wrong'),False)[0]==401
        assert get(base+'/report.html')[0]==303
        assert get(base+'/log?stream=../../environment.sh')[0]==400
        assert get(base+'/report?attempt=2')[0]==404
    finally:
        server.shutdown();server.server_close();thread.join()


def test_reuse_retains_original_identity_and_metadata_changes_force_execution(tmp_path,home):
    root,card=project(tmp_path,{'test_a.py':'def test_ok(): print("original evidence")\n'})
    first=testing_run.run(home,card)['runs'][0]
    before_root=test_reports.directory(home,first['run_id'],'unit');_,before=store.snapshot(before_root)
    second=testing_run.run(home,card)['runs'][0]
    assert second['steps'][0]['status']=='reused'
    after_root=test_reports.directory(home,second['run_id'],'unit');_,after=store.snapshot(after_root)
    assert after[0]['execution_mode']=='reused' and after[0]['seconds'] is None
    assert after[0]['case_id']==before[0]['case_id'] and after[0]['instance_id']!=before[0]['instance_id']
    assert after[0]['reused_from']['instance_id']==before[0]['instance_id']
    assert read_case(after_root,after[0])['logs']['stdout']=='original evidence\n'
    # Corrupt original raw facts: cached success is no longer reusable.
    path=before_root/'cases'/before[0]['storage_key']/'result.json'
    value=store.read(path);value['outcome']='failed';store.atomic(path,value)
    third=testing_run.run(home,card)['runs'][0]
    assert third['steps'][0]['status']=='passed'
    _,fresh=store.snapshot(test_reports.directory(home,third['run_id'],'unit'))
    assert fresh[0]['execution_mode']=='executed' and fresh[0]['outcome']=='passed'


def test_running_progress_and_cancel_preserve_known_unfinished_items(tmp_path,home):
    import os
    import signal
    import subprocess
    import time
    root,card=project(tmp_path,{'test_a.py':'import time\ndef test_slow(): time.sleep(30)\ndef test_later(): pass\n'})
    process=subprocess.Popen([sys.executable,'-m','jarvis_forge','--home',str(home.root),'--json','test','run',str(card)],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    report_root=None
    try:
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            candidates=list((home.root/'runs').glob('*/steps/unit/test-report-v1/progress.json'))
            if candidates:
                candidate=candidates[0]
                progress=store.read(candidate)
                if progress.get('current_test'):
                    report_root=candidate.parent;break
            if process.poll() is not None:break
            time.sleep(.05)
        assert report_root is not None
        assert progress['summaries'][0]['total']==2 and progress['summaries'][0]['completed']==0
        os.killpg(process.pid,signal.SIGINT)
        process.communicate(timeout=15)
        assert process.returncode==130
        _,rows=store.snapshot(report_root)
        assert len(rows)==2 and all(row['state'] in ('interrupted','not_run') for row in rows)
        assert all(row['outcome'] is None for row in rows)
        assert store.read(report_root/'progress.json')['summaries'][0]['completed']==0
        assert all((report_root/'cases'/row['storage_key']/'report.html').is_file() for row in rows)
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL);process.communicate()


def test_versioned_fixtures_match_forge_schema():
    import hashlib
    import jsonschema
    source=Path(__file__).parents[1]
    schema=json.loads((source/'src/jarvis_forge/schemas/test-report.schema.json').read_text())
    directory=source/'tests/fixtures/test_reports/v1'
    manifest=json.loads((directory/'fixtures.json').read_text())
    for name,definition in manifest['schemas'].items():
        value=json.loads((directory/name).read_text())
        jsonschema.Draft202012Validator({**schema,'$ref':'#/$defs/'+definition}).validate(value)
    api_document=json.loads((source/'src/jarvis_forge/schemas/test-report.openapi.json').read_text())
    assert api_document['openapi']=='3.1.0'
    assert '/api/jobs/{job_id}/test-cases/{instance_id}/report.html' in api_document['paths']
    def refs(value):
        if isinstance(value,dict):
            for name,item in value.items():
                if name=='$ref' and item.startswith('#/'):
                    target=api_document
                    for part in item[2:].split('/'):target=target[part]
                else:refs(item)
        elif isinstance(value,list):
            for item in value:refs(item)
    refs(api_document)
    # The packaged frontend consumes the producer's exact versioned contract;
    # stale pinned copies must fail even when each copy validates on its own.
    static=source/'src/jarvis_forge/dashboard/static'
    for name in ('test-report.schema.json','test-report.openapi.json','test-metadata.schema.json'):
        assert (static/'contracts/v1'/name).read_bytes()==(source/'src/jarvis_forge/schemas'/name).read_bytes()
    for path in directory.rglob('*'):
        if path.is_file():assert (static/'contracts/v1/fixtures'/path.relative_to(directory)).read_bytes()==path.read_bytes()
    assets=json.loads((static/'asset-manifest.json').read_text())
    for asset in assets['files']:
        assert hashlib.sha256((static/asset['path']).read_bytes()).hexdigest()==asset['sha256'],asset['path']


def test_report_rebuild_keeps_raw_facts_and_archives_presentation(tmp_path,home):
    import hashlib
    _,card,record,root,_,rows=run(tmp_path,home,'def test_ok(): pass\n')
    directory=root/'cases'/rows[0]['storage_key'];before=hashlib.sha256((directory/'result.json').read_bytes()).hexdigest()
    value=test_reports.rebuild(root,rows[0]['instance_id'])
    assert value['render_revision']==2 and (directory/'revisions/0001-render/report.html').is_file()
    assert hashlib.sha256((directory/'result.json').read_bytes()).hexdigest()==before
    selected,job=bind_job(home,record)
    result=api.dispatch(home,selected,'POST',f'/api/jobs/{job["id"]}/test-cases/{rows[0]["instance_id"]}/report/rebuild',{}, {})
    assert result['report']['render_revision']==3 and result['report']['outcome']=='passed'
    # Regenerating presentation must not invalidate immutable execution evidence.
    following=testing_run.run(home,card)['runs'][0]
    assert following['steps'][0]['status']=='reused'


def test_actual_parameters_and_frozen_metadata_observations(tmp_path,home):
    root,card=project(tmp_path,{'test_a.py':'import pytest\n@pytest.mark.parametrize("x",[2],ids=["two"])\ndef test_ok(x,jforge_observe):\n    assert jforge_observe("equal",{"value":x},passed=x==2)=={"value":2}\n'})
    directory=root/'tests/metadata';directory.mkdir()
    (directory/'index.yaml').write_text(yaml.safe_dump({'schema_version':1,'kind':'test-metadata-index','project_id':'demo','documents':['a.yaml']}))
    definition={'id':'demo.two','title':'The selected value equals two','purpose':'Detect a changed parameter before it reaches dependent calculations.',
       'binding':{'adapter':'pytest','selector':'tests/test_a.py::test_ok'},'scenario':{'inputs':{'x':2},'action':'Record and compare the selected value.'},
       'criteria':[{'id':'equal','expected':'The selected value equals 2.','verification':'automated'},{'id':'review','expected':'Inspect the parameter description.','verification':'manual'}],
       'failure_meaning':'The parameter binding changed.','limitations':['One integer input only.'],'review':{'status':'draft'}}
    document={'schema_version':1,'kind':'test-metadata','project_id':'demo','definitions':[definition]}
    (directory/'a.yaml').write_text(yaml.safe_dump(document))
    first=testing_run.run(home,card)['runs'][0];report_root=test_reports.directory(home,first['run_id'],'unit');_,rows=store.snapshot(report_root)
    assert rows[0]['parameters']=={'x':2} and rows[0]['metadata_status']=='complete'
    report=read_case(report_root,rows[0]);assert report['criteria'][0]['observed']=={'value':2} and report['criteria'][0]['status']=='passed'
    assert report['criteria'][1]['status']=='unobserved' and report['criteria'][1]['review_status']=='pending'
    inventory=test_discovery.discover_card(home,card)
    assert test_metadata.validate(directory/'index.yaml',strict=True,inventory=inventory)['binding_checked']
    source=root/'tests/test_a.py';original=source.read_text()
    source.write_text(original+'\ndef test_new(): pass\n')
    with pytest.raises(ForgeError,match='stale'):
        test_metadata.validate(directory/'index.yaml',strict=True,inventory=inventory)
    source.write_text(original)
    definition['purpose']='Protect a new, explicitly changed purpose.';(directory/'a.yaml').write_text(yaml.safe_dump(document))
    second=testing_run.run(home,card)['runs'][0]
    assert second['steps'][0]['status']=='passed'
    assert read_case(report_root,rows[0])['purpose']=='Detect a changed parameter before it reaches dependent calculations.'
    _,fresh=store.snapshot(test_reports.directory(home,second['run_id'],'unit'))
    assert fresh[0]['purpose']=='Protect a new, explicitly changed purpose.'


def test_progress_recovery_replays_complete_events_and_discards_partial_tail(tmp_path,monkeypatch):
    context={'project_id':'demo','run_id':'run-A1','step_id':'unit','target_id':'local'}
    store.atomic(tmp_path/'context.json',context)
    with store.locked(tmp_path):
        store.collect(tmp_path,context,{'definitions':[]},['tests/a.py::test_a','tests/a.py::test_b'])
        row=store.snapshot(tmp_path)[1][0]
        store.event(tmp_path,context,'case_started',row)
        store.event(tmp_path,context,'case_finished',row,state='finished',outcome='passed')
    # Simulate a crash after the complete append but before head/snapshot updates.
    (tmp_path/'event-head.json').unlink()
    (tmp_path/'state-index.json').unlink()
    with (tmp_path/'events.jsonl').open('ab') as stream:stream.write(b'{"seq":')
    recovered=store.progress_snapshot(tmp_path)
    summary=recovered['summaries'][0]
    assert summary['total']==2 and summary['completed']==1 and summary['pending']==1
    assert summary['outcomes']['passed']==1 and recovered['revision']==3
    with store.locked(tmp_path):
        assert store.event(tmp_path,context,'session_finished')['seq']==4
    assert [item['seq'] for item in store.events(tmp_path)]==[1,2,3,4]
    store.progress_snapshot(tmp_path)
    # Steady-state polling reads the small snapshot, not all result documents.
    monkeypatch.setattr(store,'snapshot',lambda *args:pytest.fail('poll loaded full results'))
    monkeypatch.setattr(store,'events',lambda *args:pytest.fail('poll scanned history'))
    assert store.progress_snapshot(tmp_path)['summaries'][0]==summary


def test_job_progress_uses_snapshots_and_case_lookup_reads_one_result(tmp_path,home,monkeypatch):
    _,_,record,root,_,rows=run(tmp_path,home,'def test_a(): pass\ndef test_b(): pass\n')
    selected,job=bind_job(home,record)
    monkeypatch.setattr(api,'snapshot',lambda *args:pytest.fail('poll loaded full results'))
    monkeypatch.setattr(store,'snapshot',lambda *args:pytest.fail('lookup loaded all results'))
    assert api.progress(home,selected,job['id'])['summaries'][0]['completed']==2
    assert test_reports.get_case(root,rows[1]['instance_id'])[0]['native_id'].endswith('test_b')


def test_report_log_decode_failure_is_separate_and_rebuild_keeps_facts(tmp_path,monkeypatch):
    context={'project_id':'demo','run_id':'run-A1','step_id':'unit','target_id':'local'}
    row=store.make_row('tests/a.py::test_a',context,{'definitions':[]},0)
    row.update(state='finished',outcome='passed')
    store.atomic(tmp_path/'inventory.json',{'items':[row],'confirmed':True})
    directory=tmp_path/'cases'/row['storage_key'];directory.mkdir(parents=True)
    (directory/'stdout.log').write_bytes(b'\xff')
    store.save_case(tmp_path,row)
    before=(directory/'result.json').read_bytes()
    assert read_case(tmp_path,row)['status']=='failed'
    assert test_reports.get_case(tmp_path,row['instance_id'])[0]['report_status']=='failed'
    monkeypatch.setattr(api,'roots',lambda *args:({},[({'id':'unit'},tmp_path)]))
    assert api.items(None,None,'job-A1')[1][0]['report_status']=='failed'
    (directory/'stdout.log').write_text('recovered log')
    assert test_reports.rebuild(tmp_path,row['instance_id'])['status']=='ready'
    assert (directory/'result.json').read_bytes()==before
    assert store.read(tmp_path/'state-index.json')['items'][row['storage_key']]['report_status']=='ready'
    assert api.items(None,None,'job-A1')[1][0]['report_status']=='ready'


def test_setup_xfail_and_strict_xpass_preserve_framework_outcome(tmp_path,home):
    source='''import pytest
@pytest.fixture
def expected_setup(): pytest.xfail("fixture unavailable by design")
def test_expected_setup(expected_setup): pass
@pytest.mark.xfail(strict=True,reason="controlled strict policy")
def test_unexpected_success(): pass
@pytest.mark.xfail(reason="")
def test_empty_reason(): assert False
'''
    _,_,record,_,_,rows=run(tmp_path,home,source)
    assert {row['outcome'] for row in rows}=={'xfailed','xpassed'}
    assert sum(row['outcome']=='xfailed' for row in rows)==2
    assert record['status']=='failed'


def test_discovery_cache_detects_inputs_and_packages_without_reimport(tmp_path,home,monkeypatch):
    root,card=project(tmp_path,{'test_a.py':'def test_a(): pass\n'})
    installed=tmp_path/'site-packages';info=installed/'demo-1.dist-info';info.mkdir(parents=True)
    (info/'METADATA').write_text('Name: demo\nVersion: 1\n')
    monkeypatch.setattr(testing_run,'probe_packages',lambda *args:('packages',[{'location':str(installed),'direct_url':None}]))
    directory=home.root/'test-discoveries/discovery-A1'
    result=test_discovery.discover_card(home,card,work=directory)
    selected={'id':'reports'}
    unit={'project':'demo','root':str(root),'card':str(card)}
    store.atomic(directory/'request.json',{'units':[unit]})
    store.atomic(directory/'state.json',{'id':directory.name,'library':'reports','status':'completed','href':'/api/test-discoveries/'+directory.name,
        'identities':{'demo':result['source']},'metadata_digests':{'demo':result['metadata_digest']},
        'input_checks':{'0':{'selection':result['selection'],'identity':result['selection_identity'],'environment':result['environment_watch']}}})
    monkeypatch.setattr(test_discovery,'discover_card',lambda *args,**kwargs:pytest.fail('GET imported tests'))
    assert test_discovery.get(home,selected,directory.name)['freshness']=='fresh'
    (info/'METADATA').write_text('Name: demo\nVersion: 2\n')
    assert test_discovery.get(home,selected,directory.name)['freshness']=='stale'
    (root/'tests/test_new.py').write_text('def test_new(): pass\n')
    assert test_discovery.get(home,selected,directory.name)['freshness']=='stale'


def test_legacy_command_reuse_never_invents_passed_outcome(tmp_path,home):
    from jarvis_forge import test_report_commands
    root=tmp_path/'project';root.mkdir()
    job={'id':'job-A1','cases':[{'id':'demo/check/unit','title':'Command check','kind':'command','unit':0,'status':'reused'}]}
    units=[{'project':'demo','root':str(root),'environment':None}]
    test_report_commands.prepare(home,tmp_path,job,units)
    test_report_commands.finish(home,tmp_path,job)
    report_root=tmp_path/'unit-0/reports/step-1'/test_reports.ROOT
    _,rows=store.snapshot(report_root)
    assert rows[0]['execution_mode']=='reused' and rows[0]['outcome'] is None
    assert store.read(report_root/'progress.json')['summaries'][0]['outcomes']['passed']==0


def test_event_pages_resume_by_byte_cursor_and_recover_lost_merge_index(tmp_path,home,monkeypatch):
    source=tmp_path/'source';source.mkdir()
    records=[{'schema_version':1,'seq':i,'time':store.utc(),'job_id':'job-A1','run_id':'run-A1',
              'step_id':'unit','target_id':'local','instance_id':None,'attempt':1,
              'event_type':'phase_finished','payload':{'phase':'call'}} for i in range(1,1006)]
    (source/'events.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in records))
    monkeypatch.setattr(api,'roots',lambda *args:({'id':'job-A1'},[({'id':'demo/tests/unit'},source)]))
    monkeypatch.setattr(api.jobs,'_directory',lambda *args:tmp_path/'job')
    first=api.job_events(home,{},'job-A1',{})
    second=api.job_events(home,{},'job-A1',{'after':[first['next_cursor']]})
    third=api.job_events(home,{},'job-A1',{'after':[second['next_cursor']]})
    assert [len(page['items']) for page in (first,second,third)]==[500,500,5]
    assert [item['seq'] for page in (first,second,third) for item in page['items']]==list(range(1,1006))
    root=tmp_path/'job/test-events'
    (root/'sources.json').unlink()
    with (root/'events.jsonl').open('ab') as stream:stream.write(b'{"seq":')
    assert api.job_events(home,{},'job-A1',{'after':[third['next_cursor']]})['items']==[]
    assert len(store.events(root))==1005
    real=api.event_records;reads=[]
    def observed(root,offset=0):
        reads.append(offset)
        yield from real(root,offset)
    monkeypatch.setattr(api,'event_records',observed)
    assert api.job_events(home,{},'job-A1',{'after':[third['next_cursor']]})['items']==[]
    assert all(offset>0 for offset in reads)
    with (source/'events.jsonl').open('a') as stream:
        stream.write(json.dumps(records[-1])+'\n')
        stream.write(json.dumps({**records[-1],'seq':1006})+'\n')
    appended=api.job_events(home,{},'job-A1',{'after':[third['next_cursor']]})
    assert [item['seq'] for item in appended['items']]==[1006]
    with pytest.raises(ForgeError,match='Invalid event cursor'):
        api.job_events(home,{},'job-A1',{'after':[api.cursor({'job':'other','seq':0,'offset':0})]})


def test_invalid_log_bytes_are_not_silently_dropped(tmp_path):
    (tmp_path/'stdout.log').write_bytes(b'valid\xff')
    row={'attempt':1,'attempts':[1],'state':'finished','execution_mode':'executed'}
    with pytest.raises(ForgeError) as error:api.log(row,tmp_path,{})
    assert error.value.code==500


def test_new_cli_commands_keep_human_output_and_json_separate(tmp_path,home,capsys):
    from jarvis_forge.cli import main
    root,card=project(tmp_path,{'test_a.py':'def test_ok(): pass\n'})
    options=['--home',str(home.root),'test']
    assert main([*options,'discover',str(card)])==0
    text=capsys.readouterr().out
    assert '1 pytest items' in text and 'Inventory:' in text and "'items':" not in text
    assert main([*options,'metadata','scaffold',str(root),'--output',str(tmp_path/'drafts')])==0
    assert 'review required' in capsys.readouterr().out
    record=testing_run.run(home,card)['runs'][0]
    _,rows=store.snapshot(test_reports.directory(home,record['run_id'],'unit'))
    argv=[*options,'report',record['run_id'],'--case',rows[0]['instance_id']]
    assert main([*argv,'--format','json'])==0
    value=json.loads(capsys.readouterr().out)
    assert value['report']['outcome']=='passed' and value['kind']=='test-case'
    assert main(argv)==0
    assert 'passed · executed · report ready' in capsys.readouterr().out
    assert main([*argv,'--format','html'])==0
    assert capsys.readouterr().out.startswith('<!doctype html>')


def test_discovery_submission_and_worker_publish_queryable_inventory(tmp_path,home,monkeypatch):
    from jarvis_forge import playlist_library
    root,card=project(tmp_path,{'test_a.py':'def test_a(): pass\n'})
    library=tmp_path/'library';library.mkdir()
    (library/'library.yaml').write_text(yaml.safe_dump({'schema_version':1,'kind':'playlist-library','id':'reports','title':'Reports',
        'shelves':[{'id':'main','title':'Main'}],
        'projects':[{'id':'demo','title':'Demo','shelf':'main','root':'../repo',
            'sources':[{'id':'tests','kind':'test','path':'tests/jforge-tests.yaml'}]}]}))
    selected=playlist_library.load(library)
    with monkeypatch.context() as patch:
        patch.setattr(test_discovery.subprocess,'Popen',lambda *args,**kwargs:object())
        response=api.dispatch(home,selected,'POST','/api/test-discoveries',{}, {'steps':['demo/tests/unit']})
    state=response['discovery'];assert state['status']=='queued'
    test_discovery.execute(home,state['id'])
    result=api.dispatch(home,selected,'GET',state['href'],{},None)['discovery']
    assert result['status']=='completed' and result['freshness'] in ('fresh','unknown')
    page=api.dispatch(home,selected,'GET',state['href']+'/test-cases',{},None)
    assert page['total_confirmed'] and page['total']==1
    assert page['items'][0]['step_id']=='demo/tests/unit'
    assert page['items'][0]['instance_id'] is None
