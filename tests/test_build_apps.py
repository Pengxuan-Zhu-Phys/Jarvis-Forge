"""Install real tiny CMake apps in disposable profiles without native package downloads."""
from pathlib import Path
import json
import pytest
import yaml
from jarvis_forge import build_apps,builds,unified_profiles as profiles
from jarvis_forge.storage import ForgeHome,ForgeError,read_json
from profile_fixtures import project_profile
from test_project_builds import projects


@pytest.fixture
def home(tmp_path):
    home=ForgeHome(tmp_path/'forge');project_profile(home)
    return home


def test_internal_workspace_build_and_prefix_belong_to_profile(home,projects):
    producer,_=projects
    result=build_apps.install(home,str(producer),confirm=lambda plan:None)
    assert result['status']=='passed',result
    scope=home.root/'profiles/default';workspace=scope/'src/tiny-1.0'
    assert (workspace/'build.yaml').is_file() and (scope/'build/tiny-1.0/tiny/CMakeCache.txt').is_file()
    assert (scope/'apps/tiny-1.0/include/value.h').is_file()
    row,=build_apps.installed(home)
    assert row['enabled'] and not row['external'] and row['workspace']==str(workspace)
    again=build_apps.install(home,'tiny',confirm=lambda plan:pytest.fail('Reuse must not rebuild'))
    assert again['status']=='passed' and not again['runs']


def test_version_switch_reenables_without_building_and_autoremove_reclaims_old_versions(home,projects):
    producer,_=projects
    first=build_apps.install(home,str(producer),confirm=lambda plan:None);assert first['status']=='passed',first
    config=yaml.safe_load(producer.read_text());config['steps'][0]['with']['export']['version']='2.0';producer.write_text(yaml.safe_dump(config))
    second=build_apps.install(home,str(producer),confirm=lambda plan:None);assert second['status']=='passed',second
    rows=build_apps.installed(home);assert len(rows)==2
    assert [row['version'] for row in rows if row['enabled']]==['2.0']
    result=build_apps.install(home,'tiny@1.0',confirm=lambda plan:None)
    assert result['status']=='passed' and not result['runs']
    assert (home.root/'profiles/default/apps/tiny-2.0').is_dir()
    build_apps.autoremove(home,confirm=lambda plan:None)
    assert not (home.root/'profiles/default/apps/tiny-2.0').exists()
    assert not (home.root/'profiles/default/src/tiny-2.0').exists()
    assert (home.root/'profiles/default/apps/tiny-1.0/include/value.h').is_file()


def test_same_version_rebuild_failure_restores_prefix_metadata_and_activation(home,projects):
    producer,_=projects
    first=build_apps.install(home,str(producer),confirm=lambda plan:None);assert first['status']=='passed',first
    workspace=home.root/'profiles/default/src/tiny-1.0';scope=ForgeHome(home.root/'profiles/default')
    prefix=scope.root/'apps/tiny-1.0';before=builds.cmake.manifest(prefix);metadata=(scope.root/'metadata.json').read_bytes();activation=(scope.root/'activate.sh').read_bytes()
    config=yaml.safe_load((workspace/'build.yaml').read_text())
    config['steps'].append({'id':'fail','uses':'command','needs':['tiny'],'with':{'argv':['false']}})
    config['outputs']['installation']['publish_after']=['fail'];(workspace/'build.yaml').write_text(yaml.safe_dump(config))
    failed=build_apps.install(home,'tiny',confirm=lambda plan:None)
    assert failed['status']=='failed',failed
    builds.cmake.verify_manifest(prefix,before)
    assert (scope.root/'metadata.json').read_bytes()==metadata and (scope.root/'activate.sh').read_bytes()==activation


def test_external_workspace_is_excluded_from_profile_activation_and_never_deleted(home,projects,tmp_path):
    producer,_=projects
    workspace=build_apps.fetch(home,str(producer),tmp_path/'user-app')
    result=build_apps.install(home,str(workspace),confirm=lambda plan:None);assert result['status']=='passed',result
    row,=build_apps.installed(home)
    assert row['external'] and Path(row['prefix'])==workspace/'install'
    assert str(workspace) not in (home.root/'profiles/default/activate.sh').read_text()
    build_apps.remove(home,['tiny'],confirm=lambda plan:None)
    assert (workspace/'install/include/value.h').is_file() and (workspace/'build.yaml').is_file()
    assert not build_apps.installed(home)


