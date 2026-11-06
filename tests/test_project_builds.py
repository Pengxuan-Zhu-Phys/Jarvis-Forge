from pathlib import Path
import json
import shutil
import sys

import pytest
import yaml

from jarvis_forge import cmake, builds, build_catalog
from jarvis_forge.cli import main
from jarvis_forge.storage import ForgeHome, ForgeError, digest, read_json, write_json


def fake_bootstrap(target, *args, **kwargs):
    activation = target.root / "environments/apt/activate.sh"
    activation.parent.mkdir(parents=True, exist_ok=True)
    activation.write_text(":\n")
    write_json(target.root / "apt/spack.json", {"schema_version": 1, "backend": "spack"})
    write_json(target.root / "apt/state.json", {
        "schema_version": 1,
        "spack": {"roots": [], "packages": []},
        "activation": str(activation),
    })


def save(path, **config):
    path.write_text(yaml.safe_dump({'schema_version': 1, 'kind': 'build', **config}, sort_keys=False))
    return path


@pytest.fixture
def home(tmp_path):
    value = ForgeHome(tmp_path / 'home')
    value.initialize()
    return value


@pytest.fixture
def projects(tmp_path):
    if not shutil.which('cmake'):
        pytest.skip('CMake not installed')
    source = tmp_path / 'producer-src'
    source.mkdir()
    (source / 'value.h').write_text('extern int forge_value();\n')
    (source / 'tiny.cpp').write_text('int forge_value(){return 42;}\n')
    (source / 'ForgeTinyConfig.cmake.in').write_text('add_library(ForgeTiny::Tiny SHARED IMPORTED)\nset_target_properties(ForgeTiny::Tiny PROPERTIES IMPORTED_LOCATION "${CMAKE_CURRENT_LIST_DIR}/../../../lib/libtiny${CMAKE_SHARED_LIBRARY_SUFFIX}" INTERFACE_INCLUDE_DIRECTORIES "${CMAKE_CURRENT_LIST_DIR}/../../../include")\n')
    (source / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.20)
project(Producer LANGUAGES CXX)
add_library(tiny SHARED tiny.cpp)
install(TARGETS tiny DESTINATION lib)
install(FILES value.h DESTINATION include)
install(FILES ForgeTinyConfig.cmake.in DESTINATION lib/cmake/ForgeTiny RENAME ForgeTinyConfig.cmake)
''')
    producer = save(tmp_path / 'producer.yaml', id='tiny-build', parameters={'cxx_standard': 17},
        sources={'tiny': {'path': str(source)}}, steps=[{'id': 'tiny', 'uses': 'cmake.project', 'with': {
            'source': '${sources.tiny}', 'cache': {'CMAKE_CXX_STANDARD': {'type': 'STRING', 'value': '${parameters.cxx_standard}'}},
            'export': {'package': 'ForgeTiny', 'version': '1.0', 'cmake_dir': 'lib/cmake/ForgeTiny', 'paths': {'LD_LIBRARY_PATH':['lib'], 'DYLD_LIBRARY_PATH':['lib']}}}}],
        outputs={'installation': {'step': 'tiny', 'publish_after': ['tiny']}})
    consumer_source = tmp_path / 'consumer-src'
    consumer_source.mkdir()
    (consumer_source / 'main.cpp').write_text('#include <value.h>\nint main(){return forge_value()==42?0:1;}\n')
    (consumer_source / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.20)
project(Consumer LANGUAGES CXX)
find_package(ForgeTiny CONFIG REQUIRED)
add_executable(consumer main.cpp)
target_link_libraries(consumer PRIVATE ForgeTiny::Tiny)
enable_testing()
add_test(NAME consumer-runs COMMAND consumer)
install(TARGETS consumer DESTINATION bin)
''')
    consumer = save(tmp_path / 'consumer.yaml', id='consumer-build', parameters={'cxx_standard': 17},
        sources={'consumer': {'path': str(consumer_source)}}, requires={'tiny': {'build': str(producer), 'output': 'installation', 'version': '>=1,<2'}},
        steps=[{'id': 'consumer', 'uses': 'cmake.project', 'with': {'source': '${sources.consumer}',
            'dependencies': ['requires.tiny'], 'phases': ['configure','build','test','install'], 'cache': {'CMAKE_CXX_STANDARD': {'type': 'STRING', 'value': '${parameters.cxx_standard}'}},
            'export': {'package': 'Consumer', 'version': '1.0'}}},
            {'id': 'smoke', 'uses': 'command', 'needs': ['consumer'], 'with': {'argv': ['${steps.consumer.prefix}/bin/consumer'], 'dependencies': ['consumer']}}],
        outputs={'installation': {'step': 'consumer', 'publish_after': ['smoke']}})
    return producer, consumer


def test_cmake_argv_types_and_literal_values():
    config = {'configure_preset': 'cbs-gnu', 'targets': ['CBS'], 'configuration': 'Release',
              'cache': {'FLAG': {'type': 'BOOL', 'value': True}, 'PATHS': {'type': 'STRING', 'value': ['a;b', 'c']}}}
    commands = cmake.commands(config, Path('/source path'), Path('/build path'), Path('/prefix'), jobs=8)
    assert '-DFLAG:BOOL=ON' in commands['configure']
    assert '-DPATHS:STRING=a\\;b;c' in commands['configure']
    assert commands['build'][-4:] == ['--target', 'CBS', '--config', 'Release']


