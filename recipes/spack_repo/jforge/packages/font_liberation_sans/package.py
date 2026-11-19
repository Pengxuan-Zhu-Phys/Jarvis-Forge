from spack_repo.builtin.build_systems.generic import Package
from spack.package import *


class FontLiberationSans(Package):
    """Liberation Sans regular, bold and italic TrueType fonts."""

    homepage = "https://github.com/liberationfonts/liberation-fonts"
    url = homepage + "/files/7261482/liberation-fonts-ttf-2.1.5.tar.gz"

    version("2.1.5", sha256="7191c669bf38899f73a2094ed00f7b800553364f90e2637010a69c0e268f25d0")
    license("OFL-1.1")
    depends_on("fontconfig", type="run")

    def install(self, spec, prefix):
        fonts = prefix.share.fonts.truetype.liberation_sans
        mkdirp(fonts, prefix.share.licenses)
        install("LiberationSans-*.ttf", fonts)
        install("LICENSE", join_path(prefix.share.licenses, "OFL.txt"))
