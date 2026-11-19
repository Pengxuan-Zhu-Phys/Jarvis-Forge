"""Exercise the published Bridson entry point and check its archived fixed points."""

import csv
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys


ENTRYPOINT = "bin/Example_Bridson_Operas.yaml"
# Jarvis check reads normalized sampler coordinates. The released card maps
# xx logarithmically onto [0.1, 5] and yy linearly onto [0, 5].
UNIT_POINTS = [(0.5, 0.0), (0.25, 0.5)]
JARVIS = (
    "from importlib.metadata import distribution; "
    "entry = next(ep for ep in distribution('Jarvis-HEP').entry_points "
    "if ep.group == 'console_scripts' and ep.name == 'Jarvis'); "
    "raise SystemExit(entry.load()())"
)


def main(project, step):
    work = step / "Eggbox"
    shutil.copytree(project, work)
    jarvis = [sys.executable, "-c", JARVIS]
    subprocess.run([*jarvis, "validate", ENTRYPOINT, "--strict"], cwd=work, check=True)
    points = work / "data" / "check_modules_points.csv"
    with points.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("xx", "yy"))
        writer.writerows(UNIT_POINTS)
    subprocess.run([*jarvis, "check", ENTRYPOINT, "--strict", "--skip-library-installation",
                    "--timeout", "90", "--console-level", "ERROR"], cwd=work, check=True)
    samples = work / "outputs" / "EggBox_Bridson_Operas" / "DATABASE" / "test" / "samples.csv"
    with samples.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    observed = {}
    for row in rows:
        index = int(row["sample_index"])
        if not 0 <= index < len(UNIT_POINTS) or index in observed or row["status"] != "Completed":
            raise SystemExit(f"Unexpected Eggbox sample {index}: {row['status']}")
        ux, uy = UNIT_POINTS[index]
        point = (0.1 * 50.0 ** ux, 5.0 * uy)
        if not all(math.isclose(float(row[key]), value, rel_tol=1e-10, abs_tol=1e-12)
                   for key, value in zip(("xx", "yy"), point)):
            raise SystemExit(f"Eggbox sample {index} has unexpected physical coordinates.")
        x, y = point
        target = (2.0 + math.sin(math.pi * x) * math.cos(math.pi * y)) ** 5
        z = float(row["z"])
        log_likelihood = -0.5 * ((target - 100.0) / 10.0) ** 2
        if not math.isclose(z, target, rel_tol=1e-10) or not all(
            math.isclose(float(row[key]), log_likelihood, rel_tol=1e-10)
            for key in ("LogL", "LogL_Z")
        ):
            raise SystemExit(f"Eggbox sample {point} has z={z}, LogL={row['LogL']}; "
                             f"expected z={target}, LogL={log_likelihood}")
        observed[index] = {"point": point, "z": z, "log_likelihood": float(row["LogL"])}
    if len(observed) != len(UNIT_POINTS):
        raise SystemExit(f"Expected {len(UNIT_POINTS)} fixed Eggbox points, got {len(observed)}.")
    report = json.dumps({"entrypoint": ENTRYPOINT, "points": [observed[index] for index in sorted(observed)]}, indent=2)
    (step / "eggbox-smoke.json").write_text(report + "\n", encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
