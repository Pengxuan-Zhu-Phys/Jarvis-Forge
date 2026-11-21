from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolated_forge_default(tmp_path, monkeypatch):
    # No test may consult or mutate the developer's actual Forge selection.
    (tmp_path/'node-tmp').mkdir()
    monkeypatch.setenv('TMPDIR',str(tmp_path/'node-tmp'))
    monkeypatch.setenv('JFORGE_HOME', str(tmp_path / 'default-forge'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'user-config'))


@pytest.fixture
def linux_apt(monkeypatch):
    """Run Spack CLI fixtures independently of the real test host's apt backend."""
    from jarvis_forge import apt_profiles, unified_profiles

    monkeypatch.setattr(apt_profiles, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(unified_profiles, "host", lambda: {"platform":"linux", "system":"ubuntu", "version":"24.04", "machine":"x86_64", "libc":"glibc 2.39"})
