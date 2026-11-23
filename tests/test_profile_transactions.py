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
            dependencies=[{'name':'lib','hash':f'lib-{versions["lib"]}-plain','parameters':{'deptypes':['link','run'],'virtuals':[]}}] if name=='app' else []
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
                write_json(env/'spack.lock',{'packages':rows,'roots':[{'hash':r['hash'],'spec':r['spec']} for r in rows if r['root']],
                           'concrete_specs':{r['hash']:{'name':r['name'],'version':r['version'],**r['node']} for r in rows}})
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


@pytest.mark.parametrize('entrypoint', ['apt', 'build'])
@pytest.mark.parametrize('fixed_root', [False, True])
def test_historical_tool_becomes_a_native_root_without_rebuilding_consumers(native, monkeypatch, tmp_path, entrypoint, fixed_root):
    from jarvis_forge import builds, toolchains, unified_profiles, apt_profiles
    from jarvis_forge.storage import digest

    home, db, calls, _, install = native
    chain = {'version': '13.3.0', 'prefix': '/compiler', 'target': 'x86_64',
             'drivers': {'CC': '/compiler/bin/gcc', 'CXX': '/compiler/bin/g++', 'FC': '/compiler/bin/gfortran'}, 'tools': {}}
    monkeypatch.setattr(toolchains, 'binding', lambda scope: chain)
    if fixed_root:
        chain['tools']['cmake'] = {'version': '1.0', 'prefix': str(home.root / 'tools/pkg/cmake-1.0')}
        Path(chain['tools']['cmake']['prefix']).mkdir(parents=True)
    monkeypatch.setattr(unified_profiles, 'record', lambda scope: {'backend': 'spack', 'toolchain': chain, 'stage': str(home.root / 'stage')} if scope.root == home.root else None)
    monkeypatch.setattr(unified_profiles, 'sync_definition', lambda *a: None)
    monkeypatch.setattr(unified_profiles, 'refresh_activation', lambda *a: None)
    monkeypatch.setattr(apt_profiles, 'validate_active', lambda *a, **k: None)
    source = home.root / 'tools/pkg/tool-1.0'; source.mkdir(parents=True)
    (home.root / 'tools/pkg/.spack-db').mkdir()
    executable = source / 'tool'; executable.write_text('source identity')
    selection = {'name': 'tool', 'version': '1.0', 'prefix': str(source), 'source_hash': 'source-tool',
                 'target_hash': 'native-tool', 'spec': 'tool@=1.0', 'parameters': {}, 'checks': {str(executable): digest(executable)},
                 'runtime_prefixes': [str(source)]}
    original_run = backend.run
    solves = []
    installed_work = []

    def package(name, hash, *, root=True, external=False, dependencies=()):
        prefix = chain['tools']['cmake']['prefix'] if name == 'cmake' else source
        return {'name': name, 'hash': hash, 'version': '1.0', 'spec': name, 'root': root,
                'external': external, 'prefix': str(prefix if external else home.root / 'pkg' / (name + '-1.0')),
                'installed': external or hash in db, 'deprecated': False,
                'node': {'arch': {'target': 'x86_64'}, 'parameters': {}, 'dependencies': list(dependencies)}}

    def engine(scope, args, op, label, **kwargs):
        env = kwargs.get('envdir')
        if label.startswith('concretize'):
            request = kwargs['planning']; solves.append((label, request))
            specs = yaml.safe_load((env / 'spack.yaml').read_text())['spack']['specs']
            names = {backend.name_of(spec) for spec in specs}
            old = read_json(env / 'spack.lock') if (env / 'spack.lock').is_file() else {}
            locked = {p['name']: p for p in old.get('packages', []) if p['root']}
            if locked:
                assert '--force' not in args and set(locked) <= names
            rules = yaml.safe_load((env / '.jforge-config/packages.yaml').read_text())['packages']
            borrowed = rules.get('tool', {}).get('buildable') is False or 'builder' in locked
            if 'builder' in locked:
                assert request['historical_tool_hashes'] == ['historical-tool']
                selectors = yaml.safe_load((env / 'spack.yaml').read_text())['spack']['concretizer']['reuse']['from']
                assert all('/historical-tool' in r.get('exclude', []) for r in selectors if r['type'] != 'external')
                assert not next(r for r in selectors if r['type'] == 'external').get('exclude')
            tool_hash = 'historical-tool' if borrowed else 'native-tool'
            builder = package('builder', 'builder-' + tool_hash, dependencies=[
                {'name': 'tool', 'hash': tool_hash, 'parameters': {'deptypes': ['build'], 'virtuals': []}}])
            rows = [builder, package('tool', tool_hash, root=False, external=borrowed)]
            rows.extend(package(name, 'root-' + name) for name in names - {'builder', 'tool', 'cmake'})
            if 'cmake' in names:
                rows.append(package('cmake', 'fixed-cmake', external=True))
            if 'tool' in names:
                rows.append(package('tool', 'native-tool'))
            write_json(env / 'spack.lock', {'packages': rows, 'roots': [{'hash': p['hash'], 'spec': p['name']} for p in rows if p['root']],
                       'concrete_specs': {p['hash']: {'name': p['name'], 'version': p['version'], **p['node'],
                                                    **({'external': {'path': p['prefix']}} if p['external'] else {})} for p in rows}})
            plan = [r for r in rows if r['root'] or not r['external'] and not builder['installed']]
            value = {'dag': {'packages': rows}, 'plan': {'packages': plan, 'modifications': []},
                     'tree': 'build tool DAG', 'policy': 'never', 'inventory': list(deepcopy(db).values())}
            checkpoint = Path(request['database_checkpoint'])
            if not checkpoint.exists(): write_json(checkpoint, db)
            spack_planning.save(op, env, request, json.dumps(value))
            return json.dumps(value)
        if label.startswith('build-tool-candidates'):
            rows = read_json(op / 'dag.json')['packages']
            eligible = next((p for p in rows if p['hash'] == 'native-tool' and not p['root']), None)
            return json.dumps({'selected': [selection] if eligible else [], 'unsafe_existing': [], 'rejected': []})
        if args[0] == 'install':
            for p in read_json(env / 'spack.lock')['packages']:
                if p['external'] or p['hash'] in db: continue
                installed_work.append(p['hash'])
                prefix = Path(p['prefix']); prefix.mkdir(parents=True, exist_ok=True)
                (prefix / 'payload').write_text(p['hash']); write_json(prefix / '.spack/spec.json', p)
                db[p['hash']] = {**p, 'installed': True}
            return ''
        if label == 'installed':
            rows = [{**p, 'installed': True} for p in read_json(env / 'spack.lock')['packages'] if p['root']]
            return json.dumps({'packages': rows, 'modifications': [], 'inventory': list(db.values())})
        return original_run(scope, args, op, label, **kwargs)

    monkeypatch.setattr(backend, 'run', engine)
    first = install(['builder'])
    builder = next(p for p in first['packages'] if p['name'] == 'builder')
    builder_file = Path(builder['prefix']) / 'payload'; before = builder_file.read_bytes()
    if fixed_root:
        install(['cmake'])  # Explicit suite-tool installation remains legal.
    # An intervening transaction must not erase history when selection is empty.
    install(['unrelated'])
    assert read_json(home.root / 'spack/build-tool-reuse.json')['selected'] == []
    assert read_json(home.root / 'spack/build-tool-reuse.json')['retained'][0]['hash'] == 'historical-tool'
    if fixed_root:
        fixed = next(p for p in backend.current(home)['packages'] if p['name'] == 'cmake')
        assert fixed['hash'] == 'fixed-cmake' and fixed['external']
    solves.clear(); installed_work.clear()
    if entrypoint == 'apt':
        result = install(['tool'])
    else:
        monkeypatch.setattr(apt_profiles, 'ensure_active', lambda *a, **k: home)
        monkeypatch.setattr(builds, '_require_spack_profile', lambda *a: None)
        # A real recipe dependency step, with only the native engine boundary substituted.
        card = yaml.safe_load('steps:\n- id: dependencies\n  uses: apt.install\n  with:\n    specs: [tool]\n')
        step = card['steps'][0]; logs = tmp_path / 'step'; logs.mkdir()
        result = builds._step(ForgeHome(tmp_path / 'forge'), step, step['with'], tmp_path, logs, {},
                              {'python': {'path': sys.executable}}, {}, {}, {}, 2, lambda *a, **k: None, None,
                              apt_profile={'path': str(home.root)}, toolchain=chain)
        assert result['status'] == 'passed'
    current = backend.current(home)
    native_tool = next(p for p in current['packages'] if p['name'] == 'tool')
    assert not native_tool['external'] and native_tool['hash'] == 'native-tool'
    assert next(p for p in current['packages'] if p['name'] == 'builder')['hash'] == builder['hash']
    assert builder_file.read_bytes() == before
    assert installed_work == ['native-tool'] and [label for label, _ in solves] == ['concretize']
    evidence = read_json(home.root / 'spack/build-tool-reuse.json')
    assert evidence['selected'] == [] and evidence['retained'][0]['hash'] == 'historical-tool'
    assert evidence['retained'][0]['source_hash'] == 'source-tool'
    assert 'cmake' not in {p['name'] for p in evidence['retained']}
    if fixed_root:
        after = next(p for p in current['packages'] if p['name'] == 'cmake')
        assert {k: after[k] for k in ('hash', 'version', 'prefix', 'external', 'root')} == {
            k: fixed[k] for k in ('hash', 'version', 'prefix', 'external', 'root')}


