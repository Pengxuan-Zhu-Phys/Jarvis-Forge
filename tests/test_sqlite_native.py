from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from jarvis_forge import official, official_dependencies
from jarvis_forge.catalog import validate_recipe
from jarvis_forge.dependencies import resolve
from jarvis_forge.downloads import unpack
from jarvis_forge.packages import quilt_patches
from jarvis_forge.storage import ForgeError, ForgeHome
from test_apt import recipe as local_recipe
from test_official import archive


@pytest.mark.parametrize('escape',[False,True])
def test_non_utf8_patch_body_is_preserved_and_target_guard_still_applies(tmp_path,escape):
    base=tmp_path/'packaging/debian/patches';base.mkdir(parents=True)
    (base/'series').write_text('translations.patch\n')
    target=b'a/../../escape' if escape else b'a/message'
    patch=b'# Latin-1: \xe4\n--- '+target+b'\n+++ '+target.replace(b'a/',b'b/',1)+b'\n@@ -1 +1 @@\n-alt \xe4\n+neu \xe4\n'
    path=base/'translations.patch';path.write_bytes(patch)
    if escape:
        with pytest.raises(ForgeError,match='Unsafe patch target'):quilt_patches(tmp_path/'packaging')
        return
    found=quilt_patches(tmp_path/'packaging')
    assert found==[(path,1)] and path.read_bytes()==patch
    source=tmp_path/'source';source.mkdir();(source/'message').write_bytes(b'alt \xe4\n')
    run=subprocess.run(['patch','--batch','-p1','-i',str(path)],cwd=source,capture_output=True)
    assert run.returncode==0,run.stderr
    assert (source/'message').read_bytes()==b'neu \xe4\n'


def test_invalid_series_encoding_has_a_contextual_error(tmp_path):
    base=tmp_path/'debian/patches';base.mkdir(parents=True);(base/'series').write_bytes(b'bad\xe4.patch\n')
    with pytest.raises(ForgeError,match='Quilt series filenames must be UTF-8'):
        quilt_patches(tmp_path)


def test_nobiarch_is_only_active_for_explicit_native_builds():
    raw='gcc-multilib <!nobiarch>, libreal-dev, doctool <!nodoc>'
    assert [g[0]['name'] for g in official_dependencies.parse(raw,'amd64')]==['gcc-multilib','libreal-dev','doctool']
    assert [g[0]['name'] for g in official_dependencies.parse(raw,'amd64',active_profiles=['nobiarch'])]==['libreal-dev','doctool']


def native_recipes(tmp_path):
    data=json.loads(Path(__file__).with_name('fixtures').joinpath('sqlite-noble-relationships.json').read_text())
    requests={'sqlite3':'sqlite3','readline':'libreadline-dev','ncurses':'libncurses-dev','tcl8.6':'tcl8.6','zlib':'zlib1g-dev'}
    recipes=[]
    for row in data['sources']:
        project=row['Package'];name=requests[project]
        contents={'project/configure':'#!/bin/sh\n', 'project/Makefile.in':'all:','project/unix/configure':'#!/bin/sh\n', 'project/library/tzdata/UTC':'UTC'}
        tar=archive(tmp_path/(project+'.tar.gz'),contents)
        backend={'tcl8.6':'autotools:unix','zlib':'cmake'}.get(project,'autotools')
        spec={'url':tar.as_uri(),'sha256':hashlib.sha256(tar.read_bytes()).hexdigest(),'subdir':'project'}
        generated=official._automatic_recipe({'name':name,'package':name,'origin':'ubuntu'},row,spec,backend,tar)
        generated['provenance'].update(kind='official-apt-source',source_package=project,debian_version=row['Version'],origin='ubuntu',suite='noble',component='main')
        generated=official_dependencies.attach(generated,row,name,data['binaries'],'amd64')
        validate_recipe(generated,'native fixture')
        recipes.append(generated)
    return recipes


