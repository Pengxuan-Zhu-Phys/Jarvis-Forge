"""Semantic stores refuse former padded or allocated layouts without migration."""
from pathlib import Path
import sys
import pytest
import yaml
from jarvis_forge import spack_backend as backend
from jarvis_forge.storage import ForgeHome,ForgeError,write_json


def configure(home):
    return backend._config(home,home.root/'repository',{'prefix':sys.prefix,'version':'3.13'})


def test_profile_and_platform_stores_use_name_version_and_real_distinct_roots(tmp_path):
    roots=[]
    for parent,name in [('platforms','ubuntu24'),('profiles','cbs'),('profiles','gambit')]:
        home=ForgeHome(tmp_path/parent/name);home.initialize()
        tree=configure(home)
        assert tree=={'root':str(home.root/'pkg'),'padded_length':False,'projections':{'all':'{name}-{version}'}}
        assert backend.store_root(home)==home.root/'pkg'
        roots.append(tree['root'])
    assert len(set(roots))==3


@pytest.mark.parametrize('tree',[{'padded_length':128}, {'padded_length':False,'projections':{'all':'{name}'}},
                                  {'padded_length':False,'projections':{'all':'{name}-{version}-{hash:12}'}}])
def test_previous_store_layout_is_refused_and_untouched(tmp_path,tree):
    home=ForgeHome(tmp_path/'forge');home.initialize()
    file=home.root/'spack/config/config.yaml';file.parent.mkdir()
    file.write_text(yaml.safe_dump({'config':{'install_tree':{'root':str(home.root/'pkg'),**tree}}}))
    original=file.read_bytes()
    with pytest.raises(ForgeError,match='Unsupported Spack store layout'):configure(home)
    assert file.read_bytes()==original


def test_missing_config_reads_current_backend_layout_without_moving_files(tmp_path):
    home=ForgeHome(tmp_path/'profile');home.initialize()
    tree={'root':str(home.root/'pkg'),'padded_length':False,'projections':{'all':'{name}-{version}'}}
    write_json(home.root/'spack/backend.json',{'schema_version':1,'backend':'spack','install_tree':tree})
    prefix=home.root/'pkg/zlib-1.3.1';prefix.mkdir(parents=True);(prefix/'payload').write_text('retain')
    assert configure(home)==tree and (prefix/'payload').read_text()=='retain'


def test_invalid_config_and_unregistered_legacy_store_are_preserved(tmp_path):
    home=ForgeHome(tmp_path/'profile');home.initialize()
    file=home.root/'spack/config/config.yaml';file.parent.mkdir();file.write_text('config: [broken]\n')
    with pytest.raises(ForgeError,match='existing Spack install layout'):configure(home)
    assert file.read_text()=='config: [broken]\n'
    file.unlink();store=home.root/'spack/store';store.mkdir();(store/'binary').write_text('old')
    with pytest.raises(ForgeError,match='Unsupported Spack store layout'):configure(home)
    assert (store/'binary').read_text()=='old'


def test_reading_store_does_not_create_any_directory(tmp_path):
    home=ForgeHome(tmp_path/'absent')
    assert backend.store_root(home)==home.root/'pkg'
    assert not home.root.exists()


def test_duplicate_native_builds_rejected_external_instances_do_not_compete(tmp_path):
    env=tmp_path/'candidate';file=env/'.jforge-config/config.yaml';file.parent.mkdir(parents=True)
    tree={'root':str(tmp_path/'pkg'),'padded_length':False,'projections':{'all':'{name}-{version}'}}
    file.write_text(yaml.safe_dump({'config':{'install_tree':tree}}))
    row={'name':'eigen','version':'5.0.1','hash':'first','external':False,'prefix':str(tmp_path/'pkg/eigen-5.0.1')}
    backend._validate_name_only_plan({'packages':[row,{**row,'external':True,'hash':'external'}]},env)
    with pytest.raises(ForgeError,match='multiple instances'):
        backend._validate_name_only_plan({'packages':[row,{**row,'hash':'second'}]},env)