def test_failed_install_automatically_resumes_only_unchanged_inputs(home,projects,tmp_path):
    producer,_=projects
    config=yaml.safe_load(producer.read_text());marker=tmp_path/'ready'
    config['steps'].append({'id':'gate','uses':'command','needs':['tiny'],'with':{'argv':['test','-f',str(marker)]}})
    config['outputs']['installation']['publish_after']=['gate'];producer.write_text(yaml.safe_dump(config))
    failed=build_apps.install(home,str(producer),confirm=lambda plan:None);assert failed['status']=='failed',failed
    marker.touch()
    passed=build_apps.install(home,'tiny',confirm=lambda plan:None);assert passed['status']=='passed',passed
    assert passed['runs'][0]['run_id']==failed['runs'][0]['run_id']


def test_dependencies_are_prepared_and_removal_checks_enabled_consumers(home,projects):
    _,consumer=projects
    result=build_apps.install(home,str(consumer),confirm=lambda plan:None);assert result['status']=='passed',result
    assert {row['name'] for row in build_apps.installed(home)}=={'tiny','consumer'}
    with pytest.raises(ForgeError,match='consumer'):build_apps.remove(home,['tiny'],confirm=lambda plan:None)
    build_apps.remove(home,['consumer'],confirm=lambda plan:None)
    build_apps.remove(home,['tiny'],confirm=lambda plan:None)
    assert not build_apps.installed(home)


def test_dependency_version_switch_refuses_enabled_consumers(home,projects):
    producer,consumer=projects
    assert build_apps.install(home,str(consumer),confirm=lambda plan:None)['status']=='passed'
    original=(home.root/'profiles/default/activate.sh').read_bytes()
    config=yaml.safe_load(producer.read_text());config['steps'][0]['with']['export']['version']='2.0';producer.write_text(yaml.safe_dump(config))
    with pytest.raises(ForgeError,match='consumer'):build_apps.install(home,str(producer),confirm=lambda plan:None)
    assert (home.root/'profiles/default/activate.sh').read_bytes()==original
    assert not (home.root/'profiles/default/apps/tiny-2.0').exists()


def test_rebuild_rollback_preserves_last_successful_workspace_state(home,projects):
    producer,_=projects
    assert build_apps.install(home,str(producer),confirm=lambda plan:None)['status']=='passed'
    workspace=home.root/'profiles/default/src/tiny-1.0';original=read_json(workspace/'metadata.json')['install_state']
    config=yaml.safe_load((workspace/'build.yaml').read_text());config['steps'][0]['with']['targets']=['does-not-exist'];(workspace/'build.yaml').write_text(yaml.safe_dump(config))
    assert build_apps.install(home,'tiny',confirm=lambda plan:None)['status']=='failed'
    assert read_json(workspace/'metadata.json')['install_state']==original


def test_catalog_upgrade_installs_new_version_and_preserves_old(home,projects,monkeypatch):
    producer,_=projects
    assert build_apps.install(home,str(producer),confirm=lambda plan:None)['status']=='passed'
    monkeypatch.setattr(build_apps.build_catalog,'acquire',lambda *a,**kw:producer)
    config=yaml.safe_load(producer.read_text());config['steps'][0]['with']['export']['version']='2.0';producer.write_text(yaml.safe_dump(config))
    planned=build_apps.upgrade(home,dry_run=True)
    assert planned['apps'][0]['version']=='2.0' and not (home.root/'profiles/default/apps/tiny-2.0').exists()
    result=build_apps.upgrade(home,confirm=lambda plan:None)
    assert result['status']=='passed',result
    assert [row['version'] for row in build_apps.installed(home) if row['enabled']]==['2.0']
    assert (home.root/'profiles/default/apps/tiny-1.0/include/value.h').is_file()