def test_actual_ubuntu_relationships_resolve_sqlite_closure_without_distribution_toolchains(tmp_path):
    recipes=native_recipes(tmp_path)
    ordered=resolve(recipes,['sqlite3'])
    assert [r['provenance']['source_package'] for r in ordered]==['ncurses','readline','zlib','tcl8.6','sqlite3']
    named={r['name']:r for r in ordered};sqlite=named['sqlite3']
    assert sqlite['depends']==['libreadline-dev','tcl8.6','zlib1g-dev']
    requests={a['name'] for r in recipes for g in r['debian_dependencies'] for a in g['alternatives']}
    assert not requests & {'lynx','chrpath','gcc-multilib','lib32ncurses-dev','tzdata','dpkg-dev','autoconf-dickey','texinfo'}
    assert any('libreadline-dev'==a['name'] for g in sqlite['debian_dependencies'] for a in g['alternatives'])
    assert '--enable-readline' in sqlite['build'][0] and '--disable-tcl' in sqlite['build'][0]
    assert any('TCLSH_CMD=${dep:tcl8.6}' in a for cmd in sqlite['build'] for a in cmd)
    assert 'readline-common' not in named['libreadline-dev']['provenance']['binary_versions']
    ncurses=named['libncurses-dev']
    assert 'include/curses.h' in ncurses['artifacts'] and 'include/ncursesw/curses.h' in ncurses['artifacts']
    assert any('library/tzdata' in d.get('reason','') or 'timezone' in d.get('reason','') for d in named['tcl8.6']['provenance']['dependency_decisions'])
    # A newly encountered real prerequisite must not disappear with known skips.
    row={'Package':'sqlite3','Version':'1.0-1','Build-Depends':'chrpath, libnew-required-dev'}
    result=official_dependencies.attach({'name':'sqlite3','check':[['true']],'provenance':{'build_adapter':'sqlite-native'}},row,'sqlite3',[{'Package':'sqlite3','Version':'1.0-1'}],'amd64')
    assert result['debian_dependencies']==[{'kind':'build','alternatives':[{'name':'libnew-required-dev'}]}]


def test_native_contract_rejects_tcl_source_without_timezone_payload(tmp_path):
    tar=archive(tmp_path/'empty.tar.gz',{'tcl/unix/configure':'#!/bin/sh'})
    with pytest.raises(ForgeError,match='bundled timezone'):
        official._automatic_recipe({'name':'tcl8.6','origin':'ubuntu'},{'Package':'tcl8.6','Version':'8.6.14'}, {'subdir':'tcl'}, 'autotools:unix',tar)


def test_discovery_accepts_nested_official_version_constraint(tmp_path):
    home=ForgeHome(tmp_path/'home');home.initialize()
    path,data=local_recipe(home,tmp_path,'pkg')
    data['provenance']={'kind':'official-apt-source','debian_version':'1.0-1'};path.write_text(yaml.safe_dump(data))
    op=home.root/'operations/probe';op.mkdir()
    assert official.ensure_recipes(home,['pkg>=1.0-1'],op)==[]
    assert json.loads((op/'dependency-discovery.json').read_text())['unresolved_candidates']==[]


def test_existing_schema_two_automatic_recipe_is_regenerated_after_adapter_revision(tmp_path,monkeypatch):
    home=ForgeHome(tmp_path/'home');home.initialize()
    path,data=local_recipe(home,tmp_path,'pkg');data.update(schema_version=2)
    data['provenance']={'kind':'official-apt-source','automatic_adapter':True,'debian_version':'1.0-1'}
    path.write_text(yaml.safe_dump(data))
    from jarvis_forge.storage import write_json
    write_json(home.root/'apt/official.json',{'schema_version':1,'source':{'origin':'ubuntu','suite':'noble','component':'main','keyring':None},'imports':[]})
    captured=[]
    def derive(home,profile,directory,session):
        captured.append(profile);new=dict(data);new['provenance']={**data['provenance'],'automatic_adapter_revision':official.AUTOMATIC_ADAPTER_REVISION}
        return {'recipe':new,'snapshot':{},'import_status':'generated'}
    monkeypatch.setattr(official,'_derive',derive);monkeypatch.setattr(official,'_store_import',lambda *args:None)
    op=home.root/'operations/probe';op.mkdir()
    official.ensure_recipes(home,['pkg'],op)
    assert len(captured)==1


def test_readline_install_command_skips_phony_docs_but_runs_install(tmp_path):
    (tmp_path/'Makefile').write_text('.PHONY: install install-doc\ninstall: install-doc\n\t@printf installed > marker\ninstall-doc:\n\t@exit 99\n')
    result=subprocess.run(['make','-o','install-doc','install'],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert (tmp_path/'marker').read_text()=='installed'


@pytest.mark.parametrize('project,requested',[('ncurses','libncurses5'),('sqlite3','libsqlite3-tcl'),('zlib','zlib1g-udeb')])
def test_native_contract_does_not_claim_incompatible_distribution_outputs(project,requested):
    from jarvis_forge.upstream_adapters import recipe
    with pytest.raises(ForgeError,match='dedicated adapter'):
        recipe({'name':requested,'package':requested},{'Package':project},{},'autotools',None,version='1.0')
