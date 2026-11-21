"""Explicit, detached pytest discovery; catalog and plan never import tests."""
import os
from pathlib import Path
import subprocess
import sys

from . import checker, testing, testing_run, test_metadata, test_reports
from .test_report_runtime import atomic, key, read, snapshot, utc
from .paths import named_path
from .storage import ForgeError, ForgeHome, new_id, read_json, write_json, digest


def selection_identity(card,selection):
    """Static inputs, including non-Git projects and additions to selected globs."""
    project=Path(card['project']);inputs={}
    for ident in selection['ids']:
        group=card['groups'].get(ident)
        step=group or card['checks'][ident]
        manifest=checker.input_manifest(project,step.get('inputs',[]))
        if group:
            manifest['pytest-files']=[(name,digest(project/name)) for name in testing.group_files(card,group)]
        inputs[ident]=manifest
    return key(Path(card['path']).read_text(),selection,inputs,
               checker.input_manifest(Path(card['path']).parent,card.get('card_inputs',[])))


def file_stamp(path):
    path=Path(path);stat=path.stat()
    return [str(path.resolve()),stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns]


def package_stamp(location):
    """Observe installed distribution identities without importing project code."""
    root=Path(location);files=[]
    for pattern in ('*.dist-info/METADATA','*.dist-info/RECORD','*.dist-info/direct_url.json','*.egg-info/PKG-INFO','*.egg-link','*.pth'):
        files.extend(root.glob(pattern))
    return key([(str(path.relative_to(root)),file_stamp(path)) for path in sorted(set(files))])


def environment_watch(home,card,origin,observed,packages):
    files={};tools={};complete=True
    if origin.get('shell'):files[origin['shell']]=digest(Path(origin['shell']))
    if origin.get('name'):
        # Existing registry lookups may publish apt shell entries. A GET checks
        # only saved files and the selection pointer, never reactivates anything.
        path=None
        from .environment_registry import _base
        if origin['name']!='apt':
            base=_base(home,origin['name'])
            path=base/'revisions'/origin['revision']/'activate.sh'
            pointer=base/'current.json'
            if not card['environment'].get('revision') and pointer.exists():files[str(pointer)]=digest(pointer)
        else:
            pointer=home.root/'packages.json'
            if pointer.exists():files[str(pointer)]=digest(pointer)
            complete=False  # No read-only registry capability for all apt transitive shells.
        if path:
            for relative in origin.get('files',{}):
                file=path.parent/relative
                files[str(file)]=digest(file)
    for value in observed.values():
        if isinstance(value,dict) and value.get('path'):
            tools[value['path']]=file_stamp(value['path'])
    roots={row['location'] for row in packages}
    from .testing_evidence import _editable_sources
    editable=_editable_sources(packages,lambda path:testing_run.Source(path).identity())
    complete &= all(item['source_key'] is not None for values in editable.values() for item in values)
    return {'files':files,'tools':tools,'packages':{root:package_stamp(root) for root in roots},'editable':editable,'complete':complete}


def watch_matches(watch):
    return (all(digest(Path(path))==expected for path,expected in watch['files'].items())
            and all(file_stamp(path)==expected for path,expected in watch['tools'].items())
            and all(package_stamp(path)==expected for path,expected in watch['packages'].items())
            and all((testing_run.Source(item['path']).identity() or {}).get('source_key')==item['source_key']
                    for values in watch.get('editable',{}).values() for item in values))


