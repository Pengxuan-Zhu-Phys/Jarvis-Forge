"""Run the pinned Spack engine with JSON-backed physical stage names."""
from pathlib import Path
import inspect
import sys


def short_stages(stage):
    # These modules use only the standard library and run under the same
    # controller Python as Spack. Do not change Spack specs or package inputs.
    from jarvis_forge.paths import physical_name
    from jarvis_forge.storage import new_id

    allocated = set()
    original_init = stage.Stage.__init__
    signature = inspect.signature(original_init)

    def compute(spec):
        root = Path(stage.get_stage_root())
        name = physical_name(root, spec.name or "source",
                             {"spec": str(spec), "dag_hash": spec.dag_hash() if spec.concrete else None}, create=True)
        allocated.add(name)
        return name

    def initialize(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        # An explicit path belongs to the upstream source or caller. Name
        # assignment here only controls stages created in the configured root.
        if bound.arguments.get("path") is None:
            name = bound.arguments.get("name")
            if name not in allocated:
                root = Path(stage.get_stage_root())
                name = (physical_name(root, "resource", {"stage": name}, create=True) if name
                        else new_id("stage", root=root))
                allocated.add(name)
                bound.arguments["name"] = name
        return original_init(*bound.args, **bound.kwargs)

    stage.compute_stage_name = compute
    stage.Stage.__init__ = initialize


def short_bootstrap_store(store, bootstrap):
    """Keep Spack's solver bootstrap in a separate, unpadded package store."""
    use_store = store.use_store

    def scoped_store(path, extra_data=None):
        if bootstrap.is_bootstrapping():
            extra_data = {**(extra_data or {}), "padded_length": False, "projections": {"all": "{name}"}}
        return use_store(path, extra_data=extra_data)

    store.use_store = scoped_store


def main():
    # -I excludes the script directory; explicitly load this installed package
    # and the selected pinned engine, without inheriting user PYTHONPATH.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    executable = Path(sys.argv.pop(1))
    sys.path.insert(0, str(executable.parent.parent / "lib/spack"))
    sys.argv[0] = str(executable)
    from spack.main import main as spack_main
    import spack.stage
    import spack.store
    import spack.bootstrap.config

    short_stages(spack.stage)
    short_bootstrap_store(spack.store, spack.bootstrap.config)
    return spack_main()


if __name__ == "__main__":
    sys.exit(main())
