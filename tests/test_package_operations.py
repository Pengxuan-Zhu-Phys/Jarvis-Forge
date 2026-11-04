"""Public backend-independent I/O protections retained after removing source builds."""

import hashlib
import io
import sys
import tarfile

import pytest

from jarvis_forge.downloads import unpack
from jarvis_forge.package_operations import (
    _inside,
    environment_path,
    locked,
    operation,
    run_command,
    verified_archive,
)
from jarvis_forge.storage import ForgeError, ForgeHome, read_json


@pytest.mark.parametrize("kind", ["traversal", "symlink", "hardlink", "fifo"])
def test_archive_escape_and_special_files_are_rejected(tmp_path, kind):
    file = tmp_path / "unsafe.tar"
    with tarfile.open(file, "w") as archive:
        info = tarfile.TarInfo("../outside" if kind == "traversal" else "link")
        info.type = {
            "traversal": tarfile.REGTYPE,
            "symlink": tarfile.SYMTYPE,
            "hardlink": tarfile.LNKTYPE,
            "fifo": tarfile.FIFOTYPE,
        }[kind]
        info.linkname = "../outside"
        archive.addfile(info, io.BytesIO())
    with pytest.raises(ForgeError):
        unpack(file, tmp_path / "unpacked")
    assert not (tmp_path / "outside").exists()


def test_verified_archive_checksum_and_cache_revalidation(tmp_path):
    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    source = tmp_path / "source.tar"
    source.write_bytes(b"original")
    pin = {
        "url": source.as_uri(),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    file = verified_archive(home, pin, tmp_path / "download")
    file.write_bytes(b"corrupt cache")
    assert (
        verified_archive(home, pin, tmp_path / "download").read_bytes() == b"original"
    )
    with pytest.raises(ForgeError, match="SHA-256 mismatch"):
        verified_archive(home, {**pin, "sha256": "0" * 64}, tmp_path / "download")
    assert not list((home.root / "cache/downloads").glob("*.part"))


def test_package_lock_is_not_stolen(tmp_path):
    home = ForgeHome(tmp_path / "forge")
    with locked(home):
        with pytest.raises(ForgeError, match="Another package operation"):
            with locked(home):
                pass


def test_managed_symlinks_are_rejected(tmp_path):
    home = ForgeHome(tmp_path / "forge")
    home.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home.root / "spack").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ForgeError):
        _inside(home, home.root / "spack/engine")
    assert list(outside.iterdir()) == []


def test_failed_commands_stream_both_channels_and_archive_bytes(tmp_path, capfdbinary):
    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    env = tmp_path / "env.sh"
    env.write_text("export FIXTURE_CONTEXT=active\n")
    with pytest.raises(ForgeError, match="exit 7"):
        with operation(home, "fixture", {}) as (op, result):
            run_command(
                [
                    sys.executable,
                    "-c",
                    'import os,sys; print(os.environ["FIXTURE_CONTEXT"]); sys.stderr.buffer.write(b"bad\\xe4\\n"); sys.exit(7)',
                ],
                home.root,
                env,
                op,
                "failure",
                10,
                progress=lambda message: None,
            )
    output = capfdbinary.readouterr()
    assert output.out == b"" and b"active" in output.err and b"bad" in output.err
    command = read_json(op / "command-failure.json")
    assert command["exit_code"] == 7 and command["elapsed_seconds"] > 0
    assert (op / "command-failure.stderr.log").read_bytes() == b"bad\xe4\n"
    assert read_json(op / "result.json")["status"] == "failed"


def test_timeout_preserves_result(tmp_path):
    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    env = tmp_path / "env.sh"
    env.write_text("true\n")
    with pytest.raises(ForgeError, match="timed out"):
        with operation(home, "fixture", {}) as (op, result):
            run_command(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                home.root,
                env,
                op,
                "timeout",
                0.1,
            )
    assert read_json(op / "command-timeout.json")["status"] == "timeout"
    assert read_json(op / "result.json")["status"] == "failed"


def test_archived_shell_is_not_an_active_environment(tmp_path):
    from jarvis_forge.storage import write_json

    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    script = home.root / "old.sh"
    script.write_text("exit 77\n")
    write_json(home.root / "apt/state.json", {"activation": str(script)})
    with pytest.raises(ForgeError, match="Spack"):
        environment_path(home)
