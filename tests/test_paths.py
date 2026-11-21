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


def test_native_stage_names_clear_only_changed_owned_stages(tmp_path):
    from types import SimpleNamespace
    from jarvis_forge.spack_entry import short_stages
    class Stage:
        def __init__(self,name=None,path=None):self.name=name;self.path=path
    adapter=SimpleNamespace(Stage=Stage,get_stage_root=lambda:str(tmp_path))
    short_stages(adapter)
    class Spec:
        name='zlib';version='1.3.1';versions=SimpleNamespace(concrete=True);concrete=True
        value='one'
        def dag_hash(self):return self.value
        def __str__(self):return 'zlib@1.3.1 '+self.value
    spec=Spec();assert adapter.compute_stage_name(spec)=='zlib-1.3.1'
    payload=tmp_path/'zlib-1.3.1/payload';payload.write_text('old stage')
    assert adapter.compute_stage_name(spec)=='zlib-1.3.1' and payload.is_file()
    spec.value='two';assert adapter.compute_stage_name(spec)=='zlib-1.3.1' and not payload.exists()
    assert read_json(tmp_path/'zlib-1.3.1/metadata.json')['identity']['dag_hash']=='two'
    opaque=adapter.Stage(name='spack-stage-resource-'+('a'*64))
    assert opaque.name=='0001-resource'
