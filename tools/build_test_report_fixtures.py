"""Deterministic contract fixtures; no test invocation and no acceptance claim."""
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from jarvis_forge import test_report_runtime as store
from jarvis_forge.test_report_api import page


def build(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    context={'project_id':'contract-fixture','project':'/fixture/project','run_id':'fixture-A1','job_id':'fixture-A1',
             'step_id':'unit','target_id':'local','source':{'repository':'fixture/project','commit':'fixture-commit'}}
    definition={'id':'contract.presentation','title':'Contract report preserves the supplied evidence',
                'purpose':'Check that the fixture identity, lifecycle and declared observation remain independently inspectable.',
                'binding':{'adapter':'pytest','selector':'tests/fixture.py::test_contract'},'component':'report.consumer','owner':'fixture maintainers','tags':['contract'],
                'scenario':{'preconditions':'Controlled development fixture; no local test execution is represented.','inputs':{'case':'fixture'},'action':'Read the supplied outcome and compare it with the criteria and evidence scope.'},
                'criteria':[{'id':'truthful','expected':'The outcome and report generation state remain separate.','verification':'automated'},
                            {'id':'review','expected':'A reviewer inspects the supplied presentation.','verification':'manual'}],
                'failure_meaning':'A consumer may have confused result, lifecycle or observation.','limitations':['Does not prove a project acceptance run.'],
                'expected_evidence':[],'review':{'status':'draft'}}
    metadata={'definitions':[definition],'digest':store.key(definition)}
    kinds=['passed','failed','setup-error','teardown-error','skipped','xfailed','xpassed','reused','not_run','interrupted','missing','running','queued','long-name','ctest','ci']
    rows=[]
    for index,kind in enumerate(kinds):
        native='tests/fixture.py::test_contract['+kind+']'
        cfg={**context,'target_id':'ci-linux-py313' if kind=='ci' else 'local'}
        row=store.make_row(native,cfg,metadata,index)
        outcome='error' if kind in ('setup-error','teardown-error') else kind if kind in store.OUTCOMES else 'passed'
        state=kind if kind in ('queued','running','not_run','interrupted') else 'finished'
        row.update(fixture=True,state=state,outcome=outcome if state=='finished' else None,seconds=.012 if state=='finished' else None)
        if kind=='reused':row.update(execution_mode='reused',seconds=None,reused_from={'run_id':'fixture-B1','job_id':'fixture-B1','instance_id':'instance-original','attempt':1})
        if kind=='missing':row.update(metadata_status='missing',definition_id=None,purpose=None,criteria=[])
        if kind=='long-name':row['title']='Long parameterized report title · '+('parameter with spaces / unicode μ 🧪 · '*12)
        if kind=='ctest':
            row=store.make_row('geometry::mesh_convergence',cfg,{'definitions':[],'digest':None},index,adapter='ctest')
            row.update(fixture=True,state='finished',outcome='passed',seconds=.012)
        if kind=='ci':row['source']={'repository':'fixture/repository','commit':'fixture-commit','workflow':'acceptance','job':'linux','matrix':{'python':'3.13'},'attempt':1,'runner':'fixture-runner'}
        if kind in ('setup-error','teardown-error'):
            phase='setup' if kind=='setup-error' else 'teardown'
            row.update(reason=f'Controlled {phase} failure',phases=[{'phase':phase,'outcome':'failed','seconds':.012,'message':f'Controlled {phase} failure','captures':[]}])
        rows.append(row)
    mapping={}
    def write(name,value,definition):
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');mapping[name]=definition
    wrap=lambda **value:{'ok':True,'api_version':1,**value}
    adapter={'adapter':'pytest','enumeration':'exact','progress':'case_events','logs':['case_captured','step_only'],'cancellation':'supported','artifacts':['case_bound','step_bound']}
    write('capabilities.json',wrap(features={'test_cases':True,'test_reports':True},adapters=[adapter,{**adapter,'adapter':'command','progress':'final_only','logs':['step_only']}],inventories={}), 'capabilities')
    write('cases.json',wrap(inventory_revision='fixture-v1',items=rows,total=len(rows),total_confirmed=True,next_cursor=None),'list')
    write('progress.json',wrap(job_id='fixture-A1',revision=42,updated_at='2026-01-01T00:00:00Z',summaries=[store.count(rows)],steps=[],current_test=next(row for row in rows if row['state']=='running')),'progress')
    write('collection-error.json',wrap(job_id='fixture-collection',revision=1,updated_at='2026-01-01T00:00:00Z',collection_status='failed',summaries=[store.count([],False)],steps=[{'id':'unit','status':'failed','collection':{'status':'failed','errors':[{'message':'Controlled collection error'}]}}],current_test=None),'progress')
    for name in ('reused','interrupted','ctest','ci','missing'):
        write(name+'.json',wrap(test_case=deepcopy(rows[kinds.index(name)])),'detail')
    report=store.report_data(rows[0]);report.update(generated_at='2026-01-01T00:00:00Z',json_href='/api/jobs/fixture-A1/test-cases/'+rows[0]['instance_id']+'/report',html_href='/fixtures/report.html',logs={'stdout':'','stderr':'','logging':'','traceback':''})
    report['artifacts']=[{'id':'evidence-A1','name':'Relative SVG fixture','relative_href':'artifacts/plot.svg','scope':'case','files':['plot.svg']}]
    write('report.json',wrap(report=report),'report_response')
    (output/'report.html').write_text(store.render(report))
    (output/'artifacts').mkdir(exist_ok=True)
    (output/'artifacts/plot.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100"><rect width="200" height="100" fill="#e0f5ed"/><path d="M20 80 70 40 120 60 180 20" stroke="#187d60" stroke-width="4" fill="none"/></svg>')
    for name,text in [('log.json','诊断 μ 🧪\n'),('empty-log.json','')]:
        write(name,wrap(text=text,next_offset=len(text.encode()),eof=True,truncated=False,available=True,scope='case'),'log')
    write('discovery.json',wrap(discovery={'id':'discovery-A1','status':'completed','href':'/api/test-discoveries/discovery-A1','freshness':'fresh','inventory_revision':'fixture-v1','selection':['unit'],'environment':None,'test_cases_href':'/api/test-discoveries/discovery-A1/test-cases','metadata_diagnostics':[]}),'discovery_response')
    events=[{**{name:context.get(name) for name in ('job_id','run_id','step_id','target_id')},'schema_version':1,'seq':1,'time':'2026-01-01T00:00:00Z','instance_id':rows[0]['instance_id'],'attempt':1,'event_type':'case_finished','payload':{'state':'finished','outcome':'passed'}}]
    write('events.json',wrap(items=events,next_cursor='opaque-fixture-cursor'),'events')
    many=[{**rows[0],'instance_id':f'fixture-instance-{i}','case_id':f'fixture-case-{i}','variant_key':str(i),'native_id':f'tests/fixture.py::test_contract[{i}]'} for i in range(635)]
    first=page(many,'fixture-635',{});write('page-1.json',wrap(**first),'list')
    write('page-2.json',wrap(**page(many,'fixture-635',{'cursor':[first['next_cursor']]})),'list')
    write('query-conflict.json',{'ok':False,'api_version':1,'error':{'type':'ForgeError','message':'Inventory or query changed; read the first page again.','hint':None}},'error')
    (output/'fixtures.json').write_text(json.dumps({'contract_version':1,'fixture_only':True,'schemas':mapping},indent=2)+'\n')

if __name__=='__main__':
    build(Path(__file__).resolve().parents[1]/'tests/fixtures/test_reports/v1')
