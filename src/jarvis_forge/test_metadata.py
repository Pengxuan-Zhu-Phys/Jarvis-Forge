"""Versioned metadata, strict static validation and binding against explicit discovery."""
from copy import deepcopy
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import re

import jsonschema
import yaml

from .storage import ForgeError
from .suite import UniqueLoader


def _load(path):
    try:
        return yaml.load(Path(path).read_text(encoding='utf-8'),Loader=UniqueLoader)
    except (OSError,ValueError,yaml.YAMLError) as exc:
        raise ForgeError(f'Cannot read test metadata {path}: {exc}') from exc


def _validate(value,kind):
    schema=json.loads(files('jarvis_forge').joinpath('schemas','test-metadata.schema.json').read_text())
    try:
        jsonschema.Draft202012Validator(schema['$defs'][kind]).validate(value)
    except jsonschema.ValidationError as exc:
        raise ForgeError(f'Invalid {kind} at {"/".join(map(str,exc.absolute_path))}: {exc.message}') from exc



def load(index,*,allow_missing=False):
    index=Path(index).resolve()
    if not index.exists() and allow_missing:
        return {'project_id':None,'definitions':[],'documents':[],'digest':None,'diagnostics':['Metadata index not supplied.']}
    top=_load(index)
    _validate(top,'index')
    definitions=[];seen=set();selectors=set();documents=[];hasher=hashlib.sha256()
    hasher.update(index.read_bytes())
    for name in top['documents']:
        path=(index.parent/name).resolve()
        if Path(name).is_absolute() or '\\' in name or not path.is_relative_to(index.parent) or path==index:
            raise ForgeError(f'Metadata document leaves its index directory: {name}')
        value=_load(path);_validate(value,'document')
        if value['project_id']!=top['project_id']: raise ForgeError(f'Metadata project mismatch in {name}.')
        hasher.update(name.encode()+b'\0'+path.read_bytes());documents.append(str(path))
        scope=value.get('scope',{})
        source=scope.get('source')
        if source and (Path(source).is_absolute() or '..' in Path(source).parts or '\\' in source):
            raise ForgeError(f'Metadata source leaves its project: {source}')
        for raw in value['definitions']:
            row=deepcopy(raw)
            if row['id'] in seen: raise ForgeError(f'Duplicate metadata definition ID: {row["id"]}')
            seen.add(row['id']);selector=(row['binding']['adapter'],row['binding']['selector'])
            file=Path(selector[1].split('::')[0])
            if selector[0]=='pytest' and (file.is_absolute() or '..' in file.parts or '\\' in selector[1]):
                raise ForgeError(f'Metadata selector leaves its project: {selector[1]}')
            if any(adapter==selector[0] and (native.split('[',1)[0]==selector[1] or selector[1].split('[',1)[0]==native) for adapter,native in selectors):
                raise ForgeError(f'Ambiguous metadata binding: {selector}')
            if selector in selectors: raise ForgeError(f'Ambiguous metadata binding: {selector}')
            selectors.add(selector)
            for field in ('owner','component','tags'):
                if field not in row and field in scope: row[field]=scope[field]
            if 'preconditions' not in row['scenario'] and scope.get('preconditions'):
                row['scenario']['preconditions']=scope['preconditions']
            for description in (row,*row.get('variants',{}).values()):
                criteria=description.get('criteria',[])
                ids=[criterion['id'] for criterion in criteria]
                if len(ids)!=len(set(ids)): raise ForgeError(f'Duplicate criterion IDs in {row["id"]}.')
                prose=[description[field] for field in ('title','purpose','failure_meaning') if field in description]
                prose.extend(description.get('limitations',[]))
                prose.extend(item['expected'] for item in criteria)
                prose.extend(description.get('scenario',{}).get(field,'') for field in ('action','preconditions'))
                for content in prose:
                    if re.fullmatch(r'\s*(TODO|TBD|待补充|正常|正确)\s*[.!。]?\s*',content,re.I):
                        raise ForgeError(f'Placeholder content in {row["id"]}.')
            if row.get('review',{}).get('status')=='reviewed' and not row['review'].get('evidence'):
                raise ForgeError(f'Reviewed metadata needs actual review evidence: {row["id"]}')
            row.setdefault('review',{'status':'draft'});row['document']=name;definitions.append(row)
    return {'schema_version':1,'project_id':top['project_id'],'definitions':definitions,'documents':[str(index),*documents],
            'digest':hasher.hexdigest(),'diagnostics':[]}


