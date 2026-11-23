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
        variants = {}
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
    def resolve(policy,flags,*,suffix,historical_tool_hashes):
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


@pytest.mark.parametrize("bad_index", [0, 2])
@pytest.mark.parametrize("failure", ["solver", "patch-order", "unused", "stale-existing", "retained-stale", "unverified"])
def test_incompatible_candidate_keeps_other_tools(tmp_path, bad_index, failure):
    home = project_profile(ForgeHome(tmp_path / "forge"), "ubuntu24")
    (home.root / "tools/pkg/.spack-db").mkdir(parents=True)
    op = tmp_path / "op"
    env = op / "candidate"
    config = env / ".jforge-config/packages.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(yaml.safe_dump({"packages": {}}))
    names = ["first", "second", "third"]
    bad = names[bad_index]
    dag = [row("app", root=True, dependencies=[edge(name, "build") for name in names])]
    candidates = []
    for name in names:
        tool = row(name)
        dag.append(tool)
        prefix = home.root / "tools/pkg" / name
        prefix.mkdir()
        tool["node"]["parameters"]["patches"] = ["a", "b"]
        tool["node"]["patches"] = ["b", "a"]
        candidates.append({
            "name": name, "target_hash": name, "prefix": str(prefix), "spec": f"{name}@=1.0",
            "parameters": tool["node"]["parameters"], "patches": ["b", "a"],
            "checks": {}, "runtime_prefixes": [str(prefix)],
        })
    initial = copy.deepcopy(dag)
    stale = initial[bad_index + 1]
    if failure in {"stale-existing", "retained-stale"}:
        stale.update(external=True, prefix=candidates[bad_index]["prefix"])
    if failure == "unused":
        candidates[bad_index]["checks"] = {str(tmp_path / "disappeared"): "old"}
    (env / "spack.yaml").write_text(yaml.safe_dump({"spack": {"concretizer": {"reuse": {"from": [{"type": "local"}]}}}}))
    write_json(op / "dag.json", {"packages": initial})
    committed = tmp_path / "committed.lock"
    write_json(committed, {"roots": [{"hash": "app", "spec": "app"}],
                           "concrete_specs": {r["hash"]: {"name": r["name"], "version": r["version"], **r["node"]} for r in initial}})
    committed_bytes = committed.read_bytes()

    def resolve(policy, flags, *, suffix, historical_tool_hashes):
        lock = env / "spack.lock"
        assert lock.read_bytes() == committed_bytes and "--force" not in flags
        assert set(historical_tool_hashes) == planner.excluded_hashes
        rules = yaml.safe_load(config.read_text())["packages"]
        lock.write_text(",".join(sorted(rules)))
        if bad in rules and failure == "solver":
            write_json(op / f"command-concretize{suffix}.json", {"status": "failed", "exit_code": 1})
            raise ForgeError("incompatible candidate")
        solved = copy.deepcopy(dag)
        for tool in solved[1:]:
            name = tool["name"]
            if name == bad and failure == "unused":
                continue
            if failure == "retained-stale" and name == bad:
                tool.update(external=True, prefix=candidates[bad_index]["prefix"])
            if name in rules:
                tool.update(external=True, prefix=rules[name]["externals"][0]["prefix"])
                if name == bad and failure == "patch-order":
                    tool["node"]["patches"].reverse()
                if name == bad and failure == "unverified":
                    tool["prefix"] = str(home.root / "tools/pkg/unverified")
        write_json(op / "dag.json", {"packages": solved})
        return {"packages": solved}

    def query(options, label):
        if failure in {"stale-existing", "retained-stale"} and label.endswith("-1"):
            return {"selected": [], "unsafe_existing": [stale], "rejected": []}
        return {"selected": candidates, "unsafe_existing": [], "rejected": []}

    messages = []
    planner = reuse.Planner(home, env, op, query, resolve, committed_lock=committed, progress=messages.append)
    if failure == "retained-stale":
        with pytest.raises(reuse.ReuseRejected, match="another solve would repeat"):
            planner.apply({"packages": initial}, "never", ["concretize"])
        return
    result = planner.apply({"packages": initial}, "never", ["concretize"])
    expected = set(names) - {bad}
    assert {r["name"] for r in result["packages"] if r["external"]} == expected
    assert {r["name"] for r in read_json(env / "build-tool-reuse.json")["selected"]} == expected
    configured = set(names) if failure == "unused" else expected
    assert set(yaml.safe_load(config.read_text())["packages"]) == configured
    assert (env / "spack.lock").read_text() == ",".join(sorted(configured))
    assert messages[-1].startswith("Reused 2 build tools") and bad not in messages[-1]


