"""Combined engine results cannot outlive the exact candidate configuration."""

import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from jarvis_forge import spack_backend as b, spack_bridge, spack_planning
from jarvis_forge.spack_entry import cache_trust_guard
from jarvis_forge.storage import ForgeError, ForgeHome, write_json, read_json


@pytest.fixture
def candidate(tmp_path):
    env = tmp_path / "candidate"
    (env / ".jforge-config").mkdir(parents=True)
    (env / "spack.yaml").write_text("spack: {specs: [zlib]}\n")
    (env / "spack.lock").write_text("original")
    (env / ".jforge-config/config.yaml").write_text("config: {}\n")
    op = tmp_path / "operation"
    op.mkdir()
    request = {"policy": "only", "overwrite": [], "omit_cached_build_deps": False}
    result = {"dag": {"packages": [{"name": "zlib"}]}, "plan": {"packages": [{"name": "zlib"}]},
              "tree": "zlib@1.3.1", "policy": "only"}
    return op, env, request, result


@pytest.mark.parametrize("changed", ["spack.yaml", "spack.lock", "build-tool-reuse.json", ".jforge-config/config.yaml", ".jforge-config/packages.yaml"])
def test_combined_plan_invalidates_on_every_solver_input(candidate, changed):
    op, env, request, result = candidate
    spack_planning.save(op, env, request, json.dumps(result))
    bridge = ["python", "spack_bridge.py"]
    assert json.loads(spack_planning.query(op, env, [*bridge, "snapshot"], "dag")) == result["dag"]
    assert spack_planning.query(op, env, [*bridge, "installed"], "installed") is None
    assert spack_planning.query(op, env, [*bridge, "installation-plan", "auto", "[]"], "source-plan") is None
    assert spack_planning.query(op, env, [*bridge, "installation-plan", "only", "[]", "omit-cached-build-deps"], "plan") is None
    (env / changed).write_text("changed")
    assert spack_planning.current(op, env) is None


def test_combined_plan_rejects_malformed_engine_output(candidate):
    op, env, request, _ = candidate
    with pytest.raises(ForgeError, match="invalid combined"):
        spack_planning.save(op, env, request, '{"dag": {"packages": []}}')
    assert not (op / "planning.json").exists()


def test_normal_install_has_three_engine_launches_and_preserves_command_logs(candidate, monkeypatch):
    op, env, request, result = candidate
    launches = []
    monkeypatch.setattr(b, "command", lambda home, args, envdir: args)
    def engine(args, cwd, context, logs, label, *unused, **kwargs):
        launches.append(args)
        value = result if label == "concretize" else {"packages": []}
        (logs / f"command-{label}.stdout.log").write_text(json.dumps(value))
        (logs / f"command-{label}.stderr.log").write_text("")
    monkeypatch.setattr("jarvis_forge.package_operations.run_command", engine)
    home = ForgeHome(op.parent / "forge")
    b.run(home, ["concretize"], op, "concretize", envdir=env, planning=request)
    dag = b._snapshot(home, env, op, "dag")
    b.run(home, ["python", "spack_bridge.py", "tree"], op, "dependency-tree", envdir=env)
    plan = b._snapshot(home, env, op, "plan", cache="only")
    b.run(home, b._install_args(8, "only"), op, "install-cache", envdir=env)
    b._snapshot(home, env, op, "installed")
    assert len(launches) == 3
    assert launches[0][2] == "resolve-plan" and launches[-1][-1] == "installed"
    assert dag == result["dag"] and plan == result["plan"]
    assert (op / "command-dependency-tree.stdout.log").read_text() == result["tree"]
    assert read_json(op / "command-plan.json")["executed"] is False
    assert len((op / "spack-calls.jsonl").read_text().splitlines()) == 3


def test_backend_bootstrap_prepares_solver_and_keys_before_marking_ready(tmp_path, monkeypatch):
    home = ForgeHome(tmp_path / "forge")
    op = tmp_path / "operation"
    write_json(home.root / "spack/backend.json", {"schema_version": 1, "backend": "spack"})
    calls = []
    monkeypatch.setattr(b, "run", lambda *args, **kwargs: calls.append(args[3]))
    def trust(*args):
        assert not b.settings(home).get("backend_prepared")
        calls.append("keys")
    monkeypatch.setattr(b, "_cache_keys", trust)
    b._prepare_backend(home, op, None)
    b._prepare_backend(home, op, None)
    assert calls == ["prepare-backend", "keys"]
    assert b.settings(home)["backend_prepared"]


