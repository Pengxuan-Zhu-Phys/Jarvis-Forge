"""Read APIs and query-bound opaque cursors over persisted adapter snapshots."""
import base64
from collections import Counter, defaultdict
import json
from pathlib import Path
from urllib.parse import quote

from . import playlist_jobs as jobs, playlist_library as library, test_discovery, test_reports
from .test_report_runtime import atomic, count, key, locked, read, snapshot, progress_snapshot, OUTCOMES, event_records
from .storage import ForgeError, identifier


def capabilities(home,selected):
    indexes=read(library._state(home,selected)/'discoveries.json',{})
    inventories={}
    for album,ident in indexes.items():
        try:inventories[album]=test_discovery.get(home,selected,ident)
        except ForgeError:continue
    return {'api_version':1,'features':{'test_cases':True,'test_reports':True,'test_discovery':True},
            'adapters':[{'adapter':'pytest','enumeration':'exact','progress':'case_events','logs':['case_captured','step_only'],
                         'cancellation':'supported','artifacts':['case_bound','step_bound']},
                        {'adapter':'command','enumeration':'exact','progress':'final_only','logs':['step_only'],
                         'cancellation':'supported','artifacts':['step_bound']}], 'inventories':inventories}


def roots(home,selected,job_id):
    job=jobs.get(home,selected,job_id);result=[]
    for step in job['cases']:
        if step.get('run_id') and step['kind']!='command':
            root=test_reports.directory(home,step['run_id'],step['id'].rsplit('/',1)[1])
        elif step['kind']=='command':
            root=jobs._directory(home,selected,job_id)/f"unit-{step['unit']}"/'reports'/identifier('step-'+str(job['cases'].index(step)+1))/test_reports.ROOT
        else:root=None
        result.append((step,root))
    return job,result


def items(home,selected,job_id):
    job,entries=roots(home,selected,job_id);rows=[];revisions=[];confirmed=True
    for step,root in entries:
        inventory,values=snapshot(root) if root and root.exists() else ({},[])
        confirmed &= inventory.get('confirmed',False)
        revisions.append([step['id'],inventory.get('inventory_revision')])
        index=read(root/'state-index.json',{}).get('items',{}) if root else {}
        for row in values:
            status=index.get(row['storage_key'],{}).get('report_status')
            if status is None:
                status=read(root/'cases'/row['storage_key']/'report.json',{}).get('status',row.get('report_status'))
            rows.append({**row,'job_id':job_id,'step_id':step['id'],'report_status':status})
    return job,rows,key(revisions),confirmed


def _number(query,name,default,maximum=None):
    value=query.get(name,[str(default)])[0]
    if not isinstance(value,str) or not value.isdecimal():raise ForgeError(f'{name} must be a nonnegative integer.',code=400)
    number=int(value)
    if maximum is not None and not 1<=number<=maximum:raise ForgeError(f'{name} must be between 1 and {maximum}.',code=400)
    return number


def cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).decode().rstrip('=')

def decode_cursor(value):
    try:
        if len(value)>4096:raise ValueError()
        data=json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
        if not isinstance(data,dict):raise ValueError()
        return data
    except (ValueError,TypeError,UnicodeError) as exc:raise ForgeError('Invalid query cursor.',code=400) from exc


