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