@pytest.mark.parametrize("external_order", [None, ["b", "a"], ["a", "b"]])
def test_external_spec_lets_spack_derive_ordered_patches(monkeypatch, external_order):
    class PatchedSpec:
        prefix = "/installed/tool"

        def __init__(self):
            self.variants = {"patches": ["a", "b"], "shared": True}

        def node_dict_with_hashes(self):
            return {"parameters": self.variants, "patches": ["a", "b"]}

        def copy(self, *, deps):
            assert deps is False
            return copy.deepcopy(self)

        def format(self, template):
            assert self.variants == {"shared": True}
            return "tool@=1.0+shared"

    source = PatchedSpec()
    externals = ModuleType("spack.externals")
    def parse(config):
        assert config == {"spec": "tool@=1.0+shared", "prefix": source.prefix}
        params = {"patches": ["a", "b"]} if external_order else {}
        return SimpleNamespace(node_dict_with_hashes=lambda: {"parameters": params, "patches": external_order})
    externals.external_spec = parse
    monkeypatch.setitem(sys.modules, "spack.externals", externals)
    if external_order == ["a", "b"]:
        assert reuse.external_spec(source) == "tool@=1.0+shared"
    else:
        with pytest.raises(ValueError, match="patch hashes and order"):
            reuse.external_spec(source)
    assert source.variants["patches"] == ["a", "b"]


def save_lock(file, packages):
    write_json(file, {'_meta': {'file-type': 'spack-lockfile', 'lockfile-version': 6, 'specfile-version': 5},
                      'roots': [{'hash': p['hash'], 'spec': p['name']} for p in packages if p['root']],
                      'concrete_specs': {p['hash']: {'name': p['name'], 'version': p['version'], **p['node'],
                                                   **({'external': {'path': p['prefix']}} if p['external'] else {})}
                                         for p in packages}})


def test_history_is_derived_from_the_lock_after_evidence_is_empty(tmp_path):
    home = project_profile(ForgeHome(tmp_path / 'forge'), 'ubuntu24')
    environment = tmp_path / 'committed'
    tool = row('tool', hash='historical')
    tool.update(external=True, prefix=str(home.root / 'tools/pkg/tool-1.0'))
    consumer = row('consumer', root=True, dependencies=[{**edge('tool', 'build'), 'hash': 'historical'}])
    save_lock(environment / 'spack.lock', [consumer, tool])
    write_json(environment / 'build-tool-reuse.json', {'selected': []})
    refs, excluded = reuse.history(home, environment, preserve=True)
    assert excluded == {'historical'} and refs[0]['consumers'] == ['consumer']
    planner = reuse.Planner(home, environment, tmp_path, None, None, historical=refs)
    # A transaction which adds no optional tool must retain all old references.
    planner._save([consumer, tool, row('tool', hash='native', root=True)])
    evidence = read_json(environment / 'build-tool-reuse.json')
    assert evidence['selected'] == [] and evidence['retained'] == refs
    refs2, excluded2 = reuse.history(home, environment, preserve=True)
    assert refs2 == refs and excluded2 == excluded
    # Retained evidence also participates in exclusions when absent from this lock.
    save_lock(environment / 'spack.lock', [row('new', root=True)])
    assert reuse.history(home, environment)[1] == {'historical'}


@pytest.mark.parametrize('kind', ['root', 'link', 'run'])
def test_corrupt_historical_root_or_runtime_stops_before_solving(tmp_path, kind):
    home = project_profile(ForgeHome(tmp_path / 'forge'), 'ubuntu24')
    tool = row('tool', root=kind == 'root')
    tool.update(external=True, prefix=str(home.root / 'tools/pkg/tool-1.0'))
    consumer = row('consumer', root=True, dependencies=[edge('tool', kind if kind != 'root' else 'build')])
    environment = tmp_path / 'committed'
    save_lock(environment / 'spack.lock', [consumer, tool])
    with pytest.raises(reuse.ReuseRejected, match='uses historical tool tool') as error:
        reuse.history(home, environment, preserve=True)
    assert 'rebuild' in error.value.hint