def test_failed_key_preparation_does_not_mark_backend_ready(tmp_path, monkeypatch):
    home = ForgeHome(tmp_path / "forge")
    op = tmp_path / "operation"
    write_json(home.root / "spack/backend.json", {"schema_version": 1, "backend": "spack"})
    monkeypatch.setattr(b, "run", lambda *args, **kwargs: "")
    def fail(*args):
        raise ForgeError("key import failed", code=1)
    monkeypatch.setattr(b, "_cache_keys", fail)
    with pytest.raises(ForgeError, match="key import failed"):
        b._prepare_backend(home, op, None)
    assert not b.settings(home).get("backend_prepared")


@pytest.mark.parametrize("cached,installed,overwrite,expected", [
    (True, False, [], "only"), (False, False, [], "never"),
    (True, True, [], "never"), (True, True, ["zlib"], "only"),
])
def test_bridge_uses_source_plan_when_no_pending_binary_exists(monkeypatch, capsys, cached, installed, overwrite, expected):
    spack = SimpleNamespace()
    class CommandError(Exception):
        pass
    command = SimpleNamespace(SpackCommand=lambda name: lambda *flags: "Concretized zlib\n",
                              SpackCommandError=CommandError)
    environment = SimpleNamespace(active_environment=lambda: SimpleNamespace(concrete_roots=lambda: []))
    spec = SimpleNamespace(tree=lambda *args, **kw: "zlib@1.3.1", DISPLAY_FORMAT="{name}")
    traverse = SimpleNamespace(by_dag_hash=lambda value: value)
    for name, value in {"main": command, "environment": environment, "spec": spec, "traverse": traverse}.items():
        setattr(spack, name, value)
        monkeypatch.setitem(sys.modules, "spack." + name, value)
    monkeypatch.setitem(sys.modules, "spack", spack)
    monkeypatch.setattr(spack_bridge, "store_inventory", lambda checkpoint=None: [])
    policies = []
    def snapshot(**kwargs):
        policies.append(kwargs.get("policy"))
        packages = [{"name": "zlib", "cached": cached, "installed": installed}]
        if kwargs.get("policy") == "never":
            packages.append({"name": "make", "installed": False})
        return {"packages": packages}
    monkeypatch.setattr(spack_bridge, "snapshot", snapshot)
    spack_bridge.resolve_plan({"flags": ["concretize"], "policy": "only", "overwrite": overwrite,
                               "source_if_uncached": True})
    result = json.loads(capsys.readouterr().out)
    assert result["policy"] == expected
    assert (len(result["plan"]["packages"]) == 2) == (expected == "never")
    assert result["concretize_output"] == "Concretized zlib\n"


def test_bridge_preserves_solver_exit_code_and_captured_diagnostic(monkeypatch, capsys):
    class CommandError(Exception):
        code = 3
    class Command:
        output = "Error: incompatible compiler constraint\n"
        def __call__(self, *args):
            raise CommandError()
    spack = SimpleNamespace(main=SimpleNamespace(SpackCommand=lambda name: Command(), SpackCommandError=CommandError),
                            environment=SimpleNamespace(), spec=SimpleNamespace(), traverse=SimpleNamespace())
    monkeypatch.setitem(sys.modules, "spack", spack)
    for name in ("main", "environment", "spec", "traverse"):
        monkeypatch.setitem(sys.modules, "spack." + name, getattr(spack, name))
    with pytest.raises(SystemExit) as failed:
        spack_bridge.resolve_plan({"flags": ["concretize"]})
    assert failed.value.code == 3
    captured = capsys.readouterr()
    assert not captured.out and "incompatible compiler constraint" in captured.err


@pytest.mark.parametrize("keys", [[], ["trusted-fingerprint"]])
def test_empty_official_key_download_is_a_failure_not_success(monkeypatch, capsys, keys):
    calls = []
    mirror = object()
    distribution = SimpleNamespace(get_keys=lambda **kwargs: calls.append(kwargs) or keys)
    mirrors = SimpleNamespace(mirror=SimpleNamespace(MirrorCollection=lambda **kwargs: {"official": mirror}))
    spack = SimpleNamespace(binary_distribution=distribution, mirrors=mirrors)
    monkeypatch.setitem(sys.modules, "spack", spack)
    monkeypatch.setitem(sys.modules, "spack.binary_distribution", distribution)
    monkeypatch.setitem(sys.modules, "spack.mirrors", mirrors)
    monkeypatch.setitem(sys.modules, "spack.mirrors.mirror", mirrors.mirror)
    if keys:
        spack_bridge.trust_cache_keys("official")
        assert json.loads(capsys.readouterr().out)["fingerprints"] == keys
    else:
        with pytest.raises(SystemExit, match="Could not import any trusted signing keys"):
            spack_bridge.trust_cache_keys("official")
    assert calls == [{"install": True, "trust": True, "yes_to_all": True, "mirrors": {"official": mirror}}]