@pytest.mark.parametrize('action,specs', [('upgrade', ['lib']), ('full-upgrade', []), ('install', ['lib@2.0'])])
def test_nonincremental_resolution_keeps_force_semantics(native, monkeypatch, action, specs):
    home, _, _, _, install = native
    install(['lib@1.0'])
    original = backend.run
    flags = []
    def capture(scope, args, op, label, **kwargs):
        if label.startswith('concretize'): flags.append(args)
        return original(scope, args, op, label, **kwargs)
    monkeypatch.setattr(backend, 'run', capture)
    backend.transact(home, action, specs, cache='never', target='x86_64', confirm=lambda plan: None)
    assert len(flags) == 1 and '--force' in flags[0]
    assert ('--fresh' in flags[0]) == (action != 'install')


@pytest.mark.parametrize('name', ['gcc', 'cmake'])
@pytest.mark.parametrize('mismatch', ['prefix', 'version'])
def test_fixed_binding_is_checked_on_the_full_committed_dag_before_solving(native, monkeypatch, name, mismatch):
    from jarvis_forge import toolchains
    home, _, calls, _, install = native
    install(['lib'])
    chain = {'id': 'ubuntu24', 'version': '13.3.0', 'prefix': str(home.root / 'tools/pkg/gcc-13.3.0'),
             'tools': {'cmake': {'version': '3.28.6', 'prefix': str(home.root / 'tools/pkg/cmake-3.28.6')}}}
    expected = toolchains.suite(chain)[name]
    actual = {**expected, mismatch: str(home.root / 'tools/pkg/wrong') if mismatch == 'prefix' else '0.1'}
    lock = home.root / 'spack/spack.lock'
    document = read_json(lock)
    # This node is build-only and absent from the published runtime inventory.
    document['concrete_specs']['tool'] = {'name': name, 'version': actual['version'], 'external': {'path': actual['prefix']}}
    document['concrete_specs'][document['roots'][0]['hash']]['dependencies'] = [
        {'name': name, 'hash': 'tool', 'parameters': {'deptypes': ['build'], 'virtuals': []}}]
    write_json(lock, document)
    before = lock.read_bytes(); state = (home.root / 'packages.json').read_bytes()
    monkeypatch.setattr(toolchains, 'binding', lambda *a: chain)
    calls.clear()
    with pytest.raises(ForgeError, match='Fixed tool binding mismatch for ' + name) as error:
        install(['new-package'])
    assert f"{name}@{actual['version']}" in str(error.value) and actual['prefix'] in str(error.value)
    assert f"{name}@{expected['version']}" in str(error.value) and expected['prefix'] in str(error.value)
    assert 'profile check ubuntu24' in error.value.hint and 'Recreate the profile' in error.value.hint
    assert calls == [] and lock.read_bytes() == before and (home.root / 'packages.json').read_bytes() == state