def page(rows,revision,query,*,confirmed=True):
    allowed={'step','file','state','outcome','execution_mode','q','component','tag','metadata','cursor','limit'}
    if query.keys()-allowed:raise ForgeError('Unknown test list filter.',code=400)
    values={name:value[0] for name,value in query.items() if name not in ('cursor','limit') and value and value[0]}
    facets={'component':sorted({row.get('component') for row in rows if row.get('component')}),'tag':sorted({tag for row in rows for tag in row.get('tags',[])}),'metadata':sorted({row.get('metadata_status') for row in rows if row.get('metadata_status')})}
    for name, allowed in {'state':{'queued','running','finished','interrupted','not_run'},'outcome':{'passed','failed','error','skipped','xfailed','xpassed'},'execution_mode':{'executed','reused'}}.items():
        if name in values and values[name] not in allowed:raise ForgeError(f'Unknown {name} filter.',code=400)
    signature=key(values);start=0
    if query.get('cursor'):
        data=decode_cursor(query['cursor'][0])
        if data.get('revision')!=revision or data.get('query')!=signature:raise ForgeError('Inventory or query changed; read the first page again.',code=409)
        start=data.get('offset')
        if type(start) is not int or start<0:raise ForgeError('Invalid cursor offset.',code=400)
    limit=_number(query,'limit',100,500)
    def variant_family(row):
        return (row.get('project_id'),row.get('step_id'),row.get('target_id'),row.get('adapter'),
                row.get('definition_id') or row.get('native_id','').split('[',1)[0])
    variants=Counter(variant_family(row) for row in rows)
    filtered=[]
    for row in rows:
        if any(row.get({'step':'step_id','metadata':'metadata_status'}.get(name,name))!=value for name,value in values.items() if name not in ('q','tag','file')):continue
        if values.get('file') and (row.get('file') or row.get('group',{}).get('file'))!=values['file']:continue
        if values.get('tag') and values['tag'] not in row.get('tags',[]):continue
        if values.get('q') and values['q'].lower() not in ' '.join(str(row.get(name) or '') for name in ('title','definition_id','native_id','file')).lower():continue
        filtered.append({**row,'variant_count':variants[variant_family(row)]})
    following=start+limit
    return {'api_version':1,'facets':facets,'inventory_revision':revision,'items':filtered[start:following],
            'total':len(filtered) if confirmed else None,'total_confirmed':confirmed,
            'next_cursor':cursor({'revision':revision,'query':signature,'offset':following}) if following<len(filtered) else None}


def progress(home,selected,job_id):
    job,entries=roots(home,selected,job_id);groups=defaultdict(list);steps=[];times=[];revision=0;current=None;collection_status='completed'
    for step,root in entries:
        context=read(root/'context.json',{}) if root else {}
        collection=read(root/'collection.json',{}) if root else {}
        adapter='pytest' if step['kind']=='pytest' else 'command'
        grouping=(step['project'],context.get('target_id','local'),'test_case' if adapter=='pytest' else 'check',adapter)
        source=progress_snapshot(root) if root and root.exists() else {}
        groups[grouping].extend(source.get('summaries') or [count([],False)])
        revision+=source.get('revision',0)
        if source.get('updated_at'):times.append(source['updated_at'])
        if not current and source.get('current_test'):
            current={**source['current_test'],'job_id':job_id,'step_id':step['id']}
        if adapter=='pytest' and not all(row['total_confirmed'] for row in groups[grouping]):
            collection_status='running' if job['status'] in jobs.ACTIVE and collection.get('status')!='failed' else 'failed'
        steps.append({**step,'adapter':adapter,'collection':collection,
                      'report_href':f'/api/jobs/{quote(job_id,safe="")}/steps/{job["cases"].index(step)}/report.html' if root and (root/'step-report.html').exists() else None})
    summaries=[]
    for grouping,values in groups.items():
        project,target,unit,adapter=grouping
        confirmed=all(value['total_confirmed'] for value in values)
        summary={name:sum(value[name] for value in values) for name in ('completed','running','pending','interrupted','not_run','reused')}
        summary.update(total=sum(value['total'] for value in values) if confirmed else None,total_confirmed=confirmed,
                       outcomes={outcome:sum(value['outcomes'][outcome] for value in values) for outcome in OUTCOMES})
        summaries.append({**summary,'unit':unit,'project_id':project,'target_id':target,'adapter':adapter,
                          'progress':'case_events' if adapter=='pytest' else 'final_only'})
    return {'api_version':1,'job_id':job_id,'revision':revision,'updated_at':max(times) if times else job.get('submitted_at'),
            'status':job['status'],'collection_status':collection_status,'summaries':summaries,'steps':steps,'current_test':current}


