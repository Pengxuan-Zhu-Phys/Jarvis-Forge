"""Verify generation and extract comparable results from the CBS JSON report."""

import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess

import yaml


_INTEGRATED_WEIGHT = re.compile(r"(?m)^\s*#\s*Integrated weight \(pb\)\s*:\s*(\S+)")


def cross_section(banner):
    match = _INTEGRATED_WEIGHT.search(banner.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"MadGraph did not report an integrated cross section in {banner}")
    value = float(match[1].lower().replace("d", "e"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"Invalid MadGraph cross section: {match[1]} pb")
    return value


def verify_hepmc(events, expected):
    opener = gzip.open if events.suffix == ".gz" else open
    with opener(events, "rb") as stream:
        start = end = False
        count = 0
        for line in stream:
            if b"START_EVENT_LISTING" in line:
                start = True
            elif b"END_EVENT_LISTING" in line:
                end = True
            elif line.startswith(b"E "):
                if not start or end:
                    raise ValueError(f"Event outside the HepMC listing: {events}")
                count += 1
    if not start or not end or count != expected or expected <= 0:
        raise ValueError(f"{events}: expected a complete HepMC file with {expected} events, found {count}")


def launch(binary, pdf_root):
    name = "NNPDF23_lo_as_0130_qed"
    grid = (pdf_root / name).resolve()
    if not (grid / f"{name}_0000.dat").is_file() or not (grid / f"{name}.info").is_file():
        raise ValueError(f"Missing the central LHAPDF member in {grid}. Run jforge bench prefetch physics.yaml.")
    pdfs = Path("pdf").resolve()
    pdfs.mkdir()
    (pdfs / name).symlink_to(grid, target_is_directory=True)
    (pdfs / "pdfsets.index").write_text(f"247000 {name} 1\n", encoding="utf-8")
    subprocess.run([str(binary), "launch.mg5"], env={**os.environ, "LHAPDF_DATA_PATH": str(pdfs)}, check=True)


def analyze(args):
    setup = yaml.safe_load(args.settings.read_text(encoding="utf-8"))
    settings = {**setup["settings"], "output": "./CBS_result.json", "seed": 1337}
    if args.sample:
        filename, count, checksum = args.sample
        events = Path(filename)
        count = int(count)
        if hashlib.sha256(events.read_bytes()).hexdigest() != checksum:
            raise ValueError("The fixed HepMC fixture does not match its pinned SHA-256.")
        verify_hepmc(events, count)
        xsec = 1.0
        settings.update(event_file=str(events.resolve()), cross_section_pb=xsec, cross_section_fractional_uncert=0.0)
    else:
        processes = []
        count = 0
        for name, filename, banner, expected in args.process:
            events = Path(filename)
            expected = int(expected)
            verify_hepmc(events, expected)
            processes.append({"name": name, "files": [str(events.resolve())],
                              "cross_section_pb": cross_section(Path(banner)), "cross_section_fractional_uncert": 0.0})
            count += expected
        if len({process["name"] for process in processes}) != len(processes):
            raise ValueError("Duplicate MadGraph process names.")
        settings["processes"] = processes
        xsec = math.fsum(process["cross_section_pb"] for process in processes)
    config = {"analyses": [args.analysis], "settings": settings}
    Path("run.yaml").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    subprocess.run([str(args.binary), "--report", "full", "run.yaml"], check=True)
    result = json.loads(Path("CBS_result.json").read_text(encoding="utf-8"))
    if result["run"]["n_events"] != count:
        raise ValueError(f"CBS did not analyze the expected {count} events.")
    analysis = result["analyses"][args.analysis]
    region = analysis["signal_regions"][args.region]
    luminosity = float(analysis["luminosity"])
    signal = float(region["n_sig_scaled"])
    error = float(region["n_sig_scaled_err"])
    if not all(math.isfinite(value) for value in (luminosity, signal, error)) or luminosity <= 0 or error < 0:
        raise ValueError(f"Invalid signal-region result for {args.analysis}/{args.region}")
    report = {"events": count, "analysis": args.analysis, "region": args.region,
              "cross_section_pb": xsec, "luminosity_fb_inverse": luminosity, "yield": signal, "yield_error": error}
    if args.process:
        report["processes"] = settings["processes"]
        normalization = xsec * 1000 * luminosity
        efficiency = signal / normalization
        if not 0 <= efficiency <= 1:
            raise ValueError(f"Unphysical efficiency: {efficiency}")
        report["metrics"] = {"efficiency": {"value": efficiency, "error": error / normalization}}
    Path("metrics.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{args.analysis}/{args.region}: {count} events analyzed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("launch")
    generate.add_argument("binary", type=Path)
    generate.add_argument("pdf_root", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("events", type=Path)
    verify.add_argument("banner", type=Path)
    verify.add_argument("count", type=int)
    cbs = commands.add_parser("analyze")
    cbs.add_argument("binary", type=Path)
    cbs.add_argument("analysis")
    cbs.add_argument("region")
    cbs.add_argument("settings", type=Path)
    source = cbs.add_mutually_exclusive_group(required=True)
    source.add_argument("--sample", nargs=3, metavar=("HEPMC", "COUNT", "SHA256"))
    source.add_argument("--process", action="append", nargs=4, metavar=("NAME", "HEPMC", "BANNER", "COUNT"))
    args = parser.parse_args()
    try:
        if args.command == "launch":
            launch(args.binary, args.pdf_root)
        elif args.command == "verify":
            xsec = cross_section(args.banner)
            verify_hepmc(args.events, args.count)
            print(f"Verified {args.count} MadGraph/Pythia8 events, cross section {xsec:.8g} pb.")
        else:
            analyze(args)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"CBS benchmark: {exc}\n")


if __name__ == "__main__":
    main()
