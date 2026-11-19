"""Run the CBS cards against small tools that exercise the file contracts."""

import os
from pathlib import Path
import shutil
import sys
import tarfile

import pytest
import yaml
from typer.testing import CliRunner

from jarvis_forge import bench, bench_exec
from jarvis_forge.apt_profiles import active_home
from jarvis_forge.cli import app
from jarvis_forge.storage import ForgeError, ForgeHome, digest, read_json, write_json


def _executable(path, code):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env python3\n" + code)
    path.chmod(0o755)


def test_cbs_fixed_and_madgraph_benches(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    home = ForgeHome(tmp_path / "forge")
    home.initialize()
    monkeypatch.setenv("JFORGE_HOME", str(home.root))
    work = tmp_path / "validation"
    work.mkdir()
    monkeypatch.chdir(work)
    runner = CliRunner()

    def command(*args):
        result = runner.invoke(app, ["bench", *args])
        assert result.exit_code == 0, result.output
        return result.output

    binary = tmp_path / "cbs-install/CBS"
    _executable(binary, """
import gzip
import json
from pathlib import Path
import sys
config = json.loads(Path(sys.argv[-1]).read_text())
settings = config["settings"]
if "processes" in settings:
    processes = settings["processes"]
    assert [process["name"] for process in processes] == ["W", "Z"]
    assert all(process["cross_section_pb"] == 0.25 for process in processes)
else:
    processes = [{"files": [settings["event_file"]], "cross_section_pb": settings["cross_section_pb"]}]
count = 0
for process in processes:
    for events in process["files"]:
        with gzip.open(events, "rb") as stream:
            count += sum(line.startswith(b"E ") for line in stream)
xsec = sum(process["cross_section_pb"] for process in processes)
assert config["analyses"] == ["ATLAS_EXOT_2019_04"]
assert {"antikt_R04", "antikt_R10", "VRTrackJets"} == set(settings["jet_collections"])
result = {
    "run": {"n_events": count},
    "analyses": {"ATLAS_EXOT_2019_04": {"luminosity": 139.0, "signal_regions": {
        "SR": {"n_sig_scaled": 0.007 * 139.0 * 1000 * xsec, "n_sig_scaled_err": 1.0}
    }}}
}
Path("CBS_result.json").write_text(json.dumps(result))
""")
    profile = active_home(home)
    artifact = "cbs-test-installation"
    write_json(home.root / "builds/artifacts" / artifact / "manifest.json", {
        "artifact_id": artifact, "build_id": "cbs", "status": "ready", "output": "installation",
        "version": "2.7.0", "prefix": str(binary.parent), "apt": {"profile": {"path": str(profile.root)}},
        "files": {"CBS": {"sha256": digest(binary)}},
    })
    archives = {}
    pdf = "NNPDF23_lo_as_0130_qed"
    files_by_fetch = {"vlq": ["VLQ_v4_4FNS_UFO/restrict_3rdL.dat"],
                      "nnpdf": [f"{pdf}/{pdf}.info", f"{pdf}/{pdf}_0000.dat"]}
    stub = tmp_path / "stub"
    stub.touch()
    for key, files in files_by_fetch.items():
        archive = tmp_path / f"{key}.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            for member in files:
                stream.add(stub, arcname=member)
        archives[key] = archive
    monkeypatch.setattr(bench_exec, "verified_archive", lambda home, source, log: archives[log.name])
    command("init", "./")
    receipt = work / ".jforge/bench.json"
    workspace = read_json(receipt)["id"]
    command("fetch", "cbs-test-atlas")
    command("fetch", "cbs-test-atlas")
    assert read_json(receipt)["id"] == workspace
    assert "reported" in command("run", "smoke.yaml")
    assert "accepted" in command("status", "smoke.yaml")
    smoke = work / "bin/smoke.yaml"
    smoke_run = bench.card_status(home, smoke)
    run_id = smoke_run["run_id"]
    assert (work / "outputs/reports" / run_id / "report.json").is_file()
    assert (work / "logs" / run_id / "atlas-bh/analyze.0/command-1.log").is_file()
    assert "cbs-fixed-sample" in command("data", "list")
    stale = work / "outputs/steps/cbs-fixed-sample/smoke/atlas-bh/analyze/r001.partial"
    stale.mkdir()
    command("data", "prune", "--yes")
    assert not stale.exists()
    sample = work / "data/smoke-cases/atlas-bh/smoke.hepmc.gz"
    original = sample.read_bytes()
    sample.write_bytes(original + b"unexpected data")
    assert not bench.card_status(home, smoke)["accepted"]
    conflict = runner.invoke(app, ["bench", "fetch", "cbs-test-atlas"])
    assert isinstance(conflict.exception, ForgeError)
    assert "conflicts" in str(conflict.exception)
    assert sample.read_bytes() != original
    assert bench.run(home, smoke, no_build=True)["status"] == "error"
    sample.write_bytes(original)

    generator = tmp_path / "madgraph/bin/mg5_aMC"
    _executable(generator, r"""
import gzip
import os
from pathlib import Path
import re
import sys
card = Path(sys.argv[1])
if card.name == "proc_card.dat":
    proc = card.read_text()
    assert "define p = g u c d s u~ c~ d~ s~" in proc
    assert "bp > b h" in proc and "bp~ > b~ h" in proc
    assert "output PROC" in proc and Path("VLQ_v4_4FNS_UFO/restrict_3rdL.dat").is_file()
    Path("PROC").mkdir()
else:
    launch = card.read_text()
    assert "shower=Pythia8" in launch and ("set iseed 4242" in launch or "set iseed 4243" in launch)
    associated = "t~" if "set iseed 4242" in launch else "b~"
    assert f"bp {associated} j" in Path("proc_card.dat").read_text()
    assert "set lhaid 247000" in launch and "set pythia8_card 25:mayDecay on" in launch
    assert "25:onMode" not in launch and "25:onIf" not in launch
    events = int(re.search(r'set nevents (\d+)', launch)[1])
    pdf = Path(os.environ["LHAPDF_DATA_PATH"])
    assert (pdf / "pdfsets.index").read_text().startswith("247000 ")
    assert next(pdf.glob("*/*_0000.dat")).is_file()
    out = Path("PROC/Events/run_01")
    out.mkdir(parents=True)
    with gzip.open(out / "tag_1_pythia8_events.hepmc.gz", "wb") as stream:
        stream.write(b"HepMC::Asciiv3-START_EVENT_LISTING\n" + b"E 0\n" * events
                     + b"HepMC::Asciiv3-END_EVENT_LISTING\n")
    (out / "run_01_tag_1_banner.txt").write_text("#  Integrated weight (pb)  :  0.25\n")
""")
    physics = work / "bin/bench.yaml"
    config = yaml.safe_load(physics.read_text())
    config["settings"]["events"] = 3
    cache = home.root / "cache/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    for item in config["fetch"]:
        archive = archives[item["id"]]
        item.update(url=archive.as_uri(), sha256=digest(archive))
        shutil.copyfile(archive, cache / item["sha256"])
    physics.write_text(yaml.safe_dump(config, sort_keys=False))
    with pytest.raises(ForgeError, match="No compatible installed software for generator"):
        bench.plan(home, physics)
    row = {"name": "jforge-mg5amc", "version": "3.6.3", "prefix": str(generator.parents[1]),
           "spec": "jforge-mg5amc@3.6.3+pythia8", "install_id": "generator-id"}
    command("prefetch")
    # A native profile is normally selected by package setup on Linux.
    from jarvis_forge import apt_profiles
    monkeypatch.setattr(apt_profiles, "backend_name", lambda home: "spack")
    monkeypatch.setattr(apt_profiles, "active_home", lambda home: profile)
    write_json(profile.root / "apt/state.json", {"spack": {"packages": [row]}})
    assert "reported" in command("run")
    run = bench.card_status(home, physics)
    assert run["accepted"]
    run = bench.report(home, run["run_id"])["report"] | {"run_id": run["run_id"]}
    assert run["status"] == "reported"
    first = run["cases"][0]["steps"][-1]["items"][0]
    assert first["reason"] == "no baseline" and "expected" not in first
    assert bench.card_status(home, physics)["accepted"]
    run = bench.run(home, physics, no_build=True, baseline=run["run_id"])
    assert run["status"] == "reported"
    assert [step["action"] for step in run["cases"][0]["steps"]] == ["reuse", "reuse", "reuse", "run"]
    assert bench.card_status(home, physics)["accepted"]
    item = run["cases"][0]["steps"][-1]["items"][0]
    assert item["expected"] == first["actual"] and item["delta"] == 0 and item["tolerance"] is None
