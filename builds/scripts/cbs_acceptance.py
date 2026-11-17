"""Acceptance for an in-place CBS build.

The executable is CBS in the source root. This checks `CBS --list-analyses`.
When a 100-event HepMC sample is passed as the second argument, or is present
as builds/scripts/cbs_smoke.hepmc, it also runs one analysis and requires a
signal region in the result JSON.
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile


def has_signal_region(payload):
    analyses = payload.get("analyses") if isinstance(payload, dict) else None
    if not isinstance(analyses, dict):
        return False
    for analysis in analyses.values():
        regions = analysis.get("signal_regions") if isinstance(analysis, dict) else None
        if not isinstance(regions, dict):
            continue
        for region in regions.values():
            if isinstance(region, dict) and "n_sig_scaled" in region:
                return True
    return False


def sample_path(argv, script_file):
    if len(argv) > 2 and argv[2]:
        return Path(argv[2])
    bundled = Path(script_file).resolve().parent / "cbs_smoke.hepmc"
    return bundled if bundled.is_file() else None


def listed_analyses(stdout):
    names = []
    for line in stdout.splitlines():
        text = line.strip()
        if text and not text.startswith("Use "):
            names.append(text.split()[0])
    return names


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2:
        raise SystemExit("usage: cbs_acceptance.py PREFIX [EVENTS.hepmc]")
    prefix = Path(argv[1])
    executable = prefix / "CBS"
    if not executable.is_file():
        raise SystemExit(f"CBS executable not found: {executable}")
    listed = subprocess.run([str(executable), "--list-analyses"], check=False, capture_output=True, text=True)
    if listed.returncode != 0:
        raise SystemExit(listed.stderr or listed.stdout or "CBS --list-analyses failed")
    names = listed_analyses(listed.stdout)
    if not names:
        raise SystemExit("CBS --list-analyses returned no analyses")
    print(f"CBS analyses: {len(names)}", flush=True)
    sample = sample_path(argv, __file__)
    if sample is None:
        print("CBS event check skipped: no 100-event HepMC sample", flush=True)
        return 0
    if not sample.is_file():
        raise SystemExit(f"CBS event sample not found: {sample}")
    analysis = names[0]
    with tempfile.TemporaryDirectory(prefix="jforge-cbs-") as temporary:
        work = Path(temporary)
        card = work / "run.yaml"
        card.write_text(
            "analyses:\n"
            f"  - {analysis}\n"
            "settings:\n"
            f"  event_file: {sample}\n"
            "  cross_section_pb: 1.0\n"
            "  cross_section_fractional_uncert: 0.2\n"
            "  output: ./runs/CBS_result.json\n"
            "  report: full\n",
            encoding="utf-8",
        )
        completed = subprocess.run([str(executable), "--report", "full", str(card)], cwd=work, check=False)
        if completed.returncode != 0:
            raise SystemExit(f"CBS analysis failed: {analysis}")
        result = work / "runs" / "CBS_result.json"
        if not result.is_file():
            raise SystemExit(f"CBS did not write {result}")
        payload = json.loads(result.read_text(encoding="utf-8"))
        if not has_signal_region(payload):
            raise SystemExit("CBS result has no signal region yield")
    print(f"CBS event check passed: {analysis}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