@pytest.mark.parametrize("reason", ["signature", "unsigned-v2", "record-unwritable"])
def test_verification_guard_escapes_native_cache_miss_catches(tmp_path, monkeypatch, reason):
    log = tmp_path / "trust-failures.jsonl"
    monkeypatch.setenv("_JFORGE_CACHE_TRUST_LOG", str(log if reason != "record-unwritable" else tmp_path))
    class CacheError(Exception):
        pass
    class V2:
        allow_unsigned = False
        def fetch_metadata(self):
            raise CacheError("Mirror does not have signed metadata for spec")
    cache = SimpleNamespace(NoVerifyException=CacheError, BuildcacheEntryError=CacheError, URLBuildcacheEntryV2=V2)
    cache_trust_guard(cache)
    def native_cache_fetch():
        try:
            if reason == "unsigned-v2":
                cache.URLBuildcacheEntryV2().fetch_metadata()
            else:
                raise cache.NoVerifyException("Required signature was not found on manifest")
        except Exception:
            return "source-fallback"
    with pytest.raises(SystemExit, match="signature verification failed"):
        native_cache_fetch()
    if reason != "record-unwritable":
        assert "signature verification failed" in json.loads(log.read_text())["message"]


def test_native_guard_preserves_missing_archive_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("_JFORGE_CACHE_TRUST_LOG", str(tmp_path / "trust.jsonl"))
    class CacheError(Exception):
        pass
    class V2:
        allow_unsigned = False
        def fetch_metadata(self):
            raise CacheError("Unable to fetch metadata from mirror")
    cache = SimpleNamespace(NoVerifyException=CacheError, BuildcacheEntryError=CacheError, URLBuildcacheEntryV2=V2)
    cache_trust_guard(cache)
    with pytest.raises(CacheError, match="Unable to fetch metadata"):
        cache.URLBuildcacheEntryV2().fetch_metadata()
    assert not (tmp_path / "trust.jsonl").exists()


def test_controller_stops_when_worker_records_failure_but_returns_success(candidate, monkeypatch):
    op, env, _, _ = candidate
    monkeypatch.setattr(b, "command", lambda home, args, envdir: args)
    def engine(args, cwd, context, logs, label, *unused, **kwargs):
        assert "export _JFORGE_CACHE_TRUST_LOG=" in context.read_text()
        (op / "cache-trust-failures.jsonl").write_text('{"message":"Required signature was not found"}\n')
        (op / f"command-{label}.stdout.log").write_text("")
    monkeypatch.setattr("jarvis_forge.package_operations.run_command", engine)
    with pytest.raises(ForgeError, match="trust verification failed"):
        b.run(ForgeHome(op.parent / "forge"), b._install_args(8, "only"), op, "install-cache", envdir=env)


def test_cache_trust_hint_is_not_duplicated_at_the_transaction_boundary(tmp_path):
    error = ForgeError("Signature verification failed", code=1)
    with pytest.raises(ForgeError) as first:
        b._reject_cache_security_failure(tmp_path, "install-cache", error)
    with pytest.raises(ForgeError) as outer:
        b._reject_cache_security_failure(tmp_path, "install-cache", first.value)
    assert outer.value is first.value
    assert outer.value.hint.count("Binary-cache trust verification failed.") == 1


def test_database_checkpoint_is_not_overwritten_by_a_retried_plan(tmp_path,monkeypatch):
    from contextlib import nullcontext
    import types
    class Database:
        contents='original'
        def read_transaction(self):return nullcontext()
        def _write_to_file(self,stream):stream.write(self.contents)
        def query_local(self):return []
    db=Database();spack=types.ModuleType('spack');store=types.ModuleType('spack.store');store.STORE=SimpleNamespace(db=db);spack.store=store
    monkeypatch.setitem(sys.modules,'spack',spack);monkeypatch.setitem(sys.modules,'spack.store',store)
    checkpoint=tmp_path/'database.json'
    spack_bridge.store_inventory(checkpoint);db.contents='partially-installed'
    spack_bridge.store_inventory(checkpoint)
    assert checkpoint.read_text()=='original'


def test_native_description_search_is_read_only_and_adapts_version_metadata(tmp_path,monkeypatch):
    home=ForgeHome(tmp_path/'absent')
    monkeypatch.setattr(b,'configured',lambda home:True)
    calls=[]
    def query(home,args):
        calls.append(args);return json.dumps([{'name':'eigen','latest_version':'5.0.1','versions':['5.0.1','3.4.0']}])
    monkeypatch.setattr(b,'_read_native',query)
    rows=b.search(home,'linear')
    assert rows[0]['name']=='eigen' and rows[0]['version']=='5.0.1' and rows[0]['status']=='available'
    assert '--search-description' in calls[0] and not home.root.exists()
    details=b.read_details(home,'eigen')
    assert details['versions']==['5.0.1','3.4.0'] and not home.root.exists()
