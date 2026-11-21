import {scenarioFromSearch, scenarioRows, scenarioProgress, scenarioLog, utf8LogChunk} from './demo-scenarios.js';
// Explicit Demo transport. Illustrative target-contract fixtures, never a live fallback.
let fixturePromise;
const discoveries = new Map();
const response = value => new Response(JSON.stringify({ok: true, api_version: 1, ...value}), {headers: {'Content-Type': 'application/json'}});
const fail = (message, status = 404, code) => new Response(JSON.stringify({ok: false, error: {message, code}}), {status});
const rows = async () => (await (fixturePromise ||= fetch('/fixtures/v1/test-cases.json').then(response => response.json()))).items;
function instances(fixtures, job) {
  return fixtures.filter(row => job.cases.some(step => step.id === row.step_id)).map(row => {
    const step = job.cases.find(step => step.id === row.step_id);
    const state = ['passed', 'failed', 'skipped', 'reused'].includes(step.status) ? 'finished' : step.status === 'interrupted' ? 'not_run' : step.status;
    const outcome = state === 'finished' ? row.fixture_outcome : null;
    return {...row, instance_id: row.case_id, state, outcome, execution_mode: step.status === 'reused' ? 'reused' : 'executed',
      seconds: state === 'finished' ? .06 : null, attempt: 1, attempts: [1], report_status: ['queued', 'running'].includes(state) ? 'provisional' : 'ready'};
  });
}
function inventory(album) {
  return discoveries.get(album.id) || {id: `fixture-${album.id}`, status: 'completed', freshness: 'fresh',
    inventory_revision: 'demo-v1', source: 'Versioned illustrative fixture v1', discovered_at: '2026-11-19T15:00:00Z', environment: 'Independent browser Demo', playlist: album.id};
}
function page(items, url, facts = {}) {
  const query = url.searchParams;
  const facets = {component:[...new Set(items.map(row=>row.component).filter(Boolean))].sort(),tag:[...new Set(items.flatMap(row=>row.tags||[]))].sort(),metadata:[...new Set(items.map(row=>row.metadata_status).filter(Boolean))].sort()};
  items = items.filter(row => (!query.get('q') || [row.title, row.native_id, row.definition_id, row.file].join(' ').toLowerCase().includes(query.get('q').toLowerCase())) &&
    (!query.get('component') || row.component===query.get('component')) && (!query.get('tag') || row.tags?.includes(query.get('tag'))) && (!query.get('metadata') || row.metadata_status===query.get('metadata')) &&
    ['state', 'outcome', 'execution_mode'].every(key => !query.get(key) || row[key] === query.get(key)));
  const offset = Number(query.get('cursor') || 0), limit = Math.min(500, Number(query.get('limit') || 100));
  return response({facets, inventory_revision: 'demo-v1', items: items.slice(offset, offset + limit), total: items.length,
    next_cursor: offset + limit < items.length ? String(offset + limit) : null, ...facts});
}
export async function demoTestFetch(url, {method = 'GET', body} = {}, state) {
  const scenario = scenarioFromSearch(location.search);
  if (scenario) return scenarioFetch(url, {method, body}, state, scenario);
  const fixtures = await rows();
  const parts = url.pathname.split('/').map(decodeURIComponent);
  const resource = parts[2], id = parts[3], action = parts[4], instance = parts[5], sub = parts[6];
  if (resource === 'capabilities') return response({features: {test_cases: true}, adapters: [{adapter: 'pytest', enumeration: 'cached', progress: 'case_events', logs: ['case_captured'], cancellation: 'supported', artifacts: ['case_bound']}],
    inventories: Object.fromEntries(state.playlists.map(album => [album.id, inventory(album)]))});
  if (resource === 'test-discoveries') {
    if (method === 'POST') {
      const album = state.playlists.find(album => album.id === JSON.parse(body).playlist);
      if (!album) return fail('Demo album not found.');
      const discovery = {...inventory(album), discovered_at: new Date().toISOString()};
      discoveries.set(album.id, discovery); return response({discovery});
    }
    const album = state.playlists.find(album => inventory(album).id === id);
    if (!album) return fail('Discovery not found.');
    if (action === 'test-cases') return page(fixtures.filter(row => album.members.includes(row.step_id)).map(row => ({...row, instance_id: null, state: 'not_run', outcome:null, execution_mode:'executed', attempt:1, attempts:[1]})), url);
    return response({discovery: inventory(album)});
  }
  if (resource !== 'jobs' || !['progress', 'test-cases', 'events'].includes(action)) return null;
  const job = state.jobs.find(job => job.id === id); if (!job) return fail('Demo run not found.');
  const items = instances(fixtures, job);
  if (action === 'progress') {
    const outcomes = Object.fromEntries(['passed', 'failed', 'error', 'skipped', 'xfailed', 'xpassed'].map(key => [key, items.filter(row => row.outcome === key).length]));
    return response({job_id: id, revision: items.filter(row => row.state === 'finished').length, updated_at: new Date().toISOString(),
      summaries: [{unit: 'test_case', total: items.length, completed: items.filter(row => row.state === 'finished').length,
        running: items.filter(row => row.state === 'running').length, pending: items.filter(row => row.state === 'queued').length,
        interrupted: items.filter(row => row.state === 'interrupted').length, not_run: items.filter(row => row.state === 'not_run').length,
        outcomes, reused: items.filter(row => row.execution_mode === 'reused').length, total_confirmed: true}],
      current_test: items.find(row => row.state === 'running') || null, steps: job.cases});
  }
  if (action === 'events') {
    const cursor = JSON.stringify(items.map(row => row.state));
    return response({items: url.searchParams.get('after') === cursor ? [] : items.map(row => ({event_type: row.state === 'finished' ? 'case_finished' : 'case_started', instance_id: row.instance_id, payload: {state: row.state, outcome: row.outcome}})), next_cursor: cursor});
  }
  if (!instance) return page(items, url);
  const row = items.find(row => row.instance_id === instance); if (!row) return fail('Demo test instance not found.');
  if (!sub) return response({test_case: row});
  if (sub === 'report') return response({report: {status: row.report_status,
    html_href: row.report_status === 'provisional' ? '/fixtures/v1/provisional.html' : row.state === 'not_run' ? '/fixtures/v1/not-run.html' : row.report_href,
    generation_error: null, template_version: 'demo-fixture-v1'}});
  if (sub === 'log') {
    const stream = url.searchParams.get('stream') || 'stdout';
    const text = row.outcome === 'failed' && stream === 'traceback' ? 'DEMO FIXTURE · no local tests executed\nAssertionError: expected axis label “Time”, observed “t”\n  tests/test_savefig.py:42\n' : '';
    const bytes = new TextEncoder().encode(text), offset = Number(url.searchParams.get('offset') || 0);
    return response({text: new TextDecoder().decode(bytes.slice(offset)), next_offset: bytes.length, eof: true, truncated: false, available: true, scope: 'case'});
  }
  return fail('Unsupported demo test action.');
}

