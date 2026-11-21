"""Portable report store copied into the selected pytest interpreter's plugin path.

Only stdlib dependencies: the project interpreter need not have Forge installed.
Facts survive the dashboard and renderer; HTML never supplies execution outcomes.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
from html import escape
import json
import os
from pathlib import Path
import tempfile

TEMPLATE = 'forge-test-report-v1'
OUTCOMES = ('passed', 'failed', 'error', 'skipped', 'xfailed', 'xpassed')

def utc():
    return datetime.now(timezone.utc).isoformat()

def key(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default

def atomic(path, value, *, text=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value if text else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

@contextmanager
def locked(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.report.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield root

def events(root):
    return [record for record,offset in event_records(root)]

def event_records(root, offset=0):
    """Stream complete records and byte cursors without loading the history."""
    path = Path(root)/'events.jsonl'
    if not path.exists():
        return
    with path.open('rb') as stream:
        stream.seek(offset)
        for line in stream:
            if not line.endswith(b'\n'):
                break
            offset+=len(line)
            yield json.loads(line),offset

def event_head(root):
    """Recover the event head and truncate only an unfinished final append.

    Caller owns .report.lock. The normal path only stats the stream; recovery
    scans the suffix since the last committed head, never the entire history.
    """
    root = Path(root)
    path = root / 'events.jsonl'
    head = read(root / 'event-head.json', {})
    size = path.stat().st_size if path.exists() else 0
    sequence = head.get('seq', 0)
    if not path.exists():
        if head:
            atomic(root/'event-head.json', {'seq':0,'offset':0})
        return {'seq':0,'offset':0}
    if head.get('offset', 0) != size:
        # Recover a completed append whose head update was interrupted, or a
        # partial tail. Normal events do not scan the growing history.
        offset = head.get('offset', 0)
        if not isinstance(offset, int) or offset < 0 or offset > size:
            offset = sequence = 0
        with path.open('r+b') as stream:
            stream.seek(offset)
            for line in stream:
                if not line.endswith(b'\n'):
                    break
                sequence = json.loads(line)['seq']
                offset += len(line)
            stream.truncate(offset)
        atomic(root/'event-head.json', {'seq':sequence, 'offset':offset})
    return {'seq':sequence,'offset':path.stat().st_size if path.exists() else 0}

def event(root, context, kind, row=None, **payload):
    root = Path(root)
    path = root/'events.jsonl'
    head = event_head(root)
    record = {name: context.get(name) for name in ('job_id', 'run_id', 'step_id', 'target_id')}
    record.update(schema_version=1, seq=head['seq']+1, time=utc(), event_type=kind,
                  instance_id=(row or {}).get('instance_id'), attempt=(row or {}).get('attempt', 1), payload=payload)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    atomic(root/'event-head.json', {'seq':record['seq'], 'offset':path.stat().st_size})
    return record


def case_row(root, native):
    """Locate one case without loading every result in its execution step."""
    index = read(Path(root)/'state-index.json', {})
    storage = index.get('native', {}).get(native)
    if storage is None:
        inventory = read(Path(root)/'inventory.json', {})
        storage = next((row['storage_key'] for row in inventory.get('items', []) if row['native_id']==native), None)
    return read(Path(root)/'cases'/storage/'result.json') if storage else None

def snapshot(root):
    inventory = read(Path(root) / 'inventory.json', {})
    items = []
    for item in inventory.get('items', []):
        items.append(read(Path(root) / 'cases' / item['storage_key'] / 'result.json', item))
    return inventory, items

def rebuild_index(root):
    """Recreate the disposable lookup/count index from persisted case facts."""
    _, rows = snapshot(root)
    index = {'native':{}, 'items':{}}
    for row in rows:
        index['native'][row['native_id']] = row['storage_key']
        index['items'][row['storage_key']] = {name:row.get(name) for name in
                ('storage_key','case_id','instance_id','state','outcome','execution_mode')}
        report=read(Path(root)/'cases'/row['storage_key']/'report.json',{})
        index['items'][row['storage_key']]['report_status']=report.get('status',row.get('report_status'))
    by_instance = {row['instance_id']:row for row in index['items'].values()}
    # Events can be durable before the last result publication completes.
    # Replaying assignments, rather than incrementing counters, is idempotent.
    for record in events(root):
        row = by_instance.get(record.get('instance_id'))
        if row is None:
            continue
        if record['event_type']=='case_started' and row['state'] in ('queued','running'):
            row.update(state='running',outcome=None)
        elif record['event_type']=='case_finished':
            payload = record['payload']
            row.update(state=payload['state'],outcome=payload.get('outcome'))
            if payload.get('execution_mode'):
                row['execution_mode']=payload['execution_mode']
    atomic(Path(root)/'state-index.json', index)
    return index

def progress_snapshot(root):
    """Read a small snapshot; repair interrupted publication without running tests."""
    root = Path(root)
    value = read(root/'progress.json')
    head = read(root/'event-head.json', {})
    path = root/'events.jsonl'
    size = path.stat().st_size if path.exists() else 0
    if value is not None and value.get('revision') == head.get('seq', 0) and head.get('offset',0)==size:
        return value
    with locked(root):
        event_head(root)
        rebuild_index(root)
        return update_progress(root, read(root/'context.json', {}))

def count(items, confirmed=True):
    return {'unit':'test_case', 'total':len(items) if confirmed else None, 'total_confirmed':confirmed,
            'completed':sum(item['state']=='finished' for item in items),
            'running':sum(item['state']=='running' for item in items),
            'pending':sum(item['state']=='queued' for item in items),
            'interrupted':sum(item['state']=='interrupted' for item in items),
            'not_run':sum(item['state']=='not_run' for item in items),
            'outcomes':{outcome:sum(item.get('outcome')==outcome and item['state']=='finished' for item in items) for outcome in OUTCOMES},
            'reused':sum(item.get('execution_mode')=='reused' and item['state']=='finished' for item in items)}

def update_progress(root, context):
    root = Path(root)
    inventory = read(root/'inventory.json', {})
    index = read(root/'state-index.json')
    items = list((index if index is not None else rebuild_index(root))['items'].values())
    state = read(Path(root)/'collection.json', {})
    current = next((item for item in items if item['state']=='running'), None)
    if current is not None:
        current = {**read(root/'cases'/current['storage_key']/'result.json', {}),**current}
    value = {**{name:context.get(name) for name in ('job_id','run_id','step_id','target_id')},
             'api_version':1,'revision':read(root/'event-head.json',{}).get('seq',0), 'updated_at':utc(),
             'collection_status':state.get('status','running'),
             'summaries':[count(items, inventory.get('confirmed',False))],
             'current_test':current}
    atomic(Path(root)/'progress.json',value)
    return value

def short_case(index):
    return f"{index+1:04d}-test"

def make_row(native, context, metadata, index, *, adapter='pytest', parameters=None):
    base = native.split('[',1)[0]
    definition = next((item for item in metadata.get('definitions',[]) if item['binding']['adapter']==adapter and item['binding']['selector']==base), None)
    if definition is None:
        definition = next((item for item in metadata.get('definitions',[]) if item['binding']['adapter']==adapter and item['binding']['selector']==native), None)
    description = json.loads(json.dumps(definition or {}))
    variant = native[len(base)+1:-1] if native != base and native.endswith(']') else 'default'
    override = description.get('variants',{}).get(variant,{})
    if override:
        description.update({name:value for name,value in override.items() if name!='scenario'})
        description['scenario']={**description.get('scenario',{}),**override.get('scenario',{})}
    definition_id = description.get('id')
    case = 'case-' + key(context['project_id'], definition_id or base, adapter, variant)[:32]
    instance = 'instance-' + key(context['run_id'],context['step_id'],context['target_id'],case)[:32]
    criteria = [{**criterion, 'criterion_id':criterion['id'], 'status':'unobserved', 'observed':None, 'evidence_refs':[],
                 **({'review_status':'pending'} if criterion['verification']=='manual' else {})} for criterion in description.get('criteria',[])]
    return {**description, **{name:context.get(name) for name in ('project_id','job_id','run_id','step_id','target_id')},
            'case_id':case,'instance_id':instance,'definition_id':definition_id,'adapter':adapter,'native_id':native,
            'variant_key':variant,'parameters':parameters if parameters is not None else {'pytest_id':variant},'group':{'file':native.split('::')[0]},
            'source':context.get('source'),'environment_digest':context.get('environment_digest'),
            'file':native.split('::')[0], 'title':description.get('title',native), 'criteria':criteria,
            'metadata_status':'complete' if definition else 'missing','metadata_digest':metadata.get('digest'),
            'review':description.get('review',{'status':'draft'}), 'storage_key':short_case(index),
            'state':'queued','outcome':None,'execution_mode':'executed','attempt':1,'attempts':[1],
            'seconds':None,'phases':[], 'artifacts':[], 'report_status':'provisional'}

def collect(root, context, metadata, natives, parameters=None):
    inventory = read(Path(root)/'inventory.json', {'items':[],'confirmed':False})
    known = {item['native_id']:item for item in inventory['items']}
    for native in natives:
        if native not in known:
            item = make_row(native,context,metadata,len(known),parameters=(parameters or {}).get(native))
            item["parameter_values_available"] = native in (parameters or {})
            item["adapter_version"] = context.get("adapter_version")
            known[native]=item
            save_case(root,item)
    inventory.update(items=list(known.values()), confirmed=True)
    inventory['inventory_revision']=key([(item['instance_id'],item['metadata_digest']) for item in inventory['items']])
    atomic(Path(root)/'inventory.json',inventory)
    atomic(Path(root)/'collection.json',{'status':'completed','updated_at':utc()})
    event(root,context,'collection_finished',total=len(known),inventory_revision=inventory['inventory_revision'])
    update_progress(root,context)

def report_data(row):
    return {**row,'schema_version':1,'template_version':TEMPLATE,
            'status':'provisional' if row['state'] in ('queued','running') else 'ready',
            'generation_error':None,'generated_at':utc()}

def render(report):
    def text(value):
        return escape(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2),quote=True)
    def block(title,value):
        return '<section><h2>'+text(title)+'</h2><pre>'+text(value)+'</pre></section>'
    criteria=''.join('<li><strong>'+text(item['expected'])+'</strong><p>'+text(item['verification'])+' · '+text(item.get('status','unobserved'))+'</p><pre>'+text(item.get('observed') if item.get('observed') is not None else 'No independent observation recorded. Test outcome does not complete manual review.')+'</pre></li>' for item in report.get('criteria',[]))
    logs=''.join(block(name,report.get('logs',{}).get(name) or 'No additional output.') for name in ('stdout','stderr','logging','traceback'))
    links=''.join('<li><a href="'+text(item['relative_href'])+'">'+text(item['name'])+'</a> · '+text(item.get('scope','case'))+'</li>' for item in report.get('artifacts',[]) if item.get('relative_href'))
    identity={name:report.get(name) for name in ('project_id','definition_id','case_id','instance_id','native_id','variant_key','parameters','run_id','step_id','target_id','attempt','seconds','reused_from','source','environment_digest','metadata_digest','adapter','template_version','render_revision')}
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>'+text(report['title'])+'</title><style>body{font:16px/1.65 system-ui,sans-serif;color:#203340;background:#f5f8fa;margin:0 auto;padding:32px;max-width:1000px}section{background:white;border:1px solid #d5e0e6;border-radius:12px;padding:24px;margin:20px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 ui-monospace,monospace}h1{line-height:1.25}h2{font-size:20px}a{color:#14795d}.state{background:#def4e9;padding:8px 16px;border-radius:6px;display:inline-block}</style><main><h1>'+text(report['title'])+'</h1><p class="state">'+text(report['status'])+' · '+text(report.get('outcome') or report['state'])+'</p>'+block('Identity & provenance',identity)+block('Purpose & coverage',{'purpose':report.get('purpose','Metadata not supplied.'),'component':report.get('component'),'owner':report.get('owner'),'limitations':report.get('limitations'),'review':report.get('review'),'references':report.get('references',[])})+block('Scenario',report.get('scenario','Metadata not supplied.'))+'<section><h2>Expected criteria & observations</h2><ul>'+criteria+'</ul></section>'+block('Actual result & failure meaning',{'state':report['state'],'outcome':report.get('outcome'),'reason':report.get('reason'),'phases':report.get('phases',[]),'failure_meaning':report.get('failure_meaning')})+'<section><h2>Evidence</h2><ul>'+links+'</ul><pre>'+text({'expected':report.get('expected_evidence',[]),'missing':report.get('missing_evidence',[]),'shared_step_artifacts':report.get('step_artifacts',[])})+'</pre></section>'+logs+'<p>HTML download does not bundle linked evidence. Execution outcome, criterion observations and formal acceptance remain separate.</p></main></html>'

def save_case(root,row,logs=None,*,render_revision=None,persist=True):
    directory=Path(root)/'cases'/row['storage_key']
    if logs is not None:
        for stream,text in logs.items():
            atomic(directory/f'{stream}.log',text,text=True)
    expected=row.get('expected_evidence',[])
    registered={item.get('expected_id') for item in row.get('artifacts',[])}
    row['missing_evidence']=[item for item in expected if (item.get('id') if isinstance(item,dict) else item) not in registered]
    row['report_status']='provisional' if row['state'] in ('queued','running') else 'ready'
    if persist:
        atomic(directory/'result.json',row)
    report=report_data(row)
    previous=read(directory/'report.json',{})
    report['render_revision']=render_revision or previous.get('render_revision',1)
    try:
        report['logs']={name:(directory/f'{name}.log').read_text() if (directory/f'{name}.log').exists() else '' for name in ('stdout','stderr','logging','traceback')}
        atomic(directory/'report.html',render(report),text=True)
    except Exception as exc:
        report.update(status='failed',generation_error=str(exc))
        (directory/'report.html').unlink(missing_ok=True)
    atomic(directory/'report.json',report)
    # Presentation status is derived data. Rebuilds update this lookup without
    # rewriting the immutable execution result or its reuse fingerprint.
    index=read(Path(root)/'state-index.json')
    if index is None:
        index={'native':{},'items':{}} if persist else rebuild_index(root)
    index['native'][row['native_id']]=row['storage_key']
    index['items'][row['storage_key']]={name:row.get(name) for name in
            ('storage_key','case_id','instance_id','state','outcome','execution_mode')}
    index['items'][row['storage_key']]['report_status']=report['status']
    atomic(Path(root)/'state-index.json',index)


def finish(root,context,reason='Session ended before this test finished.', interrupted=False):
    inventory,items=snapshot(root)
    for row in items:
        if row['state'] in ('queued','running'):
            row.update(state='interrupted' if interrupted and row['state']=='running' else 'not_run',outcome=None,reason=reason)
            save_case(root,row)
            event(root,context,'case_finished',row,state=row['state'],outcome=None)
    previous=events(root)
    if not previous or previous[-1]['event_type'] != 'session_finished':
        event(root,context,'session_finished',reason=reason)
    update_progress(root,context)
