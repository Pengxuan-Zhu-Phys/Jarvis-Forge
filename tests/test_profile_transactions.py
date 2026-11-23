"""Exercise complete native transactions with an isolated deterministic engine."""

from copy import deepcopy
from pathlib import Path
import json
import re
import sys

import pytest
import yaml

from jarvis_forge import spack_backend as backend,spack_planning
from jarvis_forge.storage import ForgeHome,ForgeError,read_json,write_json


@pytest.fixture
def native(tmp_path,monkeypatch):
    home=ForgeHome(tmp_path/'forge'/'profiles'/'cbs');home.initialize()
    config=read_json(home.root/'config.json');config['forge_home']=str(tmp_path/'forge');write_json(home.root/'config.json',config)
    python={'executable':sys.executable,'version':sys.version.split()[0],'prefix':sys.prefix,'base_prefix':sys.base_prefix}
    tree=backend._config(home,tmp_path/'repo',python)
    (home.root/'spack/config/mirrors.yaml').write_text('mirrors: {}\n')
    write_json(home.root/'spack/backend.json',{'schema_version':1,'backend':'spack','engine':str(tmp_path/'forge/spack/spack-1.2.2'),
                   'python':python,'extension_recipes':False,'install_tree':tree})
    engine=tmp_path/'forge/spack/spack-1.2.2/bin/spack'
    engine.parent.mkdir(parents=True);engine.write_text('#!/bin/sh\n')
    db={};calls=[];fail={'install':False}
    def rows_for(specs):
        requested={backend.name_of(spec):spec for spec in specs}
        versions={name:(re.search(r'@=?([0-9.]+)',spec)[1] if re.search(r'@=?([0-9.]+)',spec) else '1.0') for name,spec in requested.items()}
        names=set(requested)
        if 'app' in names:names.add('lib');versions.setdefault('lib','1.0')
        rows=[]
        for name in sorted(names):
            version=versions[name]
            variant='shared' if '+shared' in requested.get(name,'') else 'plain'
            value=f'{name}-{version}-{variant}'+('-lib'+versions['lib'] if name=='app' else '')
            dependencies=[{'name':'lib','hash':f'lib-{versions["lib"]}-plain'}] if name=='app' else []
            rows.append({'name':name,'version':version,'hash':value,'prefix':str(home.root/'pkg'/f'{name}-{version}'),
                         'spec':requested.get(name,f'{name}@={version}'),'root':name in requested,'external':False,
                         'installed':value in db,'deprecated':False,'node':{'arch':{'target':'x86_64'},'dependencies':dependencies}})
        return rows
    def refresh(rows):
        return [{**row,'installed':row['hash'] in db} for row in rows]
    def run(scope,args,op,label,**kwargs):
        env=kwargs.get('envdir')
        cached=spack_planning.query(op,env,args,label)
        if cached is not None:return cached
        calls.append(label)
        if label.startswith('concretize'):
            request=kwargs['planning']
            if request.get('from_lock'):
                locked=read_json(env/'spack.lock');rows=refresh(locked['packages'])
            else:
                specs=yaml.safe_load((env/'spack.yaml').read_text())['spack']['specs'];rows=rows_for(specs)
                write_json(env/'spack.lock',{'packages':rows,'roots':specs})
            policy='never' if request.get('source_if_uncached') else request['policy']
            value={'dag':{'packages':rows},'plan':{'packages':rows,'modifications':[]},'tree':'test DAG',
                   'policy':policy,'inventory':list(deepcopy(db).values())}
            if not Path(request['database_checkpoint']).exists():write_json(Path(request['database_checkpoint']),db)
            spack_planning.save(op,env,request,json.dumps(value))
            return json.dumps(value)
        if label=='inventory':
            write_json(Path(args[-1]),db);return json.dumps({'packages':list(db.values())})
        if label.startswith('database-'):
            options=json.loads(args[-1])
            if options['action']=='detach':
                for value in options['hashes']:db.pop(value,None)
            else:db.clear();db.update(read_json(Path(options['checkpoint'])))
            return '{}'
        if args[0]=='install':
            rows=read_json(env/'spack.lock')['packages']
            for row in rows:
                prefix=Path(row['prefix']);prefix.mkdir(parents=True,exist_ok=True)
                (prefix/'payload').write_text(row['hash'])
                (prefix/'.spack').mkdir(exist_ok=True);write_json(prefix/'.spack/spec.json',row)
                db[row['hash']]={**row,'installed':True}
                if fail['install']:
                    write_json(op/f'command-{label}.json',{'status':'failed','exit_code':2})
                    raise ForgeError('native build failed',code=1)
            return ''
        if label=='installed':
            rows=refresh(read_json(env/'spack.lock')['packages'])
            return json.dumps({'packages':rows,'inventory':list(db.values()),'modifications':[]})
        if 'installation-plan' in args:
            rows=refresh(read_json(env/'spack.lock')['packages'])
            return json.dumps({'packages':rows,'modifications':[]})
        return '{}'
    monkeypatch.setattr(backend,'run',run)
    def install(specs=(),**options):return backend.transact(home,'install',specs,target='x86_64',cache='never',**options)
    return home,db,calls,fail,install


