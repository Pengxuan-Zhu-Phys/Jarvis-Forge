from spack_repo.builtin.build_systems.generic import Package
from spack.package import *


class FontNotoSansSc(Package):
    """Noto Sans SC OpenType fonts for Simplified Chinese."""

    homepage = "https://github.com/notofonts/noto-cjk"

    version(
        "2.004",
        url=homepage + "/releases/download/Sans2.004/18_NotoSansSC.zip",
        sha256="4d107c09ada479d3e48b6e78c83835773cbd9214bf6e12cdb7b60f8e068292ec",
    )
    license("OFL-1.1")
    depends_on("fontconfig", type="run")

    def install(self, spec, prefix):
        fonts = prefix.share.fonts.opentype.noto_sans_sc
        mkdirp(fonts, prefix.share.licenses)
        install("NotoSansSC-*.otf", fonts)
        install("LICENSE", join_path(prefix.share.licenses, "OFL.txt"))
