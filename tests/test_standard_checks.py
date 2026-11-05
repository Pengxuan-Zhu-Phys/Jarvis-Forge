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
