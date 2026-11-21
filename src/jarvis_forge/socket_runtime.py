"""Only IPC sockets may use a short, owner-private node-local directory."""

import os
from pathlib import Path
import stat

from .storage import ForgeError, write_json, write_text


def directory():
    candidates = []
    if os.environ.get("XDG_RUNTIME_DIR"):
        candidates.append(Path(os.environ["XDG_RUNTIME_DIR"]))
    candidates.append(Path("/tmp") / f"jforge-{os.getuid()}")
    for base in candidates:
        if not base.is_absolute() or base.is_symlink():
            continue
        try:
            base.mkdir(mode=0o700, exist_ok=True)
            info = base.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                continue
            target = base / f"gpg-{os.getpid()}"
            if len(os.fsencode(target / "S.gpg-agent")) >= 100:
                continue
            target.mkdir(mode=0o700, exist_ok=True)
            info = target.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                continue
            return target
        except OSError:
            continue
    raise ForgeError("No short private socket runtime directory is available.")


def public_keyring(path):
    """Cache trust uses public keys only and never starts a private-key agent.

    Keys, trust DB and configuration remain in the managed shared keyring.
    Suppressing autostart prevents GnuPG 2.2/2.3 from falling back to a long
    GNUPGHOME socket when /run/user is unavailable on an HPC node.
    """
    path = Path(path)
    if path.is_symlink():
        raise ForgeError(f"Managed GnuPG keyring must not be a symbolic link: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = path / "gpg.conf"
    text = config.read_text() if config.is_file() else ""
    if "no-autostart" not in text.splitlines():
        write_text(config, text + "\nno-autostart\n")
    runtime = directory()
    write_json(
        path / "socket.json",
        {
            "schema_version": 1,
            "socket_directory": str(runtime),
            "keyring": str(path),
            "agent_autostart": False,
        },
    )
    return runtime


def configure_spack(gpg):
    path = os.environ.get("SPACK_GNUPGHOME")
    if path:
        runtimes = {}

        def sockets(instance):
            key = str(getattr(instance, "home", path))
            if key not in runtimes:
                runtimes[key] = public_keyring(key)
            return runtimes[key]

        # Metadata-only commands do not touch a keyring or runtime directory.
        gpg.Gpg.socket_dir = property(sockets)