@pytest.mark.parametrize('kind', ['root', 'runtime', 'link-edge', 'compiler', 'missing-source'])
def test_unsafe_references_report_the_dependency_use(native, kind):
    options, binary, runtime, request = native
    document = read_json(Path(options['dag']))
    packages = document['packages']
    packages[1].update(external=True, prefix=str(binary.parent.parent))
    if kind == 'root':
        packages[1]['root'] = True
    elif kind == 'runtime':
        packages[0]['node']['dependencies'][0]['parameters']['deptypes'] = ['run']
    elif kind == 'link-edge':
        packages.append(row('other', dependencies=[edge('tool', 'link')]))
        packages[0]['node']['dependencies'].append(edge('other', 'build'))
    elif kind == 'compiler':
        packages[0]['node']['dependencies'][0]['parameters']['virtuals'] = ['cxx']
    else:
        binary.unlink()
    write_json(Path(options['dag']), document)
    unsafe, = reuse.candidates(options)['unsafe_existing']
    assert unsafe['hash'] == 'tool' and unsafe['reason'] == kind


def test_fallback_requires_a_growing_hash_set_and_retains_history(tmp_path):
    home = project_profile(ForgeHome(tmp_path / 'forge'), 'ubuntu24')
    op = tmp_path / 'op'; env = op / 'candidate'
    cfg = env / '.jforge-config/packages.yaml'
    cfg.parent.mkdir(parents=True); cfg.write_text('packages: {}\n')
    (env / 'spack.yaml').write_text(yaml.safe_dump({'spack': {'concretizer': {'reuse': {'from': [{'type': 'local'}, {'type': 'external'}]}}}}))
    old = row('tool', hash='historical'); old.update(external=True, prefix=str(home.root / 'tools/pkg/tool-1.0'))
    consumer = row('consumer', root=True, dependencies=[{**edge('tool', 'build'), 'hash': 'historical'}])
    dag = [consumer, old, row('tool', hash='native', root=True)]
    write_json(op / 'dag.json', {'packages': dag})
    committed = tmp_path / 'committed.lock'; save_lock(committed, [consumer, old])
    refs = reuse.historical_references(home, [consumer, old])
    calls = []
    def resolve(policy, flags, *, suffix, historical_tool_hashes):
        calls.append(suffix)
        assert set(historical_tool_hashes) == {'historical', 'failed'}
        assert '--force' not in flags and (env / 'spack.lock').read_bytes() == committed.read_bytes()
        assert read_json(env / 'build-tool-reuse.json')['retained'] == refs
        return {'packages': [dag[-1]]}
    planner = reuse.Planner(home, env, op, None, resolve, historical=refs,
                            excluded_hashes=['historical'], committed_lock=committed)
    result = planner._fallback('never', ['concretize'], ['failed'])
    assert result['packages'] == [dag[-1]] and len(calls) == 1
    with pytest.raises(reuse.ReuseRejected, match='another solve would repeat'):
        planner._fallback('never', ['concretize'], ['historical', 'failed'])
    assert len(calls) == 1
    assert read_json(env / 'build-tool-reuse.json')['retained'] == refs


@pytest.mark.parametrize('failure', ['version', 'extract', 'define', 'body', 'none'])
def test_solver_filter_checks_the_engine_and_restores_descriptors(monkeypatch, failure):
    from jarvis_forge.spack_bridge import historical_build_tools
    from jarvis_forge.spack_backend import ENGINE_VERSION
    spack = ModuleType('spack'); spack.__version__ = ENGINE_VERSION if failure != 'version' else '9.9'
    solver = ModuleType('spack.solver'); asp = ModuleType('spack.solver.asp')
    class Spec:
        concrete = True
        def __init__(self, key, children=()): self.key, self.children = key, children
        def dag_hash(self): return self.key
        def traverse(self): return iter([self, *self.children])
    old = Spec('old'); native = Spec('native'); root = Spec('root', [old, native])
    class Solver:
        @staticmethod
        def _extract_concrete_specs(specs): return [s for r in specs for s in r.traverse()]
    class Setup:
        def define_concrete_input_specs(self, specs, possible):
            for r in specs:
                for s in r.traverse(): self.register_concrete_spec(s, possible)
        def register_concrete_spec(self, spec, possible): possible.add(spec.key)
    asp.Solver = Solver; asp.SpackSolverSetup = Setup
    if failure == 'extract': del Solver._extract_concrete_specs
    if failure == 'define': del Setup.define_concrete_input_specs
    extract, define = Solver.__dict__.get('_extract_concrete_specs'), Setup.__dict__.get('define_concrete_input_specs')
    monkeypatch.setitem(sys.modules, 'spack', spack)
    monkeypatch.setitem(sys.modules, 'spack.solver', solver)
    monkeypatch.setitem(sys.modules, 'spack.solver.asp', asp)
    if failure in {'version', 'extract', 'define'}:
        with pytest.raises(RuntimeError, match='verified'):
            with historical_build_tools(['old']): pytest.fail('must not enter')
    else:
        try:
            with historical_build_tools(['old']):
                assert [s.key for s in Solver._extract_concrete_specs([root])] == ['root', 'native']
                registered = set(); Setup().define_concrete_input_specs([root], registered)
                assert registered == {'root', 'native'}
                Setup().register_concrete_spec(old, registered)  # External selector remains untouched.
                assert 'old' in registered
                if failure == 'body': raise ValueError('probe')
        except ValueError:
            assert failure == 'body'
    assert Solver.__dict__.get('_extract_concrete_specs') is extract
    assert Setup.__dict__.get('define_concrete_input_specs') is define


