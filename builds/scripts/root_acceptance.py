"""ROOT feature and scientific smoke checks shipped with root-build.yaml."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix", type=Path)
    parser.add_argument("version")
    parser.add_argument("cxx_standard")
    parser.add_argument("features", help="JSON list of required ROOT features")
    args = parser.parse_args()
    root_config = str(args.prefix / "bin/root-config")
    version = subprocess.check_output([root_config, "--version"], text=True).strip()
    if version != args.version:
        raise RuntimeError(f"ROOT version {version}; expected {args.version}")
    features = set(subprocess.check_output([root_config, "--features"], text=True).split())
    required = set(json.loads(args.features)) | {"cxx" + args.cxx_standard}
    if not required <= features:
        raise RuntimeError(f"Missing ROOT features: {sorted(required - features)}")
    print("ROOT", version, "features:", " ".join(sorted(features)), flush=True)

    import ROOT
    if not Path(ROOT.__file__).resolve().is_relative_to(args.prefix.resolve()):
        raise RuntimeError(f"PyROOT was imported outside the built installation: {ROOT.__file__}")
    ROOT.gROOT.SetBatch(True)
    histogram = ROOT.TH1F("forge_h", "smoke", 10, 0, 1)
    histogram.Fill(0.5)
    if histogram.GetEntries() != 1:
        raise RuntimeError("Histogram filling failed")
    with tempfile.TemporaryDirectory(prefix="jforge-root-check-") as temporary:
        filename = str(Path(temporary) / "smoke.root")
        output = ROOT.TFile(filename, "RECREATE")
        if output.IsZombie():
            raise RuntimeError("ROOT file creation failed")
        histogram.Write()
        output.Close()
        restored = ROOT.TFile.Open(filename)
        if not restored or restored.IsZombie():
            raise RuntimeError("ROOT file reopening failed")
        saved = restored.Get("forge_h")
        if not saved or saved.GetEntries() != 1:
            raise RuntimeError("Histogram round trip failed")
        restored.Close()
    x = ROOT.RooRealVar("x", "x", -5, 5)
    mean = ROOT.RooRealVar("mean", "mean", 0)
    sigma = ROOT.RooRealVar("sigma", "sigma", 1)
    gaussian = ROOT.RooGaussian("g", "g", x, mean, sigma)
    if gaussian.getVal() <= 0:
        raise RuntimeError("RooFit Gaussian evaluation failed")
    for library in ("libGpad", "libHist", "libTree", "libRooFit"):
        if ROOT.gSystem.Load(library) < 0:
            raise RuntimeError(f"ROOT library loading failed: {library}")
    print("Histogram, ROOT file I/O, RooFit and library loading passed", flush=True)


if __name__ == "__main__":
    main()
