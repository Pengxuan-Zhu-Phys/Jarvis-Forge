"""Shared source ownership and safe extraction without compiling projects."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
from pathlib import Path
import tarfile

import pytest

from jarvis_forge import build_sources, downloads, build_apps, builds
from jarvis_forge.package_operations import verified_archive, archive_path
from jarvis_forge.storage import ForgeError, ForgeHome, digest


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / 'upstream.tar.gz'
    with tarfile.open(path, 'w:gz') as package:
        presets = json.dumps({'version': 3, 'configurePresets': [
            {'name': name, 'binaryDir': '${sourceDir}/build'} for name in ('cbs-gnu', 'gambit-gnu')
        ]}).encode()
        for name, content in [('upstream/revision.txt', b'fixed source'), ('upstream/sub/file', b'data'),
                              ('upstream/CMakePresets.json', presets)]:
            item = tarfile.TarInfo(name)
            item.size = len(content)
            package.addfile(item, io.BytesIO(content))
        item = tarfile.TarInfo('upstream/hard')
        item.type = tarfile.LNKTYPE
        item.linkname = 'upstream/revision.txt'
        package.addfile(item)
        item = tarfile.TarInfo('upstream/sub/link')
        item.type = tarfile.SYMTYPE
        item.linkname = '../revision.txt'
        package.addfile(item)
    return path


def spec(archive):
    return {'url': archive.as_uri(), 'sha256': digest(archive), 'subdir': 'upstream',
            'shared': {'name': 'demo', 'version': '1.0'}}


def test_concurrent_source_users_download_and_extract_once(tmp_path, archive):
    scope = ForgeHome(tmp_path / 'forge/profiles/ubuntu24')
    scope.initialize()
    requests = []
    def download(source):
        requests.append(source)
        return verified_archive(scope, source, tmp_path / 'download')
    with ThreadPoolExecutor(max_workers=2) as executor:
        paths = list(executor.map(lambda _: build_sources.acquire(scope, spec(archive), download), range(2)))
    assert paths[0] == paths[1] == scope.root / 'sources/demo-1.0'
    assert len(requests) == 1
    assert (paths[0] / 'revision.txt').read_text() == 'fixed source'
    assert (paths[0] / 'sub/link').read_text() == 'fixed source'
    assert (paths[0] / 'hard').read_text() == 'fixed source'
    assert not (paths[0] / 'upstream').exists()
    cached = archive_path(scope, requests[0])
    assert cached.name == 'demo-1.0.tar.gz' and cached.is_file()


@pytest.mark.parametrize('problem', ['identity', 'unregistered', 'symlink'])
def test_conflicting_shared_source_is_preserved(tmp_path, archive, problem):
    scope = ForgeHome(tmp_path / 'profile')
    source = spec(archive)
    target = build_sources.location(scope, source)
    if problem == 'identity':
        build_sources.acquire(scope, source, lambda _: archive)
        source = deepcopy(source)
        source['sha256'] = '0' * 64
    else:
        target.parent.mkdir(parents=True)
        target.mkdir()
        (target / 'user.txt').write_text('keep')
        if problem == 'symlink':
            retained = target.with_name('retained')
            target.rename(retained)
            target.symlink_to(retained)
    with pytest.raises(ForgeError):
        build_sources.acquire(scope, source, lambda _: pytest.fail('Must not download a conflicting source'))
    assert (target / ('revision.txt' if problem == 'identity' else 'user.txt')).is_file()


def test_failed_extraction_can_retry_without_publishing_partial_source(tmp_path, archive):
    scope = ForgeHome(tmp_path / 'profile')
    source = spec(archive)
    target = build_sources.location(scope, source)
    invalid = tmp_path / 'invalid.tar'
    invalid.write_text('invalid archive')
    with pytest.raises(ForgeError):
        build_sources.acquire(scope, source, lambda _: invalid)
    assert not target.exists()
    assert build_sources.acquire(scope, source, lambda _: archive) == target


def test_profiles_have_independent_shared_sources(tmp_path, archive):
    first, second = [ForgeHome(tmp_path / name) for name in ('ubuntu22', 'ubuntu24')]
    paths = [build_sources.acquire(scope, spec(archive), lambda _: archive) for scope in (first, second)]
    (paths[0] / 'revision.txt').write_text('first profile only')
    assert paths[0] != paths[1]
    assert (paths[1] / 'revision.txt').read_text() == 'fixed source'


def test_stripped_archive_rejects_a_link_outside_its_selected_root(tmp_path):
    archive = tmp_path / 'unsafe.tar'
    with tarfile.open(archive, 'w') as package:
        item = tarfile.TarInfo('root/link')
        item.type = tarfile.SYMTYPE
        item.linkname = '../escape'
        package.addfile(item)
    with pytest.raises(ForgeError, match='escapes'):
        downloads.unpack(archive, tmp_path / 'destination', strip_prefix='root')


def test_two_apps_use_one_managed_source_without_overrides_and_keep_it_on_remove(tmp_path, archive, monkeypatch):
    import yaml
    from profile_fixtures import project_profile
    base = ForgeHome(tmp_path / 'forge')
    scope = project_profile(base)
    requests, commands = [], []
    def download(home, source, log, name, **kwargs):
        requests.append(source)
        return archive
    def run(argv, cwd, script, logs, number, timeout, **kwargs):
        commands.append((argv, cwd))
        build = cwd / 'build'
        if '--build' not in argv:
            (build / 'CMakeCache.txt').write_text('PRESET:STRING=' + argv[argv.index('--preset') + 1] + '\n')
        else:
            program = 'CBS' if 'cbs-gnu' in argv else 'gambit'
            (cwd / program).write_text('simulated executable')
    monkeypatch.setattr(builds, '_source_archive', download)
    monkeypatch.setattr(builds, 'run_command', run)
    for app, program in [('cbs', 'CBS'), ('gambit', 'gambit')]:
        card = tmp_path / (app + '.yaml')
        card.write_text(yaml.safe_dump({'name': app, 'build': {'name': app, 'version': '1.0',
            'source': spec(archive), 'in_place': True, 'required_files': [program],
            'cmake': {'preset': app + '-gnu', 'build_preset': app + '-gnu',
                      'binary_dir': 'preset', 'lock': 'source', 'phases': ['configure', 'build']}}}))
        result = build_apps.install(base, str(card), confirm=lambda plan: None)
        assert result['status'] == 'passed', result
    shared = scope.root / 'sources/demo-1.0'
    assert {str(cwd) for argv, cwd in commands} == {str(shared)}
    assert len(requests) == 1
    assert {row['prefix'] for row in build_apps.installed(base)} == {str(shared)}
    build_apps.remove(base, ['cbs'], confirm=lambda plan: None)
    assert (shared / 'gambit').is_file() and (shared / 'build/CMakeCache.txt').is_file()
    build_apps.remove(base, ['gambit'], confirm=lambda plan: None)
    assert shared.is_dir() and not build_apps.installed(base)
