from spack_repo.builtin.build_systems.generic import Package
from spack.package import *


class Neofetch(Package):
    """Display system information in the terminal."""

    homepage = "https://github.com/dylanaraps/neofetch"
    url = "https://github.com/dylanaraps/neofetch/archive/refs/tags/7.1.0.tar.gz"

    version(
        "7.1.0",
        sha256="58a95e6b714e41efc804eca389a223309169b2def35e57fa934482a6b47c27e7",
    )
    depends_on("bash@3.2:", type="run")

    def install(self, spec, prefix):
        mkdirp(prefix.bin)
        install("neofetch", prefix.bin)
        set_executable(join_path(prefix.bin, "neofetch"))
        mkdirp(prefix.share.man.man1)
        install("neofetch.1", prefix.share.man.man1)
