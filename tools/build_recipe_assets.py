"""Build recipe Release assets without building or publishing Jarvis-Forge."""

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile


def build(version, output):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", version):
        raise ValueError("Invalid recipe release version")
    source = Path(__file__).resolve().parents[1] / "recipes"
    output.mkdir(parents=True, exist_ok=True)
    filename = "jforge-spack-recipes-" + version + ".tar.gz"
    archive = output / filename
    if archive.exists():
        raise FileExistsError(archive)
    with (
        archive.open("xb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed,
    ):
        with tarfile.open(fileobj=compressed, mode="w") as package:
            for file in sorted(source.rglob("*")):
                if file.is_file() and file.suffix in {".py", ".yaml"}:
                    content = file.read_bytes()
                    info = tarfile.TarInfo(
                        "jforge-spack-recipes/" + str(file.relative_to(source))
                    )
                    info.size = len(content)
                    info.mode = 0o644
                    package.addfile(info, io.BytesIO(content))
    manifest = {
        "schema_version": 1,
        "version": version,
        "engine_versions": ["1.2.2"],
        "archive": {
            "url": "https://github.com/Pengxuan-Zhu-Phys/Jarvis-Forge/releases/download/recipes/"
            + filename,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        },
        "packages": sorted(
            p.parent.name.replace("_", "-")
            for p in (source / "spack_repo/jforge").glob("packages/*/package.py")
        ),
    }
    (output / "recipes.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.version, args.output))