def test_same_version_catalog_upgrade_restores_recipe_on_failure(home,projects,monkeypatch):
    producer,_=projects
    assert build_apps.install(home,str(producer),confirm=lambda plan:None)['status']=='passed'
    monkeypatch.setattr(build_apps.build_catalog,'acquire',lambda *a,**kw:producer)
    workspace=home.root/'profiles/default/src/tiny-1.0';original=(workspace/'build.yaml').read_bytes()
    config=yaml.safe_load(producer.read_text());config['steps'][0]['with']['targets']=['invalid-target'];producer.write_text(yaml.safe_dump(config))
    result=build_apps.upgrade(home,confirm=lambda plan:None)
    assert result['status']=='failed'
    assert (workspace/'build.yaml').read_bytes()==original
    assert (home.root/'profiles/default/apps/tiny-1.0/include/value.h').is_file()


def test_external_workspace_profile_rebuild_failure_restores_old_owner(home,projects,tmp_path):
    from profile_fixtures import project_profile
    from jarvis_forge.storage import write_json
    producer,_=projects
    workspace=build_apps.fetch(home,str(producer),tmp_path/'external')
    assert build_apps.install(home,str(workspace),confirm=lambda plan:None)['status']=='passed'
    before=(workspace/'metadata.json').read_bytes();activation=(workspace/'activate.sh').read_bytes()
    old=(home.root/'profiles/default/metadata.json').read_bytes()
    second=project_profile(home,'second')
    config=yaml.safe_load((workspace/'build.yaml').read_text());config['steps'][0]['with']['targets']=['invalid-target'];(workspace/'build.yaml').write_text(yaml.safe_dump(config))
    failed=build_apps.install(home,str(workspace),scope=second,rebuild=True,confirm=lambda plan:None)
    assert failed['status']=='failed'
    assert (workspace/'metadata.json').read_bytes()==before and (workspace/'activate.sh').read_bytes()==activation
    assert (home.root/'profiles/default/metadata.json').read_bytes()==old
    assert not build_apps.installed(home,second)


def test_root_python_preflight_fails_before_fetching_dependency(home,projects,monkeypatch):
    producer,consumer=projects
    config=yaml.safe_load(consumer.read_text());config['steps'].insert(0,{'id':'preflight','uses':'check','with':{'spec':{'python':{'modules':['nonexistent_forge_preflight_module']}}}})
    consumer.write_text(yaml.safe_dump(config))
    original=build_apps.fetch
    def fetch(base,reference,*args,**kwargs):
        if str(reference)==str(producer):pytest.fail('Dependency must not be fetched before checking parent Python')
        return original(base,reference,*args,**kwargs)
    monkeypatch.setattr(build_apps,'fetch',fetch)
    with pytest.raises(ForgeError,match='nonexistent_forge_preflight_module'):
        build_apps.install(home,str(consumer),confirm=lambda plan:None)
    assert not build_apps.installed(home)


def test_cross_profile_plan_failure_does_not_change_workspace_owner(home,projects,tmp_path,monkeypatch):
    producer,_=projects
    workspace=build_apps.fetch(home,str(producer),tmp_path/'external')
    assert build_apps.install(home,str(workspace),confirm=lambda plan:None)['status']=='passed'
    before=(workspace/'metadata.json').read_bytes()
    second=project_profile(home,'second')
    def fail(*args,**kwargs):raise ForgeError('bad recipe input')
    monkeypatch.setattr(build_apps,'_input',fail)
    with pytest.raises(ForgeError,match='bad recipe input'):
        build_apps.install(home,str(workspace),scope=second,rebuild=True,confirm=lambda plan:None)
    assert (workspace/'metadata.json').read_bytes()==before


def test_late_rebuild_publication_failure_restores_original_prefix(home,projects,monkeypatch):
    producer,_=projects
    assert build_apps.install(home,str(producer),confirm=lambda plan:None)['status']=='passed'
    scope=ForgeHome(home.root/'profiles/default');prefix=scope.root/'apps/tiny-1.0'
    (prefix/'original-sentinel').write_text('keep')
    # The output is not re-enabled from a tampered receipt: force a fresh rebuild.
    original=(scope.root/'metadata.json').read_bytes();activation=(scope.root/'activate.sh').read_bytes()
    sync=profiles.sync_definition;count=[0]
    def fail_late(scope):
        count[0]+=1
        if count[0]>=1:raise ForgeError('cannot publish profile definition')
        return sync(scope)
    monkeypatch.setattr(profiles,'sync_definition',fail_late)
    with pytest.raises(ForgeError,match='cannot publish'):
        build_apps.install(home,'tiny',rebuild=True,confirm=lambda plan:None)
    assert (prefix/'original-sentinel').read_text()=='keep'
    assert (scope.root/'metadata.json').read_bytes()==original and (scope.root/'activate.sh').read_bytes()==activation


