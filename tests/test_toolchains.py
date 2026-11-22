"""Platform compilers constrain the whole native dependency graph and preserve Python."""
from pathlib import Path
import os
import pytest
from jarvis_forge import toolchains
from jarvis_forge.storage import ForgeError
from profile_fixtures import project_profile
from jarvis_forge.storage import ForgeHome


def test_platform_binding_and_requirements_use_only_the_platform_compiler(tmp_path):
    base=ForgeHome(tmp_path/'forge');scope=project_profile(base)
    record=toolchains.binding(scope);record={**record,'version':'13.3.0','target':'x86_64','drivers':{**record['drivers'],'FC':'/usr/bin/gfortran'}}
    packages={'python':{'buildable':False}}
    toolchains.spack_requirements(record,packages)
    assert packages['gcc']['buildable'] is False
    assert packages['gcc']['externals'][0]['prefix']==record['prefix']
    assert packages['c']['require']==packages['cxx']['require']==packages['fortran']['require']==['gcc@=13.3.0']
    assert packages['all']['target']==['x86_64'] and packages['python']=={'buildable':False}
    expected={'name':'gcc','version':'13.3.0','external':True,'prefix':record['prefix']}
    toolchains.validate_plan(record,[expected])
    for bad in ({'external':False},{'version':'11.4.0'},{'prefix':'/other'}):
        with pytest.raises(ForgeError,match='Spack selected'):toolchains.validate_plan(record,[{**expected,**bad}])


def test_external_compiler_does_not_prepend_system_python_directory():
    record={'external':True,'drivers':{'CC':'/usr/bin/cc','CXX':'/usr/bin/c++'},'tools':{}}
    original={'PATH':'/pyenv/bin:/usr/bin:/bin','PYENV_VERSION':'3.13'}
    result=toolchains.environment(record,original)
    assert result['PATH']==original['PATH'] and result['PYENV_VERSION']=='3.13'
    assert result['CC']=='/usr/bin/cc'


def test_managed_compiler_tools_and_runtimes_are_available_without_python_overrides():
    record={'drivers':{'CC':'/forge/platforms/ubuntu24/pkg/gcc-13.3.0/bin/gcc'},'tools':{
        'cmake':{'commands':{'cmake':'/forge/platforms/ubuntu24/pkg/cmake-3.28.6/bin/cmake'}}},
        'runtimes':{'libgcc_s.so.1':'/forge/platforms/ubuntu24/pkg/gcc-13.3.0/lib64/libgcc_s.so.1'}}
    result=toolchains.environment(record,{'PATH':'/pyenv/bin:/usr/bin'})
    assert result['PATH'].endswith('/pyenv/bin:/usr/bin')
    cache={'CMAKE_INSTALL_RPATH':{'type':'STRING','value':'/project/lib'}}
    toolchains.cmake_runtime_paths(record,cache)
    assert cache['CMAKE_INSTALL_RPATH']['value'].endswith(';/project/lib')
    assert cache['CMAKE_BUILD_RPATH']['value'].endswith('gcc-13.3.0/lib64')


def test_independent_toolchain_install_and_binding_are_replaced(tmp_path):
    home=ForgeHome(tmp_path/'absent')
    with pytest.raises(ForgeError,match='removed'):toolchains.install(home,'gcc@13.3.0')
    with pytest.raises(ForgeError,match='platform'):toolchains.use(home,'gcc')
    assert not home.root.exists()


@pytest.mark.parametrize('value,expected', [
    (['/dep/a', '/dep path/b', '/dep/a', ''], ['/compiler/lib64', '/dep/a', '/dep path/b']),
    ('/dep/a;/dep path/b;/dep/a;;', '/compiler/lib64;/dep/a;/dep path/b'),
    ([], ['/compiler/lib64']),
    ('', '/compiler/lib64'),
    (['/dep/a;b'], ['/compiler/lib64', '/dep/a;b']),
    (r'/dep/a\;b;/dep/c', r'/compiler/lib64;/dep/a\;b;/dep/c'),
    ('$<IF:$<CONFIG:Debug>,/debug;/keep;/keep,/release>',
     '/compiler/lib64;$<IF:$<CONFIG:Debug>,/debug;/keep;/keep,/release>'),
    (['/compiler/lib64', '/dep/a'], ['/compiler/lib64', '/dep/a']),
    ('/compiler/lib64;/dep/a', '/compiler/lib64;/dep/a'),
    ('/compiler/lib64;/dep/a;/compiler/lib64', '/compiler/lib64;/dep/a'),
])
def test_cmake_rpath_merging_preserves_typed_values(value, expected):
    import copy
    from jarvis_forge import cmake
    record={'runtimes':{'gcc':'/compiler/lib64/libgcc_s.so','cxx':'/compiler/lib64/libstdc++.so'}}
    original=copy.deepcopy(value)
    cache={name:{'type':'STRING','value':value} for name in ('CMAKE_BUILD_RPATH','CMAKE_INSTALL_RPATH')}
    cache['UNRELATED']={'type':'STRING','value':['keep','this']}
    toolchains.cmake_runtime_paths(record,cache)
    for name in ('CMAKE_BUILD_RPATH','CMAKE_INSTALL_RPATH'):
        assert cache[name]['value']==expected
    assert value==original and cache['UNRELATED']['value']==['keep','this']
    once=copy.deepcopy(cache)
    toolchains.cmake_runtime_paths(record,cache)
    assert cache==once
    argv=cmake.commands({'cache':cache},Path('/src'),Path('/build'),Path('/install'))['configure']
    actual=next(a.split('=',1)[1] for a in argv if a.startswith('-DCMAKE_BUILD_RPATH:STRING='))
    if isinstance(expected,list):
        assert actual==(';'.join(expected) if ';' not in ''.join(expected) else r'/compiler/lib64;/dep/a\;b')
    else:
        assert actual==expected
    assert "['" not in actual and "', '" not in actual


