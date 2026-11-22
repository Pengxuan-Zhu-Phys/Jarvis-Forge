"""Disposable platform/profile records backed by existing local build tools.

No package installation, user shell changes, network or compiler bootstrap.
"""
from pathlib import Path
import shlex
import shutil

from jarvis_forge import unified_profiles as profiles
from jarvis_forge.storage import ForgeHome,digest,read_json,write_json,write_text


def project_profile(base,name='default',*,select=True):
    base.initialize()
    scope=profiles._scoped_home(base,'profiles',name)
    platform_home=ForgeHome(scope.root/'tools')
    profiles._initialize_scope(base,scope)
    platform=name
    if not (platform_home.root/'metadata.json').exists():
        profiles._initialize_scope(base,platform_home)
        drivers={key:shutil.which(command) or '/usr/bin/'+command for key,command in [('CC','cc'),('CXX','c++')]}
        chain={'schema_version':3,'id':platform,'version':'test','target':None,'prefix':'/usr',
               'profile':str(platform_home.root),'drivers':drivers,'tools':{},'runtimes':{},'external':True,
               'fingerprints':{path:digest(Path(path)) for path in drivers.values()}}
        script=platform_home.root/'activate.sh'
        write_text(script,''.join(f'export {key}={shlex.quote(path)}\n' for key,path in drivers.items())+'return 0\n')
        write_json(platform_home.root/'metadata.json',{'schema_version':3,'kind':'tool-suite','id':platform,
                    'preset':'ubuntu24','revision':1,'backend':'spack','host':profiles.host(),'target':None,
                    'status':'ready','activation':str(script),'toolchain':chain})
    platform_data=read_json(platform_home.root/'metadata.json')
    profiles._initialize_scope(base,scope)
    python=profiles._python()
    write_json(scope.root/'metadata.json',{'schema_version':3,'kind':'profile','model':'builtin-profile/1','id':name,'name':name,
                'preset':'ubuntu24','backend':'spack','host':profiles.host(),'target':None,
                'status':'ready','python':python,'toolchain':platform_data['toolchain'],
                'tools':platform_data,'stage':str(scope.root/'stage'),'apps':[]})
    runtime=scope.root/'spack/runtime';runtime.mkdir(parents=True,exist_ok=True)
    write_text(runtime/'activate.sh','return 0\n')
    write_text(scope.root/'spack/spack.yaml','spack: {specs: []}\n')
    write_json(scope.root/'spack/spack.lock',{'_meta':{'file-type':'spack-lockfile','lockfile-version':5},'roots':[],'concrete_specs':{}})
    engine=base.root/'spack/spack-1.2.2/bin/spack'
    if not engine.exists():write_text(engine,'#!/bin/sh\nexit 1\n',mode=0o700)
    write_json(scope.root/'spack/backend.json',{'schema_version':1,'backend':'spack','python':python,'engine':str(engine.parent.parent),'extension_recipes':False})
    write_json(scope.root/'packages.json',{'schema_version':1,'backend':'spack','last_operation_id':'0001-initialize',
                'activation':str(runtime/'activate.sh'),'spack':{'roots':[],'packages':[],
                'environment':str(scope.root/'spack'),'activation':str(runtime/'activate.sh')}})
    profiles.refresh_activation(scope)
    if select:profiles.set_default(base,name)
    return scope