def test_cmake_producer_consumer_and_reuse(home, projects):
    producer, consumer = projects
    first = builds.execute(home, consumer, jobs=2)
    assert first['status'] == 'passed', first
    assert len(list((home.root / 'builds/artifacts').glob('*/manifest.json'))) == 2
    output = first['outputs']['installation']
    assert Path(output['prefix'], 'bin/consumer').is_file()
    assert output['dependencies']
    assert home.show(first['run_id'])['status'] == 'passed'
    second = builds.execute(home, consumer, jobs=2)
    assert second['status'] == 'passed', second
    assert any(n['status'] == 'reused' for n in second['nodes'].values())
    builds.artifact(home, output['artifact_id'])
    (Path(output['prefix']) / 'bin/consumer').write_text('tampered')
    with pytest.raises(ForgeError, match='modified'):
        builds.artifact(home, output['artifact_id'])


def test_resume_uses_original_recipe_and_retries_failed_check(home, projects, tmp_path):
    producer, _ = projects
    config = yaml.safe_load(producer.read_text())
    marker = tmp_path / 'allow-success'
    config['steps'].append({'id': 'check', 'uses': 'command', 'needs': ['tiny'], 'with': {
        'argv': [sys.executable, '-c', 'from pathlib import Path; assert Path(' + repr(str(marker)) + ').exists()']}})
    config['outputs']['installation']['publish_after'] = ['check']
    producer.write_text(yaml.safe_dump(config))
    first = builds.execute(home, producer)
    assert first['status'] == 'failed'
    assert not list((home.root / 'builds/artifacts').glob('*/manifest.json'))
    producer.write_text('broken original; resume must use snapshot')
    marker.touch()
    second = builds.execute(home, resume=first['run_id'], jobs=2)
    assert second['status'] == 'passed', second
    assert len(list((Path(second['path']) / 'attempts').iterdir())) == 2


def test_incompatible_standard_blocks_dependency(home, projects):
    producer, consumer = projects
    config = yaml.safe_load(consumer.read_text())
    config['parameters']['cxx_standard'] = 20
    consumer.write_text(yaml.safe_dump(config))
    result = builds.execute(home, consumer)
    assert result['status'] == 'failed'
    assert 'Incompatible' in result['error']


def test_cross_workflow_cycle_and_bad_dependency(tmp_path, home):
    a = save(tmp_path / 'a.yaml', id='a', requires={'b': {'build': 'b.yaml', 'output': 'installation'}}, steps=[{'id':'x','uses':'command','with':{'argv':['true']}}])
    save(tmp_path / 'b.yaml', id='b', requires={'a': {'build': 'a.yaml', 'output': 'installation'}}, steps=[{'id':'x','uses':'command','with':{'argv':['true']}}])
    with pytest.raises(ForgeError, match='cycle'):
        builds.graph(home, str(a))


def test_catalog_is_independent_and_failure_retains_cache(tmp_path, home):
    source = save(tmp_path / 'build.yaml', id='test', steps=[{'id':'x','uses':'command','with':{'argv':['true']}}])
    index = tmp_path / 'index.json'
    value = {'schema_version':1, 'requires_forge':'>=1.0.12', 'builds':[{'id':'test','revision':'r1','url':source.as_uri(),'sha256':digest(source)}]}
    index.write_text(json.dumps(value))
    build_catalog.update(home, index.as_uri())
    source.unlink()
    assert build_catalog.resolve(home, 'test').is_file()
    old = (home.root / 'builds/catalog/index.json').read_bytes()
    with pytest.raises(ForgeError):
        build_catalog.update(home, index.as_uri())
    assert (home.root / 'builds/catalog/index.json').read_bytes() == old


def test_build_cli_json_and_read_only_plan(tmp_path, capsys):
    path = save(tmp_path / 'flow.yaml', id='test', steps=[{'id':'x','uses':'command','with':{'argv':['true']}}])
    absent = tmp_path / 'absent'
    assert main(['--home',str(absent),'build','plan',str(path),'--json']) == 0
    assert json.loads(capsys.readouterr().out)['ok']
    assert not absent.exists()
    assert main(['--home',str(absent),'build','validate',str(path),'--json']) == 0
    assert json.loads(capsys.readouterr().out)['nodes'] == 1


def test_duplicate_yaml_rejected(tmp_path):
    path = tmp_path / 'bad.yaml'
    path.write_text('id: a\nid: b\n')
    with pytest.raises(ForgeError, match='Duplicate'):
        builds.load(path)


def test_root_recipe_validates_and_contains_manual_build_fixes(tmp_path):
    path = Path(__file__).parents[1] / 'builds/root-build.yaml'
    if not path.exists():
        pytest.skip('Independent Release recipe sources not included in distribution')
    value = builds.load(path)['config']
    root = next(s for s in value['steps'] if s['uses'] == 'cmake.project')['with']
    assert 'packages.libmd.prefix' in str(root['paths']['LIBRARY_PATH'])
    assert 'packages.fontconfig.prefix' in str(root['paths']['CPATH'])
    assert 'packages.mesa-glu.prefix' in str(root['paths']['CPATH'])
    assert 'packages.openssl.prefix' in str(root['export']['paths']['LD_LIBRARY_PATH'])
    assert root['cache']['CMAKE_INSTALL_RPATH_USE_LINK_PATH']['value'] is True
    assert root['paths']['CMAKE_INCLUDE_PATH'] == root['paths']['CPATH']
    assert root['paths']['CMAKE_LIBRARY_PATH'] == root['paths']['LIBRARY_PATH']
    assert 'packages.openssl.prefix' in str(root['paths']['LD_LIBRARY_PATH'])
    assert root['cache']['OPENSSL_ROOT_DIR']['value'] == '${packages.openssl.prefix}'
    assert value['outputs']['installation']['publish_after'] == ['ROOT.command-2']
    assert builds.plan(ForgeHome(tmp_path/'unused'), str(path))['cmake_commands']