def test_successful_same_version_rebuild_replaces_receipt_at_original_prefix(home,projects):
    producer,_=projects
    first=build_apps.install(home,str(producer),confirm=lambda plan:None)
    old=first['apps'][0];prefix=Path(old['prefix'])
    second=build_apps.install(home,'tiny',rebuild=True,confirm=lambda plan:None)
    assert second['status']=='passed',second
    row,=build_apps.installed(home)
    assert row['prefix']==str(prefix) and row['enabled']
    assert builds.artifact(home,row['artifact_id'])['prefix']==str(prefix)
    assert not list((prefix.parent/'rollback').glob('[0-9]*'))


def test_unregistered_app_prefix_is_preserved_before_build(home,projects):
    producer,_=projects
    prefix=home.root/'profiles/default/apps/tiny-1.0';prefix.mkdir(parents=True);(prefix/'user-data').write_text('keep')
    with pytest.raises(ForgeError,match='Unregistered app directory'):
        build_apps.install(home,str(producer),confirm=lambda plan:None)
    assert (prefix/'user-data').read_text()=='keep' and not (prefix/'include').exists()


def test_explicit_version_is_not_overridden_by_another_current_workspace(home,projects,monkeypatch):
    producer,_=projects
    first=build_apps.install(home,str(producer),confirm=lambda plan:None)
    config=yaml.safe_load(producer.read_text());config['steps'][0]['with']['export']['version']='2.0';producer.write_text(yaml.safe_dump(config))
    second=build_apps.install(home,str(producer),confirm=lambda plan:None)
    monkeypatch.chdir(home.root/'profiles/default/src/tiny-2.0')
    enabled=build_apps.install(home,'tiny@1.0',confirm=lambda plan:None)
    assert enabled['status']=='passed' and not enabled['runs']
    assert [row['version'] for row in build_apps.installed(home) if row['enabled']]==['1.0']


def test_initial_publication_failure_does_not_enable_app_and_can_resume(home,projects,monkeypatch):
    producer,_=projects
    scope=ForgeHome(home.root/'profiles/default');before=(scope.root/'metadata.json').read_bytes();activation=(scope.root/'activate.sh').read_bytes()
    sync=profiles.sync_definition
    monkeypatch.setattr(profiles,'sync_definition',lambda scope:(_ for _ in ()).throw(ForgeError('publication failed')))
    with pytest.raises(ForgeError,match='publication failed'):
        build_apps.install(home,str(producer),confirm=lambda plan:None)
    assert (scope.root/'metadata.json').read_bytes()==before and (scope.root/'activate.sh').read_bytes()==activation
    assert not build_apps.installed(home)
    monkeypatch.setattr(profiles,'sync_definition',sync)
    resumed=build_apps.install(home,'tiny',confirm=lambda plan:None)
    assert resumed['status']=='passed',resumed
    assert len(build_apps.installed(home))==1


def test_companion_only_catalog_upgrade_rebuilds_and_preserves_local_edits(home,projects,tmp_path,monkeypatch):
    import hashlib
    producer,_=projects
    script=tmp_path/'accept.py';script.write_text('print("first")\n')
    config=yaml.safe_load(producer.read_text())
    config['steps'].append({'id':'accept','uses':'command','needs':['tiny'],'with':{'argv':['python3','${build_dir}/accept.py']}})
    config['outputs']['installation']['publish_after']=['accept']
    def publish():
        config['resources']=[{'path':'accept.py','sha256':hashlib.sha256(script.read_bytes()).hexdigest()}]
        producer.write_text(yaml.safe_dump(config))
    publish()
    assert build_apps.install(home,str(producer),confirm=lambda plan:None)['status']=='passed'
    monkeypatch.setattr(build_apps.build_catalog,'acquire',lambda *a,**kw:producer)
    script.write_text('print("second")\n');publish()
    result=build_apps.upgrade(home,confirm=lambda plan:None)
    assert result['status']=='passed',result
    workspace=home.root/'profiles/default/src/tiny-1.0'
    assert (workspace/'accept.py').read_text()==script.read_text()
    (workspace/'accept.py').write_text('# my edits\n')
    script.write_text('print("third")\n');publish()
    with pytest.raises(ForgeError,match='local edits'):build_apps.upgrade(home,confirm=lambda plan:None)
    assert (workspace/'accept.py').read_text()=='# my edits\n'


