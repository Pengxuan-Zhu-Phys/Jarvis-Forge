"""Forge-owned report orchestration shared by CLI, runner and dashboard."""
from copy import deepcopy
import json
from pathlib import Path
import shutil

from . import test_metadata, test_report_runtime as store
from .paths import named_path
from .storage import ForgeError, read_json, write_json

ROOT='test-report-v1'

def evidence_files(root):
    """Immutable facts bind reuse; rebuilding HTML is presentation maintenance."""
    root=Path(root)
    for path in root.rglob('*'):
        if not path.is_file():continue
        relative=path.relative_to(root).parts
        if relative in (('inventory.json',),('metadata.json',)):
            yield path
        elif len(relative)>=3 and relative[0]=='cases':
            if relative[2]=='artifacts' or len(relative)==3 and (relative[2]=='result.json' or path.suffix=='.log'):
                yield path

def directory(home,run_id,step):
    return named_path(home.run_path(run_id)/'steps',step)/ROOT

def context(request,step):
    return {'project_id':request.get('project_id') or request['suite_id'],'project':request['project'],
            'run_id':request['run_id'],'job_id':request.get('playlist_job_id'),'step_id':step,
            'target_id':(request.get('environment') or {}).get('name') or 'local',
            'source':request.get('source'),'environment_digest':store.key(request.get('environment'))}

def prepare_run(run):
    metadata=run.test_metadata
    run.request['metadata_digest']=metadata['digest']
    write_json(run.directory/'test-metadata.json',metadata)
    for item in run.selection['ids']:
        group=run.card['groups'].get(item)
        from . import testing
        step=testing.step_id(item,run.index,run.count) if group else item
        step_dir=named_path(run.directory/'steps',step,create=True)
        root=step_dir/ROOT
        cfg=context(run.request,step)
        cfg['environment_digest']=store.key(run.request.get('environment'),run.request.get('packages_digest'),run.observed,
                                            {name:group.get(name) for name in ('env','pythonpath','isolate')} if group else None)
        if metadata.get('project_id'): cfg['project_id']=metadata['project_id']
        store.atomic(root/'context.json',cfg)
        store.atomic(root/'metadata.json',metadata)
        if not group:
            row=store.make_row(item,cfg,metadata,0,adapter='command')
            row.update(file=None,group={'file':'Command checks'},source=cfg['source'],environment_digest=cfg['environment_digest'])
            store.save_case(root,row)
            store.atomic(root/'inventory.json',{'items':[row],'confirmed':True,'inventory_revision':store.key(row['instance_id'])})
            store.atomic(root/'collection.json',{'status':'completed'})
        else:
            store.atomic(root/'inventory.json',{'items':[],'confirmed':False,'inventory_revision':store.key(cfg)})
            store.atomic(root/'collection.json',{'status':'queued'})
        store.update_progress(root,cfg)

def configure_plugin(step_dir,project,rootdir=None):
    root=Path(step_dir)/ROOT
    if not (root/'context.json').exists():
        return  # Direct identity-only callers do not create an execution snapshot.
    base=Path(__file__).parent
    plugin=Path(step_dir)/'plugin'
    shutil.copyfile(base/'test_report_runtime.py',plugin/'jforge_reports_runtime.py')
    shutil.copyfile(base/'test_report_plugin.py',plugin/'jforge_test_plugin.py')
    write_json(plugin/'jforge_reports.json',{'root':str(root),'context':read_json(root/'context.json'),
                                           'metadata':read_json(root/'metadata.json'),'rootdir':str(rootdir or project)})

def _reuse(root,home,record,context):
    origin=record['reused_from']
    previous=directory(home,origin,record['id'])
    if not (previous/'inventory.json').exists():
        raise ForgeError('Reusable step has no new-contract per-test snapshot; rerun it with --full.',code=1)
    inventory,items=store.snapshot(previous)
    reused=[]
    for index,old in enumerate(items):
        row=deepcopy(old)
        row.update({name:context.get(name) for name in ('job_id','run_id','step_id','target_id')})
        row.update(instance_id='instance-'+store.key(context['run_id'],context['step_id'],context['target_id'],old['case_id'])[:32],
                   storage_key=store.short_case(index),execution_mode='reused',seconds=None,
                   reused_from=old.get('reused_from') or {'job_id':old.get('job_id'),'run_id':old['run_id'],
                            'step_id':old['step_id'],'instance_id':old['instance_id'],'attempt':old['attempt']})
        old_dir=previous/'cases'/old['storage_key'];new_dir=root/'cases'/row['storage_key']
        if new_dir.exists(): shutil.rmtree(new_dir)
        shutil.copytree(old_dir,new_dir,symlinks=False)
        store.save_case(root,row);reused.append(row)
        store.event(root,context,'case_finished',row,state=row['state'],outcome=row['outcome'],execution_mode='reused')
    store.atomic(root/'metadata.json',read_json(previous/'metadata.json'))
    store.atomic(root/'inventory.json',{**inventory,'items':reused,'inventory_revision':store.key([row['instance_id'] for row in reused])})
    store.atomic(root/'collection.json',{'status':'completed'})