def test_artifact_runtime_works_in_a_fresh_child_environment(home, projects, tmp_path):
    _, consumer = projects
    result = builds.execute(home, consumer, jobs=2)
    assert result['status'] == 'passed', result
    output = result['outputs']['installation']
    activated = cmake.capture(Path(output['activation']))
    import subprocess
    completed = subprocess.run([output['prefix']+'/bin/consumer'],env=activated)
    assert completed.returncode == 0


def test_large_download_retries_partial_transfer(tmp_path, monkeypatch):
    from jarvis_forge import downloads
    import subprocess
    calls=[]
    def fake_run(argv, **kwargs):
        calls.append(argv)
        target=Path(argv[argv.index('--output')+1])
        target.write_bytes(b'first' if len(calls)==1 else b'complete')
        return subprocess.CompletedProcess(argv,18 if len(calls)==1 else 0)
    monkeypatch.setattr(downloads.subprocess,'run',fake_run)
    monkeypatch.setattr(downloads.shutil,'which',lambda name: '/usr/bin/curl' if name=='curl' else None)
    downloads.fetch('https://example.org/source.tar.gz',tmp_path/'archive',tmp_path/'download',timeout=7200)
    assert len(calls)==2 and '--continue-at' in calls[1]


def test_download_progress_uses_total_size_after_resuming(tmp_path, monkeypatch):
    import shlex
    from jarvis_forge import downloads

    curl = tmp_path / "curl"
    curl.write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " - \"$@\" <<'PY'\n"
        "from pathlib import Path\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "target = Path(args[args.index('--output') + 1])\n"
        "headers = Path(args[args.index('--dump-header') + 1])\n"
        "if '--continue-at' not in args:\n"
        "    headers.write_bytes(b'HTTP/1.1 302 Found\\r\\nContent-Length: 2\\r\\n\\r\\n'"
        " b'HTTP/2 200\\r\\nContent-Length: 10\\r\\n\\r\\n')\n"
        "    target.write_bytes(b'abcde')\n"
        "    print('interrupted', file=sys.stderr)\n"
        "    sys.exit(18)\n"
        "headers.write_bytes(b'HTTP/2 206\\r\\nContent-Length: 5\\r\\nContent-Range: bytes 5-9/10\\r\\n\\r\\n')\n"
        "with target.open('ab') as out: out.write(b'fghij')\n"
        "print('resumed', file=sys.stderr)\n"
        "PY\n"
    )
    curl.chmod(0o700)
    monkeypatch.setattr(downloads.shutil, "which", lambda name: str(curl) if name == "curl" else None)
    sizes = []
    target = tmp_path / "archive"
    downloads.fetch("https://example.org/source.tar.gz", target, tmp_path / "download",
                    timeout=7200, progress=lambda done, total: sizes.append((done, total)))
    assert target.read_bytes() == b"abcdefghij"
    assert (5, 10) in sizes and sizes[-1] == (10, 10)
    assert (tmp_path / "download.stderr.log").read_text() == "interrupted\nresumed\n"


def test_compact_library_and_calculator_cards_share_installation(home, projects, tmp_path):
    producer, consumer = projects
    library=tmp_path/'library'; library.mkdir()
    template=yaml.safe_load(producer.read_text())
    (library/'tiny.yaml').write_text(yaml.safe_dump({'Library':{'name':'Tiny','version':'1.0','workflow':template}}))
    consumer_source=yaml.safe_load(consumer.read_text())['sources']['consumer']['path']
    compact=tmp_path/'compact.yaml'
    compact.write_text(yaml.safe_dump({'Workflow':{'name':'combined'},
        'LibDeps':{'Modules':[{'name':'Tiny','recipe':'tiny','version':'1.0'}]},
        'Calculators':{'Modules':[{'name':'Consumer','path':consumer_source,'required_modules':['Tiny'],
            'installation':{'cmake':{'options':{'CMAKE_BUILD_TYPE':'Release'}}},
            'execution':{'commands':[['${path}/bin/consumer']]}}]}}))
    loaded=builds.load(compact)
    assert loaded['config']['parameters']['cxx_standard']==17
    assert 'installation' in loaded['config']['outputs']
    assert loaded['card_sources']
    result=builds.execute(home,compact,jobs=2)
    assert result['status']=='passed',result
    assert Path(result['outputs']['installation']['prefix'],'bin/consumer').is_file()


def test_compact_root_overrides_and_default_provenance():
    path=Path(__file__).parents[1]/'builds/root-build.yaml'
    if not path.exists(): pytest.skip('Independent recipe checkout needed')
    loaded=builds.load(path)
    raw=loaded['raw_config']
    root=raw['build']
    assert 'libbsd' in raw['dependencies']
    assert root['source']['sha256']
    options = root['cmake']['options']
    assert options['all'] is True
    assert options['fcgi'] is False and options['dcache'] is False and options['cuda'] is False
    assert options['qt6web'] is False and options['tmva-gpu'] is False and options['tmva-cudnn'] is False
    assert 'fail-on-missing' not in options and 'xrootd' not in options
    assert 'recipe' not in root and 'home' not in str(raw.keys())
    effective=next(s for s in loaded['ordered'] if s['uses']=='cmake.project')['with']
    assert effective['cache']['CMAKE_BUILD_TYPE']['value']=='Release'
    assert loaded['sha256'] != loaded['raw_sha256']
    assert raw['checks']['before']['python']['modules'] == ['numpy']
    assert raw['checks']['after']['python']['modules'] == ['ROOT']
    assert not any('<<' in str(value) for value in raw['checks'].values())
    assert any(p.get('path') == 'scripts/root_acceptance.py' for p in loaded['card_sources'])


