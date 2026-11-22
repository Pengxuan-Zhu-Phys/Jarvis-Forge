"""Directory names describe software or ordered records; identity stays in JSON."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jarvis_forge.paths import named_path, object_path, record_path, sequence, semantic_name
from jarvis_forge.storage import ForgeError, read_json, write_json, new_id


def test_software_names_preserve_name_and_version_without_length_limit(tmp_path):
    name='a-meaningful-package-name-longer-than-the-old-arbitrary-limit'
    root=tmp_path/'software'
    assert object_path(root,name,{'version':'1.3.1','hash':'a'*64}) is None
    assert not root.exists()
    path=object_path(root,name,{'version':'1.3.1','hash':'a'*64},create=True)
    assert path.name==name+'-1.3.1'
    assert read_json(path/'metadata.json')['identity']['hash']=='a'*64
    assert not (root/'metadata.json').exists()
    with pytest.raises(ForgeError,match='Different inputs'):
        object_path(root,name,{'version':'1.3.1','hash':'b'*64},create=True)
    assert object_path(root,name,{'version':'2.0'},create=True).name==name+'-2.0'


def test_named_reads_do_not_allocate_or_write_indexes(tmp_path):
    root=tmp_path/'absent'
    name='gambit-development-with-a-long-readable-name'
    assert named_path(root,name)==root/name
    assert not root.exists()
    for value in ('../escape','x/y','', '..'):
        with pytest.raises(ForgeError):named_path(root,value)


def test_ordered_records_and_run_ids_are_concurrent_and_durable(tmp_path):
    root=tmp_path/'runs'
    with ThreadPoolExecutor(max_workers=8) as pool:
        names=list(pool.map(lambda _:new_id('build-cbs',root=root),range(32)))
    assert len(set(names))==32
    assert sorted(int(name.split('-')[0]) for name in names)==list(range(1,33))
    assert all(name.endswith('-build-cbs') for name in names)
    assert sequence(root,'install')=='0033-install'


def test_records_reuse_full_identity_without_hash_directory(tmp_path):
    root=tmp_path/'state'
    assert record_path(root,'card',{'hash':'a'*64}) is None
    assert not root.exists()
    one=record_path(root,'card',{'hash':'a'*64},create=True)
    two=record_path(root,'card',{'hash':'b'*64},create=True)
    assert one.name=='0001-card' and two.name=='0002-card'
    assert record_path(root,'card',{'hash':'a'*64})==one


def test_allocated_legacy_indexes_are_rejected_not_migrated(tmp_path):
    write_json(tmp_path/'metadata.json',{'kind':'path-index','objects':{}})
    before=(tmp_path/'metadata.json').read_bytes()
    with pytest.raises(ForgeError,match='Unsupported allocated-name layout'):
        object_path(tmp_path,'zlib',{'version':'1.3.1'},create=True)
    assert (tmp_path/'metadata.json').read_bytes()==before


def test_all_controlled_generation_sites_use_semantic_or_ordered_names():
    """Guard the controller's physical naming boundary, including future edits."""
    import ast
    import jarvis_forge
    root=Path(jarvis_forge.__file__).parent
    violations=[]
    for file in root.glob('*.py'):
        tree=ast.parse(file.read_text())
        for node in ast.walk(tree):
            if not isinstance(node,ast.Call):continue
            function=node.func.attr if isinstance(node.func,ast.Attribute) else node.func.id if isinstance(node.func,ast.Name) else ''
            if function in {'physical_name','short_label','_reserve_instance'}:
                violations.append(f'{file.name}:{node.lineno}: obsolete allocator {function}')
            if function in {'object_path','record_path','named_path','new_id'}:
                # Identity arguments may contain full hashes; physical labels may not.
                arguments=node.args[1:2] if function!='new_id' else node.args[:1]
                for label in arguments:
                    text=ast.unparse(label)
                    if any(word in text for word in ('hexdigest','strftime','timestamp','[:8]','[:12]','[:16]')):
                        violations.append(f'{file.name}:{node.lineno}: identity in label {text}')
    assert violations==[]


@pytest.mark.parametrize("package, version", [("zlib", "1.3.1"), ("compiler-wrapper", "1.1.0")])
def test_native_stage_names_clear_only_changed_owned_stages(tmp_path, package, version):
    from types import SimpleNamespace
    from jarvis_forge.spack_entry import short_stages
    class Stage:
        def __init__(self,name=None,path=None):self.name=name;self.path=path
    adapter=SimpleNamespace(Stage=Stage,get_stage_root=lambda:str(tmp_path))
    short_stages(adapter)
    class Spec:
        versions=SimpleNamespace(concrete=True);concrete=True
        value='one'
        def dag_hash(self):return self.value
        def __str__(self):return f'{self.name}@{self.version} '+self.value
    spec=Spec();spec.name=package;spec.version=version
    label=f'{package}-{version}'
    assert adapter.compute_stage_name(spec)==label
    payload=tmp_path/label/'payload';payload.parent.mkdir();payload.write_text('old stage')
    assert adapter.compute_stage_name(spec)==label and payload.is_file()
    spec.value='two';assert adapter.compute_stage_name(spec)==label and not payload.exists()
    assert read_json(tmp_path/'stages.json')['stages'][label]['dag_hash']=='two'
    opaque=adapter.Stage(name='spack-stage-resource-'+('a'*64))
    assert opaque.name=='0001-resource'

    # Spack can destroy and recreate a directory in another installer worker.
    # The registry must survive, with no loose file in the source stage.
    import shutil
    name = adapter.compute_stage_name(spec)
    folder = tmp_path / name
    folder.mkdir()
    (folder / "spack-src").mkdir()
    (folder / "spack-src/source.c").write_text("source")
    assert not (folder / "metadata.json").exists()
    def fresh_adapter():
        class FreshStage:
            def __init__(self, name=None, path=None): self.name=name; self.path=path
        result = SimpleNamespace(Stage=FreshStage, get_stage_root=lambda: str(tmp_path))
        short_stages(result)
        return result
    assert fresh_adapter().compute_stage_name(spec) == name
    assert (folder / "spack-src/source.c").read_text() == "source"
    shutil.rmtree(folder)
    assert fresh_adapter().compute_stage_name(spec) == name
    assert not folder.exists()
    # Unknown contents and symlink targets still cannot be adopted or removed.
    registry = tmp_path / "stages.json"
    saved = registry.read_bytes()
    registry.unlink()
    folder.mkdir(); (folder / "user-file").write_text("preserve")
    with pytest.raises(ForgeError, match="Unregistered build stage"):
        fresh_adapter().compute_stage_name(spec)
    assert (folder / "user-file").read_text() == "preserve"
    registry.write_bytes(saved)
    shutil.rmtree(folder)
    target = tmp_path / "user-dir"; target.mkdir()
    folder.symlink_to(target, target_is_directory=True)
    with pytest.raises(ForgeError, match="symbolic link"):
        fresh_adapter().compute_stage_name(spec)
    assert target.is_dir()
