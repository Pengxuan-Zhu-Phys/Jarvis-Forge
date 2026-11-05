import os

from spack_repo.builtin.build_systems.generic import Package
from spack.package import *


class ForgeBuilder(Package):
    has_code = False
    version("1.0")
    depends_on("forge-lib", type="build")

    def install(self, spec, prefix):
        assert os.path.isfile(join_path(spec["forge-lib"].prefix.include, "forge.h"))
        mkdirp(prefix.share)
        touch(join_path(prefix.share, "built"))
