from spack_repo.builtin.build_systems.generic import Package

from spack.package import *


class CepcMadgraph(Package):
    """MadGraph5_aMC@NLO for the CEPC parton-level baseline."""

    homepage = "https://launchpad.net/mg5amcnlo"
    url = "https://launchpad.net/mg5amcnlo/3.0/3.8.x/+download/MG5_aMC_v3.8.1.tar.gz"

    version("3.8.1", sha256="281b9055af99f45217dc99378c520215f3129e65a9ab2fde6636e274aa6cd692",
            fetch_options={"timeout": 180})
    depends_on("python@3.10:", type="run")
    depends_on("bash", type="run")
    depends_on("perl@5.8:", type="run")
    depends_on("gmake", type="run")

    def install(self, spec, prefix):
        install_tree(self.stage.source_path, prefix)
