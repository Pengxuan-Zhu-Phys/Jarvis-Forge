from __future__ import annotations
import gzip,hashlib,io,json,subprocess,tarfile
from pathlib import Path
import pytest,yaml
from jarvis_forge import official,official_dependencies,packages
from jarvis_forge.catalog import Catalog,validate_recipe
from jarvis_forge.dependencies import resolve,matches
from jarvis_forge.downloads import unpack,unpack_component
from jarvis_forge.storage import ForgeHome,ForgeError,write_json
from jarvis_forge.presentation import render,error
from rich.console import Console
from test_apt import recipe
from test_official import archive


def test_components_mount_before_patches_and_are_recorded(tmp_path):
    home=ForgeHome(tmp_path/'forge');home.initialize()
    component=archive(tmp_path/'component.tar.gz',{'upstream-docs/index.html':'original\n'})
    path,data=recipe(home,tmp_path)
    packaging=archive(tmp_path/'patches.tar.gz',{'debian/patches/series':'fix.patch\n','debian/patches/fix.patch':'--- a/www/index.html\n+++ b/www/index.html\n@@ -1 +1 @@\n-original\n+patched\n'})
    data['schema_version']=2
    data['source']['components']=[{'name':'www','url':component.as_uri(),'sha256':hashlib.sha256(component.read_bytes()).hexdigest()}]
    data['source']['patch_archive']={'url':packaging.as_uri(),'sha256':hashlib.sha256(packaging.read_bytes()).hexdigest()}
    data['requires_tools']=['patch']
    data['install']=[['${python}','-c',"from pathlib import Path; import sys; (Path(sys.argv[1])/'page').write_text(Path('www/index.html').read_text())",'${prefix}']]
    data['check']=[['${python}','-c',"from pathlib import Path; import sys; assert (Path(sys.argv[1])/'page').read_text()=='patched\\n'",'${prefix}']]
    path.write_text(yaml.safe_dump(data))
    result=packages.install(home,['toy'],official=False)
    assert (Path(result['path'])/'toy/source-components.json').is_file()
    assert Path(result['packages'][0]['prefix'],'page').read_text()=='patched\n'


def test_component_flat_layout_and_replacement(tmp_path):
    component=archive(tmp_path/'doc.tar.gz',{'a':'A','b':'B'})
    source=tmp_path/'source';source.mkdir();target=source/'www';target.mkdir();(target/'obsolete').write_text('old')
    unpack_component(component,target,tmp_path/'stage')
    assert sorted(p.name for p in target.iterdir())==['a','b']


def test_source_archives_accept_sqlite_components_and_native():
    def row(names):return {'Package':'sqlite3','Checksums-Sha256':'\n'.join('a'*64+' 1 '+n for n in names)}
    assert official.source_archives(row(['sqlite3_1.0.orig.tar.xz','sqlite3_1.0.orig-www.tar.xz','sqlite3_1.0-1.debian.tar.xz']))==('sqlite3_1.0.orig.tar.xz',[('www','sqlite3_1.0.orig-www.tar.xz')])
    assert official.source_archives(row(['native_1.0.tar.xz']))==('native_1.0.tar.xz',[])
    with pytest.raises(ForgeError,match='component'):
        official.source_archives(row(['sqlite3_1.0.orig.tar.gz','different_1.0.orig-www.tar.gz']))


def test_safe_archive_links_and_unsafe_chains(tmp_path):
    file=tmp_path/'source.tar'
    with tarfile.open(file,'w') as tar:
        data=b'data';info=tarfile.TarInfo('pkg/file');info.size=len(data);tar.addfile(info,io.BytesIO(data))
        for name,target,kind in [('pkg/link','file',tarfile.SYMTYPE),('pkg/hard','pkg/file',tarfile.LNKTYPE)]:
            info=tarfile.TarInfo(name);info.linkname=target;info.type=kind;tar.addfile(info)
    unpack(file,tmp_path/'good')
    assert (tmp_path/'good/pkg/link').read_bytes()==b'data'
    assert (tmp_path/'good/pkg/hard').read_bytes()==b'data'
    with tarfile.open(file,'w') as tar:
        for name,target in [('a','b'),('b','../escape')]:
            info=tarfile.TarInfo(name);info.type=tarfile.SYMTYPE;info.linkname=target;tar.addfile(info)
    with pytest.raises(ForgeError,match='escape'):
        unpack(file,tmp_path/'bad')
    assert not (tmp_path/'escape').exists()


@pytest.mark.parametrize('version,expected',[('1:1.3.dfsg-3.1ubuntu2','1.3+dfsg'),('8c-2ubuntu11','8+c'),('1.9.4-1build1','1.9.4')])
def test_debian_repack_version_adapters(version,expected):
    assert official._upstream_version(version)==expected


