"""Portable managed versions, exact variant comparison and no host path replay."""
from copy import deepcopy
import hashlib
from pathlib import Path
import pytest
import yaml
from jarvis_forge import profile_exports as exports, unified_profiles as profiles, spack_backend,build_apps
from jarvis_forge.storage import ForgeHome,ForgeError,write_json,read_json
from profile_fixtures import project_profile
from test_build_apps import projects


def document(tmp_path):
    value={'schema_version':1,'kind':exports.FORMAT,'name':'science','backend':'spack',
           'platform':{'name':'ubuntu24','preset':'ubuntu24','revision':1,'target':'x86_64'},
           'python':{'version':profiles._python()['version']},'roots':['zlib@=1.3.1'],
           'packages':[{'name':'zlib','version':'1.3.1','external':False,'variants':{'shared':True}}],'apps':[]}
    path=tmp_path/'profile.yaml';path.write_text(yaml.safe_dump(value));return value,path


def test_export_uses_full_lock_dag_and_portable_package_identity(tmp_path,monkeypatch):
    home=ForgeHome(tmp_path/'forge');scope=project_profile(home)
    state=read_json(scope.root/'packages.json');state['spack']['roots']=['zlib@=1.3.1'];write_json(scope.root/'packages.json',state)
    rows=[{'name':'zlib','version':'1.3.1','hash':'host-specific','prefix':'/host/specific',
           'node':{'parameters':{'shared':True}}},{'name':'glibc','version':'2.39','external':True,'node':{'parameters':{}}}]
    monkeypatch.setattr(spack_backend,'locked_packages',lambda home:rows)
    value=exports.export(home,'default')
    assert value['packages']==[exports.package_identity(row) for row in rows]
    assert 'host-specific' not in yaml.safe_dump(value) and '/host/specific' not in yaml.safe_dump(value)
    value['platform']['target']='x86_64';file=tmp_path/'export.yaml';file.write_text(yaml.safe_dump(value))
    assert exports.load(file)==value


def test_variant_diff_refuses_before_install_and_pins_native_syntax(tmp_path):
    value,path=document(tmp_path);exports.load(path)
    exports.compare(value['packages'],[{'name':'zlib','version':'1.3.1','node':{'parameters':{'shared':True}}}])
    assert exports.constraints(value['packages'])=={'zlib':'@=1.3.1 +shared'}
    with pytest.raises(ForgeError,match='versions or variants differ'):
        exports.compare(value['packages'],[{'name':'zlib','version':'1.3.1','node':{'parameters':{'shared':False}}}])


def test_python_mismatch_fails_before_creating_any_platform(tmp_path,monkeypatch):
    value,file=document(tmp_path);value['python']['version']='2.7.18';file.write_text(yaml.safe_dump(value))
    monkeypatch.setattr(profiles,'create',lambda *a,**kw:pytest.fail('Python must be checked first'))
    home=ForgeHome(tmp_path/'absent')
    with pytest.raises(ForgeError,match='pyenv'):exports.reproduce(home,'new',file)
    assert not home.root.exists()


@pytest.mark.parametrize('mutation',[lambda d:d['platform'].update(revision=True),lambda d:d['packages'][0]['variants'].update(bad=float('nan')),
    lambda d:d['packages'].append(deepcopy(d['packages'][0])),lambda d:d['platform'].update(target='../escape')])
def test_export_rejects_ambiguous_or_invalid_identity(tmp_path,mutation):
    value,file=document(tmp_path);mutation(value);file.write_text(yaml.safe_dump(value))
    with pytest.raises(ForgeError,match='Invalid profile export'):exports.load(file)


def test_app_export_rejects_unavailable_local_source_paths(tmp_path,projects):
    home=ForgeHome(tmp_path/'forge');project_profile(home)
    result=build_apps.install(home,str(projects[0]),confirm=lambda plan:None)
    assert result['status']=='passed'
    with pytest.raises(ForgeError,match='local sources'):exports.export(home,'default')


