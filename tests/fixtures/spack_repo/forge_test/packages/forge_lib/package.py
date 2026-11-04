from spack_repo.builtin.build_systems.generic import Package
from spack.package import *


class ForgeLib(Package):
    has_code = False
    version("1.0")

    def install(self, spec, prefix):
        mkdirp(prefix.include)
        with open(join_path(prefix.include, "forge.h"), "w") as f:
            f.write("/* fixture */")

    def setup_run_environment(self, env):
        env.set("FORGE_FIXTURE", str(self.prefix))