def test_hep_style_process_log_preserves_paths_and_is_plain(tmp_path):
    from datetime import datetime
    from jarvis_forge.build_logging import ProcessLog, format_record
    message='/a/very long path/file.yaml'
    text=format_record(message,timestamp=datetime(2026,11,5,10,20,30,123456))
    assert text.startswith('\n·•· Jarvis-Forge.Build \n\t-> 11-05 10:20:30.123 - [INFO] >>> \n')
    assert message in text and '\x1b' not in text
    log=tmp_path/'build.log'; seen=[]
    ProcessLog(log,'Jarvis-Forge.Checker',seen.append)('check passed')
    assert '·•· Jarvis-Forge.Checker' in log.read_text()
    assert len(seen)==1


def test_self_contained_release_card_can_be_edited_offline(home,tmp_path):
    source=Path(__file__).parents[1]/'builds'
    if not source.exists(): pytest.skip('Recipe checkout required')
    from tools.build_project_assets import build
    assets=tmp_path/'assets'
    built=build('compact-v1',assets,source=source,base_url=assets.as_uri())
    assert built['libraries']==[]
    build_catalog.update(home,(assets/'builds.json').as_uri())
    cached=build_catalog.resolve(home,'root-build')
    editable=tmp_path/'my-root.yaml'; build_catalog.copy_bundle(cached, editable, home=home)
    config=yaml.safe_load(editable.read_text())
    config['build']['cmake']['options'].update({'CMAKE_BUILD_TYPE':'Debug','CMAKE_CXX_STANDARD':20})
    editable.write_text(yaml.safe_dump(config))
    shutil.rmtree(assets)
    expanded=builds.load(editable,home=home)
    root=next(s for s in expanded['ordered'] if s['uses']=='cmake.project')['with']
    assert root['cache']['CMAKE_BUILD_TYPE']=={'type':'STRING','value':'Debug'}
    assert expanded['config']['parameters']['cxx_standard']==20
    assert expanded['config']['sources']['ROOT']['sha256']


def test_standard_and_companion_checks_run_and_resume_frozen_script(home, tmp_path):
    marker = tmp_path / 'allow-success'
    script = tmp_path / 'acceptance.py'
    content = 'from pathlib import Path\nimport sys\nprint("companion check")\nassert Path(sys.argv[1]).is_file()\n'
    script.write_text(content)
    card = tmp_path / 'card.yaml'
    card.write_text(yaml.safe_dump({'name': 'checks-only', 'checks': {'after': {
        'python': {'version': '>=3.10', 'modules': ['json']},
        'scripts': [{'path': 'acceptance.py', 'args': [str(marker)]}]}}}))
    first = builds.execute(home, card)
    assert first['status'] == 'failed', first
    assert list(Path(first['attempt']).rglob('checks.json'))
    assert list(Path(first['attempt']).rglob('scripts/acceptance.py'))[0].read_text() == content
    script.write_text('raise RuntimeError("original must not run")')
    card.write_text('invalid original yaml')
    marker.touch()
    resumed = builds.execute(home, resume=first['run_id'])
    assert resumed['status'] == 'passed', resumed
    assert 'companion check' in ''.join(p.read_text() for p in Path(resumed['attempt']).rglob('stdout.log'))


def test_failed_standard_preflight_stops_downstream(home, tmp_path):
    card = tmp_path / 'card.yaml'
    script = tmp_path / 'never.py'
    script.write_text('raise RuntimeError("must never run")')
    card.write_text(yaml.safe_dump({'name': 'preflight', 'checks': {
        'before': {'python': {'modules': ['forge_absent_657194']}},
        'after': {'scripts': ['never.py']}}}))
    result = builds.execute(home, card)
    assert result['status'] == 'failed'
    assert 'forge_absent_657194' in result['error']
    assert len(list(Path(result['attempt']).rglob('command-*.json'))) <= 1


def test_release_script_update_gets_distinct_snapshot_and_rejects_tampering(home, tmp_path):
    from tools.build_project_assets import build
    source = tmp_path / 'source'; source.mkdir()
    script = source / 'acceptance.py'; script.write_text('print("one")\n')
    card = source / 'test.yaml'
    card.write_text(yaml.safe_dump({'name': 'release-test', 'checks': {'after': {'scripts': ['acceptance.py']}}}))
    def release(revision):
        assets = tmp_path / revision
        build(revision, assets, source=source, base_url=assets.as_uri())
        build_catalog.update(home, (assets / 'builds.json').as_uri())
        return build_catalog.resolve(home, 'release-test'), assets
    first, _ = release('one')
    original_sha = builds.load(first)['sha256']
    script.write_text('print("two")\n')
    second, assets = release('two')
    assert first.parent != second.parent
    assert digest(first) == digest(second)
    assert builds.load(second)['sha256'] != original_sha
    assert (first.parent / 'acceptance.py').read_text() == 'print("one")\n'
    editable = tmp_path / 'editable/card.yaml'
    build_catalog.copy_bundle(second, editable)
    assert (editable.parent / 'acceptance.py').read_text() == 'print("two")\n'
    shutil.rmtree(assets)
    assert builds.load(editable)['config']['id'] == 'release-test'
    (second.parent / 'acceptance.py').write_text('tampered')
    with pytest.raises(ForgeError, match='companion script'):
        build_catalog.resolve(home, 'release-test')