def find(home,selected,job_id,instance):
    identifier(instance)
    job,entries=roots(home,selected,job_id)
    for step,root in entries:
        if not root or not root.exists():continue
        try:row,directory=test_reports.get_case(root,instance)
        except ForgeError as exc:
            if exc.code==404:continue
            raise
        return {**row,'job_id':job_id,'step_id':step['id']},directory
    raise ForgeError('Unknown test instance in this job.',code=404)


def log(row,directory,query):
    if query.keys()-{'stream','attempt','offset','limit'}:raise ForgeError('Unknown log option.',code=400)
    stream=query.get('stream',['stdout'])[0]
    if stream not in ('stdout','stderr','logging','traceback'):raise ForgeError('Unknown log stream.',code=400)
    attempt=_number(query,'attempt',row['attempt'])
    if attempt<1:raise ForgeError('Attempt must be positive.',code=400)
    if attempt not in row['attempts']:raise ForgeError('Unknown test attempt.',code=404)
    if attempt!=row['attempt']:raise ForgeError('Requested attempt evidence is unavailable.',code=404)
    offset=_number(query,'offset',0);limit=_number(query,'limit',65536,262144)
    path=directory/f'{stream}.log'
    if not path.exists():
        report=read(directory/'report.json',{})
        cleaned=bool(report.get('logs',{}).get(stream))
        if offset and not cleaned:raise ForgeError('Log offset is beyond this stream.',code=416)
        return {'api_version':1,'text':'','next_offset':offset if cleaned else 0,'eof':row['state'] not in ('queued','running'),
                'truncated':False,'available':not cleaned,'reason':'cleaned' if cleaned else None,'scope':row.get('log_scope','case')}
    size=path.stat().st_size
    if offset>size:raise ForgeError('Log offset is beyond this stream.',code=416)
    with path.open('rb') as source:
        source.seek(offset);data=source.read(limit+4)
    if offset and data and data[0]&0xc0==0x80:raise ForgeError('Log offset is not a UTF-8 boundary.',code=400)
    cut=min(limit,len(data))
    while cut>0:
        try:text=data[:cut].decode('utf-8');break
        except UnicodeDecodeError as exc:
            if exc.reason!='unexpected end of data':raise ForgeError('Invalid UTF-8 evidence.',code=500) from exc
            cut=exc.start
    else:text=''
    if data and cut==0:raise ForgeError('Log limit must fit the next UTF-8 character.',code=400)
    following=offset+cut
    return {'api_version':1,'text':text,'next_offset':following,'eof':following>=size and row['state'] not in ('queued','running'),
            'truncated':False,'available':True,'scope':row.get('log_scope','case'),'execution_mode':row['execution_mode'],'reused_from':row.get('reused_from')}


def report(row,directory,query):
    if query.keys()-{'attempt'}:raise ForgeError('Unknown report option.',code=400)
    attempt=_number(query,'attempt',row['attempt'])
    if attempt<1:raise ForgeError('Attempt must be positive.',code=400)
    if attempt not in row['attempts']:raise ForgeError('Unknown test attempt.',code=404)
    value=read(directory/'report.json')
    if not value:raise ForgeError('Test report evidence is unavailable.',code=410)
    if attempt!=row['attempt']:raise ForgeError('Requested attempt report is unavailable.',code=404)
    base=f'/api/jobs/{quote(row["job_id"],safe="")}/test-cases/{quote(row["instance_id"],safe="")}'
    return {**value,'json_href':base+'/report','html_href':base+'/report.html' if value['status']!='failed' and (directory/'report.html').exists() else None}