def test_bundle_integrity_and_companion_materialization(tmp_path):
    home=ForgeHome(tmp_path/'forge');scope=project_profile(home)
    text='schema_version: 1\nkind: build\nid: check\nsteps:\n- id: smoke\n  uses: command\n  with:\n    argv: [true]\n'
    # Resource integrity is checked by both the export reader and the recipe loader.
    text=text.replace('[true]','["true"]')
    bundle={'text':text,'sha256':hashlib.sha256(text.encode()).hexdigest(),'files':[]}
    value,file=document(tmp_path);value['apps']=[{'name':'check','version':'1.0','recipe':bundle}];file.write_text(yaml.safe_dump(value))
    app=exports.load(file)['apps'][0]
    workspace=build_apps.import_bundle(home,scope,app)
    assert (workspace/'build.yaml').read_text()==text
    value['apps'][0]['recipe']['text']+='changed';file.write_text(yaml.safe_dump(value))
    with pytest.raises(ForgeError,match='checksum'):exports.load(file)


def test_release_app_bundle_builds_in_another_empty_profile(tmp_path,projects):
    from jarvis_forge import build_catalog
    from test_project_builds import workspace_release
    first=ForgeHome(tmp_path/'original');project_profile(first)
    index=workspace_release(tmp_path,projects)
    build_catalog.update(first,index.as_uri())
    result=build_apps.install(first,'workspace-tiny',confirm=lambda plan:None)
    assert result['status']=='passed',result
    document=exports.export(first,'default')
    second=ForgeHome(tmp_path/'destination');scope=project_profile(second)
    app,=document['apps']
    assert app['recipe']['files'] and str(first.root) not in app['recipe']['text']
    workspace=build_apps.import_bundle(second,scope,app)
    result=build_apps.install(second,str(workspace),scope=scope,confirm=lambda plan:None)
    assert result['status']=='passed',result
    assert (scope.root/'apps/workspace-tiny-1.0/include/value.h').is_file()


def test_export_preserves_actual_parameter_overrides(tmp_path,projects):
    home=ForgeHome(tmp_path/'forge');project_profile(home)
    assert build_apps.install(home,str(projects[0]),parameters={'cxx_standard':20},confirm=lambda plan:None)['status']=='passed'
    row=build_apps.installed(home)[0]
    # Local source export is deliberately refused; inspect a portable snapshot.
    manifest=read_json(Path(row['metadata']))
    graph_file=home.root/'runs'/manifest['run_id']/'graph.json';graph=read_json(graph_file)
    node=graph['nodes'][manifest['recipe']]
    node['config']['sources']={'tiny':{'url':'https://example.test/tiny.tar.gz','sha256':'a'*64}}
    write_json(graph_file,graph)
    exported=exports.export(home,'default')['apps'][0]['recipe']
    assert yaml.safe_load(exported['text'])['parameters']['cxx_standard']==20


def test_export_refuses_a_partially_published_profile_without_writes(tmp_path):
    home=ForgeHome(tmp_path/'forge');scope=project_profile(home)
    before={file:file.read_bytes() for file in home.root.rglob('*') if file.is_file()}
    from jarvis_forge.builds import file_lock
    with file_lock(scope.root/'locks/apps.lock'):
        with pytest.raises(ForgeError,match='changing this profile'):exports.export(home,'default')
    assert all(file.read_bytes()==content for file,content in before.items())
    assert {file for file in home.root.rglob('*') if file.is_file()}==set(before)


def test_companion_copy_preserves_bytes_used_by_its_checksum(tmp_path):
    import hashlib
    from jarvis_forge import build_catalog
    source=tmp_path/'recipe';source.mkdir()
    script=source/'accept.py';content=b'print("portable")\r\n';script.write_bytes(content)
    config={'schema_version':1,'kind':'build','id':'portable','resources':[{'path':'accept.py','sha256':hashlib.sha256(content).hexdigest()}],
            'steps':[{'id':'run','uses':'command','with':{'argv':['python3','${build_dir}/accept.py']}}]}
    entry=source/'build.yaml';entry.write_text(yaml.safe_dump(config))
    output=tmp_path/'copy/build.yaml';build_catalog.copy_bundle(entry,output)
    assert output.with_name('accept.py').read_bytes()==content
