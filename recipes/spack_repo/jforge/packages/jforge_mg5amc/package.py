"""MadGraph5_aMC@NLO 3.6.3 for jforge, with a fixed Pythia8 interface.

Launchpad no longer publishes the 3.6.3 tarball. The v3.6.3 git tag is that
release. The +pythia8 variant builds MG5aMC_PY8_interface during installation
and writes input/mg5_configuration.txt so a later run does not download tools
or open a browser.
"""

import os
import re

from spack_repo.builtin.build_systems.generic import Package

from spack.package import *


class JforgeMg5amc(Package):
    homepage = "https://launchpad.net/mg5amcnlo"
    url = "https://github.com/mg5amcnlo/mg5amcnlo/archive/refs/tags/v3.6.3.tar.gz"

    version(
        "3.6.3",
        sha256="2bb0d085a37b704ef956e8e8ed468bdcfef13e638c82ed3f1c4e968a090ebd98",
        url="https://github.com/mg5amcnlo/mg5amcnlo/archive/refs/tags/v3.6.3.tar.gz",
    )

    variant("pythia8", default=False,
            description="Build MG5aMC_PY8_interface against Spack Pythia8 and HepMC3")

    depends_on("python@3.10:", type="run")
    depends_on("bash", type="run")
    depends_on("perl@5.8:", type="run")
    depends_on("gmake", type="run")
    depends_on("lhapdf", type=("build", "run"))
    depends_on("pythia8", when="+pythia8", type=("build", "run"))
    depends_on("hepmc3", when="+pythia8", type=("build", "run"))
    depends_on("cxx", when="+pythia8", type="build")
    depends_on("fortran", when="+pythia8", type="build")

    def install(self, spec, prefix):
        self._write_configuration(self.stage.source_path, spec, interface=None)
        install_tree(self.stage.source_path, prefix)
        interface = None
        if spec.satisfies("+pythia8"):
            interface = self._install_interface(prefix, spec)
            self._write_configuration(prefix, spec, interface=interface)

    def _install_interface(self, prefix, spec):
        script = join_path(prefix, "install-pythia8-interface.mg5")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(
                "set auto_update 0\n"
                "set automatic_html_opening False\n"
                f"set pythia8_path {spec['pythia8'].prefix}\n"
                f"set hepmc_path {spec['hepmc3'].prefix}\n"
                "install mg5amc_py8_interface\n"
            )
        Executable(join_path(prefix, "bin", "mg5_aMC"))(script)
        interface = join_path(prefix, "HEPTools", "MG5aMC_PY8_interface")
        if not os.path.isdir(interface):
            raise InstallError(f"MG5aMC_PY8_interface was not installed at {interface}")
        return interface

    def _write_configuration(self, root, spec, interface):
        values = {
            "auto_update": "0",
            "automatic_html_opening": "False",
            "output_dependencies": "internal",
            "lhapdf_py3": join_path(spec["lhapdf"].prefix.bin, "lhapdf-config"),
        }
        if spec.satisfies("+pythia8"):
            values["pythia8_path"] = str(spec["pythia8"].prefix)
            values["hepmc_path"] = str(spec["hepmc3"].prefix)
            if interface:
                values["mg5amc_py8_interface_path"] = interface
        directory = join_path(root, "input")
        default = join_path(directory, ".mg5_configuration_default.txt")
        active = join_path(directory, "mg5_configuration.txt")
        text = _assign(open(default, encoding="utf-8").read(), values)
        with open(default, "w", encoding="utf-8") as handle:
            handle.write(text)
        with open(active, "w", encoding="utf-8") as handle:
            handle.write(text)


def _assign(text, values):
    """Uncomment or append the selected mg5_configuration keys."""
    pattern = re.compile(r"^#?\s*([A-Za-z0-9_]+)\s*=")
    seen = set()
    lines = []
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match and match.group(1) in values:
            lines.append(f"{match.group(1)} = {values[match.group(1)]}")
            seen.add(match.group(1))
        else:
            lines.append(line)
    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"
