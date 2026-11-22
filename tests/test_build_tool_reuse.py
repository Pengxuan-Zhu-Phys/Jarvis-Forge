"""Graph-driven reuse; fake engine boundaries, real temporary files and scripts."""
from contextlib import nullcontext
import copy
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest
import yaml

from jarvis_forge import build_tool_reuse as reuse
from jarvis_forge.storage import ForgeError, ForgeHome, digest, read_json, write_json
from profile_fixtures import project_profile


def row(name, hash=None, root=False, dependencies=(), **extra):
    return dict(name=name, hash=hash or name, version='1.0', root=root, installed=False,
                external=False, prefix='/candidate/'+name, node={
                    'parameters': {'shared': True, 'cflags': []},
                    'arch': {'platform': 'linux', 'platform_os': 'almalinux9', 'target': 'x86_64'},
                    'package_hash': 'same-recipe', 'dependencies': list(dependencies)}, **extra)


def edge(name, *kinds, virtuals=()):
    return {'name': name, 'hash': name, 'parameters': {'deptypes': list(kinds), 'virtuals': list(virtuals)}}


@pytest.mark.parametrize('barrier', ['none', 'link', 'run', 'transitive', 'link-from-build-tool', 'compiler', 'explicit-root', 'incomplete', 'unknown'])
def test_tool_eligibility_uses_the_complete_dependency_graph(barrier):
    rows=[row('app', root=True, dependencies=[edge('tool', 'build'), edge('library', 'link')]),
          row('tool'), row('library')]
    if barrier in {'link','run'}:
        rows[0]['node']['dependencies'].append(edge('tool', barrier))
    if barrier == 'transitive':
        rows[2]['node']['dependencies']=[edge('tool','run')]
    if barrier == 'link-from-build-tool':
        rows += [row('other',dependencies=[edge('tool','link')])]
        rows[0]['node']['dependencies'].append(edge('other','build'))
    if barrier == 'compiler':
        rows[0]['node']['dependencies'][0]['parameters']['virtuals']=['cxx']
    if barrier == 'explicit-root': rows[1]['root']=True
    if barrier == 'incomplete': rows[0]['node']['dependencies'].append(edge('absent','run'))
    if barrier == 'unknown': rows[0]['node']['dependencies'][0]['parameters']['deptypes']=[]
    assert ('tool' in reuse.build_only(rows)) == (barrier == 'none')


@pytest.mark.parametrize('changed', ['version','variants','flags','recipe','target'])
def test_tool_reuse_requires_exact_noncompiler_inputs(changed):
    a=row('tool'); b=copy.deepcopy(a)
    assert reuse.matches(a,b)
    if changed=='version':b['version']='2.0'
    if changed=='variants':b['node']['parameters']['shared']=False
    if changed=='flags':b['node']['parameters']['cflags']=['-O3']
    if changed=='recipe':b['node']['package_hash']='new-recipe'
    if changed=='target':b['node']['arch']['target']='x86_64_v3'
    assert not reuse.matches(a,b)


@pytest.fixture
def native(tmp_path, monkeypatch):
    tools=tmp_path/'tools'; prefix=tools/'pkg/tool-1.0'; runtime=tools/'pkg/runtime-1.0'
    for folder, hash in ((prefix,'old-tool'),(runtime,'old-runtime')):
        write_json(folder/'.spack/spec.json',{'hash':hash})
    binary=prefix/'bin/tool'; binary.parent.mkdir(); binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o700)
    request=row('tool'); dag=[row('app',root=True,dependencies=[edge('tool','build')]),request]
    file=tmp_path/'dag.json';write_json(file,{'packages':dag})
    class Spec:
        external=False
        name='tool';version='1.0'
        def __init__(self,prefix,hash):self.prefix=prefix;self.hash=hash
        def dag_hash(self):return self.hash
        def node_dict_with_hashes(self):return request['node']
        def format(self,fmt):
            assert fmt=='{name}{@versions}{compiler_flags}{variants}'
            return 'tool@=1.0+shared'
        @staticmethod
        def from_specfile(file):
            return Spec(Path(file).parent.parent,read_json(Path(file))['hash'])
    source=Spec(prefix,'old-tool');dependency=Spec(runtime,'old-runtime')
    spack=ModuleType('spack'); spack.store=ModuleType('spack.store');spack.spec=ModuleType('spack.spec')
    spack.error=ModuleType('spack.error');spack.error.SpackError=RuntimeError
    spack.traverse=ModuleType('spack.traverse')
    spack.store.STORE=SimpleNamespace(db=SimpleNamespace(read_transaction=lambda:nullcontext(),query_local=lambda:[source]))
    spack.store.use_store=lambda *a,**k:nullcontext()
    spack.spec.Spec=Spec
    spack.traverse.traverse_nodes=lambda *a,**k:[source,dependency]
    for name in ('spack','spack.store','spack.spec','spack.error','spack.traverse'):
        monkeypatch.setitem(sys.modules,name,spack if name=='spack' else getattr(spack,name.split('.')[1]))
    options={'dag':str(file),'tools':str(tools),'needed':['tool']}
    return options,binary,runtime,request