def test_removed_catalog_does_not_break_reuse_of_installed_root(tmp_path):
    home=ForgeHome(tmp_path/'forge');home.initialize()
    path,_=recipe(home,tmp_path,'old')
    first=packages.install(home,['old'],official=False)
    path.unlink()
    recipe(home,tmp_path,'new')
    second=packages.install(home,['new'],official=False)
    assert next(p for p in second['packages'] if p['name']=='old')['action']=='reuse'
    assert Path(first['packages'][0]['prefix']).is_dir()


def test_unrelated_install_does_not_upgrade_existing_dependencies(tmp_path):
    home=ForgeHome(tmp_path/'forge');home.initialize()
    recipe(home,tmp_path,'b','1.0');recipe(home,tmp_path,'a',depends=['b>=1'])
    packages.install(home,['a'],official=False)
    recipe(home,tmp_path,'b','2.0');recipe(home,tmp_path,'c')
    result=packages.install(home,['c'],official=False)
    b=next(p for p in result['packages'] if p['name']=='b')
    assert b['version']=='1.0' and b['action']=='reuse'


def test_metadata_update_rolls_back_local_catalog_on_official_failure(tmp_path,monkeypatch):
    home=ForgeHome(tmp_path/'forge');home.initialize()
    original=b'{"schema_version":1,"packages":[],"sources":[]}\n'
    path=home.root/'cache/apt/catalog.json';path.parent.mkdir(parents=True);path.write_bytes(original)
    source=tmp_path/'recipes';source.mkdir()
    local,data=recipe(home,tmp_path,'local');(source/'local.yaml').write_text(yaml.safe_dump(data))
    packages.edit_sources(home,add=str(source))
    def fail(*args,**kwargs):raise ForgeError('repository failure',code=1)
    monkeypatch.setattr(official,'refresh_official',fail)
    with pytest.raises(ForgeError,match='repository failure'):
        packages.update(home)
    assert path.read_bytes()==original


def test_reuse_checks_modified_artifact_before_consumer_build(tmp_path):
    home=ForgeHome(tmp_path/'forge');home.initialize()
    path,data=recipe(home,tmp_path,'b');data.update(schema_version=2,artifacts=['bin/b']);path.write_text(yaml.safe_dump(data))
    first=packages.install(home,['b'],official=False)
    Path(first['packages'][0]['prefix'],'bin/b').write_text('damaged')
    recipe(home,tmp_path,'a',depends=['b'])
    with pytest.raises(ForgeError,match='artifact'):
        packages.install(home,['a'],official=False)
    assert not any(r['name']=='a' for r in home.packages())


def test_update_output_keeps_paths_and_pending_reasons_in_narrow_terminal():
    stream=io.StringIO();console=Console(file=stream,width=28,color_system=None)
    path='/very/long/user/directory/operations/apt-1234567890/report.json'
    render(console,'apt update',{'status':'passed','official_source':{'origin':'ubuntu','suite':'noble','component':'main'},'official_imports':[{'name':'libsqlite3-dev','debian_version':'3.45.1-1ubuntu2','import_status':'generated'}], 'pending_imports':[{'name':'sl','reason':'Needs a layout adapter','report':path}],'path':path})
    text=stream.getvalue()
    assert 'Official imports (1):' in text and '3.45.1-1ubuntu2' in text and 'Needs a layout adapter' in text and path in text
    stream.seek(0);stream.truncate(0);error(console,'jforge','Command failed: '+path,'Inspect '+path)
    assert path in stream.getvalue()


def test_source_pockets_components_by_hash_and_latest_candidate(tmp_path,monkeypatch):
    home=ForgeHome(tmp_path/'forge');home.initialize()
    monkeypatch.setattr(official_dependencies,'architecture',lambda:'amd64')
    blobs={};urls=[]
    suites=['noble','noble-updates','noble-security']
    for suite in suites:
        hashes=[]
        for component in ('main','universe'):
            version={'noble':'1.0-1','noble-updates':'2.0-1','noble-security':'1.5-1'}[suite]
            source_row=f'Package: sample\nBinary: libsample-dev\nVersion: {version}\n\n' if component=='universe' else ''
            binary=f'Package: libsample-dev\nSource: sample\nVersion: {version}\nArchitecture: amd64\n\n' if component=='universe' else ''
            for folder,base,content in [('source','Sources',source_row),('binary-amd64','Packages',binary)]:
                data=gzip.compress(content.encode(),mtime=0)
                sha=hashlib.sha256(data).hexdigest();path=f'{component}/{folder}/{base}.gz'
                hashes.append(f' {sha} {len(data)} {path}')
                blobs[f'https://archive.ubuntu.com/ubuntu/dists/{suite}/{component}/{folder}/by-hash/SHA256/{sha}']=data
        release='-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nOrigin: Ubuntu\nSuite: '+suite+'\nCodename: noble\nAcquire-By-Hash: yes\nSHA256:\n'+'\n'.join(hashes)+'\n\n-----BEGIN PGP SIGNATURE-----\nfixture\n-----END PGP SIGNATURE-----\n'
        blobs[f'https://archive.ubuntu.com/ubuntu/dists/{suite}/InRelease']=release.encode()
    def fetch(url,target,logs):
        urls.append(url)
        assert url in blobs,url
        target.write_bytes(blobs[url])
    monkeypatch.setattr(official,'fetch',fetch)
    result=official.import_repository(home,origin='ubuntu',suite='noble',component='main,universe')
    saved=official._cached_repository(home)
    assert len(saved['snapshot']['indexes'])==6
    assert saved['source']['pockets']==['updates','security']
    assert result['source_packages']==1
    assert official.cached_packages(home,'libsample-dev',exact=True)[0]['version']=='2.0-1'
    assert len([url for url in urls if '/by-hash/' in url])==len({record[key] for record in saved['snapshot']['indexes'] for key in ('index_sha256','binary_sha256')})
    # An unchanged index is not read and hashed again for every list command.
    monkeypatch.setattr(official,'digest',lambda path: (_ for _ in ()).throw(AssertionError('unexpected full index hash')))
    assert official._cached_repository(home)['source']==saved['source']


