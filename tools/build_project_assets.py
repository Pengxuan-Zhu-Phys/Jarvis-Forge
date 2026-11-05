"""Build independent build Release assets; never upload or run a build."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys


def build(version, output, *, source=None, base_url='https://github.com/Pengxuan-Zhu-Phys/Jarvis-Forge/releases/download/builds'):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', version):
        raise ValueError('Invalid build revision')
    from jarvis_forge.builds import load
    source = source or Path(__file__).resolve().parents[1] / 'builds'
    if (output / 'builds.json').exists():
        raise FileExistsError(output / 'builds.json')
    output.mkdir(parents=True, exist_ok=True)
    rows, libraries = [], []
    from jarvis_forge.build_cards import read_library
    for file in sorted((source / 'library').glob('*.yaml')):
        read_library(file)
        sha = hashlib.sha256(file.read_bytes()).hexdigest()
        name = 'library-' + file.stem + '-' + version + '-' + sha[:12] + '.yaml'
        with (output / name).open('xb') as stream:
            stream.write(file.read_bytes())
        libraries.append({'id':file.stem,'revision':version,'url':base_url.rstrip('/')+'/'+name,'sha256':sha})
    for file in sorted(source.glob('*.yaml')):
        loaded = load(file)
        config = loaded['config']
        sha = hashlib.sha256(file.read_bytes()).hexdigest()
        name = config['id'] + '-' + version + '-' + sha[:12] + '.yaml'
        target = output / name
        with target.open('xb') as stream:
            stream.write(file.read_bytes())
        companions = []
        for resource in loaded['card_sources']:
            if 'script_id' not in resource:
                continue
            script_name = config['id'] + '-' + version + '-' + resource['sha256'] + '.py'
            target_script = output / script_name
            if not target_script.exists():
                target_script.write_bytes(resource['content'].encode('utf-8'))
            companions.append({'path':resource['path'], 'url':base_url.rstrip('/')+'/'+script_name, 'sha256':resource['sha256']})
        rows.append({'id':config['id'], 'revision':version, 'url':base_url.rstrip('/')+'/'+name, 'sha256':sha,
                     'description':config.get('description',''), 'files':companions})
    if not rows:
        raise ValueError('No build YAML files found')
    result = {'schema_version':1, 'requires_forge':'>=1.1.0', 'builds':rows, 'libraries':libraries}
    (output / 'builds.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version',required=True)
    parser.add_argument('--output',required=True,type=Path)
    options=parser.parse_args()
    print(json.dumps(build(options.version,options.output),indent=2))
