from __future__ import annotations

import copy
from pathlib import Path
import pytest

from jarvis_forge import official
from jarvis_forge.catalog import Catalog
from jarvis_forge.storage import ForgeHome,write_json,read_json
from test_apt import recipe


def history(home,specs):
    path=home.root/'operations/old-install/request.json';path.parent.mkdir()
    write_json(path,{'schema_version':1,'action':'install','request':{'specs':specs}})


def profile(name,automatic=True):
    return {'name':name,'package':name,'origin':'ubuntu','suite':'noble','component':'main','keyring':None,'automatic':automatic,'template':None}


def test_retirement_preserves_profiles_and_reactivates_on_explicit_request(tmp_path,monkeypatch):
    home=ForgeHome(tmp_path/'home');home.initialize()
    _,toy=recipe(home,tmp_path,'toy')
    history(home,['toy'])
    selected={'toy':{'recipe':toy,'origin':'official:ubuntu:noble'}}
    config={'schema_version':1,'imports':[profile('toy'),profile('chrpath'),profile('lib32ncurses-dev')]}
    updated,retired=official._retire_unused_automatic_imports(home,config,selected)
    assert [p['name'] for p in updated['imports']]==['toy']
    assert {p['name'] for p in retired}=={'chrpath','lib32ncurses-dev'}
    assert {p['name'] for p in updated['inactive_imports']}=={'chrpath','lib32ncurses-dev'}
    assert config['imports'][1]['name']=='chrpath' # Original metadata stays intact.
    updated['source']={'origin':'ubuntu','suite':'noble','component':'main','keyring':'/trusted/archive.gpg'}
    write_json(home.root/'apt/official.json',updated)
    # A dormant cached recipe must trigger refresh and retain the source keyring.
    _,data=recipe(home,tmp_path,'chrpath')
    captured=[]
    def derive(home,selected,directory,session):
        captured.append(selected)
        return {'recipe':data,'snapshot':{},'import_status':'generated'}
    monkeypatch.setattr(official,'_derive',derive)
    monkeypatch.setattr(official,'_save_repository',lambda *a:None)
    op=home.root/'operations/new-install';op.mkdir()
    official.ensure_recipes(home,['chrpath'],op)
    assert captured[0]['keyring']=='/trusted/archive.gpg'
    saved=read_json(home.root/'apt/official.json')
    assert 'chrpath' in [p['name'] for p in saved['imports']]
    assert 'chrpath' not in [p['name'] for p in saved['inactive_imports']]


@pytest.mark.parametrize('kind',['explicit-import','explicit-request','corrupt-history','unknown-roots'])
def test_unresolved_or_unknown_roots_cannot_be_retired(tmp_path,kind):
    home=ForgeHome(tmp_path/'home');home.initialize()
    _,toy=recipe(home,tmp_path,'toy')
    config={'schema_version':1,'imports':[profile('toy'),profile('chrpath',automatic=kind!='explicit-import')]}
    if kind=='explicit-request':history(home,['toy','chrpath'])
    elif kind=='explicit-import':history(home,['toy'])
    elif kind=='corrupt-history':
        history(home,['toy']);(home.root/'operations/old-install/request.json').write_text('bad JSON')
    before=copy.deepcopy(config)
    updated,retired=official._retire_unused_automatic_imports(home,config,{'toy':{'recipe':toy,'origin':'official:ubuntu:noble'}})
    assert updated==before and retired==[]


def test_rejected_automatic_recipe_is_not_left_as_a_candidate(tmp_path,monkeypatch):
    home=ForgeHome(tmp_path/'home');home.initialize()
    path,data=recipe(home,tmp_path,'lib32ncurses-dev');path.unlink()
    data['provenance']={'kind':'official-apt-source','automatic_adapter':True}
    write_json(home.root/'apt/official.json',{'schema_version':1,'imports':[profile('lib32ncurses-dev')]})
    write_json(home.root/'cache/apt/official.json',{'schema_version':1,'packages':[{'recipe':data,'origin':'official:ubuntu:noble'}],'pending':[]})
    def derive(*args):
        return {'recipe':None,'reason':'Not provided by native contract','source_record':{'Version':'6.4-1'},'snapshot':{'apt_signature_verified':False},'import_status':'needs_adapter'}
    monkeypatch.setattr(official,'_derive',derive)
    op=home.root/'operations/update';op.mkdir()
    result=official.refresh_official(home,op,refresh_repository=False)
    assert result['pending_imports'][0]['name']=='lib32ncurses-dev'
    assert not [p for p in Catalog(home).list() if p['name']=='lib32ncurses-dev']
