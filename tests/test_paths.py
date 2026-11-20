"""Physical names remain short while JSON owns object identity."""
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from jarvis_forge.paths import MAX_NAME, object_path, physical_name
from jarvis_forge.storage import ForgeError, read_json, write_json


def reserve(args):
    root, identity = args
    return physical_name(root, 'ubuntu-24.04-long-host-cpu-python', identity, create=True)


def test_read_only_lookup_does_not_initialize(tmp_path):
    parent = tmp_path / 'absent'
    assert object_path(parent, 'root', {'fingerprint': 'a' * 64}) is None
    assert not parent.exists()


def test_full_identity_short_physical_name(tmp_path):
    identity = {'preset': 'ubuntu-24.04', 'python': '/external/3.13.15', 'hash': 'a' * 64}
    one = object_path(tmp_path, 'ubuntu-24.04', identity, create=True)
    assert one == object_path(tmp_path, 'ignored-label', identity)
    two = object_path(tmp_path, 'ubuntu-24.04', {**identity, 'hash': 'b' * 64}, create=True)
    assert one != two
    assert all(len(p.name.encode()) <= MAX_NAME and not p.exists() for p in (one, two))
    values = read_json(tmp_path / 'metadata.json')['objects'].values()
    assert identity in [v['identity'] for v in values]


def test_multiprocess_reservations_share_identity(tmp_path):
    args = [(str(tmp_path), {'input': i % 3}) for i in range(18)]
    with ProcessPoolExecutor(max_workers=3) as pool:
        names = list(pool.map(reserve, args))
    assert len(set(names)) == 3
    assert len(read_json(tmp_path / 'metadata.json')['objects']) == 3
    assert all(len(name) <= MAX_NAME for name in names)


def test_collision_and_corrupt_index(tmp_path):
    (tmp_path / 'root-A1').mkdir()
    assert physical_name(tmp_path, 'root', {'id': 1}, create=True) == 'root-A2'
    write_json(tmp_path / 'metadata.json', {'schema_version': 99})
    with pytest.raises(ForgeError, match='Invalid short-path index'):
        physical_name(tmp_path, 'root', {'id': 1}, create=True)


def test_symlink_index_and_object_are_rejected(tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    parent = tmp_path / 'objects'
    name = physical_name(parent, 'root', {'id': 1}, create=True)
    (parent / name).symlink_to(other)
    with pytest.raises(ForgeError, match='symbolic link'):
        physical_name(parent, 'root', {'id': 1})
    (parent / 'metadata.json').unlink()
    (parent / 'metadata.json').symlink_to(other / 'index.json')
    with pytest.raises(ForgeError, match='symbolic link'):
        physical_name(parent, 'root', {'id': 2}, create=True)


def test_long_declared_names_and_stage_identity(tmp_path):
    from types import SimpleNamespace
    from jarvis_forge.paths import named_path, logical_name
    from jarvis_forge.spack_entry import short_stages

    logical = 'a-very-long-benchmark-step-with-meaningful-name'
    preview = named_path(tmp_path / 'steps', logical)
    assert not preview.exists() and not (tmp_path / 'steps').exists()
    actual = named_path(tmp_path / 'steps', logical, create=True)
    assert logical_name(actual) == logical and len(actual.name) <= MAX_NAME
    assert actual == named_path(tmp_path / 'steps', logical)

    class Stage:
        def __init__(self, url, name=None, path=None):
            self.name, self.path = name, path

    stage = SimpleNamespace(Stage=Stage, get_stage_root=lambda: str(tmp_path / 'stage'))
    short_stages(stage)
    spec = SimpleNamespace(name=logical, concrete=True, dag_hash=lambda: 'a' * 32)
    name = stage.compute_stage_name(spec)
    assert Stage('source', name=name).name == name
    resource = Stage('resource', name='resource-' + 'b' * 64)
    assert len(resource.name) <= MAX_NAME and resource.name != name
    assert Stage('resource', name='resource-' + 'b' * 64).name == resource.name
    external = Stage('source', name=logical, path='/provided/source')
    assert external.name == logical and external.path == '/provided/source'
    spec.dag_hash = lambda: 'c' * 32
    assert stage.compute_stage_name(spec) != name


def test_semantic_names_have_two_character_suffixes_without_padded_zeros(tmp_path):
    import re
    names = [physical_name(tmp_path, 'root', {'instance': i}, create=True) for i in range(240)]
    assert names[:3] == ['root-A1', 'root-A2', 'root-A3']
    assert len(set(names)) == 240
    assert all(re.fullmatch('root-[A-Z][1-9]', name) for name in names[:234])
    assert names[234:] == [f'root-A{i}' for i in range(10, 16)]
    assert all(not name.split('-')[-1][1:].startswith('0') for name in names)


def test_profile_uses_preset_name_and_two_character_suffix(tmp_path):
    from jarvis_forge.storage import ForgeHome
    from jarvis_forge.unified_profiles import _reserve_instance

    home = ForgeHome(tmp_path)
    profile = _reserve_instance(home, 'ubuntu-24.04', 'full-fingerprint', {'host': 'almalinux9', 'python': 'external'})
    assert profile.root.name == 'ubuntu24-U1'
    assert _reserve_instance(home, 'ubuntu-24.04', 'full-fingerprint', {'host': 'almalinux9', 'python': 'external'}) == profile
    other = _reserve_instance(home, 'ubuntu-24.04', 'another-fingerprint', {'host': 'another-host', 'python': 'external'})
    assert other.root.name == 'ubuntu24-U2'
