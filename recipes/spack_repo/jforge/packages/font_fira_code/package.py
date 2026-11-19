from pathlib import Path

from spack_repo.builtin.build_systems.generic import Package
from spack.package import *


class FontFiraCode(Package):
    """Fira Code monospace fonts with programming ligatures."""

    homepage = "https://github.com/tonsky/FiraCode"
    url = "https://github.com/tonsky/FiraCode/releases/download/6.2/Fira_Code_v6.2.zip"

    version("6.2", sha256="0949915ba8eb24d89fd93d10a7ff623f42830d7c5ffc3ecbf960e4ecad3e3e79")
    license("OFL-1.1")
    depends_on("fontconfig", type="run")

    def install(self, spec, prefix):
        fonts = prefix.share.fonts.truetype.firacode
        mkdirp(fonts, prefix.share.licenses)
        install_tree("ttf", fonts)
        install(str(Path(__file__).with_name("OFL.txt")), join_path(prefix.share.licenses, "OFL.txt"))
