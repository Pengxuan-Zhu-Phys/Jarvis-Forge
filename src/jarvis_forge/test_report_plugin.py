"""Pytest adapter, copied beside the existing identity plugin for each invocation."""
import json
import hashlib
import os
from pathlib import Path
import pytest
import jforge_reports_runtime as store

_CONFIG=Path(__file__).with_name('jforge_reports.json')
_config=None

def config():
    global _config
    if _config is None:
        _config=store.read(_CONFIG,{})
    return _config

def enabled():
    return bool(config().get('root')) and not os.environ.get('PYTEST_XDIST_WORKER')

def native(nodeid):
    root=Path(config()['context']['project'])
    path,sep,suffix=nodeid.partition('::')
    full=Path(path)
    if not full.is_absolute():
        full=Path(config().get('rootdir',root))/path
    try:
        path=full.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        pass
    return path+(sep+suffix if sep else '')

def row_for(root,nodeid):
    return store.case_row(root,native(nodeid))

def pytest_configure(config):
    cfg=globals()['config']()
    if cfg.get('root'):
        cfg['rootdir']=str(config.rootpath)
        cfg['context']['adapter_version']=pytest.__version__

def pytest_sessionstart(session):
    if not enabled(): return
    cfg=config()
    with store.locked(cfg['root']) as root:
        store.atomic(root/'metadata.json',cfg['metadata'])
        store.atomic(root/'collection.json',{'status':'running','updated_at':store.utc(),'errors':[]})
        store.event(root,cfg['context'],'collection_started')
        store.update_progress(root,cfg['context'])

def pytest_collectreport(report):
    if not enabled() or not report.failed: return
    cfg=config()
    with store.locked(cfg['root']) as root:
        value=store.read(root/'collection.json',{})
        value.setdefault('errors',[]).append({'native_id':report.nodeid,'message':str(report.longrepr)})
        value['status']='failed'
        store.atomic(root/'collection.json',value)

def collect_ids(ids):
    cfg=config()
    with store.locked(cfg['root']) as root:
        errors=store.read(root/'collection.json',{}).get('errors',[])
        store.collect(root,cfg['context'],cfg['metadata'],[native(item) for item in ids],store.read(root/'parameters.json',{}))
        if errors:
            inventory=store.read(root/'inventory.json',{})
            inventory['confirmed']=False
            store.atomic(root/'inventory.json',inventory)
            store.atomic(root/'collection.json',{'status':'failed','errors':errors,'updated_at':store.utc()})
            store.update_progress(root,cfg['context'])

def pytest_collection_finish(session):
    if not config().get('root'): return
    def value(parameter):
        try:
            json.dumps(parameter,allow_nan=False)
            return parameter
        except (ValueError,TypeError):
            return {'representation':repr(parameter),'type':type(parameter).__name__}
    parameters={native(item.nodeid):{name:value(parameter) for name,parameter in getattr(getattr(item,'callspec',None),'params',{}).items()} for item in session.items}
    with store.locked(config()['root']) as root:
        store.atomic(root/'parameters.json',parameters)
    if enabled() and not session.config.getoption('numprocesses',default=None):
        collect_ids([item.nodeid for item in session.items])

@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node,ids):
    if enabled(): collect_ids(ids)

def pytest_runtest_logstart(nodeid,location):
    if not enabled(): return
    cfg=config()
    with store.locked(cfg['root']) as root:
        row=row_for(root,nodeid)
        if not row: return
        row.update(state='running',started_at=store.utc())
        store.save_case(root,row)
        store.event(root,cfg['context'],'case_started',row)
        store.update_progress(root,cfg['context'])

def pytest_runtest_logreport(report):
    if not enabled(): return
    cfg=config()
    with store.locked(cfg['root']) as root:
        row=row_for(root,report.nodeid)
        if not row: return
        if any(phase['phase']==report.when for phase in row['phases']): return
        message=str(report.longrepr) if report.longrepr else None
        original=report.outcome
        phase={'phase':report.when,'outcome':original,'seconds':report.duration,'message':message,
               'wasxfail':getattr(report,'wasxfail',None),'captures':[]}
        logs={}
        for label,text in report.sections:
            if not label.endswith(' '+report.when): continue
            stream=next((name for name in ('stdout','stderr','log') if label.startswith('Captured '+name+' ')),None)
            if not stream: continue
            stream='logging' if stream=='log' else stream
            if report.when=='call':
                logs[stream]=text
            else:
                # Fixture output can be shared at module/session scope. Never claim it
                # as exclusive output for the first item that happens to trigger it.
                phase['captures'].append({'stream':stream,'scope':'step','text':text})
        if message: logs['traceback']='\n\n'.join(filter(None,[next((part.get('message') for part in row['phases'] if part.get('message')),None),message]))
        row['phases'].append(phase)
        store.event(root,cfg['context'],'phase_finished',row,phase=report.when,outcome=original)
        if report.when=='teardown':
            failed=[part for part in row['phases'] if part['outcome']=='failed']
            if any(part['phase']!='call' and part['outcome']=='failed' for part in row['phases']): outcome='error'
            elif failed: outcome='xpassed' if any('[XPASS(strict)]' in (part['message'] or '') for part in failed) else 'failed'
            elif any(part.get('wasxfail') is not None for part in row['phases']):
                outcome='xfailed' if any(part['outcome']=='skipped' and part.get('wasxfail') is not None for part in row['phases']) else 'xpassed'
            elif any(part['outcome']=='skipped' for part in row['phases']): outcome='skipped'
            else: outcome='passed'
            row.update(state='finished',outcome=outcome,seconds=sum(part['seconds'] for part in row['phases']),
                       finished_at=store.utc(),reason=next((part['message'] or part.get('wasxfail') for part in row['phases'] if part['message'] or part.get('wasxfail')),None))
            store.event(root,cfg['context'],'case_finished',row,state='finished',outcome=outcome)
        store.save_case(root,row,logs)
        if report.when=='teardown': store.update_progress(root,cfg['context'])

