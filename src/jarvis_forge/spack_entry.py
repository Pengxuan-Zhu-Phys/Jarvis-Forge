"""Run the pinned Spack engine with JSON-backed physical stage names."""
from pathlib import Path
import inspect
import json
import os
import sys


def short_stages(stage):
    # Spack owns stage contents: archive expansion can move every loose file
    # into spack-src, and destroy/recreate removes them altogether. Ownership
    # must survive outside that directory, including across installer workers.
    from jarvis_forge.paths import semantic_name, sequence, directory_lock
    from jarvis_forge.storage import ForgeError, read_json, write_json
    import shutil

    allocated = set()
    original_init = stage.Stage.__init__
    signature = inspect.signature(original_init)

    def register(root, name, identity):
        path = root / name
        with directory_lock(root, ".stages.lock"):
            registry = root / "stages.json"
            if registry.is_symlink():
                raise ForgeError(f"Stage registry must not be a symbolic link: {registry}")
            data = read_json(registry) if registry.exists() else {
                "schema_version": 1, "kind": "stage-registry", "stages": {},
            }
            if (data.get("schema_version") != 1 or data.get("kind") != "stage-registry"
                    or not isinstance(data.get("stages"), dict)):
                raise ForgeError(f"Invalid stage registry: {registry}")
            if path.is_symlink():
                raise ForgeError(f"Managed stage must not be a symbolic link: {path}")
            if path.exists():
                if name not in data["stages"]:
                    raise ForgeError(f"Unregistered build stage is preserved: {path}")
                if not path.is_dir():
                    raise ForgeError(f"Managed stage must be a directory: {path}")
                if data["stages"][name] != identity:
                    shutil.rmtree(path)
            data["stages"][name] = identity
            write_json(registry, data)
        allocated.add(path)
        return name

    def compute(spec):
        root = Path(stage.get_stage_root())
        version = str(spec.version) if spec.versions.concrete else "dev"
        name = semantic_name(spec.name or "source", version)
        identity = {"spec": str(spec), "dag_hash": spec.dag_hash() if spec.concrete else None}
        return register(root, name, identity)

    def initialize(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        # An explicit path belongs to the upstream source or caller.
        if bound.arguments.get("path") is None:
            root = Path(stage.get_stage_root())
            name = bound.arguments.get("name")
            if name is None or root / name not in allocated:
                native_name = name
                name = sequence(root, "resource" if name else "stage")
                register(root, name, {"native_name": native_name})
                bound.arguments["name"] = name
        return original_init(*bound.args, **bound.kwargs)

    stage.compute_stage_name = compute
    stage.Stage.__init__ = initialize


def short_bootstrap_store(store, bootstrap):
    """Keep Spack's solver bootstrap in a separate, unpadded package store."""
    use_store = store.use_store

    def scoped_store(path, extra_data=None):
        if bootstrap.is_bootstrapping():
            extra_data = {**(extra_data or {}), "padded_length": False, "projections": {"all": "{name}-{version}"}}
        return use_store(path, extra_data=extra_data)

    store.use_store = scoped_store


def cache_trust_guard(cache):
    """Spack 1.2 catches ordinary verification errors and treats them as misses.

    A trust failure must escape those ``except Exception`` handlers, including
    when GnuPG warnings are suppressed. An operation-local record survives the
    installer worker boundary so the controller can also refuse any retry.
    """
    destination = os.environ.get("_JFORGE_CACHE_TRUST_LOG")
    if not destination:
        return

    class VerificationFailure(SystemExit):
        def __init__(self, message):
            detail = "Binary-cache signature verification failed: " + str(message)
            try:
                descriptor = os.open(destination, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                try:
                    os.write(descriptor, (json.dumps({"message": detail}) + "\n").encode())
                finally:
                    os.close(descriptor)
            except OSError as exc:
                # Inability to write evidence must never become a cache miss.
                detail += f"\nCould not record verification failure: {exc}"
            # SystemExit bypasses Spack's cache-miss catches, never its actual
            # signature verification. No --unsigned or skipped GPG checks.
            super().__init__(detail)

    cache.NoVerifyException = VerificationFailure
    original_metadata = cache.URLBuildcacheEntryV2.fetch_metadata

    def metadata(self):
        try:
            return original_metadata(self)
        except cache.BuildcacheEntryError as exc:
            if not self.allow_unsigned and "does not have signed metadata" in str(exc):
                raise VerificationFailure(str(exc)) from exc
            raise

    cache.URLBuildcacheEntryV2.fetch_metadata = metadata


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

    import spack.util.gpg
    from jarvis_forge.socket_runtime import configure_spack
    configure_spack(spack.util.gpg)
    short_stages(spack.stage)
    short_bootstrap_store(spack.store, spack.bootstrap.config)
    if os.environ.get("_JFORGE_CACHE_TRUST_LOG"):
        import spack.url_buildcache
        cache_trust_guard(spack.url_buildcache)
    return spack_main()


if __name__ == "__main__":
    sys.exit(main())
