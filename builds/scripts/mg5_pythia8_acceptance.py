"""Acceptance for the mg5-pythia8 build.

Checks the installed MadGraph configuration, then generates 100 showered
`p p > t t~` events in the build work directory. The first compilation of the
process happens here, on a machine that can run MadGraph.
"""

from pathlib import Path
import re
import subprocess
import sys


_ASSIGNMENT = re.compile(r"^#?\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$")
REQUIRED = (
    "auto_update",
    "automatic_html_opening",
    "lhapdf_py3",
    "pythia8_path",
    "hepmc_path",
    "mg5amc_py8_interface_path",
)


def configuration(text):
    values = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line.strip())
        if match and not line.strip().startswith("#"):
            values[match.group(1)] = match.group(2)
    return values


def configuration_errors(text):
    values = configuration(text)
    errors = [f"missing {key}" for key in REQUIRED if key not in values or not values[key]]
    if values.get("auto_update") not in (None, "0"):
        errors.append(f"auto_update is {values.get('auto_update')}, expected 0")
    if values.get("automatic_html_opening") not in (None, "False"):
        errors.append("automatic_html_opening must be False")
    return errors


def write_smoke(work):
    work = Path(work)
    proc = work / "proc_card.dat"
    launch = work / "launch.mg5"
    proc.write_text("import model sm\ngenerate p p > t t~\noutput PROC\n", encoding="utf-8")
    launch.write_text(
        "launch PROC -n run_01\n"
        "shower=Pythia8\n"
        "done\n"
        "set nevents 100\n"
        "set iseed 1\n"
        "done\n",
        encoding="utf-8",
    )
    return proc, launch


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) != 3:
        raise SystemExit("usage: mg5_pythia8_acceptance.py MG5_PREFIX WORK")
    prefix, work = Path(argv[1]), Path(argv[2])
    binary = prefix / "bin" / "mg5_aMC"
    config_path = prefix / "input" / "mg5_configuration.txt"
    if not binary.is_file():
        raise SystemExit(f"mg5_aMC not found: {binary}")
    if not config_path.is_file():
        raise SystemExit(f"MadGraph configuration not found: {config_path}")
    errors = configuration_errors(config_path.read_text(encoding="utf-8"))
    if errors:
        raise SystemExit("MadGraph configuration: " + "; ".join(errors))
    work.mkdir(parents=True, exist_ok=True)
    proc, launch = write_smoke(work)
    completed = subprocess.run([str(binary), str(proc)], cwd=work, check=False)
    if completed.returncode != 0:
        raise SystemExit("MadGraph process generation failed")
    completed = subprocess.run([str(binary), str(launch.name)], cwd=work, check=False)
    if completed.returncode != 0:
        raise SystemExit("MadGraph Pythia8 shower failed")
    events = list((work / "PROC").glob("Events/run_01/*pythia8_events.hepmc*"))
    if not events:
        raise SystemExit("MadGraph did not write showered HepMC events")
    print(f"MG5+Pythia8 events: {events[0]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