def test_version_upgrade_keeps_old_then_autoremove_physically_deletes_it(native):
    home,db,calls,fail,install=native
    first=install(['lib@1.0']);old=home.root/'pkg/lib-1.0'
    second=install(['lib@2.0'])
    assert old.is_dir() and (home.root/'pkg/lib-2.0').is_dir()
    assert backend.current(home)['roots']==['lib@2.0']
    preview=backend.autoremove(home,dry_run=True)
    assert [row['version'] for row in preview['unused']]==['1.0']
    removed=backend.autoremove(home,confirm=lambda plan:None)
    assert not old.exists() and (home.root/'pkg/lib-2.0/payload').is_file()
    assert len(db)==1 and removed['state_committed']
    for folder in (home.root/'history').glob('[0-9]*'):
        assert {file.name for file in folder.iterdir()}=={'spack.yaml','spack.lock','result.json'}


def test_same_version_variant_failure_restores_native_index_files_and_activation(native):
    home,db,calls,fail,install=native
    first=install(['lib@1.0'])
    prefix=home.root/'pkg/lib-1.0';payload=(prefix/'payload').read_bytes()
    state=(home.root/'packages.json').read_bytes();activation=Path(first['activation']).read_bytes();before=deepcopy(db)
    fail['install']=True
    with pytest.raises(ForgeError,match='native build failed'):
        install(['lib@1.0 +shared'],confirm=lambda plan:None)
    assert (prefix/'payload').read_bytes()==payload and db==before
    assert (home.root/'packages.json').read_bytes()==state and Path(first['activation']).read_bytes()==activation
    assert not list((home.root/'pkg/rollback').glob('[0-9]*'))


def test_rebuild_requires_plan_confirmation_before_detaching_or_moving(native):
    home,db,calls,fail,install=native
    install(['lib@1.0']);before=deepcopy(db)
    with pytest.raises(ForgeError,match='confirmation'):
        install(['lib@1.0 +shared'])
    assert db==before and (home.root/'pkg/lib-1.0/payload').read_text()=='lib-1.0-plain'
    assert not any(label=='database-detach' for label in calls)
    preview = install(["lib@1.0 +shared"], dry_run=True)
    assert not preview["changes"]["install"]
    assert [p["name"] for p in preview["changes"]["rebuild"]] == ["lib"]
    selected=[]
    result = install(['lib@1.0 +shared'],confirm=lambda plan:selected.extend(plan['rebuild']))
    assert result['changes']['install'] == []
    assert selected[0]['name']=='lib' and len(db)==1


def test_additive_install_preserves_constraints_and_refuses_changed_roots(native, monkeypatch):
    home, db, calls, _, install = native
    first = install(["app@1.0"])
    before = deepcopy(db)
    state = (home.root / "packages.json").read_bytes()
    previous_lock = Path(first["lockfile"]).read_bytes()
    run = backend.run

    def drift(scope, args, operation, label, **kwargs):
        result = run(scope, args, operation, label, **kwargs)
        if label == "dag":
            dag = json.loads(result)
            next(row for row in dag["packages"] if row["name"] == "app")["hash"] = "different"
            return json.dumps(dag)
        return result

    calls.clear()
    monkeypatch.setattr(backend, "run", drift)
    with pytest.raises(ForgeError, match="would change installed roots: app"):
        install(["app", "new-package"])
    assert "install" not in calls and "database-detach" not in calls
    assert db == before and (home.root / "packages.json").read_bytes() == state
    assert Path(first["lockfile"]).read_bytes() == previous_lock
    monkeypatch.setattr(backend, "run", run)
    result = install(["app", "new-package"])
    assert result["roots"] == ["app@1.0", "new-package"]
    assert [row["name"] for row in result["changes"]["install"]] == ["new-package"]
    assert not result["changes"]["rebuild"]