def pytest_sessionfinish(session,exitstatus):
    if not enabled(): return
    cfg=config()
    with store.locked(cfg['root']) as root:
        store.finish(root,cfg['context'],reason=f'pytest session exited {int(exitstatus)}',interrupted=int(exitstatus)==2)

@pytest.fixture
def jforge_evidence(request):
    """Explicitly associate a step-owned file/bundle with this instance and criterion."""
    def register(path,criterion_id=None,expected_id=None):
        import shutil
        cfg=config();step=Path(cfg['root']).parent.resolve();source=Path(path).resolve()
        if not source.is_relative_to(step) or not source.exists():
            raise ValueError('Evidence must be an existing file or bundle inside JFORGE_STEP_DIR.')
        if source.is_relative_to(Path(cfg['root']).resolve()):
            raise ValueError('Report internals cannot be registered as evidence.')
        with store.locked(cfg['root']) as root:
            row=row_for(root,request.node.nodeid)
            if row is None: raise ValueError('This test has no report instance.')
            if criterion_id and criterion_id not in {item['criterion_id'] for item in row['criteria']}:
                raise ValueError('Unknown criterion ID.')
            expected={item.get('id') if isinstance(item,dict) else item for item in row.get('expected_evidence',[])}
            if expected_id and expected_id not in expected:raise ValueError('Unknown expected evidence ID.')
            number=len(row['artifacts'])+1
            name=f'evidence-A{number}'
            target=root/'cases'/row['storage_key']/'artifacts'/name
            target.mkdir(parents=True)
            sources=list(source.rglob('*')) if source.is_dir() else [source]
            allowed={'.png','.jpg','.jpeg','.svg','.webp','.gif','.html','.css','.js','.json','.csv','.txt','.pdf'}
            registered=[]
            for item in sources:
                if item.is_symlink() or not item.is_file() or not item.resolve().is_relative_to(step) or item.suffix.lower() not in allowed: continue
                relative=item.relative_to(source) if source.is_dir() else Path(item.name)
                dest=target/relative;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(item,dest)
                registered.append(relative.as_posix())
            if not registered: raise ValueError('No supported evidence files registered.')
            entry='index.html' if 'index.html' in registered else registered[0]
            artifact={'id':name,'name':source.name,'scope':'case','criterion_id':criterion_id,'expected_id':expected_id,'relative_href':f'artifacts/{name}/{entry}',
                      'files':registered,'sha256':{item:hashlib.sha256((target/item).read_bytes()).hexdigest() for item in registered}}
            row['artifacts'].append(artifact)
            if criterion_id:
                next(item for item in row['criteria'] if item['criterion_id']==criterion_id)['evidence_refs'].append(name)
            store.save_case(root,row)
            store.event(root,cfg['context'],'artifact_registered',row,artifact=artifact)
            return artifact
    return register


@pytest.fixture
def jforge_observe(request):
    """Record an explicitly instrumented automated criterion; never complete manual review."""
    def observe(criterion_id,observed,*,passed=None,evidence_refs=()):
        if passed is not None and type(passed) is not bool:raise ValueError('passed must be a boolean or None.')
        json.dumps(observed,allow_nan=False)
        cfg=config()
        with store.locked(cfg['root']) as root:
            row=row_for(root,request.node.nodeid)
            if row is None:raise ValueError('This test has no report instance.')
            criterion=next((item for item in row['criteria'] if item['criterion_id']==criterion_id),None)
            if criterion is None:raise ValueError('Unknown criterion ID.')
            if criterion['verification']!='automated':raise ValueError('Manual review cannot be completed by a test helper.')
            if any(ref not in {item['id'] for item in row['artifacts']} for ref in evidence_refs):raise ValueError('Unknown case evidence reference.')
            criterion.update(observed=observed,status='passed' if passed is True else 'failed' if passed is False else 'unobserved',evidence_refs=list(evidence_refs))
            store.save_case(root,row)
        return observed
    return observe


def pytest_deselected(items):
    if not config().get('root'):return
    with store.locked(config()['root']) as root:
        store.atomic(root/'deselected.json',[native(item.nodeid) for item in items])
