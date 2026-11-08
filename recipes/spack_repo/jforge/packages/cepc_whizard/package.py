import json
from pathlib import Path

from spack_repo.builtin.packages.whizard.package import Whizard

from spack.package import *


class CepcWhizard(Whizard):
    """WHIZARD with the CEPC parton-level build and upstream test suite."""

    version(
        "3.2.0",
        url="https://launchpad.net/whizard/3.2.x/3.2.0/+download/whizard-3.2.0.tar.gz",
        sha256="193efb6e2b5c5a141db16b0433885a195a987111f87c88edf4bb3ab5597c5c60",
        fetch_options={"timeout": 180},
    )
    depends_on("ocaml@4.14", type=("build", "run"), when="@3.2.0")
    depends_on("gmake", type=("build", "run"))

    force_autoreconf = False

    def configure_args(self):
        return [*super().configure_args(), "--disable-hepmc2", "--disable-pythia6", "--disable-python"]

    def setup_build_environment(self, env):
        super().setup_build_environment(env)
        env.set("FCFLAGS", "-O2 -ffree-line-length-none")
        env.set("FFLAGS", "-O2 -std=legacy -fallow-argument-mismatch")

    def setup_run_environment(self, env):
        env.prepend_path("LD_LIBRARY_PATH", self.prefix.lib)

    @run_after("build")
    def check_cepc_build(self):
        with working_dir(self.stage.source_path):
            make("check", parallel=False)

    @run_after("install")
    def record_cepc_build(self):
        path = Path(self.prefix.share) / "cepc-whizard-build.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "compiler": {"CC": self.compiler.cc, "CXX": self.compiler.cxx,
                         "FC": self.compiler.fc, "F77": self.compiler.fc},
            "configure_args": self.configure_args(),
            "make_check": "passed",
        }, indent=2) + "\n")