def test_native_source_registry_and_runtime_files_are_checked(native):
    options,binary,runtime,request=native
    result=reuse.candidates(options)
    selected,=result['selected']
    assert selected['source_hash']=='old-tool' and selected['spec']=='tool@=1.0+shared'
    assert len(selected['runtime_prefixes'])==2
    reuse.check_files([selected])
    binary.write_text('#!/bin/sh\nexit 1\n')
    with pytest.raises(ForgeError,match='changed'):reuse.check_files([selected])


@pytest.mark.parametrize('failure',['missing-runtime','changed-spec','missing-interpreter','missing-library','loader-env','hidden-python','not-needed','fixed','direct-root'])
def test_unusable_or_unneeded_source_tools_are_not_borrowed(native,monkeypatch,failure):
    options,binary,runtime,request=native
    if failure=='missing-runtime':
        import shutil
        shutil.rmtree(runtime)
    if failure=='changed-spec':write_json(binary.parent.parent/'.spack/spec.json',{'hash':'changed'})
    if failure=='missing-interpreter':binary.write_text('#!/missing/interpreter\nexit 0\n')
    if failure=='missing-library':
        binary.write_bytes(b'\x7fELFfixture')
        monkeypatch.setattr(reuse.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='libmissing.so => not found',stderr=''))
    if failure=='loader-env':
        binary.write_bytes(b'\x7fELFfixture')
        monkeypatch.setenv('LD_LIBRARY_PATH','/profile/lib')
        monkeypatch.setattr(reuse.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='undefined symbol: missing' if k['env'].get('LD_LIBRARY_PATH') else '',stderr=''))
    if failure=='hidden-python':binary.write_text('#!/usr/bin/env python\nprint(1)\n')
    if failure=='not-needed':options['needed']=[]
    if failure=='fixed':options['fixed']=['tool']
    if failure=='direct-root':
        dag=read_json(Path(options['dag']));dag['packages'][1]['root']=True;write_json(Path(options['dag']),dag)
    assert reuse.candidates(options)['selected']==[]


@pytest.mark.parametrize('mode',['reuse','role-change','source-changed','solver-failure','timeout','signature','replay','cache-only'])
def test_planner_resolves_again_and_rolls_back_unsafe_optional_externals(tmp_path,monkeypatch,mode):
    base=ForgeHome(tmp_path/'forge');home=project_profile(base,'ubuntu24')
    (home.root/'tools/pkg/.spack-db').mkdir(parents=True)
    op=tmp_path/'op';env=op/'candidate';config=env/'.jforge-config/packages.yaml'
    original={'packages':{'gcc':{'buildable':False},'tool':{'require':['+shared']}}}
    config.parent.mkdir(parents=True);config.write_text(yaml.safe_dump(original))
    (env/'spack.yaml').write_text(yaml.safe_dump({'spack':{'concretizer':{'reuse':{'from':[{'type':'local'},{'type':'external'}]}}}}))
    (env/'spack.lock').write_text('baseline-lock')
    dag=[row('app',root=True,dependencies=[edge('tool','build')]),row('tool')]
    write_json(op/'dag.json',{'packages':dag})
    source=home.root/'tools/pkg/tool-1.0';source.mkdir(parents=True)
    file=source/'binary';file.write_text('stable source')
    selected={'name':'tool','version':'1.0','prefix':str(source),'source_hash':'old', 'target_hash':'tool',
              'spec':'tool@=1.0+shared','parameters':dag[1]['node']['parameters'],
              'checks':{str(file):digest(file)},'runtime_prefixes':[str(source)]}
    def query(options,label):return {'selected':[selected],'rejected':[],'unsafe_existing':[]}
    calls=[]
    def resolve(policy,flags,*,suffix):
        calls.append(suffix)
        assert '--force' in flags and not (env/'spack.lock').exists()
        if 'fallback' in suffix:
            assert yaml.safe_load(config.read_text())==original
            write_json(op/'dag.json',{'packages':dag})
            return {'packages':dag}
        assert yaml.safe_load(config.read_text())['packages']['tool']['buildable'] is False
        if mode in {'solver-failure','timeout','signature'}:
            if mode=='signature':
                (op/f'command-concretize{suffix}.stderr.log').write_text('Signature verification failed for archive')
            write_json(op/f'command-concretize{suffix}.json',{'status':'timeout' if mode=='timeout' else 'failed','exit_code':1})
            raise ForgeError('fixture solver failed')
        changed=copy.deepcopy(dag);changed[1].update(external=True,prefix=str(source),hash='borrowed')
        changed[0]['node']['dependencies'][0]['hash']='borrowed'
        if mode=='role-change':changed[0]['node']['dependencies'][0]['parameters']['deptypes']=['link']
        if mode=='source-changed':file.write_text('modified source')
        write_json(op/'dag.json',{'packages':changed})
        return {'packages':changed}
    planner=reuse.Planner(home,env,op,query,resolve,replay=mode=='replay',allow_new=mode!='cache-only')
    if mode in {'timeout','signature'}:
        with pytest.raises(ForgeError,match='fixture solver'):planner.apply({'packages':dag},'never',['concretize'])
        assert len(calls)==1
        return
    result=planner.apply({'packages':dag},'never',['concretize'])
    if mode=='reuse':
        assert result['packages'][1]['external'] and len(calls)==1
        assert read_json(env/'build-tool-reuse.json')['selected'][0]['external_hashes']==['borrowed']
    elif mode in {'role-change','source-changed','solver-failure'}:
        assert len(calls)==2 and not result['packages'][1]['external']
        assert read_json(env/'build-tool-reuse.json')['selected']==[]
    else:
        assert calls==[] and yaml.safe_load(config.read_text())==original
    assert file.read_text()==('modified source' if mode=='source-changed' else 'stable source')


