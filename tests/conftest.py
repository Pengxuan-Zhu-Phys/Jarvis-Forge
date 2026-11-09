from types import SimpleNamespace

import pytest


@pytest.fixture
def linux_apt(monkeypatch):
    """Run Spack CLI fixtures independently of the real test host's apt backend."""
    from jarvis_forge import apt_profiles

    monkeypatch.setattr(apt_profiles, "sys", SimpleNamespace(platform="linux"))