def finalize_step(home,step_dir,record):
    root=Path(step_dir)/ROOT
    if not (root/'context.json').exists(): return
    with store.locked(root):
        cfg=read_json(root/'context.json')
        if record['status']=='reused':
            _reuse(root,home,record,cfg)
        else:
            inventory,items=store.snapshot(root)
            if record.get('kind')!='pytest' and items:
                row=items[0];state=record['status']
                row.update(state='not_run' if state in ('blocked','not_run') else 'interrupted' if state=='interrupted' else 'finished',
                           outcome=state if state in store.OUTCOMES else None,seconds=record.get('duration_seconds'),reason=record.get('reason'))
                logs={}
                for name in ('stdout','stderr'):
                    path=Path(step_dir)/f'command-1.{name}.log'
                    if path.exists():logs[name]=path.read_text(errors='replace')
                row['log_scope']='step'
                store.save_case(root,row,logs)
                store.event(root,cfg,'case_finished',row,state=row['state'],outcome=row['outcome'])
            if not inventory['items'] and record['status']=='passed' and record.get('reason')=='empty batch':
                inventory['confirmed']=True;store.atomic(root/'inventory.json',inventory)
            if not inventory.get('confirmed'):
                collection=store.read(root/'collection.json',{})
                if collection.get('status') in ('queued','running'):
                    collection.update(status='interrupted' if record['status']=='interrupted' else 'failed',
                                      error=record.get('reason') or 'Test collection was not confirmed.')
                    store.atomic(root/'collection.json',collection)
            store.finish(root,cfg,record.get('reason') or 'The execution step ended.',interrupted=record['status']=='interrupted')
        step_report={'schema_version':1,'kind':'test-step-report','step_id':record['id'],'run_id':cfg['run_id'],
                     'status':record['status'],'reason':record.get('reason'),'collection':store.read(root/'collection.json',{}),
                     'inventory_confirmed':store.read(root/'inventory.json',{}).get('confirmed',False),'metadata_digest':store.read(root/'metadata.json',{}).get('digest')}
        store.atomic(root/'step-report.json',step_report)
        from html import escape
        store.atomic(root/'step-report.html','<!doctype html><meta charset="utf-8"><title>Execution step report</title><h1>Execution step report</h1><pre>'+escape(json.dumps(step_report,ensure_ascii=False,indent=2))+'</pre>',text=True)
        store.update_progress(root,cfg)


def get_case(root,instance):
    root=Path(root)
    index=store.read(root/'state-index.json')
    candidates=index['items'].values() if index is not None else store.read(root/'inventory.json',{}).get('items',[])
    item=next((item for item in candidates if item['instance_id']==instance),None)
    row=None
    if item is not None:
        directory_=(root/'cases'/item['storage_key']).resolve()
        if not directory_.is_relative_to((root/'cases').resolve()):
            raise ForgeError('Invalid test case storage reference.',code=500)
        row=store.read(directory_/'result.json')
    if row is None:raise ForgeError('Unknown test instance in this run.',code=404)
    report=store.read(directory_/'report.json',{})
    return {**row,'report_status':report.get('status',row.get('report_status'))},directory_


def run_case(home,run_id,instance,format='json',*,rebuild=False):
    path=home.run_path(run_id)
    for context_file in (path/'steps').glob(f'*/{ROOT}/context.json'):
        root=context_file.parent
        try:row,directory_=get_case(root,instance)
        except ForgeError as exc:
            if exc.code==404:continue
            raise
        if rebuild:globals()['rebuild'](root,instance)
        if format=='html':
            report=read_json(directory_/'report.json')
            if report['status']=='failed' or not (directory_/'report.html').is_file():
                raise ForgeError('HTML report is unavailable; rebuild it from the recorded facts.',code=1)
            return (directory_/'report.html').read_text()
        return read_json(directory_/'report.json')
    raise ForgeError('Test instance is not part of this run.',code=404)


def rebuild(root,instance):
    with store.locked(root):
        row,directory_=get_case(root,instance)
        previous=store.read(directory_/'report.json',{})
        revision=previous.get('render_revision',1)
        archive=directory_/'revisions'/f'{revision:04d}-render'
        archive.mkdir(parents=True,exist_ok=True)
        for name in ('report.json','report.html'):
            if (directory_/name).exists():shutil.copyfile(directory_/name,archive/name)
        store.save_case(root,row,render_revision=revision+1,persist=False)
        return read_json(directory_/'report.json')