def project_metadata(project):
    project=Path(project).resolve();index=project/'tests/metadata/index.yaml'
    if not index.resolve().is_relative_to(project):raise ForgeError('Metadata index leaves the project checkout.')
    metadata=load(index,allow_missing=True)
    for row in metadata['definitions']:
        if row['binding']['adapter']!='pytest':continue
        source=(project/row['binding']['selector'].split('::')[0]).resolve()
        if not source.is_relative_to(project):raise ForgeError('Metadata selector leaves the project checkout.')
        if not source.is_file():metadata['diagnostics'].append(f'{row["id"]}: test source file is missing.')
    return metadata


def bindings(metadata,items,*,strict=False,deselected=(),selected_files=()):
    diagnostics=list(metadata.get('diagnostics',[]));matched=set()
    for item in items:
        base=item['native_id'].split('[',1)[0]
        definitions=[row for row in metadata['definitions'] if row['binding']['adapter']==item['adapter'] and row['binding']['selector'] in (base,item['native_id'])]
        if len(definitions)!=1: diagnostics.append(f'{item["native_id"]}: {len(definitions)} metadata bindings.')
        else: matched.add(definitions[0]['id'])
    # Only definitions whose source was selected can be declared stale here.
    selected_files=set(selected_files)|{row['native_id'].split('::')[0] for row in items}
    for row in metadata['definitions']:
        if row['binding']['selector'].split('::')[0] in selected_files and row['id'] not in matched and not any(row['binding']['selector'] in (node,node.split('[',1)[0]) for node in deselected):
            diagnostics.append(f'{row["id"]}: selector absent from selected inventory (or deselected by marker).')
    if strict and diagnostics: raise ForgeError('Metadata binding validation failed: '+'; '.join(diagnostics))
    return diagnostics


def validate(index,*,strict=False,inventory=None):
    metadata=load(index)
    if strict and inventory is None:
        raise ForgeError('--strict requires an explicit discovery inventory.',hint='Run jforge test discover <target>, then pass --inventory <inventory.json>.')
    if inventory is not None:
        if not inventory.get('confirmed'):
            raise ForgeError('Binding validation requires a confirmed discovery inventory.')
        if inventory.get('metadata_digest')!=metadata['digest'] or inventory.get('project_id')!=metadata['project_id']:
            raise ForgeError('Discovery inventory does not match this metadata. Discover again.',code=409)
        project=inventory.get('project')
        if project and inventory.get('card'):
            from . import testing, testing_run, test_discovery
            card=testing.load_card(inventory['card'])
            if (test_discovery.selection_identity(card,inventory['selection'])!=inventory['selection_identity']
                    or testing_run.Source(project).identity()!=inventory['source']
                    or not test_discovery.watch_matches(inventory['environment_watch'])):
                raise ForgeError('Discovery inventory is stale. Discover again before checking bindings.',code=409)
        if project and (Path(project)/'tests/metadata/index.yaml').resolve()==Path(index).resolve():
            metadata=project_metadata(project)
    diagnostics=bindings(metadata,inventory['items'],strict=strict,deselected=inventory.get('deselected',[]),selected_files=inventory.get('selected_files',[])) if inventory else []
    return {'schema_version':1,'project_id':metadata['project_id'],'definitions':len(metadata['definitions']),
            'metadata_digest':metadata['digest'],'diagnostics':diagnostics,'binding_checked':inventory is not None,
            'reviewed':sum(row['review']['status']=='reviewed' for row in metadata['definitions'])}


def scaffold(target,output):
    """AST-only drafts; deliberately not an accepted metadata document."""
    import ast
    target=Path(target).resolve();output=Path(output).resolve()
    if output.exists(): raise ForgeError(f'Scaffold output already exists: {output}')
    sources=[target] if target.is_file() else sorted(target.glob('tests/test_*.py'))
    drafts=[]
    for path in sources:
        try: tree=ast.parse(path.read_text())
        except (OSError,SyntaxError) as exc: raise ForgeError(f'Cannot inspect {path}: {exc}') from exc
        for node in ast.walk(tree):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                drafts.append({'source':str(path),'function':node.name,'line':node.lineno,'docstring':ast.get_docstring(node),
                               'review':{'status':'draft'},'required':['stable definition ID','purpose','scenario','criteria','failure_meaning','limitations']})
    output.mkdir(parents=True)
    (output/'drafts.yaml').write_text(yaml.safe_dump({'schema_version':1,'kind':'test-metadata-drafts','drafts':drafts},allow_unicode=True,sort_keys=False))
    return {'output':str(output),'definitions':len(drafts),'status':'draft','binding_checked':False}
