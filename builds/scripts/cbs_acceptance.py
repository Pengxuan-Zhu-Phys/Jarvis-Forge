"""Run the built CBS executable and check that it can read its analysis list."""

from pathlib import Path
import re
import subprocess
import sys


def listed_count(output):
    match = re.search(r"(?m)^CBS analyses: ([1-9][0-9]*)\b", output)
    return int(match[1]) if match else 0


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: cbs_acceptance.py PREFIX")
    executable = Path(sys.argv[1]).resolve() / "CBS"
    if not executable.is_file():
        raise SystemExit(f"CBS executable not found: {executable}")
    listed = subprocess.run([str(executable), "--list-analyses"], check=False, capture_output=True, text=True)
    if listed.returncode != 0:
        raise SystemExit(listed.stderr or listed.stdout or "CBS --list-analyses failed")
    count = listed_count(listed.stdout)
    if not count:
        raise SystemExit("CBS --list-analyses returned no analysis entries")
    print(f"CBS startup passed: {count} analysis entries.", flush=True)


if __name__ == "__main__":
    main()