def test_source_queries_detect_changed_index_stat(tmp_path):
    home=ForgeHome(tmp_path/'forge');home.initialize()
    source={'origin':'debian','suite':'test','component':'main','keyring':None}
    write_json(home.root/'apt/official.json',{'schema_version':1,'source':source,'imports':[]})
    index=home.root/'cache/Sources.gz';index.write_bytes(gzip.compress(b''))
    official._save_repository(home,source,{'index':str(index),'index_sha256':hashlib.sha256(index.read_bytes()).hexdigest()})
    index.write_bytes(b'changed')
    with pytest.raises(ForgeError,match='modified'):
        official._cached_repository(home)


@pytest.mark.parametrize('spec',['pkg=1:2.0-1ubuntu2','pkg (>= 2.0~rc1-1)','pkg=2.0-1'])
def test_explicit_debian_versions_match_full_package_metadata(spec):
    from jarvis_forge.dependencies import native
    candidate={'name':'pkg','version':'2.0','provenance':{'kind':'official-apt-source','debian_version':'1:2.0-1ubuntu2','binary_versions':{'pkg':'1:2.0-1ubuntu2'}}}
    request=native(spec,root=True)
    assert matches(candidate,request)==(spec!='pkg=2.0-1')


def test_same_upstream_debian_revision_sorting_and_no_downgrade():
    from copy import deepcopy
    base={'name':'pkg','version':'1.0','source':{},'provenance':{'kind':'official-apt-source','origin':'debian','suite':'test','component':'main','source_package':'pkg'}}
    old=deepcopy(base);old['provenance']['debian_version']='1.0-1';old['provenance']['binary_versions']={'pkg':'1.0-1'}
    new=deepcopy(base);new['provenance']['debian_version']='1.0-2';new['provenance']['binary_versions']={'pkg':'1.0-2'}
    assert resolve([old,new],['pkg'])[0]['provenance']['debian_version']=='1.0-2'
    assert resolve([old,new],['pkg'],root_requirements=[{'name':'pkg','relation':'>=','version':'1.0-2'}])[0]['provenance']['debian_version']=='1.0-2'


def test_source_rebuild_does_not_inherit_precompiled_ubuntu_glibc_minimum():
    recipe={'name':'example','schema_version':1,'requires_tools':[],'check':[['true']]}
    row={'Package':'example','Version':'1.0-1'}
    binaries=[{'Package':'example','Source':'example','Version':'1.0-1','Depends':'libc6 (>= 2.38), libstdc++6 (>= 13)'}]
    result=official_dependencies.attach(recipe,row,'example',binaries,'amd64')
    external=result['provenance']['external_dependencies']
    assert {e['name'] for e in external}=={'libc6','libstdc++6'}
    assert all('relation' not in e and e['version_policy']=='rebuilt-native-abi' for e in external)
    assert all(e['binary_requirement']['relation']=='>=' for e in external)


def test_non_linux_negative_architecture_wildcards_are_valid():
    groups=official_dependencies.parse('libx [!kfreebsd-any], liby [linux-any], libz [hurd-any]', 'amd64')
    assert [g[0]['name'] for g in groups]==['libx','liby']


@pytest.mark.parametrize('filename',['sample#part.hpp','file?.cpp','percent%.cpp'])
def test_archive_layout_accepts_filesystem_names_not_url_names(tmp_path, filename):
    path=archive(tmp_path/'source.tar.gz',{'pkg/CMakeLists.txt':'project(sample)','pkg/'+filename:'source'})
    assert official._archive_layout(path)==('pkg','cmake')


