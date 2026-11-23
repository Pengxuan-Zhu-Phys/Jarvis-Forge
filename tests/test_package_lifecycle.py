"""Physical package deletion, same-prefix rebuilding and recoverable metadata."""

from pathlib import Path
import json

import pytest

from jarvis_forge.package_lifecycle import Transaction,reverse_dependents,protect_apps,history,historical
from jarvis_forge.storage import ForgeHome,ForgeError,write_json,read_json,digest


def package(home,name='lib',version='1.0',hash='old'):
    prefix=home.root/'pkg'/f'{name}-{version}'
    prefix.mkdir(parents=True,exist_ok=True)
    (prefix/'payload').write_text(hash)
    row={'name':name,'version':version,'hash':hash,'prefix':str(prefix),'external':False,'installed':True,'node':{}}
    write_json(prefix/'metadata.json',{'kind':'package',**row})
    return row


@pytest.fixture
def transaction(tmp_path):
    home=ForgeHome(tmp_path/'profile');home.initialize()
    row=package(home)
    write_json(home.root/'packages.json',{'selected':'old'})
    (home.root/'activate.sh').write_text('old activation\n')
    write_json(home.root/'spack/build-tool-reuse.json', {'selected': ['old-tool']})
    op=tmp_path/'operation';op.mkdir()
    checkpoint=op/'database-before.json';checkpoint.write_text('{"old":"native-record"}')
    calls=[]
    tx=Transaction(home,op,[row],lambda options:calls.append(options),checkpoint)
    return home,row,tx,calls


def test_same_version_rebuild_uses_original_prefix_and_restores_files_database_and_shell(transaction):
    home,old,tx,calls=transaction
    new={**old,'hash':'new','installed':False}
    tx.prepare({'packages':[new]})
    assert not Path(old['prefix']).exists()
    assert calls==[{'action':'detach','hashes':['old']}]
    prefix=Path(new['prefix']);prefix.mkdir();(prefix/'payload').write_text('new but failed')
    (home.root/'activate.sh').write_text('failed new activation')
    write_json(home.root/'packages.json',{'selected':'failed'})
    (home.root/'spack/build-tool-reuse.json').unlink()
    tx.rollback()
    assert (prefix/'payload').read_text()=='old'
    assert read_json(home.root/'packages.json')=={'selected':'old'}
    assert read_json(home.root/'spack/build-tool-reuse.json') == {'selected': ['old-tool']}
    assert (home.root/'activate.sh').read_text()=='old activation\n'
    assert calls[-1]['action']=='restore'
    assert not tx.backup.exists()


def test_version_upgrade_keeps_old_prefix_and_reclaims_no_old_data_early(transaction):
    home,old,tx,calls=transaction
    new={**old,'version':'2.0','hash':'new','prefix':str(home.root/'pkg/lib-2.0'),'installed':False}
    tx.prepare({'packages':[new]})
    assert not calls
    package(home,version='2.0',hash='new')
    tx.finish()
    assert (Path(old['prefix'])/'payload').read_text()=='old'
    assert (Path(new['prefix'])/'payload').read_text()=='new'


def test_failed_fresh_install_removes_partial_prefixes_and_restores_db(transaction):
    home,old,tx,calls=transaction
    new={**old,'name':'other','hash':'partial','prefix':str(home.root/'pkg/other-1.0'),'installed':False}
    tx.prepare({'packages':[new]});package(home,name='other',hash='partial')
    tx.rollback()
    assert not Path(new['prefix']).exists()
    assert Path(old['prefix']).is_dir()
    assert calls[-1]['action']=='restore'


def test_unregistered_collision_is_never_overwritten(transaction):
    home,old,tx,calls=transaction
    prefix=home.root/'pkg/unmanaged-1.0';prefix.mkdir();(prefix/'user-file').write_text('preserve')
    with pytest.raises(ForgeError,match='Unregistered'):
        tx.prepare({'packages':[{**old,'name':'unmanaged','prefix':str(prefix)}]})
    assert not calls and (prefix/'user-file').read_text()=='preserve'


