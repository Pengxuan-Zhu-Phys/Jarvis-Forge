from spack_repo.builtin.build_systems.generic import Package
from spack.package import *


class FontInter(Package):
    """Inter sans-serif OpenType fonts in static weights and styles."""

    homepage = "https://github.com/rsms/inter"
    url = homepage + "/releases/download/v4.1/Inter-4.1.zip"

    version("4.1", sha256="9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e")
    license("OFL-1.1")
    depends_on("fontconfig", type="run")

    def install(self, spec, prefix):
        fonts = prefix.share.fonts.opentype.inter
        mkdirp(fonts, prefix.share.licenses)
        install("extras/otf/Inter-*.otf", fonts)
        install("LICENSE.txt", join_path(prefix.share.licenses, "OFL.txt"))