def test_replay_validates_even_unused_historical_build_edges(tmp_path):
    home = project_profile(ForgeHome(tmp_path / 'forge'), 'ubuntu24')
    (home.root / 'tools/pkg/.spack-db').mkdir(parents=True)
    op = tmp_path / 'op'; env = op / 'candidate'
    cfg = env / '.jforge-config/packages.yaml'; cfg.parent.mkdir(parents=True); cfg.write_text('packages: {}\n')
    tool = row('tool', hash='historical'); tool.update(external=True, prefix=str(home.root / 'tools/pkg/tool-1.0'))
    consumer = row('consumer', root=True, dependencies=[{**edge('tool', 'build'), 'hash': 'historical'}])
    dag = [consumer, tool]; write_json(op / 'dag.json', {'packages': dag}); save_lock(env / 'spack.lock', dag)
    before = (env / 'spack.lock').read_bytes()
    def query(options, label):
        assert set(options['needed']) == {'consumer', 'historical'}
        return {'selected': [], 'rejected': [], 'unsafe_existing': [{**tool, 'reason': 'missing-source'}]}
    planner = reuse.Planner(home, env, op, query, lambda *a, **k: pytest.fail('no solve during replay'), replay=True)
    with pytest.raises(reuse.ReuseRejected, match='Historical build tools are unavailable'):
        planner.apply({'packages': [consumer]}, 'only', ['concretize'])
    assert (env / 'spack.lock').read_bytes() == before


def test_fixed_tool_suite_roots_are_not_optional_historical_loans(tmp_path, monkeypatch):
    from jarvis_forge import unified_profiles, toolchains, spack_backend
    home = project_profile(ForgeHome(tmp_path / 'forge'), 'ubuntu24')
    cmake = row('cmake', root=True); cmake.update(external=True, prefix=str(home.root / 'tools/pkg/cmake-1.0'))
    chain = {'id': 'ubuntu24', 'version': '13.3.0', 'prefix': str(home.root / 'tools/pkg/gcc-13.3.0'),
             'tools': {'cmake': {'version': '1.0', 'prefix': cmake['prefix']}}}
    monkeypatch.setattr(unified_profiles, 'record', lambda scope: {'backend': 'spack', 'toolchain': chain})
    environment = tmp_path / 'committed'; save_lock(environment / 'spack.lock', [cmake])
    assert reuse.history(home, environment, preserve=True) == ([], set())
    # Same name still means fixed tool; a changed identity is a binding error.
    cmake['prefix'] = str(home.root / 'tools/pkg/other-cmake-1.0')
    save_lock(environment / 'spack.lock', [cmake])
    assert reuse.history(home, environment, preserve=True) == ([], set())
    state = read_json(home.root / 'packages.json')
    state['spack'].update(roots=['cmake'], packages=[cmake], environment=str(environment))
    write_json(home.root / 'packages.json', state)
    monkeypatch.setattr(toolchains, 'binding', lambda scope: chain)
    monkeypatch.setattr('jarvis_forge.apt_profiles.validate_active', lambda *a, **k: None)
    monkeypatch.setattr(spack_backend, 'run', lambda *a, **k: pytest.fail('binding must fail before solving'))
    with pytest.raises(ForgeError, match='Fixed tool binding mismatch for cmake') as error:
        spack_backend.transact(home, 'install', ['new-package'], cache='never')
    assert cmake['prefix'] in str(error.value) and chain['tools']['cmake']['prefix'] in str(error.value)
    assert 'profile check ubuntu24' in error.value.hint and 'Recreate the profile' in error.value.hint
    assert 'rebuild cmake' not in error.value.hint


