"""Run the pinned Spack engine with JSON-backed physical stage names."""
from pathlib import Path
import inspect
import json
import os
import sys


def short_stages(stage):
    # These modules use only the standard library and run under the same
    # controller Python as Spack. Do not change Spack specs or package inputs.
    from jarvis_forge.paths import semantic_name, metadata, sequence
    from jarvis_forge.storage import new_id

    allocated = set()
    original_init = stage.Stage.__init__
    signature = inspect.signature(original_init)

    def compute(spec):
        from jarvis_forge.paths import directory_lock
        from jarvis_forge.storage import ForgeError,read_json
        import shutil
        root = Path(stage.get_stage_root())
        version=str(spec.version) if spec.versions.concrete else 'dev'
        name=semantic_name(spec.name or 'source',version)
        identity={'spec':str(spec),'dag_hash':spec.dag_hash() if spec.concrete else None}
        path=root/name
        with directory_lock(root,'.stages.lock'):
            if path.is_symlink():raise ForgeError(f'Managed stage must not be a symbolic link: {path}')
            marker=path/'metadata.json'
            if path.exists():
                if not marker.is_file() or read_json(marker).get('kind')!='stage':
                    raise ForgeError(f'Unregistered build stage is preserved: {path}')
                if read_json(marker).get('identity')!=identity:shutil.rmtree(path)
            metadata(path,'stage',spec.name or 'source',version=version,identity=identity)
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
                name = sequence(root,'resource' if name else 'stage')
                metadata(root/name,'stage','resource' if bound.arguments.get('name') else 'stage',
                         identity={'native_name':bound.arguments.get('name')})
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
