"""Check the architecture supported by the pinned CBS ONNX Runtime archive."""

import platform


def supported(system, machine):
    return system == "Linux" and machine.lower() in {"x86_64", "amd64"}


if __name__ == "__main__":
    if not supported(platform.system(), platform.machine()):
        raise SystemExit("The full CBS recipe requires Linux x86_64 for its upstream ONNX Runtime archive.")
    print("CBS platform passed: Linux x86_64.", flush=True)