def test_retargeted_tool_prefix_remains_a_historical_reference(tmp_path):
    home = project_profile(ForgeHome(tmp_path / 'forge'), 'ubuntu24')
    outside = tmp_path / 'outside'; outside.mkdir()
    link = home.root / 'tools/pkg/tool-1.0'; link.parent.mkdir(parents=True, exist_ok=True); link.symlink_to(outside)
    tool = row('tool'); tool.update(external=True, prefix=str(link))
    assert reuse.historical_references(home, [tool])[0]['hash'] == 'tool'


def test_suite_names_are_shared_by_history_planner_and_inventory(tmp_path, monkeypatch):
    from jarvis_forge import toolchains, unified_profiles
    home = project_profile(ForgeHome(tmp_path / 'forge'), 'ubuntu24')
    tools = home.root / 'tools'; (tools / 'pkg/.spack-db').mkdir(parents=True)
    chain = {'version': '13.3.0', 'prefix': str(tools / 'pkg/gcc-13.3.0'),
             'tools': {'cmake': {'version': '3.28.6', 'prefix': str(tools / 'pkg/cmake-3.28.6')}}}
    monkeypatch.setattr(unified_profiles, 'record', lambda scope: {'backend': 'spack', 'toolchain': chain} if scope.root == home.root else None)
    fixed = toolchains.suite(chain)
    assert fixed == {'gcc': {'version': chain['version'], 'prefix': chain['prefix']}, 'cmake': chain['tools']['cmake']}
    rules = {}; toolchains.spack_requirements({**chain, 'drivers': {'CC': '/cc', 'CXX': '/cxx', 'FC': '/fc'}}, rules)
    assert {n for n, r in rules.items() if r.get('buildable') is False} == set(fixed)
    # Root/manual flags deliberately disagree with suite membership.
    inventory = [dict(name=n, hash=n, version='0.1', prefix=str(tools / 'pkg' / n), root=False, automatic=True) for n in fixed]
    inventory.append(dict(name='pkgconf', hash='pkgconf', version='1.0', prefix=str(tools / 'pkg/pkgconf-1.0'), root=True, automatic=False))
    write_json(tools / 'packages.json', {'spack': {'packages': inventory}})
    classified = unified_profiles.package_inventory(home)
    assert {r['name'] for r in classified if r['category'] == 'base-tool'} == set(fixed)
    assert next(r for r in classified if r['name'] == 'pkgconf')['category'] == 'bootstrap-dependency'
    refs = [row(r['name']) for r in inventory]
    for ref, item in zip(refs, inventory): ref.update(external=True, prefix=item['prefix'])
    assert {r['name'] for r in reuse.historical_references(home, refs)} == {'pkgconf'}

    # A trial's buildable:false external remains a candidate, not a fixed tool.
    op = tmp_path / 'op'; env = op / 'candidate'; config = env / '.jforge-config/packages.yaml'
    config.parent.mkdir(parents=True); config.write_text(yaml.safe_dump({'packages': {'pkgconf': {'buildable': False}}}))
    source = tools / 'pkg/pkgconf-1.0'; source.mkdir()
    tool = row('pkgconf'); tool.update(external=True, prefix=str(source))
    dag = [row('app', root=True, dependencies=[edge('pkgconf', 'build')]), tool]
    write_json(op / 'dag.json', {'packages': dag})
    def query(options, label):
        assert set(options['fixed']) == set(fixed) and 'pkgconf' in options['needed']
        return {'selected': [{'name': 'pkgconf', 'version': '1.0', 'prefix': str(source), 'target_hash': 'pkgconf',
                              'parameters': tool['node']['parameters'], 'checks': {}, 'runtime_prefixes': [str(source)]}],
                'rejected': [], 'unsafe_existing': []}
    planner = reuse.Planner(home, env, op, query, lambda *a, **k: pytest.fail('already concrete tool'))
    planner.apply({'packages': dag}, 'never', ['concretize'])
    assert read_json(env / 'build-tool-reuse.json')['selected'][0]['name'] == 'pkgconf'
    # Without a suite record, preserve the original root/manual classification.
    monkeypatch.setattr(unified_profiles, 'record', lambda scope: {'backend': 'spack'} if scope.root == home.root else None)
    legacy = unified_profiles.package_inventory(home)
    assert {r['name'] for r in legacy if r['category'] == 'base-tool'} == {'pkgconf'}