const scenarioFailures = new Set(), scenarioLogCache = new Map();
function scenarioFetch(url, {method, body}, state, scenario) {
  const [,,resource,id,action,instance,sub] = url.pathname.split('/').map(decodeURIComponent), job = state.jobs[0], album = state.playlists[0];
  const items = scenarioRows(scenario, job), discovery = {id:`discovery-${scenario.id}`,status:'completed',freshness:'fresh',playlist:album.id,source:'Frontend acceptance fixture v2',discovered_at:'2026-11-20T12:00:00Z',environment:'Isolated Demo scenario',inventory_revision:'demo-v2'};
  if (resource === 'capabilities') return response({features:{test_cases:true},adapters:[{adapter:scenario.id==='final-only'?'command':'pytest',enumeration:'exact',progress:scenario.id==='final-only'?'final_only':'case_events',logs:['case_captured'],cancellation:'supported',artifacts:['case_bound']}],inventories:{[album.id]:discovery}});
  if (resource === 'test-discoveries') {
    if (method === 'POST' || !action) return response({discovery});
    if (action === 'test-cases') return page(items.map(row=>({...row,instance_id:null,state:'not_run',outcome:null})),url);
  }
  if (resource !== 'jobs' || id !== job.id) return null;
  if (action === 'progress') return response(scenarioProgress(scenario,job,items));
  if (action === 'events') {
    if (scenario.id==='reconnect' && !scenarioFailures.has('events')) {scenarioFailures.add('events');return fail('Controlled temporary disconnect',503);}
    const events=items.slice(0,12).map((row,index)=>({seq:index+1,event_type:row.state==='finished'?'case_finished':'case_started',instance_id:row.instance_id,payload:{state:row.state,outcome:row.outcome}}));
    return response({items:url.searchParams.get('after')==='fixture-events-v2'?[]:events,next_cursor:'fixture-events-v2'});
  }
  if (action !== 'test-cases') return null;
  if (!instance) return page(items,url,['collecting','collection-error','blocked'].includes(scenario.id) ? {total:null,total_confirmed:false} : {});
  const row=items.find(item=>item.instance_id===instance);if (!row) return fail('Fixture instance not found',404);
  if (!sub) return response({test_case:row});
  const attempt=Number(url.searchParams.get('attempt')||row.attempt);
  if (!row.attempts.includes(attempt)) return fail('Fixture attempt not found',404);
  if (sub==='report' && url.pathname.endsWith('/rebuild')) return fail('Controlled fixture: renderer failure is fixed for this scenario. Real Forge can rebuild from raw facts.',409);
  if (sub==='report') {
    if (row.fixture_kind==='cleaned') return fail('The controlled fixture report was cleaned.',410,'evidence_cleaned');
    return response({report:{status:row.report_status,outcome:row.fixture_kind==='attempts'&&attempt===1?'failed':row.outcome,html_href:row.fixture_kind==='renderer-failed'?null:`/fixtures/v2/${row.instance_id}-attempt-${attempt}.html`,generation_error:row.fixture_kind==='renderer-failed'?'Controlled renderer failure; original test outcome retained.':null,template_version:'frontend-fixture-v2',metadata_digest:'demo-v2'}});
  }
  if (sub==='log') {
    const stream=url.searchParams.get('stream')||'stdout',offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||65536);
    if (scenario.id==='reconnect'&&offset>0&&!scenarioFailures.has('log')) {scenarioFailures.add('log');return fail('Controlled log disconnect after the first chunk',503);}
    const key=JSON.stringify([row.case_id,stream,attempt]);if(!scenarioLogCache.has(key))scenarioLogCache.set(key,scenarioLog(row,stream,attempt));
    return response(utf8LogChunk(scenarioLogCache.get(key),offset,limit));
  }
  return fail('Unsupported fixture request',404);
}