def discover_card(home,target,*,profile=None,groups=(),environment_name=None,work=None):
    prepared=testing_run.preflight(home,target,profile=profile,groups=groups,environment_name=environment_name)
    if len(prepared['plans'])!=1:raise ForgeError('Discover one project card at a time; select the project in its library.')
    plan=prepared['plans'][0];card=plan['card'];project=Path(card['project']);metadata=test_metadata.project_metadata(project)
    if work is None:work=home.root/'test-discoveries'/new_id('discovery',root=home.root/'test-discoveries')
    work=Path(work);work.mkdir(parents=True,exist_ok=True)
    before=testing_run.Source(project).identity()
    environment,observed,origin,script,name=checker.prepare_environment(home,work,card['environment'],Path(card['path']).parent,plan['environment_name'])
    python=observed.get('python',{}).get('path')
    if not python:raise ForgeError('The selected discovery environment has no Python.',code=1)
    inputs_before=selection_identity(card,plan['selection'])
    result=[];steps=[];errors=[];deselected=[];packages=[];selected_files=[]
    _,base_packages=testing_run.probe_packages(python,environment,project);packages.extend(base_packages)
    watch=environment_watch(home,card,origin,observed,packages)
    for step in plan['selection']['ids']:
        group=card['groups'].get(step);step_dir=named_path(work/'steps',step,create=True);root=step_dir/test_reports.ROOT
        context={'project_id':metadata.get('project_id') or prepared['target']['id'],'project':str(project),'run_id':work.name,
                 'job_id':None,'step_id':step,'target_id':name or 'local','source':before,
                 'environment_digest':key(origin,observed,watch,{name:group.get(name) for name in ('env','pythonpath','isolate')} if group else None)}
        if not group:
            from .test_report_runtime import make_row
            result.append(make_row(step,context,metadata,len(result),adapter='command'));continue
        paths=testing.group_files(card,group)
        selected_files.extend(paths)
        if not paths:continue
        atomic(root/'context.json',context);atomic(root/'metadata.json',metadata)
        plugin=testing_run.write_plugin(step_dir,project,step_dir/'work' if group['isolate']=='installed' else project)
        env=testing.python_environment(environment,project,group)
        cwd=step_dir/'work' if group['isolate']=='installed' else project;cwd.mkdir(parents=True,exist_ok=True)
        if group.get('pythonpath') or group.get('env') or group['isolate']=='installed':
            _,group_packages=testing_run.probe_packages(python,env,cwd)
            for row in group_packages:
                watch['packages'].setdefault(row['location'],package_stamp(row['location']))
            group_watch=environment_watch(home,card,origin,observed,group_packages)
            watch['complete'] &= group_watch['complete']
            for name,values in group_watch['editable'].items():
                for value in values:
                    if value not in watch['editable'].setdefault(name,[]):watch['editable'][name].append(value)
        env.update(JFORGE_STEP_DIR=str(step_dir),JFORGE_RUN_DIR=str(work))
        env['PYTHONPATH']=os.pathsep.join([str(plugin),*([env['PYTHONPATH']] if env.get('PYTHONPATH') else [])])
        if group['isolate']=='installed':
            from . import testing_package
            probe=testing_package.probe(python,env,cwd,project,group['distributions'])
            problems=testing_package.distribution_problems(probe)
            if problems:raise ForgeError('; '.join(problems),code=1)
        argv=testing_run.pytest_argv(python,group,step_dir,paths,project)
        argv.insert(3,'--collect-only')
        try:
            with (step_dir/'discovery.log').open('wb') as output:
                process=subprocess.run(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,timeout=group['timeout_seconds'])
        except subprocess.TimeoutExpired as exc:
            raise ForgeError('Test discovery timed out; inspect its discovery log.',code=1) from exc
        inventory,items=snapshot(root)
        deselected.extend(read(root/'deselected.json',[]))
        steps.append({'id':step,'python':python,'environment':name,'source':before,'exit_code':process.returncode})
        result.extend(items)
        if process.returncode not in (0,5):
            errors.append({'step_id':step,'collection':read(root/'collection.json',{}),'exit_code':process.returncode})
    after=testing_run.Source(project).identity()
    if before!=after or inputs_before!=selection_identity(card,plan['selection']) or metadata['digest']!=test_metadata.project_metadata(project)['digest']:
        raise ForgeError('Project changed while collecting tests. Discover again.',code=409)
    if not watch_matches(watch):raise ForgeError('Discovery environment changed during collection. Discover again.',code=409)
    seen={};unique=[]
    for row in result:
        # A repeated definition under a different target/step retains its context.
        identity=(row['case_id'],row['step_id'],row['target_id'])
        if identity not in seen:seen[identity]=True;unique.append(row)
    value={'api_version':1,'items':[{**row,'instance_id':None,'state':'not_run','outcome':None} for row in unique],
           'confirmed':not errors,'inventory_revision':key([(row['case_id'],row['step_id']) for row in unique],metadata['digest'],before,inputs_before,watch),
           'metadata_digest':metadata['digest'],'project_id':metadata.get('project_id') or prepared['target']['id'],
           'selection_identity':inputs_before,'environment_watch':watch,
           'source':before,'project':str(project),'card':str(card['path']),'selected_files':sorted(set(selected_files)),
           'metadata_diagnostics':test_metadata.bindings(metadata,unique,deselected=deselected,selected_files=selected_files),'deselected':deselected,
           'steps':steps,'errors':errors,'environment':name,'python':python,'selection':plan['selection'],'discovered_at':utc()}
    write_json(work/'inventory.json',value)
    return {**value,'inventory_path':str(work/'inventory.json')}


