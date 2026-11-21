"""Final-only leaf evidence for registered command sources, separate from pytest counts."""
from pathlib import Path

from . import test_metadata, test_reports, test_report_runtime as store
from .storage import read_json


def prepare(home,directory,job,units):
    for index,step in enumerate(job['cases']):
        if step['kind']!='command':continue
        unit=units[step['unit']]
        root=Path(directory)/f"unit-{step['unit']}"/'reports'/f'step-{index+1}'/test_reports.ROOT
        metadata=test_metadata.project_metadata(unit['root'])
        cfg={'project_id':metadata.get('project_id') or unit['project'],'project':unit['root'],'job_id':job['id'],'run_id':job['id'],
             'step_id':step['id'],'target_id':unit['environment'] or 'local'}
        row=store.make_row(step['id'],cfg,metadata,0,adapter='command')
        row.update(title=step['title'],file=None,group={'file':'Command checks'},log_scope='step')
        store.atomic(root/'context.json',cfg);store.atomic(root/'metadata.json',metadata)
        store.atomic(root/'inventory.json',{'items':[row],'confirmed':True,'inventory_revision':store.key(row['instance_id'])})
        store.atomic(root/'collection.json',{'status':'completed'});store.save_case(root,row);store.update_progress(root,cfg)


def finish(home,directory,job,unit=None):
    for index,step in enumerate(job['cases']):
        if step['kind']!='command' or unit is not None and step['unit']!=unit:continue
        root=Path(directory)/f"unit-{step['unit']}"/'reports'/f'step-{index+1}'/test_reports.ROOT
        if not (root/'context.json').exists():continue
        with store.locked(root):
            cfg=read_json(root/'context.json');_,rows=store.snapshot(root)
            if not rows:continue
            row=rows[0];state=step['status']
            if row['state'] not in ('queued','running'):continue
            row.update(state=state if state in ('queued','running','interrupted','not_run') else 'finished',
                       outcome=state if state in store.OUTCOMES else None,
                       seconds=step.get('seconds'),reason=step.get('reason'))
            logs={};path=Path(directory)/f"unit-{step['unit']}"/'output.log'
            if path.exists():logs['stdout']=path.read_text(errors='replace')
            # The command protocol does not supply a reusable instance identity.
            # Never invent an execution or provenance for its old-style reused flag.
            if state=='reused':row.update(execution_mode='reused',reason='Command source reported reuse; no original per-test evidence reference was supplied.',seconds=None)
            store.save_case(root,row,logs)
            if row['state'] not in ('queued','running'):store.event(root,cfg,'case_finished',row,state=row['state'],outcome=row['outcome'])
            store.update_progress(root,cfg)
            step_report={'id':step['id'],'status':state,'reason':step.get('reason')}
            store.atomic(root/'step-report.json',step_report)
            from html import escape
            store.atomic(root/'step-report.html','<!doctype html><meta charset="utf-8"><h1>Command check</h1><pre>'+escape(str(step_report))+'</pre>',text=True)
