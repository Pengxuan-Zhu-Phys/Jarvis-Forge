import json
from pathlib import Path
import subprocess
import sys
import venv

import pytest

from jarvis_forge import standard_checks
from jarvis_forge.storage import ForgeError


def run_check(spec, python, directory, **kwargs):
    def invoke(argv):
        result = subprocess.run(argv, capture_output=True, text=True, **kwargs)
        assert result.returncode == 0, result.stderr
    return standard_checks.execute(spec, str(python), invoke, directory)


def test_worker_runs_in_target_python_without_forge_or_packaging(tmp_path):
    target = tmp_path / 'target'
    venv.create(target, with_pip=False)
    python = target / 'bin/python'
    missing = subprocess.run([str(python), '-c', 'import jarvis_forge'], capture_output=True)
    assert missing.returncode != 0
    report = run_check({'python': {'version': '>=3.10', 'development': True, 'modules': ['json']}}, python, tmp_path)
    assert report['status'] == 'passed'
    assert report['python']['path'] == str(python)
    assert report['python']['development']
    assert report['modules']['json']['status'] == 'found'


def test_module_versions_and_missing_module_are_reported(tmp_path):
    (tmp_path / 'forge_probe_fixture.py').write_text('__version__="1.2.3"\ndef get_version(): return "2.4.0"\n')
    import os
    env = {**os.environ, 'PYTHONPATH': str(tmp_path)}
    report = run_check({'python': {'modules': {'forge_probe_fixture': {'version': '>=2', 'version_attribute': 'get_version'}}}}, sys.executable, tmp_path, env=env)
    assert report['modules']['forge_probe_fixture']['version'] == '2.4.0'
    with pytest.raises(ForgeError, match='Dependency checks failed'):
        run_check({'python': {'version': '>=99', 'modules': {'forge_probe_fixture': '>=3', 'forge_nonexistent_module_3912': None}}}, sys.executable, tmp_path, env=env)
    saved = json.loads((tmp_path / 'checks.json').read_text())
    assert saved['status'] == 'failed'
    assert len(saved['failures']) == 3
    assert 'ModuleNotFoundError' in saved['modules']['forge_nonexistent_module_3912']['error']


def test_tools_follow_selected_compiler_and_versions(tmp_path, monkeypatch):
    compiler = tmp_path / 'my compiler'
    compiler.write_text('#!/bin/sh\necho "Forge CC 12.3.0"\n')
    compiler.chmod(0o755)
    monkeypatch.setenv('CC', '"' + str(compiler) + '"')
    report = run_check({'tools': {'cc': '>=12,<13'}}, sys.executable, tmp_path)
    assert report['tools']['cc']['path'] == str(compiler)
    with pytest.raises(ForgeError, match='version 12.3.0 does not satisfy'):
        run_check({'tools': {'cc': '>=13'}}, sys.executable, tmp_path)


@pytest.mark.parametrize('spec', [
    {'python': {'modules': ['json', 'json']}},
    {'python': {'modules': ['bad-name']}},
    {'python': {'version': '3.13'}},
    {'python': {'development': 'yes'}},
    {'python': {'modules': {'numpy': {'unknown': 'value'}}}},
    {'tools': {'cc': 'latest'}},
])
def test_invalid_declarations_fail_before_execution(spec):
    with pytest.raises(ForgeError):
        standard_checks.validate(spec)


def test_python_module_cmake_directory_is_checked_in_the_target_python(tmp_path):
    import os
    directory = tmp_path / 'native-config'
    directory.mkdir()
    config = directory / 'FixtureNativeConfig.cmake'
    config.write_text('# fixture\n')
    module = tmp_path / 'native_fixture.py'
    module.write_text('__version__="1.0"\ndef get_cmake_dir(): return ' + repr(str(directory)) + '\n')
    env = {**os.environ, 'PYTHONPATH': str(tmp_path)}
    spec = {'python': {'modules': {'native_fixture': {'version': '>=1', 'cmake': 'FixtureNative'}}}}
    report = run_check(spec, sys.executable, tmp_path, env=env)
    assert report['modules']['native_fixture']['cmake_dir'] == str(directory)
    assert report['modules']['native_fixture']['cmake_package'] == 'FixtureNative'
    config.unlink()
    with pytest.raises(ForgeError, match='CMake package FixtureNative is missing'):
        run_check(spec, sys.executable, tmp_path, env=env)


@pytest.mark.parametrize('shared', [False, True])
def test_python_shared_library_requirement_rejects_a_static_library(tmp_path, monkeypatch, shared):
    include = tmp_path / 'include'
    include.mkdir()
    (include / 'Python.h').write_text('')
    library = 'libpython.so' if shared else 'libpython.a'
    (tmp_path / library).write_text('fixture')
    monkeypatch.setattr(standard_checks.sysconfig, 'get_path', lambda _: str(include))
    values = {'LIBDIR': str(tmp_path), 'LDLIBRARY': library, 'LIBRARY': 'libpython.a'}
    monkeypatch.setattr(standard_checks.sysconfig, 'get_config_var', values.get)
    spec = {'python': {'development': True, 'shared': True}}
    report = standard_checks.probe(spec)
    assert report['python']['development']
    assert bool(standard_checks.evaluate(report, spec)) is (not shared)