def test_wrong_pyenv_blocks_before_app_environment_mutation(home,projects,monkeypatch):
    producer,_=projects
    assert build_apps.install(home,str(producer),confirm=lambda plan:None)['status']=='passed'
    before=(home.root/'profiles/default/metadata.json').read_bytes()
    def fail(*args):raise ForgeError('Wrong selected Python')
    monkeypatch.setattr(profiles,'validate_python',fail)
    with pytest.raises(ForgeError,match='Wrong selected Python'):build_apps.install(home,'tiny',confirm=lambda plan:None)
    assert (home.root/'profiles/default/metadata.json').read_bytes()==before


def test_unchanged_yaml_with_new_companion_is_an_upgrade(home,projects,tmp_path,monkeypatch):
    from test_project_builds import workspace_release
    from jarvis_forge import build_catalog
    index=workspace_release(tmp_path,projects)
    build_catalog.update(home,index.as_uri())
    assert build_apps.install(home,'workspace-tiny',confirm=lambda plan:None)['status']=='passed'
    source=build_catalog.resolve(home,'workspace-tiny',local_workspaces=False)
    before=source.read_bytes();script=source.parent/'scripts/accept.py'
    script.write_text(script.read_text()+'print("updated companion")\n')
    monkeypatch.setattr(build_catalog,'acquire',lambda *a,**kw:source)
    planned=build_apps.upgrade(home,dry_run=True)
    assert planned['plans']
    upgraded=build_apps.upgrade(home,confirm=lambda plan:None)
    assert upgraded['status']=='passed',upgraded
    assert source.read_bytes()==before
    assert 'updated companion' in (home.root/'profiles/default/src/workspace-tiny-1.0/scripts/accept.py').read_text()


def test_foreign_forge_workspace_is_not_adopted_by_matching_profile_name(home,projects,tmp_path):
    other=ForgeHome(tmp_path/'other');project_profile(other)
    workspace=build_apps.fetch(home,str(projects[0]),tmp_path/'external')
    before=(workspace/'metadata.json').read_bytes()
    with pytest.raises(ForgeError,match='another Forge home'):
        build_apps.install(other,str(workspace),rebuild=True,confirm=lambda plan:None)
    assert (workspace/'metadata.json').read_bytes()==before


def test_backup_cleanup_failure_preserves_successful_rebuild(home,projects,monkeypatch):
    assert build_apps.install(home,str(projects[0]),confirm=lambda plan:None)['status']=='passed'
    remove=build_apps.shutil.rmtree
    def denied(path,*args,**kwargs):
        if 'rollback' in Path(path).parts:raise PermissionError('cleanup denied')
        return remove(path,*args,**kwargs)
    monkeypatch.setattr(build_apps.shutil,'rmtree',denied)
    result=build_apps.install(home,'tiny',rebuild=True,confirm=lambda plan:None)
    assert result['status']=='passed' and result['cleanup_pending']
    assert builds.artifact(home,result['apps'][0]['artifact_id'])['status']=='ready'


def test_internal_workspace_cannot_be_transferred_to_another_profile(home,projects):
    workspace=build_apps.fetch(home,str(projects[0]));original=(workspace/'metadata.json').read_bytes()
    second=project_profile(home,'second')
    with pytest.raises(ForgeError,match='Internal app workspaces'):
        build_apps.install(home,str(workspace),scope=second,rebuild=True,confirm=lambda plan:None)
    assert (workspace/'metadata.json').read_bytes()==original
    assert not (second.root/'apps/tiny-1.0').exists()


def test_dependency_build_uses_requested_shared_cxx_standard(home,projects):
    _,consumer=projects
    result=build_apps.install(home,str(consumer),parameters={'cxx_standard':20},confirm=lambda plan:None)
    assert result['status']=='passed',result
    for app in build_apps.installed(home):
        assert builds.artifact(home,app['artifact_id'])['context']['cxx_standard']=='20'
