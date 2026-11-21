"""Only short owner-private socket locations leave shared persistent storage."""
from pathlib import Path
from types import SimpleNamespace
import os
import stat
import pytest
from jarvis_forge import socket_runtime
from jarvis_forge.storage import ForgeError,read_json


def test_shared_keyring_keeps_keys_and_config_out_of_runtime(tmp_path,monkeypatch):
    runtime=tmp_path/'ipc';runtime.mkdir(mode=0o700)
    monkeypatch.setattr(socket_runtime,'directory',lambda:runtime)
    keyring=tmp_path/'a-long-shared-forge-root'/'profiles/cbs/spack/gpg'
    keyring.mkdir(parents=True);(keyring/'pubring.kbx').write_bytes(b'keys')
    monkeypatch.setenv('SPACK_GNUPGHOME',str(keyring))
    class Gpg:pass
    socket_runtime.configure_spack(SimpleNamespace(Gpg=Gpg))
    assert Gpg().socket_dir==runtime
    assert (keyring/'pubring.kbx').read_bytes()==b'keys'
    assert (keyring/'gpg.conf').read_text().splitlines().count('no-autostart')==1
    socket_runtime.public_keyring(keyring)
    assert (keyring/'gpg.conf').read_text().splitlines().count('no-autostart')==1
    assert list(runtime.iterdir())==[]
    assert read_json(keyring/'socket.json')['agent_autostart'] is False


def test_runtime_uses_short_private_directory_and_rejects_symlinks(tmp_path,monkeypatch):
    # Use a genuinely short local temp path; Unix socket limits count bytes.
    import tempfile,shutil
    short=Path(tempfile.mkdtemp(prefix='jf-ipc-',dir='/tmp'));short.chmod(0o700)
    monkeypatch.setenv('XDG_RUNTIME_DIR',str(short))
    try:
        runtime=socket_runtime.directory()
        assert runtime.parent==short
        assert stat.S_IMODE(runtime.stat().st_mode)==0o700 and runtime.stat().st_uid==os.getuid()
        assert len(os.fsencode(runtime/'S.gpg-agent'))<100
        link=tmp_path/'keyring';link.symlink_to(short,target_is_directory=True)
        with pytest.raises(ForgeError,match='symbolic link'):socket_runtime.public_keyring(link)
    finally:shutil.rmtree(short)
