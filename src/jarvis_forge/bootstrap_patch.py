"""Prepare the native patch executable needed before Spack can build from source."""

from contextlib import nullcontext
import os
from pathlib import Path
import shlex
import shutil
from tempfile import TemporaryDirectory

from .downloads import unpack
from .package_operations import _inside, run_command, verified_archive
from .storage import ForgeError, write_text
from .unified_profiles import host
from .paths import object_path


VERSION = "2.8.1"
SOURCE = {
    "url": f"https://ftp.gnu.org/gnu/patch/patch-{VERSION}.tar.xz",
    "sha256": "3240c7959c5347c8ed347e3c4201c0337bf4240d5cf09d68889e58eeaba30e17",
}


def _activity(progress, description, *, download=False):
    if activity := getattr(progress, "activity", None):
        return activity(description, download=download)
    if progress:
        progress(description)
    return nullcontext()


def bin_dir(home, op, env, *, jobs, timeout, progress=None):
    """Return a managed bin directory when the host PATH lacks patch."""
    path = env.get("PATH", os.defpath)
    if shutil.which("patch", path=path):
        return None
    current = host()
    tools = object_path(_inside(home, home.root / "spack/bootstrap-tools"), "patch",
                        {"host": current, "version": VERSION, "source": SOURCE}, create=True)
    binary = _inside(home, tools / "bin/patch")
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary.parent

    cc = shutil.which("cc", path=path) or shutil.which("gcc", path=path)
    make = shutil.which("make", path=path)
    if not cc or not make:
        raise ForgeError("Building GNU patch requires a host C compiler and make on PATH.")
    tools.mkdir(parents=True, exist_ok=True)
    name = f"GNU patch {VERSION}"
    with _activity(progress, f"Fetch {name} source", download=True) as update:
        archive = verified_archive(home, SOURCE, op / "bootstrap-patch-download", progress=update)
    with TemporaryDirectory(prefix=".patch-", dir=tools) as directory:
        work = Path(directory)
        unpack(archive, work / "source")
        source = work / "source" / f"patch-{VERSION}"
        cflags = "-O2 -march=x86-64 -mtune=generic" if current["machine"] == "x86_64" else "-O2"
        context = op / "bootstrap-patch-context.sh"
        variables = {"CC": cc, "CFLAGS": cflags, "HOME": str(work), "TMPDIR": str(work),
                     "XDG_CACHE_HOME": str(work / "cache")}
        write_text(context, "".join(f"export {key}={shlex.quote(value)}\n" for key, value in variables.items()))
        candidate = source / "src/patch"
        for label, argv in (
            ("Configure", ["./configure", f"--prefix={binary.parent.parent}"]),
            ("Build", [make, "-j", str(jobs)]),
            ("Verify", [str(candidate), "--version"]),
        ):
            with _activity(progress, f"{label} {name}"):
                run_command(argv, source, context, op, f"bootstrap-patch-{label.lower()}", timeout, progress,
                            process_env=env, combined_logs=getattr(progress, "combined_logs", None))
        binary.parent.mkdir(parents=True, exist_ok=True)
        os.replace(candidate, binary)
    if progress:
        progress(f"{name} ready")
    return binary.parent