def test_boost_modular_headers_adapter_installs_all_header_trees(tmp_path):
    import sys
    path=archive(tmp_path/'boost.tar.gz',{'boost/CMakeLists.txt':'project(Boost)', 'boost/libs/config/include/boost/version.hpp':'#define BOOST_VERSION 108300', 'boost/libs/core/include/boost/core/example.hpp':'// core'})
    source={'url':path.as_uri(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'subdir':'boost'}
    generated=official._automatic_recipe({'name':'libboost1.83-dev','package':'libboost1.83-dev','origin':'ubuntu'}, {'Package':'boost1.83','Version':'1.83.0-2ubuntu1'},source,'cmake',path)
    assert generated['provenance']['build_adapter']=='boost-headers'
    unpack(path,tmp_path/'unpacked');prefix=tmp_path/'prefix';prefix.mkdir()
    argv=generated['install'][0]
    result=subprocess.run([sys.executable,argv[1],argv[2],str(tmp_path/'unpacked/boost'),str(prefix)],text=True,capture_output=True)
    assert result.returncode==0,result.stderr
    assert (prefix/'include/boost/version.hpp').is_file()
    assert (prefix/'include/boost/core/example.hpp').is_file()
    assert official._automatic_recipe({'name':'libboost-filesystem1.83-dev','origin':'ubuntu'}, {'Package':'boost1.83','Version':'1.83.0-2ubuntu1'},source,'cmake',path) is None


def test_full_official_revision_controls_upgrade_candidates():
    from jarvis_forge.dependencies import newer
    def r(version):return {'version':'1.0','provenance':{'kind':'official-apt-source','debian_version':version}}
    assert newer(r('1.0-10'),r('1.0-2'))
    assert not newer(r('1.0-2'),r('1.0-10'))
    assert not newer(r('1.0-2'),r('1.0-2'))


@pytest.mark.parametrize('spec,expected',[('pkg>=2.0-2',True),('pkg>=2.0-20',False),('pkg (<< 2.0-20)',True)])
def test_debian_constraints_that_also_parse_as_pep_versions(spec,expected):
    from jarvis_forge.dependencies import native
    candidate={'name':'pkg','version':'2.0','provenance':{'kind':'official-apt-source','debian_version':'2.0-10'}}
    assert matches(candidate,native(spec,root=True))==expected


def test_attached_alias_keeps_highest_binary_rebuild_version():
    base={'Package':'sample','Version':'1.0-1'}
    binaries=[{'Package':'sample','Source':'sample (1.0-1)','Version':v} for v in ('1.0-1+b2','1.0-1+b1')]
    recipe={'name':'sample','schema_version':1,'check':[['true']]}
    result=official_dependencies.attach(recipe,base,'sample',binaries,'amd64')
    assert result['provenance']['binary_versions']['sample']=='1.0-1+b2'


def test_textual_debian_equality_still_requires_consumer_mapping():
    from jarvis_forge.dependencies import native
    candidate={'name':'pkg','version':'2.0','provenance':{'kind':'official-apt-source','debian_version':'2.0-1'}}
    assert matches(candidate,native('pkg=2.0-1',root=True))
    assert not matches(candidate,native('pkg=2.0-1'))
    candidate['provenance']['allow_debian_equality']=True
    assert matches(candidate,native('pkg=2.0-1'))


def test_reinstall_and_unrelated_upgrade_keep_full_installed_debian_revision(tmp_path):
    home=ForgeHome(tmp_path/'forge');home.initialize()
    path,data=recipe(home,tmp_path,'pkg')
    data['provenance']={'kind':'official-apt-source','origin':'debian','suite':'test','component':'main','source_package':'pkg','debian_version':'1.0-1','binary_versions':{'pkg':'1.0-1'}}
    path.write_text(yaml.safe_dump(data))
    packages.install(home,['pkg'],official=False)
    data['provenance']['debian_version']='1.0-2';data['provenance']['binary_versions']={'pkg':'1.0-2'};path.write_text(yaml.safe_dump(data))
    again=packages.install(home,['pkg'],mode='reinstall',official=False)
    assert next(r for r in home.packages() if r['install_id']==again['packages'][0]['install_id'])['provenance']['debian_version']=='1.0-1'
    recipe(home,tmp_path,'other');packages.install(home,['other'],official=False)
    plan=packages.plan_install(home,['other'],mode='upgrade')
    assert next(p for p in plan['packages'] if p['name']=='pkg')['recipe']['provenance']['debian_version']=='1.0-1'
    upgraded=packages.plan_install(home,['pkg>=1.0-2'],mode='upgrade')
    assert next(p for p in upgraded['packages'] if p['name']=='pkg')['recipe']['provenance']['debian_version']=='1.0-2'
