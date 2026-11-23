"""Fresh-process real solver probe; never invokes a package install or build."""
import contextlib
import tempfile
import json
import os
from pathlib import Path
import shutil
import sys


def main():
    engine, builtin, solver, workspace = map(Path, sys.argv[1:])
    workspace.mkdir(parents=True)
    os.environ.update(SPACK_USER_CONFIG_PATH=str(workspace / 'user-config'),
                      SPACK_SYSTEM_CONFIG_PATH=str(workspace / 'system-config'),
                      SPACK_USER_CACHE_PATH=str(workspace / 'cache'))
    sys.path[:0] = [str(solver), str(engine / 'lib/spack'), str(engine / 'lib/spack/external')]
    import clingo  # Prebuilt wheel only: bootstrap is explicitly disabled.
    import yaml
    import spack
    import spack.config
    import spack.environment as ev
    import spack.repo
    import spack.solver.asp as asp
    import spack.store
    from jarvis_forge.spack_backend import ENGINE_VERSION
    from jarvis_forge.spack_bridge import resolve_plan
    from jarvis_forge.build_tool_reuse import lock_packages, historical_references
    from jarvis_forge.storage import ForgeHome, write_json
    from jarvis_forge.toolchains import suite, validate_plan

    assert spack.__version__ == ENGINE_VERSION == '1.2.2'
    original_extract = asp.Solver.__dict__['_extract_concrete_specs']
    original_define = asp.SpackSolverSetup.define_concrete_input_specs
    prefix = workspace / 'tools/pkg/forge-lib-1.0'
    prefix.mkdir(parents=True)
    repo = Path(__file__).parent / 'fixtures/spack_repo/forge_test'
    fixed_prefix = workspace / 'tools/pkg/cmake-3.28.6'
    fixed_prefix.mkdir()
    toolchain = {'version': '13.3.0', 'prefix': str(workspace / 'tools/pkg/gcc-13.3.0'),
                 'tools': {'cmake': {'version': '3.28.6', 'prefix': str(fixed_prefix)}}}
    home = ForgeHome(workspace)
    write_json(workspace / 'metadata.json', {'schema_version': 3, 'kind': 'profile', 'model': 'builtin-profile/1',
                                           'id': workspace.name, 'backend': 'spack', 'toolchain': toolchain})

    def solve(name, specs, *, locked=None, excluded=(), filtered=(), external=False, external_excluded=False, fixed=False):
        folder = workspace / name
        folder.mkdir()
        sources = [{'type': 'local', 'exclude': ['/' + h for h in excluded]},
                   {'type': 'external', 'exclude': ['/' + h for h in excluded] if external_excluded else []}]
        document = {'spack': {'specs': specs, 'view': False,
                              'concretizer': {'unify': True, 'reuse': {'from': sources}},
                              'packages': {'forge-lib': {'externals': [{'spec': 'forge-lib@1.0', 'prefix': str(prefix)}],
                                                         'buildable': False}} if external else {}}}
        if fixed:
            cmake = suite(toolchain)['cmake']
            document['spack']['packages']['cmake'] = {'externals': [{
                'spec': 'cmake@=' + cmake['version'], 'prefix': cmake['prefix']}], 'buildable': False}
        (folder / 'spack.yaml').write_text(yaml.safe_dump(document))
        if locked:
            shutil.copy2(locked, folder / 'spack.lock')
        environment = ev.Environment(str(folder))
        with environment, tempfile.TemporaryFile(mode='w+', encoding='utf-8') as output, contextlib.redirect_stdout(output):
            resolve_plan({'flags': ['concretize'], 'policy': 'never', 'overwrite': [],
                          'historical_tool_hashes': list(filtered)})
        # Reload what the command actually wrote, independently of bridge output.
        with ev.Environment(str(folder)) as solved:
            roots = {s.name: s for s in solved.concrete_roots()}
        assert asp.Solver.__dict__['_extract_concrete_specs'] is original_extract
        assert asp.SpackSolverSetup.define_concrete_input_specs is original_define
        return folder / 'spack.lock', roots

    with (spack.repo.use_repositories(str(repo), str(builtin)),
          spack.store.use_store(str(workspace / 'store'), extra_data={
              'padded_length': False, 'projections': {'all': '{name}-{version}'}}),
          spack.config.override('bootstrap:enable', False)):
        lock, first = solve('initial', ['forge-builder'], external=True)
        builder = first['forge-builder'].dag_hash()
        historical = first['forge-builder']['forge-lib'].dag_hash()
        _, baseline = solve('baseline', ['forge-builder', 'forge-lib'], locked=lock, excluded=[historical])
        assert baseline['forge-lib'].dag_hash() == historical and baseline['forge-lib'].external
        _, filtered = solve('filtered', ['forge-builder', 'forge-lib'], locked=lock,
                            excluded=[historical], filtered=[historical])
        assert not filtered['forge-lib'].external and filtered['forge-lib'].dag_hash() != historical
        assert filtered['forge-builder'].dag_hash() == builder
        assert str(filtered['forge-builder'].prefix) == str(first['forge-builder'].prefix)
        assert filtered['forge-builder']['forge-lib'].dag_hash() == historical
        _, linked = solve('linked', ['forge-builder', 'forge-app@1.0'], locked=lock,
                          excluded=[historical], filtered=[historical])
        assert not linked['forge-app']['forge-lib'].external
        assert linked['forge-app']['forge-lib'].dag_hash() != historical
        assert linked['forge-builder'].dag_hash() == builder
        assert linked['forge-builder']['forge-lib'].dag_hash() == historical
        # Re-offer precisely H to a new consumer while preserving the old one.
        _, trial = solve('trial', ['forge-builder', 'forge-consumer'], locked=lock,
                         excluded=[historical], filtered=[historical], external=True)
        assert trial['forge-builder'].dag_hash() == builder
        assert trial['forge-consumer']['forge-lib'].dag_hash() == historical
        # D7: excluding H from external blocks the newly validated proposal too.
        try:
            solve('external-excluded', ['forge-builder', 'forge-consumer'], locked=lock,
                  excluded=[historical], filtered=[historical], external=True, external_excluded=True)
        except asp.UnsatisfiableSpecError as exc:
            assert "no externals satisfy" in str(exc)
        else:
            raise AssertionError('external selector unexpectedly ignored its hash exclusion')
        assert asp.Solver.__dict__['_extract_concrete_specs'] is original_extract
        assert asp.SpackSolverSetup.define_concrete_input_specs is original_define
        # An explicitly requested fixed CMake root coexists with a historical
        # optional tool; later root promotion changes neither installed root.
        suite_lock, pinned = solve('suite-initial', ['forge-builder', 'cmake'], external=True, fixed=True)
        rows = lock_packages(suite_lock)
        validate_plan(toolchain, rows)
        refs = historical_references(home, rows)
        assert {r['name'] for r in refs} == {'forge-lib'}
        blocked = [r['hash'] for r in refs]
        promoted_lock, promoted = solve('suite-promoted', ['forge-builder', 'cmake', 'forge-lib'], locked=suite_lock,
                                       excluded=blocked, filtered=blocked, fixed=True)
        validate_plan(toolchain, lock_packages(promoted_lock))
        assert promoted['cmake'].external and str(promoted['cmake'].prefix) == str(fixed_prefix)
        assert promoted['cmake'].dag_hash() == pinned['cmake'].dag_hash()
        assert promoted['forge-builder'].dag_hash() == pinned['forge-builder'].dag_hash()
        assert not promoted['forge-lib'].external and promoted['forge-lib'].dag_hash() not in blocked
    print(json.dumps({'status': 'passed', 'builder': builder, 'historical': historical,
                      'native': filtered['forge-lib'].dag_hash()}))


if __name__ == '__main__':
    main()