def test_release_rejects_bad_companion_checksum_and_keeps_index(home, tmp_path):
    from tools.build_project_assets import build
    source = tmp_path / 'source'; source.mkdir()
    (source / 'test.py').write_text('print("test")')
    (source / 'test.yaml').write_text(yaml.safe_dump({'name':'test','checks':{'after':{'scripts':['test.py']}}}))
    assets = tmp_path / 'assets'
    index = build('test', assets, source=source, base_url=assets.as_uri())
    url = (assets / 'builds.json').as_uri()
    build_catalog.update(home, url)
    original = (home.root / 'builds/catalog/index.json').read_bytes()
    companion = index['builds'][0]['files'][0]
    next(assets.glob('*.py')).write_text('tampered')
    with pytest.raises(ForgeError, match='script SHA-256 mismatch'):
        build_catalog.update(home, url)
    assert (home.root / 'builds/catalog/index.json').read_bytes() == original
    companion['path'] = '../outside.py'
    (assets / 'builds.json').write_text(json.dumps(index))
    with pytest.raises(ForgeError, match='relative Python'):
        build_catalog.update(home, url)


def test_fetch_bundle_refuses_script_collision_before_writing_yaml(tmp_path):
    source = tmp_path / 'source'; source.mkdir()
    (source / 'test.py').write_text('print("expected")')
    card = source / 'test.yaml'
    card.write_text(yaml.safe_dump({'name':'test','checks':{'after':{'scripts':['test.py']}}}))
    output = tmp_path / 'editable/test.yaml'; output.parent.mkdir()
    (output.parent / 'test.py').write_text('print("user content")')
    with pytest.raises(ForgeError, match='Refusing to overwrite'):
        build_catalog.copy_bundle(card, output)
    assert not output.exists()
    assert 'user content' in (output.parent / 'test.py').read_text()


@pytest.mark.parametrize('relative', ['../outside.py', '/tmp/outside.py', 'scripts/check.sh'])
def test_invalid_companion_paths_rejected(tmp_path, relative):
    card = tmp_path / 'card.yaml'
    card.write_text(yaml.safe_dump({'name':'test','checks':{'after':{'scripts':[relative]}}}))
    with pytest.raises(ForgeError, match='relative'):
        builds.load(card)


def test_compact_command_cwd_and_hep_command_mappings(home,tmp_path):
    path=tmp_path/'commands.yaml'
    path.write_text(yaml.safe_dump({'Workflow':{'name':'commands'},'Calculators':{'Modules':[{
        'name':'Echo','path':'&J','execution':{'commands':[
            [sys.executable,'-c',"from pathlib import Path; Path('nested').mkdir(exist_ok=True)"],
            'cd nested',
            {'cmd':[sys.executable,'-c',"from pathlib import Path; Path('done').write_text('yes')"]}
        ]}}]}}))
    result=builds.execute(home,path)
    assert result['status']=='passed',result
    assert (tmp_path/'nested/done').read_text()=='yes'


def test_show_compact_card_does_not_require_library_download(tmp_path,capsys):
    path=tmp_path/'root.yaml'
    path.write_text('Workflow:\n  name: root-build\nLibDeps:\n  Modules:\n    - name: ROOT\n      recipe: root\n      version: "6.40.04"\n')
    home=tmp_path/'absent'
    assert main(['--home',str(home),'build','show',str(path)])==0
    shown=capsys.readouterr().out
    assert 'ROOT' in shown and '6.40.04' in shown and 'recipe: root' in shown
    assert 'steps:' not in shown and 'LibDeps:' not in shown
    assert not home.exists()


