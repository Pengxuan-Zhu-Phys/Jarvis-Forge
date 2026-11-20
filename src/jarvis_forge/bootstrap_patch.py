"""Prepare the native patch executable needed before Spack can build from source."""

import os
from pathlib import Path
import shlex
import shutil
from tempfile import TemporaryDirectory

from .downloads import unpack
from .package_operations import _inside, run_command, verified_archive
from .storage import ForgeError, write_text
from .unified_profiles import _host_id, host


VERSION = "2.8.1"
SOURCE = {
    "url": f"https://ftp.gnu.org/gnu/patch/patch-{VERSION}.tar.xz",
    "sha256": "3240c7959c5347c8ed347e3c4201c0337bf4240d5cf09d68889e58eeaba30e17",
}


def bin_dir(home, op, env, *, jobs, timeout, progress=None):
    """Return a managed bin directory when the host PATH lacks patch."""
    path = env.get("PATH", os.defpath)
    if shutil.which("patch", path=path):
        return None
    current = host()
    tools = home.root / "spack/bootstrap-tools" / _host_id(current)
    binary = _inside(home, tools / f"patch-{VERSION}/bin/patch")
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary.parent

    cc = shutil.which("cc", path=path) or shutil.which("gcc", path=path)
    make = shutil.which("make", path=path)
    if not cc or not make:
        raise ForgeError("Building GNU patch requires a host C compiler and make on PATH.")
    if progress:
        progress(f"Prepare GNU patch {VERSION} for source builds")
    tools.mkdir(parents=True, exist_ok=True)
    archive = verified_archive(home, SOURCE, op / "bootstrap-patch-download", progress=progress)
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
            ("configure", ["./configure", f"--prefix={binary.parent.parent}"]),
            ("build", [make, "-j", str(jobs)]),
            ("verify", [str(candidate), "--version"]),
        ):
            run_command(argv, source, context, op, f"bootstrap-patch-{label}", timeout, progress,
                        process_env=env, combined_logs=getattr(progress, "combined_logs", None))
        binary.parent.mkdir(parents=True, exist_ok=True)
        os.replace(candidate, binary)
    return binary.parent