@pytest.mark.parametrize('name',['CMAKE_BUILD_RPATH','CMAKE_INSTALL_RPATH'])
@pytest.mark.parametrize('value',[None,False,123,['/valid',123],[['/nested']]])
def test_invalid_rpath_values_fail_before_cmake(name,value):
    record={'runtimes':{'gcc':'/compiler/lib64/libgcc_s.so'}}
    with pytest.raises(ForgeError,match=name+' must be a string or a list of strings'):
        toolchains.cmake_runtime_paths(record,{name:{'type':'STRING','value':value}})


def test_cmake_rpath_reconfigure_replaces_malformed_cache_and_links(tmp_path):
    import copy
    import shutil
    import subprocess
    from jarvis_forge import cmake
    executable,cc=shutil.which('cmake'),shutil.which('cc')
    if not executable or not cc:
        pytest.skip('Needs local CMake and a C compiler for the tiny link regression')
    source=tmp_path/'source';source.mkdir()
    (source/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.18)\nproject(RpathProbe C)\nadd_executable(probe main.c)\ninstall(TARGETS probe RUNTIME DESTINATION bin)\n')
    (source/'main.c').write_text('#include <stdio.h>\nint main(void) { puts("rpath-ok"); return 0; }\n')
    runtime=tmp_path/'compiler libraries';runtime.mkdir()
    first=tmp_path/'dependency one/lib64';first.mkdir(parents=True)
    # Recipes can list lib and lib64 candidates: a missing RPATH directory
    # alone must not be converted into a linker input filename.
    second=tmp_path/'dependency two/lib'
    values=[str(first),str(second)]
    options={'executable':executable,'cache':{
        'CMAKE_C_COMPILER':{'type':'FILEPATH','value':cc},
        'CMAKE_BUILD_RPATH':{'type':'STRING','value':values},
        'CMAKE_INSTALL_RPATH':{'type':'STRING','value':list(values)}}}
    build,prefix=tmp_path/'build',tmp_path/'installed'
    env={k:v for k,v in os.environ.items() if k not in {'CC','CXX','FC','CFLAGS','CPPFLAGS','CXXFLAGS','LDFLAGS','LD_LIBRARY_PATH','LD_PRELOAD'}}
    def run(argv):
        p=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=60)
        assert p.returncode==0,p.stdout+p.stderr
        return p
    # Seed the exact malformed cache representation from the failed build.
    bad=copy.deepcopy(options)
    for name in ('CMAKE_BUILD_RPATH','CMAKE_INSTALL_RPATH'):
        bad['cache'][name]['value']=str(values)
    run(cmake.commands(bad,source,build,prefix)['configure'])
    assert "['" in (build/'CMakeCache.txt').read_text()
    marker=build/'retained-build-file';marker.write_text('keep')
    toolchains.cmake_runtime_paths({'runtimes':{'gcc':str(runtime/'libgcc_s.so')}},options['cache'])
    commands=cmake.commands(options,source,build,prefix)
    run(commands['configure']);run(commands['build']);run(commands['install'])
    assert run([str(prefix/'bin/probe')]).stdout.strip()=='rpath-ok'
    assert marker.read_text()=='keep'
    cache=(build/'CMakeCache.txt').read_text()
    expected=';'.join([str(runtime),*values])
    for name in ('CMAKE_BUILD_RPATH','CMAKE_INSTALL_RPATH'):
        assert f'{name}:STRING={expected}' in cache


@pytest.mark.parametrize('value', ['/dependency', ['/dependency']])
def test_compiler_runtime_directory_with_literal_semicolon_is_escaped_once(value):
    from jarvis_forge import cmake
    cache={'CMAKE_BUILD_RPATH':{'type':'STRING','value':value}}
    toolchains.cmake_runtime_paths({'runtimes':{'gcc':'/compiler;set/lib/libgcc_s.so'}},cache)
    argv=cmake.commands({'cache':cache},Path('/s'),Path('/b'),Path('/p'))['configure']
    flag=next(a for a in argv if a.startswith('-DCMAKE_BUILD_RPATH:'))
    assert flag=='-DCMAKE_BUILD_RPATH:STRING=/compiler'+chr(92)+';set/lib;/dependency'
