"""Native plan identities and variant constraints; no profile export CLI."""
from pathlib import Path
import pytest
import yaml
from jarvis_forge import profile_exports as constraints
from jarvis_forge.storage import ForgeError


def test_variant_diff_refuses_before_install_and_pins_native_syntax():
    expected=[{'name':'zlib','version':'1.3.1','variants':{'shared':True}}]
    constraints.compare(expected,[{'name':'zlib','version':'1.3.1','node':{'parameters':{'shared':True}}}])
    assert constraints.constraints(expected)=={'zlib':'@=1.3.1 +shared'}
    with pytest.raises(ForgeError,match='versions or variants differ'):
        constraints.compare(expected,[{'name':'zlib','version':'1.3.1','node':{'parameters':{'shared':False}}}])


def test_native_identity_omits_host_paths_and_dag_hashes():
    row={'name':'zlib','version':'1.3.1','hash':'host-specific','prefix':'/host/specific',
         'node':{'parameters':{'shared':True}}}
    assert constraints.package_identity(row)=={'name':'zlib','version':'1.3.1','variants':{'shared':True},'external':False}


def test_companion_copy_preserves_bytes_used_by_its_checksum(tmp_path):
    import hashlib
    from jarvis_forge import build_catalog
    source=tmp_path/'recipe';source.mkdir()
    script=source/'accept.py';content=b'print("portable")\r\n';script.write_bytes(content)
    config={'schema_version':1,'kind':'build','id':'portable','resources':[{'path':'accept.py','sha256':hashlib.sha256(content).hexdigest()}],
            'steps':[{'id':'run','uses':'command','with':{'argv':['python3','${build_dir}/accept.py']}}]}
    entry=source/'build.yaml';entry.write_text(yaml.safe_dump(config))
    output=tmp_path/'copy/build.yaml';build_catalog.copy_bundle(entry,output)
    assert output.with_name('accept.py').read_bytes()==content