def test_build_show_summarizes_root_recipe(tmp_path, capsys):
    root = Path(__file__).parents[1] / "builds/root-build.yaml"
    home = tmp_path / "absent"
    assert main(["--home", str(home), "build", "show", str(root)]) == 0
    shown = capsys.readouterr().out
    assert "root-build" in shown and "ROOT 6.40.04" in shown
    assert "-Dall=ON" in shown and "Off: fcgi, dcache, r, qt6web, cuda, tmva-gpu, tmva-cudnn" in shown
    assert "Required features: pyroot, roofit, opengl, mathmore, arrow, davix, vecgeom, pythia8" in shown
    assert "Apt packages (" in shown and "pythia8" in shown
    assert "name: root-build" not in shown and "fail-on-missing" not in shown
    assert not home.exists()
    assert main(["--home", str(home), "build", "show", str(root), "--expanded"]) == 0
    expanded = capsys.readouterr().out
    assert "Steps (" in expanded and "apt.install" in expanded and "cmake.project" in expanded
    assert "-Dall=ON" in expanded and "phases configure, build, install" in expanded
    assert not home.exists()
    assert main(["--home", str(home), "--json", "build", "show", str(root)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["build"]["build"]["cmake"]["options"]["all"] is True


def test_package_install_uses_active_apt_profile(home, tmp_path, monkeypatch):
    from jarvis_forge import spack_backend
    seen = {}
    monkeypatch.setattr(spack_backend, "bootstrap", fake_bootstrap)

    def install(target, action, specs, **kwargs):
        seen["root"] = target.root
        seen["action"] = action
        seen["specs"] = list(specs)
        activation = target.root / "activate.sh"
        activation.write_text(":\n")
        lock = target.root / "spack.lock"
        lock.write_text("{}")
        return {"activation": str(activation), "lockfile": str(lock), "packages": []}

    monkeypatch.setattr(spack_backend, "transact", install)
    path = tmp_path / "deps.yaml"
    path.write_text(yaml.safe_dump({"name": "deps", "dependencies": ["gsl"], "checks": {"after": [["true"]]}}))
    result = builds.execute(home, path)
    assert result["status"] == "passed", result
    assert seen == {"root": home.root / "apt/profiles/spack-default", "action": "install", "specs": ["gsl"]}
    assert read_json(home.root / "apt/selection.json")["id"] == "spack-default"
    assert not list((home.root / "apt/profiles").glob("spack-build-*"))


@pytest.mark.parametrize("verbose", [False, True])
def test_build_package_install_uses_apt_progress_display(home, tmp_path, monkeypatch, capfdbinary, verbose):
    from jarvis_forge import spack_backend
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(spack_backend, "bootstrap", fake_bootstrap)
    tree = b"==> Concretized dependency tree: gsl@2.8\n"
    status = b"[+] abcdefg gsl@2.8 /prefix\n"

    def install(target, action, specs, **kwargs):
        progress = kwargs["progress"]
        assert progress.verbose is verbose
        progress("[1/4] Resolve dependencies")
        progress("Spack concretize: concretize")
        progress.output("stdout", tree)
        progress.output("stdout", status)
        progress.installation_plan([])
        progress("[4/4] Save environment")
        activation = target.root / "activate.sh"
        activation.write_text(":\n")
        lock = target.root / "spack.lock"
        lock.write_text("{}")
        return {"activation": str(activation), "lockfile": str(lock), "packages": []}

    monkeypatch.setattr(spack_backend, "transact", install)
    path = tmp_path / "deps.yaml"
    path.write_text(yaml.safe_dump({"name": "deps", "dependencies": ["gsl"], "checks": {"after": [["true"]]}}))
    screen = []
    result = builds.execute(home, path, verbose=verbose, progress=lambda message: screen.append(message))
    assert result["status"] == "passed", result
    output = capfdbinary.readouterr()
    assert b"[1/4] Resolve dependencies" in output.err
    assert b"[4/4] Save environment" in output.err
    assert (tree in output.err) is verbose
    assert (b"Spack concretize: concretize" in output.err) is verbose
    assert (status in output.err) is verbose
    assert not any("Spack concretize" in message or "Concretized dependency tree" in message for message in screen)
    log = next((home.root / "runs").glob("*/attempts/*/build.log"))
    recorded = log.read_text()
    assert "Jarvis-Forge.APT" in recorded and "Resolve dependencies" in recorded


def test_inline_package_archive_and_cmake_paths(home,projects,tmp_path,monkeypatch):
    from jarvis_forge import spack_backend
    import tarfile
    producer,_=projects
    source=Path(yaml.safe_load(producer.read_text())['sources']['tiny']['path'])
    (source/'tiny.cpp').write_text('#include <dependency.h>\nint forge_value(){return DEP_VALUE;}\n')
    archive=tmp_path/'source.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        tar.add(source,arcname='source')
    package=tmp_path/'dependency'; (package/'include').mkdir(parents=True); (package/'lib').mkdir()
    (package/'include/dependency.h').write_text('#define DEP_VALUE 42\n')
    monkeypatch.setattr(spack_backend,'bootstrap',fake_bootstrap)
    def install(target,*args,**kwargs):
        activation=target.root/'activate.sh'; activation.write_text(':\n')
        lock=target.root/'spack.lock'; lock.write_text('{}')
        return {'activation':str(activation),'lockfile':str(lock),'packages':[{
            'name':'dep','version':'1','hash':'dep-hash','prefix':str(package)}]}
    monkeypatch.setattr(spack_backend,'transact',install)
    path=tmp_path/'inline.yaml'
    path.write_text(yaml.safe_dump({'Workflow':{'name':'inline'},'LibDeps':{'Modules':[{
        'name':'Packages','installation':{'packages':['dep']}}]},'Calculators':{'Modules':[{
        'name':'Tiny','version':'1.0','required_modules':['Packages'],
        'installation':{'source':{'url':archive.as_uri(),'sha256':digest(archive),'subdir':'source'},
                        'cmake':{'options':{'CMAKE_CXX_STANDARD':17},'includes':['dep'],
                                 'libraries':['dep'],'runtime':['dep']}}}]}}))
    result=builds.execute(home,path,jobs=2)
    assert result['status']=='passed',result
    output=result['outputs']['installation']
    assert Path(output['prefix'],'include/value.h').is_file()
    activation=Path(output['activation']).read_text()
    assert str(package/'lib64') in activation and str(package/'lib') in activation
    assert read_json(home.root/'apt/selection.json')['id'] == 'spack-default'
    assert not list((home.root/'apt/profiles').glob('spack-build-*'))


def test_package_paths_reach_cmake_discovery_and_nested_build(home, projects, tmp_path, monkeypatch):
    """Reproduce ROOT's nested header/-l search failures with real compiler commands."""
    import subprocess
    from jarvis_forge import spack_backend
    producer, _ = projects
    source = Path(yaml.safe_load(producer.read_text())['sources']['tiny']['path'])
    cmakelists = source / 'CMakeLists.txt'
    cmakelists.write_text(cmakelists.read_text().replace('SHARED tiny.cpp', 'STATIC tiny.cpp'))
    dependency = builds.execute(home, producer)
    assert dependency['status'] == 'passed', dependency
    prefix = dependency['outputs']['installation']['prefix']
    monkeypatch.setattr(spack_backend, 'bootstrap', fake_bootstrap)
    def install(target, *args, **kwargs):
        # No Spack search-path activation: Forge must provide the selected prefix itself.
        activation = target.root / 'activate.sh'; activation.write_text(':\n')
        lock = target.root / 'spack.lock'; lock.write_text('{}')
        return {'activation': str(activation), 'lockfile': str(lock), 'packages': [
            {'name':'dep', 'version':'1', 'hash':'dep-hash', 'prefix':prefix}]}
    monkeypatch.setattr(spack_backend, 'transact', install)
    project = tmp_path / 'nested-source'; (project / 'nested').mkdir(parents=True)
    (project / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.20)
project(NestedParent LANGUAGES CXX)
find_path(VALUE_INCLUDE value.h REQUIRED)
find_library(TINY_LIBRARY tiny REQUIRED)
find_package(ForgeTiny CONFIG REQUIRED)
add_custom_target(nested ALL
 COMMAND "${CMAKE_COMMAND}" -S "${CMAKE_CURRENT_SOURCE_DIR}/nested" -B "${CMAKE_CURRENT_BINARY_DIR}/nested"
 COMMAND "${CMAKE_COMMAND}" --build "${CMAKE_CURRENT_BINARY_DIR}/nested" --parallel 2
 VERBATIM)
install(PROGRAMS "${CMAKE_CURRENT_BINARY_DIR}/nested/raw" DESTINATION bin)
''')
    (project / 'nested/CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.20)
project(RawNested LANGUAGES CXX)
add_executable(raw main.cpp)
# Deliberately use a bare -ltiny and no target include path, like upstream nested builds.
target_link_libraries(raw PRIVATE tiny)
''')
    (project / 'nested/main.cpp').write_text('#include <value.h>\nint main(){return forge_value()==42?0:1;}\n')
    card = tmp_path / 'nested.yaml'
    card.write_text(yaml.safe_dump({'name':'nested-build', 'dependencies':['dep'], 'build':{
        'name':'Nested', 'source':str(project), 'cmake':{
            'options':{'CMAKE_CXX_STANDARD':17}, 'includes':['dep'], 'libraries':['dep'], 'runtime':['dep']},
        'required_files':['bin/raw']}}))
    result = builds.execute(home, card, jobs=2)
    assert result['status'] == 'passed', result
    subprocess.run([result['outputs']['installation']['prefix'] + '/bin/raw'], check=True)
    report = next(Path(result['attempt']).rglob('dependency-paths.json'))
    paths = read_json(report)
    assert prefix in paths['CMAKE_PREFIX_PATH']
    assert str(Path(prefix) / 'include') in paths['CPATH']
    assert str(Path(prefix) / 'lib') in paths['LIBRARY_PATH']
    assert str(Path(prefix) / 'lib') in paths['LD_LIBRARY_PATH']


def test_native_project_card_builds_and_exports_with_defaults(home,projects,tmp_path):
    producer,_=projects
    source=yaml.safe_load(producer.read_text())['sources']['tiny']['path']
    path=tmp_path/'native.yaml'
    path.write_text(yaml.safe_dump({'name':'native-build','build':{'name':'Tiny','version':'1.0','source':source},
        'checks':{'before':[['true']],'after':[['true']]}}))
    result=builds.execute(home,path,jobs=2)
    assert result['status']=='passed',result
    assert Path(result['outputs']['installation']['prefix'],'include/value.h').is_file()
    assert builds.load(path)['raw_config']['name']=='native-build'


def test_native_card_consumes_another_workflow_output(home,projects,tmp_path):
    producer,consumer=projects
    source=yaml.safe_load(consumer.read_text())['sources']['consumer']['path']
    path=tmp_path/'native-consumer.yaml'
    path.write_text(yaml.safe_dump({'name':'native-consumer',
        'requires':{'tiny':{'build':str(producer),'version':'>=1,<2'}},
        'build':{'name':'Consumer','source':source,'cmake':{'options':{'CMAKE_CXX_STANDARD':17}}},
        'checks':{'after':[['${path}/bin/consumer']]}}))
    result=builds.execute(home,path,jobs=2)
    assert result['status']=='passed',result
    assert result['outputs']['installation']['dependencies']


def workspace_release(tmp_path, projects):
    import tarfile
    from tools.build_project_assets import build
    producer, _ = projects
    project = Path(yaml.safe_load(producer.read_text())['sources']['tiny']['path'])
    archive = tmp_path / 'tiny.tar.gz'
    with tarfile.open(archive, 'w:gz') as stream:
        stream.add(project, arcname='tiny')
    source = tmp_path / 'release-source'; (source / 'scripts').mkdir(parents=True)
    (source / 'scripts/accept.py').write_text('from pathlib import Path\nimport sys\nassert Path(sys.argv[1], "include/value.h").is_file()\n')
    (source / 'tiny.yaml').write_text(yaml.safe_dump({'name':'workspace-tiny', 'build':{
        'name':'Tiny', 'version':'1.0', 'source':{'url':archive.as_uri(), 'sha256':digest(archive), 'subdir':'tiny'},
        'cmake':{'options':{'CMAKE_CXX_STANDARD':17}}, 'required_files':['include/value.h']},
        'checks':{'after':{'scripts':[{'path':'scripts/accept.py', 'args':['${path}']}]}}}))
    assets = tmp_path / 'release-assets'
    build('workspace-v1', assets, source=source, base_url=assets.as_uri())
    return assets / 'builds.json'


def test_release_workspace_keeps_download_source_build_install_and_activation(home, projects, tmp_path, monkeypatch, capsys):
    index = workspace_release(tmp_path, projects)
    update = build_catalog.update
    calls = []
    def acquire_release(target):
        calls.append('download')
        return update(target, index.as_uri())
    monkeypatch.setattr(build_catalog, 'update', acquire_release)
    directory = tmp_path / 'my ROOT workspace'
    assert main(['--home',str(home.root),'build','fetch','workspace-tiny','--directory',str(directory),'--json']) == 0
    fetched = json.loads(capsys.readouterr().out)
    assert fetched['workspace'] == str(directory)
    assert calls == ['download']
    assert (directory / 'scripts/accept.py').is_file()
    assert builds.plan(home, str(directory))['workspace'] == str(directory)
    assert main(['--home',str(home.root),'build','run',str(directory),'--jobs','2','--json']) == 0
    result = json.loads(capsys.readouterr().out)
    prefix = Path(result['outputs'][0]['prefix'])
    instance = prefix.parent.parent
    assert instance == directory / 'instances/Tiny-1.0'
    assert (instance / 'sources/Tiny/tiny/CMakeLists.txt').is_file()
    assert (instance / 'build/Tiny.install/CMakeCache.txt').is_file()
    assert (instance / 'install/Tiny.install/include/value.h').is_file()
    assert list((instance / 'downloads').iterdir())
    assert (directory / 'runs' / result['run_id']).resolve() == home.run_path(result['run_id']).resolve()
    assert cmake.capture(directory / 'activate.sh')['CMAKE_PREFIX_PATH'].split(':')[0] == str(prefix)
    original = (directory / 'activate.sh').read_bytes()
    current = read_json(directory / '.jforge/current.json')
    identity = builds.artifact(home, current['artifact_id'])['instance']
    assert read_json(instance / '.jforge-instance.json')['instance'] == identity
    from jarvis_forge.build_workspace import create
    other = tmp_path / 'other-workspace'
    create(home, build_catalog.resolve(home, 'workspace-tiny'), other)
    separate = builds.execute(home, str(other))
    assert separate['status'] == 'passed', separate
    assert separate['outputs']['installation']['prefix'] != str(prefix)
    assert Path(separate['outputs']['installation']['prefix']).is_relative_to(other)
    (directory / 'scripts/accept.py').write_text('raise RuntimeError("acceptance failed")\n')
    failed = builds.execute(home, str(directory))
    assert failed['status'] == 'failed'
    assert (directory / 'instances/Tiny-1.0-2').is_dir()
    assert (directory / 'activate.sh').read_bytes() == original
    assert read_json(directory / '.jforge/current.json') == current
    builds.artifact(home, current['artifact_id'])
    assert calls == ['download']  # Local workspaces remain usable without the Release service.


@pytest.mark.parametrize("legacy", [False, True])
def test_workspace_resume_preserves_directory_and_script_snapshot(home, projects, tmp_path, monkeypatch, legacy):
    from jarvis_forge.build_workspace import create
    index = workspace_release(tmp_path, projects)
    build_catalog.update(home, index.as_uri())
    directory = tmp_path / 'workspace'
    entry = create(home, build_catalog.resolve(home, 'workspace-tiny'), directory)
    marker = tmp_path / 'allow-success'
    script = directory / 'scripts/accept.py'
    script.write_text('from pathlib import Path\nassert Path(' + repr(str(marker)) + ').exists()\n')
    def legacy_directory(base, config, instance, *, installs=None):
        directory = base / instance
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    with monkeypatch.context() as patch:
        if legacy:
            patch.setattr(builds, "_instance_directory", legacy_directory)
        first = builds.execute(home, str(directory))
    assert first['status'] == 'failed'
    assert not (directory / 'activate.sh').exists()
    instance = next((directory / 'instances').iterdir())
    entry.write_text('invalid edited yaml')
    script.unlink()
    marker.touch()
    resumed = builds.execute(home, resume=first['run_id'])
    assert resumed['status'] == 'passed', resumed
    assert Path(resumed['outputs']['installation']['prefix']).is_relative_to(instance)
    assert len(list((directory / 'instances').iterdir())) == 1
    assert (directory / 'activate.sh').is_file()
    assert home.show(first['run_id'])['status'] == 'passed'


def test_run_can_fetch_workspace_in_one_command_and_reuse_offline(home, projects, tmp_path, monkeypatch, capsys):
    index = workspace_release(tmp_path, projects)
    update = build_catalog.update
    monkeypatch.setattr(build_catalog, 'update', lambda target: update(target, index.as_uri()))
    directory = tmp_path / 'one-command'
    argv = ['--home',str(home.root),'build','run','workspace-tiny','--directory',str(directory),'--json']
    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(build_catalog, 'update', lambda target: pytest.fail('Offline rerun must not access Release'))
    assert main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second['outputs'][0]['artifact_id'] == first['outputs'][0]['artifact_id']
    assert second['nodes'][0]['status'] == 'reused'
    assert main(['--home',str(home.root),'build','run','other','--directory',str(directory),'--json']) == 2
    assert json.loads(capsys.readouterr().out)['ok'] is False


def test_workspace_fetch_rejects_collisions_and_read_only_plan_stays_offline(home, tmp_path, monkeypatch, capsys):
    from jarvis_forge.build_workspace import create
    source = save(tmp_path / 'flow.yaml', id='test', steps=[{'id':'x','uses':'command','with':{'argv':['true']}}])
    destination = tmp_path / 'occupied'; destination.mkdir()
    (destination / 'activate.sh').write_text('user content')
    with pytest.raises(ForgeError, match='already contains'):
        create(home, source, destination)
    assert not (destination / 'build.yaml').exists()
    monkeypatch.setattr(build_catalog, 'update', lambda target: pytest.fail('Read-only plan must stay offline'))
    with pytest.raises(ForgeError, match='not found'):
        builds.plan(home, 'not-cached')
    assert main(['--home',str(home.root),'build','fetch','not-cached','--json']) == 2
    assert 'exactly one' in json.loads(capsys.readouterr().out)['error']['message']