def test_historical_build_edges_do_not_require_a_deleted_unused_tool(native):
    options,binary,runtime,request=native
    document=read_json(Path(options['dag']))
    document['packages'][1].update(external=True,prefix=str(binary.parent.parent))
    write_json(Path(options['dag']),document)
    binary.unlink()
    options['needed']=['app']
    result=reuse.candidates(options)
    assert result['selected']==[] and result['unsafe_existing']==[]
    # The same reference becomes invalid when a new consumer actually needs it.
    options['needed']=['app','tool']
    assert reuse.candidates(options)['unsafe_existing']


def test_replay_refuses_missing_source_store_without_rewriting_the_lock(tmp_path):
    home=project_profile(ForgeHome(tmp_path/'forge'),'ubuntu24')
    op=tmp_path/'op';env=op/'candidate';cfg=env/'.jforge-config/packages.yaml'
    cfg.parent.mkdir(parents=True);cfg.write_text('packages: {}\n')
    source=home.root/'tools/pkg/tool-1.0'
    tool=row('tool');tool.update(external=True,prefix=str(source))
    write_json(op/'dag.json',{'packages':[row('app',root=True,dependencies=[edge('tool','build')]),tool]})
    lock=env/'spack.lock';lock.write_text('exact historical lock')
    planner=reuse.Planner(home,env,op,lambda *a:pytest.fail('no source DB'),lambda *a,**k:pytest.fail('no re-solve'),replay=True)
    with pytest.raises(reuse.ReuseRejected,match='unavailable'):planner.apply({'packages':[tool]},'only',['concretize'])
    assert lock.read_text()=='exact historical lock'


def test_resolved_loader_libraries_are_fingerprinted(native, monkeypatch):
    options,binary,runtime,request=native
    binary.write_bytes(b'\x7fELFfixture')
    library=runtime/'libhelper.so';library.write_bytes(b'stable library')
    monkeypatch.setattr(reuse.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=f'libhelper.so => {library} (0x1234)\n',stderr=''))
    selected,=reuse.candidates(options)['selected']
    assert str(library) in selected['checks']
    library.write_bytes(b'changed library')
    with pytest.raises(ForgeError,match='changed'):reuse.check_files([selected])


def test_executable_symlink_retargeting_invalidates_source_identity(native):
    options,binary,runtime,request=native
    binary.unlink()
    folder=binary.parent.parent/'private';folder.mkdir()
    first=folder/'first';second=folder/'second'
    for p in (first,second):p.write_text('#!/bin/sh\nexit 0\n');p.chmod(0o700)
    binary.symlink_to(first)
    selected,=reuse.candidates(options)['selected']
    reuse.check_files([selected])
    binary.unlink();binary.symlink_to(second)
    with pytest.raises(ForgeError,match='changed'):reuse.check_files([selected])