def submit(home,selected,body):
    from . import playlist_library as library,playlist_collections as collections,playlist_jobs as jobs
    library._fields(body,'Test discovery',(),('playlist','steps','env'))
    if ('playlist' in body)==('steps' in body):raise ForgeError('Choose one playlist or step IDs.')
    if body.get('env') is not None:
        from .storage import identifier
        identifier(library._text(body['env'],'Discovery environment'))
    if body.get('playlist') is not None:library._text(body['playlist'],'Playlist ID')
    inventory=library.inventory(home,selected);by_id={row['id']:row for row in inventory['cases']}
    name=body.get('playlist');album=None
    if name:
        albums,_=collections.catalog(home,selected,inventory=inventory)
        album=next((row for row in albums if row['id']==name),None)
        if not album:raise ForgeError('Unknown discovery playlist.',code=404)
        members=album['members']
    else:members=library._strings(body.get('steps'),'Discovery step IDs',nonempty=True)
    members=collections._closure(members,by_id)
    units=jobs._units(inventory,by_id,members,body.get('env') or (album or {}).get('environment'))
    base=home.root/'test-discoveries';ident=new_id('discovery',root=base);work=base/ident
    request={'id':ident,'library':selected['id'],'library_path':selected['path'],'library_sha256':digest(Path(selected['path'])),
             'playlist':name,'units':units,'submitted_at':utc()}
    write_json(work/'request.json',request)
    state={**{name:request[name] for name in ('id','library','playlist','submitted_at')},'status':'queued',
           'href':f'/api/test-discoveries/{ident}','environment':body.get('env') or (album or {}).get('environment')}
    write_json(work/'state.json',state)
    try:
        with (work/'discovery.log').open('wb') as output:
            subprocess.Popen([sys.executable,'-m','jarvis_forge.test_discovery',str(home.root),ident],stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
    except OSError as exc:
        state.update(status='failed',error={'message':str(exc)});write_json(work/'state.json',state)
        raise ForgeError('Could not start test discovery.',code=1) from exc
    return state


def get(home,selected,ident):
    from .storage import identifier
    root=home.root/'test-discoveries'/identifier(ident)
    state=read(root/'state.json')
    if state is None or state['library']!=selected['id']:raise ForgeError('Unknown test discovery.',code=404)
    request=read_json(root/'request.json')
    fresh=True;unknown=False
    if request.get('library_sha256'):
        try:fresh=digest(Path(request['library_path']))==request['library_sha256']
        except OSError:fresh=False
    for index,unit in enumerate(request['units']):
        identity=state.get('identities',{}).get(unit['project'])
        if unit['project'] not in state.get('identities',{}):unknown=True
        elif testing_run.Source(unit['root']).identity()!=identity:fresh=False
        check=state.get('input_checks',{}).get(str(index))
        if check is None:unknown=True
        else:
            try:
                card=testing.load_card(unit['card'])
                if not check['environment'].get('complete',False):unknown=True
                if selection_identity(card,check['selection'])!=check['identity'] or not watch_matches(check['environment']):fresh=False
            except (ForgeError,OSError,ValueError,KeyError):fresh=False
        expected=state.get('metadata_digests',{}).get(unit['project'])
        try:
            if unit['project'] not in state.get('metadata_digests',{}):unknown=True
            elif expected!=test_metadata.project_metadata(unit['root'])['digest']:fresh=False
        except (ForgeError,OSError):fresh=False
    return {**state,'test_cases_href':state['href']+'/test-cases',
            'freshness':'fresh' if state['status']=='completed' and fresh and not unknown else 'stale' if not fresh else 'unknown'}


def execute(home,ident):
    work=home.root/'test-discoveries'/ident;request=read_json(work/'request.json');state=read_json(work/'state.json')
    state.update(status='running',started_at=utc());write_json(work/'state.json',state)
    rows=[];errors=[];identities={};digests={};steps=[];input_checks={};diagnostics=[]
    try:
        for index,unit in enumerate(request['units']):
            if unit['source']['kind']=='test':
                result=discover_card(home,unit['card'],groups=[case.rsplit('/',1)[1] for case in unit['cases']],environment_name=unit['environment'],work=work/f'unit-{index}')
                identities[unit['project']]=result['source'];digests[unit['project']]=result['metadata_digest']
                input_checks[str(index)]={'selection':result['selection'],'identity':result['selection_identity'],'environment':result['environment_watch']}
                diagnostics.extend(f"{unit['project']}: {message}" for message in result['metadata_diagnostics'])
                prefix=f"{unit['project']}/{unit['source']['id']}/"
                rows.extend({**row,'step_id':prefix+row['step_id']} for row in result['items']);errors.extend(result['errors']);steps.extend(result['steps'])
            else:
                from .test_report_runtime import make_row
                metadata=test_metadata.project_metadata(unit['root'])
                identities[unit['project']]=testing_run.Source(unit['root']).identity();digests[unit['project']]=metadata['digest']
                context={'project_id':metadata.get('project_id') or unit['project'],'project':unit['root'],'run_id':ident,'job_id':None,'step_id':'','target_id':unit['environment'] or 'local'}
                for step in unit['cases']:
                    row=make_row(step,{**context,'step_id':step},metadata,len(rows),adapter='command');row['instance_id']=None;rows.append(row)
        revision=key([(row['case_id'],row['step_id'],row['metadata_digest']) for row in rows],identities,input_checks)
        write_json(work/'inventory.json',{'items':rows,'confirmed':not errors,'inventory_revision':revision})
        state.update(status='failed' if errors else 'completed',inventory_revision=revision,source='Explicit adapter discovery',discovered_at=utc(),
                     selection=[step for unit in request['units'] for step in unit['cases']],identities=identities,metadata_digests=digests,
                     input_checks=input_checks,metadata_diagnostics=diagnostics,steps=steps,errors=errors)
    except Exception as exc:
        state.update(status='failed',error={'message':str(exc)})
    write_json(work/'state.json',state)
    if state['status']=='completed' and request.get('playlist'):
        from .builds import file_lock
        from .playlist_library import _state,load
        base=_state(home,load(Path(request['library_path']).parent),create=True)
        with file_lock(base/'discoveries.lock',blocking=True):
            index=read(base/'discoveries.json',{})
            index[request['playlist']]=ident;write_json(base/'discoveries.json',index)

if __name__=='__main__':execute(ForgeHome(Path(sys.argv[1])),sys.argv[2])