def job_events(home,selected,job_id,query):
    if query.keys()-{'after'}:raise ForgeError('Unknown event option.',code=400)
    job,entries=roots(home,selected,job_id);directory=jobs._directory(home,selected,job_id)/'test-events'
    with locked(directory):
        state=read(directory/'sources.json',{'offset':0,'seq':0,'sources':{}})
        event_file=directory/'events.jsonl'
        size=event_file.stat().st_size if event_file.exists() else 0
        if state['offset']>size:state={'offset':0,'seq':0,'sources':{}}
        # Recover only appends after the last atomic merge-state publication.
        # Losing the disposable state file requires a single full replay.
        for item,offset in event_records(directory,state['offset']):
            state.update(offset=offset,seq=item['seq'])
            state['sources'][item['source_key']]={'seq':item['source_seq'],'offset':item['source_offset']}
        if size!=state['offset']:
            with event_file.open('r+b') as stream:stream.truncate(state['offset'])
        with (directory/'events.jsonl').open('a',encoding='utf-8') as output:
            for step,root in entries:
                if not root:continue
                source=key(str(root));position=state['sources'].get(source,{'seq':0,'offset':0})
                for item,offset in event_records(root,position['offset']):
                    if item['seq']<=position['seq']:
                        position['offset']=offset
                        state['sources'][source]=position
                        continue
                    state['seq']+=1
                    value={**item,'seq':state['seq'],'source_seq':item['seq'],'source_offset':offset,'source_key':source,'job_id':job_id,'step_id':step['id']}
                    output.write(json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n')
                    position={'seq':item['seq'],'offset':offset}
                    state['sources'][source]=position
            output.flush()
            import os
            os.fsync(output.fileno())
        state['offset']=event_file.stat().st_size
        atomic(directory/'sources.json',state)
    after=offset=0
    if query.get('after',[None])[0]:
        value=decode_cursor(query['after'][0])
        if value.get('job')!=job_id or any(type(value.get(name)) is not int or value[name]<0 for name in ('seq','offset')):
            raise ForgeError('Invalid event cursor.',code=400)
        after=value['seq'];offset=value['offset']
        if after>state['seq'] or offset>state['offset']:raise ForgeError('Event cursor is ahead of this stream.',code=409)
    following=[]
    for item,next_offset in event_records(directory,offset):
        if item['seq']!=after+1:raise ForgeError('Event cursor does not match this stream.',code=409)
        following.append(item);after=item['seq'];offset=next_offset
        if len(following)==500:break
    if not following and after!=state['seq']:raise ForgeError('Event cursor does not match this stream.',code=409)
    return {'api_version':1,'items':following,'next_cursor':cursor({'job':job_id,'seq':after,'offset':offset})}


def dispatch(home,selected,method,path,query,body):
    if path=='/api/capabilities' and method=='GET':return capabilities(home,selected)
    parts=path.split('/')
    if parts[:3]==['','api','test-discoveries']:
        if method=='POST' and len(parts)==3:return {'api_version':1,'discovery':test_discovery.submit(home,selected,body)}
        if method=='GET' and len(parts) in (4,5):
            discovery=test_discovery.get(home,selected,parts[3])
            if len(parts)==4:return {'api_version':1,'discovery':discovery}
            if parts[4]=='test-cases':
                inventory=read(home.root/'test-discoveries'/identifier(parts[3])/'inventory.json',{'items':[]})
                return page(inventory['items'],inventory.get('inventory_revision',parts[3]),query,confirmed=inventory.get('confirmed',False))
    if method=='POST' and parts[:3]==['','api','jobs'] and len(parts)==8 and parts[4]=='test-cases' and parts[6:]==['report','rebuild']:
        if body:raise ForgeError('Report rebuild takes an empty JSON object.',code=400)
        row,directory=find(home,selected,parts[3],parts[5])
        test_reports.rebuild(directory.parent.parent,row['instance_id'])
        return {'api_version':1,'report':report(row,directory,{})}
    if method!='GET' or parts[:3]!=['','api','jobs'] or len(parts)<5:return None
    job=parts[3];action=parts[4]
    if action=='progress' and len(parts)==5:return progress(home,selected,job)
    if action=='events' and len(parts)==5:return job_events(home,selected,job,query)
    if action!='test-cases':return None
    if len(parts)==5:
        _,rows,revision,confirmed=items(home,selected,job);return page(rows,revision,query,confirmed=confirmed)
    if len(parts) not in (6,7):return None
    row,directory=find(home,selected,job,parts[5])
    if len(parts)==6:return {'api_version':1,'test_case':row}
    if parts[6]=='log':return log(row,directory,query)
    if parts[6]=='report':return {'api_version':1,'report':report(row,directory,query)}
    return None