def test_removal_moves_directories_then_really_reclaims_space(transaction):
    _,old,tx,calls=transaction
    tx.move([old]);assert not Path(old['prefix']).exists()
    backup=tx.backup;assert (backup/Path(old['prefix']).name/'payload').is_file()
    tx.finish();assert not backup.exists()
    assert calls==[{'action':'detach','hashes':['old']}]


def test_reverse_closure_and_app_references_block_package_removal(tmp_path):
    packages=[{'name':'lib','node':{}},{'name':'middle','node':{'dependencies':[{'name':'lib'}]}},
              {'name':'app','node':{'dependencies':[{'name':'middle'}]}}]
    assert reverse_dependents(packages,{'lib'})=={'lib','middle','app'}
    home=ForgeHome(tmp_path/'profile')
    write_json(home.root/'metadata.json',{'apps':[{'name':'cbs','enabled':True,'packages':[{'name':'lib','hash':'old'}]}]})
    with pytest.raises(ForgeError,match='cbs'):protect_apps(home,[{'name':'lib','hash':'old'}])
    protect_apps(home,[{'name':'unrelated','hash':'other'}])


def test_history_has_only_lock_manifest_and_result_and_checks_integrity(tmp_path):
    home=ForgeHome(tmp_path/'profile');candidate=tmp_path/'candidate';candidate.mkdir()
    (candidate/'spack.yaml').write_text('spack: {specs: [lib]}\n');(candidate/'spack.lock').write_text('{"lock":"old"}')
    path=history(home,'install',candidate,{'roots':['lib'],'status':'planned'})
    assert path.name=='0001-install'
    assert {p.name for p in path.iterdir()}=={'spack.yaml','spack.lock','result.json'}
    assert historical(home,'1')[0]==path
    (path/'spack.lock').write_text('modified')
    with pytest.raises(ForgeError,match='changed'):historical(home,'1')


def test_retry_replaces_partial_new_prefix_without_losing_original_backup(transaction):
    home,old,tx,calls=transaction
    partial={**old,'hash':'partial'}
    tx.prepare({'packages':[partial]})
    package(home,hash='partial')
    retried={**old,'hash':'retry'}
    tx.prepare({'packages':[retried],'inventory':[partial]})
    assert not Path(old['prefix']).exists()
    package(home,hash='retry')
    tx.rollback()
    assert (Path(old['prefix'])/'payload').read_text()=='old'
    assert [call['hashes'] for call in calls if call['action']=='detach']==[['old'],['partial']]


def test_retry_reuses_a_partial_new_install_registered_by_native_engine(transaction):
    home,old,tx,calls=transaction
    new={**old,'name':'other','prefix':str(home.root/'pkg/other-1.0'),'hash':'new'}
    tx.prepare({'packages':[new]});package(home,name='other',hash='new')
    tx.prepare({'packages':[new],'inventory':[old,new]})
    tx.finish()
    assert (home.root/'pkg/other-1.0/payload').read_text()=='new'
    assert not calls


def test_database_restore_failure_still_restores_shell_files_and_preserves_recovery_evidence(transaction):
    home,old,tx,calls=transaction
    tx.prepare({'packages':[{**old,'hash':'new'}]});package(home,hash='new')
    (home.root/'activate.sh').write_text('failed')
    def failed(options):raise ForgeError('database unavailable')
    tx.bridge=failed
    with pytest.raises(ForgeError,match='database restore failed') as caught:tx.rollback()
    assert (home.root/'activate.sh').read_text()=='old activation\n'
    assert (Path(old['prefix'])/'payload').read_text()=='old'
    assert tx.backup.exists() and str(tx.checkpoint) in caught.value.hint


def test_app_hash_protects_its_exact_version_not_an_unused_older_version(tmp_path):
    home=ForgeHome(tmp_path/'scope')
    write_json(home.root/'metadata.json',{'apps':[{'name':'app','packages':[{'name':'lib','hash':'new'}]}]})
    protect_apps(home,[{'name':'lib','hash':'old'}])
    with pytest.raises(ForgeError,match='app'):protect_apps(home,[{'name':'lib','hash':'new'}])
