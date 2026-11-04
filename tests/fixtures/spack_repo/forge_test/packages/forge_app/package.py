import os
from spack_repo.builtin.build_systems.generic import Package
from spack.package import *


class ForgeApp(Package):
    has_code = False
    version("1.0")
    version("2.0")
    depends_on("forge-lib", type=("build", "link", "run"))

    def install(self, spec, prefix):
        assert os.path.isfile(join_path(spec["forge-lib"].prefix.include, "forge.h"))
        mkdirp(prefix.bin)
        path = join_path(prefix.bin, "forge-fixture")
        with open(path, "w") as f:
            f.write("#!/bin/sh\necho fixture-ok\n")
        set_executable(path)
