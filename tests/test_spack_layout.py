from pathlib import Path
import json
import sys

import pytest
import yaml

from jarvis_forge import spack_backend as backend
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeError, ForgeHome, read_json, write_json


def configure(home):
    return backend._config(home, home.root / 'repository', {'prefix':sys.prefix, 'version':'3.13.13'})


def test_fresh_profile_uses_real_flat_store_under_shared_home(tmp_path, capsys):
    base = ForgeHome(tmp_path / 'forge'); base.initialize()
    profile = ForgeHome(base.root / 'apt/profiles/spack-default'); profile.initialize()
    write_json(profile.root / 'config.json', {**read_json(profile.root / 'config.json'), 'apt_base_home':str(base.root)})
    tree = configure(profile)
    assert tree == {'root':str(base.root / 'apps/default'), 'padded_length':False,
                    'projections':{'all':'{name}'}}
    assert backend.store_root(profile) == base.root / 'apps/default'
    assert not (base.root / 'apps').is_symlink()
    assert not (base.root / 'apps').exists()  # Spack creates the real store when installing.
    write_json(profile.root / 'apt/spack.json', {'schema_version':1, 'backend':'spack', 'install_tree':tree})
    write_json(base.root / 'apt/selection.json', {'schema_version':1, 'id':'spack-default'})
    assert main(['--home',str(base.root),'paths','--json']) == 0
    assert json.loads(capsys.readouterr().out)['spack_store'] == tree['root']


def test_profiles_and_direct_store_have_distinct_real_roots(tmp_path):
    base = ForgeHome(tmp_path / 'forge'); base.initialize()
    assert configure(base)['root'] == str(base.root / 'apps/_direct')
    roots = []
    for name in ('default','root','build-123-Tiny.install'):
        home = ForgeHome(base.root / 'apt/profiles' / ('spack-' + name)); home.initialize()
        write_json(home.root / 'config.json', {**read_json(home.root / 'config.json'), 'apt_base_home':str(base.root)})
        roots.append(configure(home)['root'])
    assert len(set(roots)) == 3
    assert all(Path(root).parent == base.root / 'apps' for root in roots)


def test_existing_layout_is_kept_when_regenerating_config(tmp_path):
    home = ForgeHome(tmp_path / 'forge'); home.initialize()
    old = {'root':str(home.root / 'spack/store'), 'padded_length':128,
           'projections':{'all':'{architecture.platform}-{architecture.target}/{name}-{version}-{hash}'}}
    file = home.root / 'spack/config/config.yaml'; file.parent.mkdir(parents=True)
    file.write_text(yaml.safe_dump({'config':{'install_tree':old}}))
    assert configure(home) == old
    assert backend.store_root(home) == Path(old['root'])
    assert yaml.safe_load(file.read_text())['config']['install_tree'] == old


def test_missing_config_recovers_persisted_layout_without_moving_packages(tmp_path):
    home = ForgeHome(tmp_path / 'forge'); home.initialize()
    old = {'root':str(home.root / 'old-store'), 'padded_length':256}
    write_json(home.root / 'apt/spack.json', {'schema_version':1, 'backend':'spack', 'install_tree':old})
    (Path(old['root']) / 'existing-package').mkdir(parents=True)
    assert configure(home) == old
    assert (Path(old['root']) / 'existing-package').is_dir()


def test_legacy_settings_without_layout_keep_original_padding(tmp_path):
    home = ForgeHome(tmp_path / 'forge'); home.initialize()
    write_json(home.root / 'apt/spack.json', {'schema_version':1, 'backend':'spack'})
    tree = configure(home)
    assert tree['root'] == str(home.root / 'spack/store')
    assert tree['padded_length'] == max(128,len(tree['root'])+32)
    assert 'projections' not in tree


def test_invalid_existing_layout_is_not_replaced(tmp_path):
    home = ForgeHome(tmp_path / 'forge'); home.initialize()
    file = home.root / 'spack/config/config.yaml'; file.parent.mkdir(parents=True)
    file.write_text('config: [broken]\n')
    original = file.read_bytes()
    with pytest.raises(ForgeError, match='existing Spack install layout'):
        configure(home)
    assert file.read_bytes() == original


def test_existing_store_is_preserved_when_settings_are_missing(tmp_path):
    home = ForgeHome(tmp_path / 'forge'); home.initialize()
    store = home.root / 'spack/store'; store.mkdir(parents=True)
    (store / 'old-binary').write_bytes(b'original')
    assert configure(home)['root'] == str(store)
    assert (store / 'old-binary').read_bytes() == b'original'


def test_legacy_multi_instance_policy_is_not_restricted(tmp_path):
    env = tmp_path / 'environment'
    file = env / '.jforge-config/config.yaml'; file.parent.mkdir(parents=True)
    file.write_text(yaml.safe_dump({'config':{'install_tree':{'root':str(tmp_path / 'store'),
        'projections':{'all':'{name}-{version}-{hash:12}'}}}}))
    original = file.read_bytes()
    backend._validate_name_only_plan({'packages':[{'name':'eigen','hash':'first'},
                                                   {'name':'eigen','hash':'second'}]}, env)
    assert file.read_bytes() == original


def test_matching_native_metadata_allows_same_build_retry(tmp_path):
    env = tmp_path / 'environment'
    store = tmp_path / 'apps/test'
    prefix = store / 'eigen'; prefix.mkdir(parents=True)
    file = env / '.jforge-config/config.yaml'; file.parent.mkdir(parents=True)
    file.write_text(yaml.safe_dump({'config':{'install_tree':{'root':str(store), 'projections':{'all':'{name}'}}}}))
    package = {'name':'eigen', 'hash':'first', 'prefix':str(prefix), 'external':False,
               'installed':False, 'prefix_matches':True}
    backend._validate_name_only_plan({'packages':[package]}, env)
    package['prefix_matches'] = False
    with pytest.raises(ForgeError, match='another build'):
        backend._validate_name_only_plan({'packages':[package]}, env)


def test_external_instances_do_not_compete_for_store_directory(tmp_path):
    env = tmp_path / 'environment'
    store = tmp_path / 'apps/test'
    file = env / '.jforge-config/config.yaml'; file.parent.mkdir(parents=True)
    file.write_text(yaml.safe_dump({'config': {'install_tree': {
        'root': str(store), 'projections': {'all': '{name}'}}}}))
    backend._validate_name_only_plan({'packages': [
        {'name': 'tool', 'hash': 'external-one', 'external': True},
        {'name': 'tool', 'hash': 'external-two', 'external': True},
        {'name': 'tool', 'hash': 'managed', 'external': False,
         'prefix': str(store / 'tool'), 'installed': False},
    ]}, env)
