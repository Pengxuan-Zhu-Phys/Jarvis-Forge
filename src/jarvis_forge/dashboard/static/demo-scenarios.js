// Frontend acceptance fixtures only. Never used by the live API transport.
export const scenarioDefinitions = [
  {id: 'large', title: '635 tests · complete filtering', description: 'Ten finished, one running; rare tags, metadata and filenames beyond page one.'},
  {id: 'states', title: 'Results & report lifecycle', description: 'Failure phases, skip/xfail/xpass, reuse, not-run, missing metadata, attempts, cleaned and failed reports, CTest and CI identities.'},
  {id: 'logs', title: 'Large UTF-8 log', description: 'Byte pagination, stream/attempt switching, copy/download, follow-tail pause and a bounded display buffer.'},
  {id: 'reconnect', title: 'Disconnect & resume', description: 'One controlled event failure and one log failure after the first chunk; retries retain their cursor.'},
  {id: 'final-only', title: 'Final-only command', description: 'A known command check with no live percentage until final evidence is available.'},
  {id: 'collecting', title: 'Collecting · unknown total', description: 'No leaf count is fabricated during collection.'},
  {id: 'collection-error', title: 'Collection failed', description: 'Step-level collection failure, without invented parameterized items.'},
  {id: 'blocked', title: 'Precondition blocked', description: 'The prerequisite failed before leaf discovery. Step evidence remains separate.'},
];
export function scenarioFromSearch(search) {const id = new URLSearchParams(search).get('scenario'); return scenarioDefinitions.find(row => row.id === id) || null;}
export function scenarioCatalog(scenario) {
  const id = `scenario-${scenario.id}`, command = scenario.id === 'final-only';
  const pre = {id: `report-demo/demo/precondition::${scenario.id}`, title: 'Environment prerequisite', project: 'report-demo', kind: 'command', tags: ['environment'], status: scenario.id === 'blocked' ? 'failed' : 'passed', example: 'Controlled frontend fixture · no local command executed.'};
  const step = {id: `report-demo/demo/tests::${scenario.id}`, title: scenario.title, project: 'report-demo', kind: command ? 'command' : 'pytest', tags: ['model'], status: ['large', 'reconnect', 'final-only', 'collecting'].includes(scenario.id) ? 'running' : scenario.id === 'states' ? 'interrupted' : scenario.id === 'collection-error' ? 'failed' : scenario.id === 'blocked' ? 'not_run' : 'passed', example: 'Controlled fixture evidence. A collection error or precondition failure has no invented leaf inventory.'};
  const job = {id, playlist: id, project: 'report-demo', label: scenario.title, status: step.status === 'not_run' ? 'failed' : step.status,
    submitted_at: '2026-11-20T12:00:00Z', simulated: true, fixtureScenario: scenario.id, cases: [pre, step],
    collection_status: scenario.id === 'collecting' ? 'running' : scenario.id === 'collection-error' ? 'failed' : undefined,
    reason: ['collecting','collection-error','blocked'].includes(scenario.id) ? scenario.description : undefined};
  return {projects: [{id: 'report-demo', title: 'Report scenarios', icon: '🧪', shelf: 'examples', theme: 'mint', available: true}], cases: [pre, step],
    playlists: [{id, project: 'report-demo', title: scenario.title, description: scenario.description, shelf: 'examples', stored: true, members: [pre.id, step.id], entries: [{case: pre.id},{case: step.id}], engine: command ? 'Command' : 'pytest', digest: `fixture-v2-${scenario.id}`}], jobs: [job]};
}
const stateKinds = ['passed','failed','setup-error','teardown-error','skipped','xfailed','xpassed','reused','not_run','interrupted','missing','renderer-failed','cleaned','attempts','ctest','ci'];
export function scenarioRows(scenario, job) {
  if (['collecting','collection-error','blocked'].includes(scenario.id)) return [];
  const length = scenario.id === 'large' ? 635 : scenario.id === 'states' ? stateKinds.length : 1;
  return Array.from({length}, (_, index) => {
    const kind = scenario.id === 'states' ? stateKinds[index] : scenario.id;
    const state = scenario.id === 'large' ? index < 10 ? 'finished' : index === 10 ? 'running' : 'queued' : ['reconnect','final-only'].includes(kind) ? 'running' : ['not_run','interrupted'].includes(kind) ? kind : 'finished';
    const finalState = job.status === 'interrupted' && ['running','queued'].includes(state) ? 'not_run' : state;
    const caseId = `${scenario.id}-${index + 1}`, outcome = finalState === 'finished' ? ['failed','setup-error','teardown-error'].includes(kind) ? kind === 'failed' ? 'failed' : 'error' : ['skipped','xfailed','xpassed'].includes(kind) ? kind : scenario.id === 'large' && index === 9 ? 'failed' : 'passed' : null;
    return {case_id: caseId, instance_id: caseId, definition_id: `frontend.fixture.${kind}.${index}`, step_id: job.cases[1].id,
      native_id: kind === 'ctest' ? 'geometry::mesh_convergence' : `tests/${scenario.id === 'large' && index >= 600 ? 'export' : 'model'}/test_${index + 1}.py::test_${kind}[scenario-${index + 1}]`,
      title: scenario.id === 'large' ? `Reference test ${index + 1}${index === 0 ? ' · long parameter name '.repeat(12) : ''}` : `${kind} · controlled report scenario`,
      file: `tests/${scenario.id === 'large' && index >= 600 ? 'export' : 'model'}/test_${index + 1}.py`,
      component: kind === 'ctest' ? 'ctest' : kind === 'ci' ? 'ci' : index >= 600 ? 'export' : 'model', tags: [index === 620 ? 'rare' : 'regression'],
      adapter: kind === 'ctest' ? 'ctest' : scenario.id === 'final-only' ? 'command' : 'pytest', variant_key: `scenario-${index + 1}`, variant_count: 1, parameters: {index},
      metadata_status: kind === 'missing' || index === 610 ? 'missing' : 'complete', state: finalState, outcome,
      execution_mode: kind === 'reused' ? 'reused' : 'executed', seconds: finalState === 'finished' ? .06 : null,
      target_id: kind === 'ci' ? 'ci-python-3.12-linux' : 'browser-fixture', attempt: kind === 'attempts' ? 2 : 1, attempts: kind === 'attempts' || ['logs','reconnect'].includes(kind) ? [1,2] : [1],
      phase: kind === 'setup-error' ? 'setup' : kind === 'teardown-error' ? 'teardown' : 'call',
      reused_from: kind === 'reused' ? {job_id: job.id, instance_id: 'states-1', attempt: 1} : undefined,
      source: kind === 'ci' ? {repository: 'fixture/repository', commit: 'fixture-commit', workflow: 'acceptance', job: 'linux', runner: 'isolated-fixture'} : undefined,
      purpose: 'Verify that the frontend presents this explicitly controlled state without converting missing evidence into successful observations.',
      scenario: {preconditions: 'Independent browser Demo; no test runner is invoked.', inputs: {kind,index}, action: 'Inspect the supplied identity, state, report and byte-scoped evidence.'},
      criteria: [{criterion_id: 'truthful-state', expected: 'The supplied test outcome, report lifecycle and evidence scope stay separate.', verification: 'automated', status: 'unobserved', observed: null, evidence_refs: []},
        {criterion_id: 'manual-review', expected: 'Review the presentation.', verification: 'manual', status: 'pending', observed: null, evidence_refs: []}],
      failure_meaning: 'A display or API consumer regression may conflate states, scopes or attempts.', limitations: ['Frontend fixture only; not acceptance evidence from a project checkout.'],
      report_status: ['running','queued'].includes(finalState) ? 'provisional' : kind === 'renderer-failed' ? 'failed' : 'ready', fixture_kind: kind};
  });
}
export function scenarioProgress(scenario, job, items) {
  const outcomes = Object.fromEntries(['passed','failed','error','skipped','xfailed','xpassed'].map(key => [key, items.filter(row => row.outcome === key).length]));
  const unknown = ['collecting','collection-error','blocked'].includes(scenario.id);
  return {ok:true, api_version:1, job_id:job.id, revision:1, updated_at:'2026-11-20T12:00:01Z', collection_status:job.collection_status,
    summaries:[{unit:'test_case', total:unknown ? null : items.length, completed:items.filter(row=>row.state==='finished').length,
      running:items.filter(row=>row.state==='running').length,pending:items.filter(row=>row.state==='queued').length,interrupted:items.filter(row=>row.state==='interrupted').length,
      not_run:items.filter(row=>row.state==='not_run').length, outcomes,reused:items.filter(row=>row.execution_mode==='reused').length,total_confirmed:!unknown}],
    current_test:items.find(row=>row.state==='running')||null,steps:job.cases.map(row=>({...row,adapter:row.kind}))};
}
export function scenarioLog(row, stream = 'stdout', attempt = 1) {
  if (!['logs','reconnect','failed','setup-error','teardown-error'].includes(row.fixture_kind)) return '';
  if (stream === 'traceback') return `DEMO · ${row.phase} phase · attempt ${attempt}\nAssertionError: controlled ${row.fixture_kind} fixture\n`;
  const count = ['logs','reconnect'].includes(row.fixture_kind) ? 16000 : 3;
  return Array.from({length:count},(_,i)=>`${String(i+1).padStart(5,'0')} · 诊断 μ Δ 🧪 · ${stream} · attempt ${attempt} · controlled fixture evidence\n`).join('');
}
export function utf8LogChunk(text, offset = 0, limit = 65536) {
  const bytes = new TextEncoder().encode(text); let end = Math.min(bytes.length, offset + limit);
  while (end < bytes.length && (bytes[end] & 0xc0) === 0x80) end--;
  return {text:new TextDecoder().decode(bytes.slice(offset,end)),next_offset:end,eof:end===bytes.length,truncated:false,available:true,scope:'case'};
}