@pytest.mark.parametrize("plan_inventory", [True, False])
def test_rollback_keeps_installed_packages_after_plan_cache_invalidation(native, monkeypatch, plan_inventory):
    from jarvis_forge.build_tool_reuse import Planner

    home, db, calls, fail, install = native
    install(["app"])
    before = deepcopy(db)
    original = Planner.apply

    def invalidate(planner, *args, **kwargs):
        plan = original(planner, *args, **kwargs)
        write_json(planner.envdir / "build-tool-reuse.json", {"selected": []})
        if plan_inventory:
            plan["inventory"] = list(before.values())
        return plan

    monkeypatch.setattr(Planner, "apply", invalidate)
    fail["install"] = True
    with pytest.raises(ForgeError, match="native build failed"):
        install(["new-package"])
    assert db == before
    for old in before.values():
        assert (Path(old["prefix"]) / "payload").read_text() == old["hash"]
    assert not (home.root / "pkg/new-package-1.0").exists()


def test_remove_dependency_also_removes_dependent_roots_but_keeps_automatic_libraries(native):
    home,db,calls,fail,install=native
    install(['app'])
    removed=backend.transact(home,'remove',['app'],target='x86_64',confirm=lambda plan:None)
    assert not (home.root/'pkg/app-1.0').exists()
    assert (home.root/'pkg/lib-1.0').is_dir()
    assert backend.current(home)['roots']==[]
    backend.autoremove(home,confirm=lambda plan:None)
    assert not (home.root/'pkg/lib-1.0').exists()
    install(['app'])
    backend.transact(home,'remove',['lib'],target='x86_64',confirm=lambda plan:None)
    assert not (home.root/'pkg/app-1.0').exists() and not (home.root/'pkg/lib-1.0').exists()


def test_dry_run_solves_and_saves_history_then_from_uses_exact_lock_without_resolving(native):
    home,db,calls,fail,install=native
    result=install(['lib@1.0'],dry_run=True)
    assert not db and not (home.root/'pkg/lib-1.0').exists()
    history=Path(result['history_path']);lock=(history/'spack.lock').read_bytes()
    applied=install(from_history=result['history'])
    assert (home.root/'pkg/lib-1.0').is_dir()
    assert (home.root/'spack/spack.lock').read_bytes()==lock
    install(['lib@2.0'])
    restored=install(from_history=result['history'])
    assert backend.current(home)['roots']==['lib@1.0']
    assert (home.root/'pkg/lib-2.0').is_dir()
    assert {file.name for file in Path(restored['history_path']).iterdir()}=={'spack.yaml','spack.lock','result.json'}


def test_enabled_app_blocks_remove_and_autoremove(native):
    home,db,calls,fail,install=native
    install(['lib@1.0'])
    write_json(home.root/'metadata.json',{'kind':'test-fixture','apps':[{'name':'cbs','packages':[{'name':'lib','hash':'lib-1.0-plain'}]}]})
    with pytest.raises(ForgeError,match='cbs'):
        backend.transact(home,'remove',['lib'],target='x86_64',confirm=lambda plan:None)
    assert (home.root/'pkg/lib-1.0').exists()
    install(['lib@2.0'])
    with pytest.raises(ForgeError,match='cbs'):backend.autoremove(home,confirm=lambda plan:None)


def test_named_upgrade_holds_other_roots_to_current_versions():
    from jarvis_forge.spack_backend import _requested_roots
    before={'roots':['gsl','zlib+shared'],'packages':[{'name':'gsl','version':'2.7','node':{'parameters':{}}},
            {'name':'zlib','version':'1.3.1','node':{'parameters':{'shared':True}}}]}
    assert _requested_roots(before,'upgrade',['gsl'])==['gsl','zlib@=1.3.1 +shared']
    assert _requested_roots(before,'upgrade',[])==before['roots']


def test_reproduction_diff_is_reported_before_native_install(native):
    home,db,calls,fail,install=native
    expected=[{'name':'lib','version':'2.0','variants':{},'external':False}]
    with pytest.raises(ForgeError,match='versions or variants differ'):
        install(['lib@1.0'],expected_packages=expected)
    assert not db and not (home.root/'pkg/lib-1.0').exists()
    assert not any(label.startswith('install') for label in calls)
    candidate=next((home.root/'operations').glob('*/candidate-environment/.jforge-config/packages.yaml'))
    constraints=yaml.safe_load(candidate.read_text())['packages']
    assert '@=2.0' in constraints['lib']['require']
