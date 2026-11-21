import {el, button, link, status, empty, field, pageHeader, progress} from './components.js?v=1.0.9';
import {capabilities, request, jobPath, casePath, reportRoute, safeHref} from './test-api.js';
import {jobProgress} from './progress-model.js';
import {InventoryPager, rowIdentity, reportPresentation, logAvailability, appendLog, inventoryNeedsReload} from './test-model.js';
import {demoMode} from './api.js';
const time = value => value ? new Date(value).toLocaleString() : 'Unknown';
const caseState = row => row.outcome || row.state || 'not_run';
const duration = value => value == null ? '—' : `${value.toFixed(2)} s`;
const memo = new Map();
const replace = (node, ...children) => node.replaceChildren(...children.flat(Infinity).filter(value => value != null && typeof value !== 'boolean'));
function selector(label, values, change) {
  return field(label, el('select', {'aria-label': label, onchange: event => change(event.target.value)}, values.map(([value, text]) => el('option', {value}, text))));
}
const errorBox = (error, retry) => empty('Could not load test data', error.message, button('Retry', retry));
export function progressPanel(job, {compact = false} = {}) {
  const model = jobProgress(job);
  return el('div', {class: `test-progress-panel${compact ? ' is-compact' : ''}`}, el('div', {class: 'test-progress-heading'}, el('strong', {}, model.label), status(job.status)),
    progress(model.completed, model.total, model.noun === 'steps' ? 'Completed execution steps' : 'Completed test cases'), model.current && el('p', {class: 'test-current', title: model.current}, 'Now · ', model.current),
    el('div', {class: 'test-outcomes'}, Object.entries(model.outcomes || {}).filter(([, count]) => count > 0).map(([key, count]) => el('span', {class: `outcome-${key}`}, `${count} ${key}`)),
      model.reused > 0 && el('span', {}, `${model.reused} reused · included above`)),
    model.groups?.map(row => el('p', {class: 'subtext'}, `${row.project_id || row.target_id || row.unit || 'Test group'} · ${row.label}`)),
    el('small', {class: 'subtext'}, job.progress_snapshot ? `Updated ${time(job.progress_snapshot.updated_at)} · ${job.cases.length} execution steps tracked separately` : 'Waiting for authoritative test progress'));
}
export function testBrowser({album, job, compact = false, onNavigate = () => {}}) {
  const key = (job ? `job:${job.id}` : `album:${album.id}`) + (compact ? ':compact' : '');
  if (memo.has(key)) return memo.get(key).root;
  const root = el('section', {class: `test-browser${compact ? ' is-compact' : ''}`, 'aria-label': job ? 'Actual test cases' : 'Album tests'});
  const span = columns => compact ? {'data-grid-span': columns} : {};
  const measure = () => { if (compact && root.isConnected) root.dispatchEvent(new Event('test-layout-change', {bubbles: true})); };
  const connection = el('span', {class: 'test-connection', role: 'status'}, 'Connecting…'), meta = el('div', {class: 'inventory-meta'});
  const list = el('div', {class: 'test-list'}), paging = el('div', {class: 'test-paging'}), count = el('p', {class: 'test-list-count', role: 'status'});
  const detail = el('aside', {class: 'test-inspector', ...span(3), 'aria-label': 'Selected test details'}, empty('A closer look', 'Select a test to read its purpose, scenario and expected criteria.'));
  const controls = el('div', {class: 'test-filters'}), cards = new Map(), groupNodes = new Map();
  const previousPage = button('Previous page',()=>load(local.index-1)), nextPage = button('Next page',()=>load(local.index+1));
  const local = {root, rows: [], selected: null, query: {}, busy: false, endpoint: null, loadToken: 0, selectionToken: 0, index: 0, inventoryTotal: null, eventCursor: '', lastSeq: -1};
  local.pager = new InventoryPager(params => request(`${local.endpoint}?${params}`, {signal: local.controller?.signal})); memo.set(key, local);
  const title = el('div', {class: 'test-section-title'}, el('div', {}, el('span', {class: 'test-eyebrow'}, 'TEST INVENTORY'), el('h3', {}, job ? 'Test results' : 'Tests')), connection);
  const refresh = button('Refresh tests', async () => {
    if (local.busy || !album) return; refresh.disabled = true;
    try { local.loadToken++; local.discovery = (await request('/api/test-discoveries', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({playlist: album.id})})).discovery;
      local.pager.invalidate(); local.facetsLoaded = false; local.facetsPromise = null; await watchDiscovery();
    } catch (error) { meta.replaceChildren(errorBox(error, () => refresh.click())); } finally { refresh.disabled = false; }
  }); refresh.setAttribute('aria-label', 'Refresh tests');
  const search = el('input', {type: 'search', placeholder: 'Search title, ID or file…', 'aria-label': 'Search tests'}); let timer;
  search.oninput = () => { clearTimeout(timer); local.loadToken++; timer = setTimeout(() => {local.query.q = search.value.trim(); load(0);}, 250); };
  const facetControls = new Map();
  async function loadFacets() {
    if (!local.endpoint || local.facetsLoaded) return;
    try { const page = await (local.facetsPromise ||= local.pager.page({},0,{current:()=>root.isConnected}));
      for (const [name, control] of facetControls) for (const value of (page.facets?.[name] || [])) if (![...control.options].some(option=>option.value===value)) control.append(el('option',{value},value));
      local.facetsLoaded = true;
    } catch { local.facetsPromise = null; }
  }
  const filter = (name, label, values) => { const wrapper = selector(label, [['', label === 'Status' ? 'All statuses' : `All ${label.toLowerCase()}`], ...values.map(value => [value, value.replaceAll('_', ' ')])], value => {local.query[name] = value; load(0);});
    if (['component','tag','metadata'].includes(name)) {const control=wrapper.querySelector('select');facetControls.set(name,control);control.addEventListener('focus',loadFacets);}
    return wrapper;
  };
  controls.append(field('Search tests', search), filter('outcome', 'Status', ['passed', 'failed', 'error', 'skipped', 'xfailed', 'xpassed']),
    filter('component', 'Components', []),
    filter('tag', 'Tags', []), filter('metadata', 'Metadata', []));
  if (job) controls.append(filter('state', 'States', ['queued', 'running', 'finished', 'interrupted', 'not_run']), filter('execution_mode', 'Execution', ['executed', 'reused']));
  if (compact) { [...controls.children].forEach((control, index) => {control.dataset.gridSpan = index === 0 ? '4' : '2';}); controls.firstElementChild.classList.add('test-filter-search'); refresh.dataset.gridSpan = '2'; if (!job) controls.append(refresh); }
  root.append(title, el('p', {class: 'subtext'}, `${job?.cases.length ?? album?.members.length ?? 0} execution steps · Test items have their own count. Preconditions and builds remain separate.`), meta,
    el('div', {class: 'test-inventory-actions'}, controls, !job && !compact && refresh), count,
    el('div', {class: 'test-workbench'}, el('div', {class: 'test-list-pane', ...span(5)}, list, paging), detail));
  function selectedNote() {
    const note = detail.querySelector('.selection-note');
    if (note) note.textContent = local.selected && !local.rows.some(row => rowIdentity(row) === rowIdentity(local.selected)) ? 'Selected test is outside this page or filter. Selection retained.' : '';
  }
  function rowCard(row) {
    const latestRow = () => local.rows.find(item=>rowIdentity(item)===rowIdentity(row)) || row;
    const select = button('', () => choose(latestRow()), {variant: 'test-row-main'}); select.setAttribute('aria-label', `Inspect ${row.title || row.native_id}`);
    select.append(el('span', {class: 'test-row-heading'}, el('strong', {}, row.title || row.native_id), el('span', {class: 'test-row-status'}, status(caseState(row)))),
      el('code', {class: 'test-native-id'}, row.native_id), el('span', {class: 'test-row-meta'}));
    const report = row.instance_id && job ? link('', reportRoute(job.id, row.instance_id), {variant: 'test-report-link'}) : null; report?.addEventListener('click', onNavigate);
    const card = el('article', {class: 'test-row', 'data-instance': rowIdentity(row)}, select, el('div', {class: 'test-row-actions'}, report,
      button(job ? 'Log' : 'Description', () => choose(latestRow(), !!job), {variant: 'subtle'})));
    cards.set(rowIdentity(row), card); updateCard(row); return card;
  }
  function updateCard(row) {
    const card = cards.get(rowIdentity(row)); if (!card) return;
    card.querySelector('.test-row-main').setAttribute('aria-label',`Inspect ${row.title || row.native_id}`);
    card.querySelector('.test-row-heading strong').textContent=row.title||row.native_id;
    card.querySelector('.test-native-id').textContent=row.native_id;
    card.querySelector('.test-row-main').setAttribute('aria-pressed', String(rowIdentity(local.selected || {}) === rowIdentity(row)));
    card.querySelector('.test-row-status').replaceChildren(status(caseState(row)));
    card.querySelector('.test-row-meta').textContent = `${row.variant_key || 'Default variant'} · ${row.variant_count ?? 1} variant(s) · ${duration(row.seconds)} · metadata ${row.metadata_status || 'unknown'}${row.execution_mode === 'reused' ? ' · Reused' : ''}`;
    const report = card.querySelector('.test-report-link'); if (report) report.textContent = row.report_status === 'provisional' ? 'Provisional report' : row.state === 'not_run' || row.state === 'interrupted' ? 'Explanation report' : 'Report';
  }
  function paint(page) {
    if (page.facets) for (const [name, control] of facetControls) for (const value of page.facets[name] || []) if (![...control.options].some(option=>option.value===value)) control.append(el('option',{value},value));
    const groups = new Map(), open = new Map([...list.querySelectorAll('details')].map(node => [node.dataset.file, node.open]));
    for (const row of local.rows) { const file = row.file || row.group?.file || 'Other tests'; if (!groups.has(file)) groups.set(file, []); groups.get(file).push(row); }
    const scroll = list.scrollTop, focused = list.contains(document.activeElement) ? document.activeElement : null;
    for (const [file, group] of groupNodes) if (!groups.has(file)) {group.remove(); groupNodes.delete(file);}
    for (const node of list.querySelectorAll('.test-empty')) node.remove();
    let position = 0;
    for (const [file, rows] of groups) {
      let group = groupNodes.get(file);
      if (!group) {group=el('details',{class:'test-file-group',open:open.get(file)??true,'data-file':file},el('summary',{},el('code',{},file),el('span')));groupNodes.set(file,group);}
      group.querySelector('summary span').textContent=`${rows.length} on this page`;
      if (list.children[position] !== group) list.insertBefore(group,list.children[position]||null);
      const wanted = new Set(rows.map(rowIdentity));
      for (const card of group.querySelectorAll('.test-row')) if (!wanted.has(card.dataset.instance)) card.remove();
      rows.forEach((row,index)=>{const card=cards.get(rowIdentity(row))||rowCard(row);updateCard(row);if(group.children[index+1]!==card)group.insertBefore(card,group.children[index+1]||null);});
      position++;
    }
    for (const [id] of cards) if (!local.rows.some(row=>rowIdentity(row)===id)) cards.delete(id);
    if (!local.rows.length) {const blank=page.total == null ? empty('Test inventory not confirmed','No test items are available in this snapshot. Collection and prerequisite evidence remain in execution steps.') : empty('No tests found','No matches in the complete queried inventory. Try changing the filters.');blank.classList.add('test-empty');list.append(blank);}
    list.scrollTop=scroll;
    if (focused && !focused.isConnected) {list.tabIndex=-1;list.focus({preventScroll:true});}
    count.textContent = `${local.rows.length} shown · page ${local.index + 1} · ${page.total ?? '?'} matching · ${local.inventoryTotal ?? '?'} total tests`;
    previousPage.disabled=local.index===0;nextPage.disabled=!page.hasNext;
    if (!paging.contains(previousPage)) paging.replaceChildren(previousPage,nextPage); selectedNote();
    if (!local.selected && local.rows.length) choose(local.rows.find(row => row.state === 'running') || local.rows.find(row => ['failed', 'error'].includes(row.outcome)) || local.rows[0]);
    measure();
  }
  async function choose(row, log = false) {
    const pass = ++local.selectionToken; local.selected = row;
    if (job && row.instance_id) {
      try { const value = await request(casePath(job.id, row.instance_id)); if (pass !== local.selectionToken) return; Object.assign(row, value.test_case); }
      catch (error) { if (pass === local.selectionToken) detail.replaceChildren(errorBox(error, () => choose(row, log))); return; }
    }
    for (const item of local.rows) updateCard(item);
    replace(detail, el('p', {class: 'selection-note subtext'}), el('div', {class: 'inspector-header'}, el('span', {class: 'test-eyebrow'}, 'SELECTED TEST'), status(caseState(row))),
      el('h3', {}, row.title || row.native_id), el('code', {class: 'breakable'}, row.native_id),
      el('p', {class: 'subtext'}, `${row.adapter || 'Unknown adapter'} · ${row.target_id || 'Target not supplied'} · attempt ${row.attempt || 1}`),
      row.execution_mode === 'reused' && el('p', {class: 'reused-label'}, 'Reused · original evidence retained'),
      el('h4', {}, 'Purpose'), el('p', {}, row.purpose || 'Metadata missing · no purpose supplied.'),
      el('h4', {}, 'Scenario'), el('p', {}, `Preconditions · ${row.scenario?.preconditions || 'Not supplied'}`), el('p', {}, `Inputs · ${typeof row.scenario?.inputs === 'object' ? JSON.stringify(row.scenario.inputs) : row.scenario?.inputs || 'Not supplied'}`), el('p', {}, row.scenario?.action || 'Action not supplied.'),
      el('h4', {}, 'Expected criteria'), (row.criteria || []).map(criterion => el('p', {class: 'criterion-preview'}, criterion.expected, el('small', {}, ` · ${criterion.verification || 'unknown'} · ${criterion.status || 'unobserved'}${criterion.observed != null ? ` · observed: ${typeof criterion.observed === 'object' ? JSON.stringify(criterion.observed) : criterion.observed}` : ' · No independent observation'}`))),
      !(row.criteria?.length) && el('p', {class: 'subtext'}, 'No criteria supplied.'),
      el('h4', {}, 'Failure meaning'), el('p', {}, row.failure_meaning || 'Failure guidance not supplied.'),
      el('h4', {}, 'Coverage & limitations'), el('p', {}, row.limitations?.join(' ') || 'Coverage boundaries not supplied.'),
      row.instance_id && job && el('a', {class: 'button primary', href: reportRoute(job.id, row.instance_id), onclick: onNavigate}, 'Read full report'));
    selectedNote(); if (job && row.instance_id) { const panel = logPanel(job.id, row); detail.append(panel); if (log) panel.scrollIntoView({block: 'nearest'}); } measure();
  }
  async function load(index = 0, {fresh = false, retried = false} = {}) {
    if (!local.endpoint) return;
    if (fresh) {local.pager.invalidate();local.facetsLoaded=false;local.facetsPromise=null;}
    const token = ++local.loadToken; local.controller?.abort(); local.controller = new AbortController(); local.busy = true; connection.textContent = 'Loading…';
    try {
      const page = await local.pager.page({...local.query}, index, {current: () => token === local.loadToken,
        progress: (loaded, total) => {if (token === local.loadToken) connection.textContent = `Filtering complete inventory · ${loaded} / ${total ?? '?'} loaded`;}});
      if (token !== local.loadToken) return;
      local.rows = page.items; local.index = page.index; local.revision = page.inventory_revision;
      if (!Object.values(local.query).some(Boolean)) local.inventoryTotal = page.total ?? local.inventoryTotal;
      connection.textContent = `Connected · ${time(new Date().toISOString())}`; paint(page); return true;
    } catch (error) {
      if (token !== local.loadToken || error.name === 'AbortError') return;
      if (error.status === 409 && !retried) {local.pager.invalidate(); return load(0,{retried:true});}
      connection.textContent = 'Disconnected · last page retained'; paging.replaceChildren(errorBox(error, () => load(index, {fresh: true}))); return false;
    } finally {if (token === local.loadToken) local.busy = false;}
  }
  async function watchDiscovery() {
    const discovery = (await request(`/api/test-discoveries/${encodeURIComponent(local.discovery.id)}`)).discovery; local.discovery = discovery;
    meta.replaceChildren(el('span', {class: `inventory-state ${discovery.freshness || 'unknown'}`, ...span(2)}, `Inventory ${discovery.freshness || 'unknown'}`),
      el('span', {...span(3)}, `Source · ${discovery.source || 'Unknown'}`), el('span', {...span(3)}, `Discovered · ${time(discovery.discovered_at)}`), el('span', {...span(8)}, `Environment · ${discovery.environment || 'Unknown'}`)); measure();
    if (['queued', 'running'].includes(discovery.status)) {connection.textContent = 'Collecting'; setTimeout(() => {if (root.isConnected) watchDiscovery().catch(error => meta.replaceChildren(errorBox(error, watchDiscovery)));}, 1000);}
    else if (discovery.status === 'completed') {local.endpoint = `/api/test-discoveries/${encodeURIComponent(discovery.id)}/test-cases`; await load(0, {fresh: true});}
    else throw Error(discovery.error?.message || `Discovery ${discovery.status}`);
  }
  async function init() {
    try {
      const caps = await capabilities();
      if (job) {local.endpoint = `${jobPath(job.id)}/test-cases`; await load();}
      else {const cached = caps.inventories?.[album.id]; if (cached?.id) {local.discovery = cached; await watchDiscovery();} else {connection.textContent = 'Inventory unknown'; meta.textContent = 'No cached inventory · Refresh tests explicitly to discover tests.';}}
    } catch (error) {list.replaceChildren(errorBox(error, init)); connection.textContent = 'Connection error';}
  }
  local.reload = () => load(local.index, {fresh: true});
  local.patch = async updates => {
    const token=local.loadToken;local.pager.patch(updates);
    if (local.selected) {const fresh=updates.find(row=>rowIdentity(row)===rowIdentity(local.selected));if(fresh){Object.assign(local.selected,fresh);detail.querySelector('.inspector-header .status')?.replaceWith(status(caseState(fresh)));}}
    const page=await local.pager.page({...local.query},local.index,{current:()=>token===local.loadToken});
    if (token!==local.loadToken || !root.isConnected) return;
    local.rows=page.items;local.index=page.index;paint(page);
  };
  local.connection = connection; init(); return root;
}
export async function pollTestView(job) {
  const view = memo.get(`job:${job.id}`); if (!view?.root.isConnected || view.busy || view.polling) return;
  view.polling = true;
  try {
    const value = await request(`${jobPath(job.id)}/events?after=${encodeURIComponent(view.eventCursor)}`);
    const events = (value.items || value.events || []).filter(event => event.seq == null || event.seq > view.lastSeq);
    if (inventoryNeedsReload(events, view.query, view.pager.scans.has('{}'))) {if (!await view.reload()) return;}
    else {
      const ids = [...new Set(events.map(event => event.instance_id).filter(id => id && (view.pager.scans.has('{}') || view.rows.some(row => row.instance_id === id) || view.selected?.instance_id === id)))];
      if (ids.length > 16) {if (!await view.reload()) return;}
      const updates = ids.length > 16 ? [] : await Promise.all(ids.map(async id => (await request(casePath(job.id, id))).test_case));
      if (!view.root.isConnected) return; await view.patch(updates);
    }
    if (view.connection.textContent.startsWith('Disconnected')) view.connection.textContent = `Connected · ${time(new Date().toISOString())}`;
    view.eventCursor = value.next_cursor ?? view.eventCursor;
    for (const event of events) if (Number.isSafeInteger(event.seq)) view.lastSeq = Math.max(view.lastSeq, event.seq);
  } catch {view.connection.textContent = 'Disconnected · retrying from last event cursor';}
  finally {view.polling = false;}
}
function logPanel(job, row) {
  let buffer = {text: '', offset: 0}, stream = 'stdout', attempt = row.attempt || 1, busy = false, follow = true, token = 0, eof = false;
  const panel = el('section', {class: 'case-log-panel'}), output = el('pre', {class: 'test-log', tabindex: 0, 'aria-label': 'Selected test log'}), note = el('p', {class: 'subtext'});
  const followButton = button('Follow tail: on', () => {follow = !follow; followButton.textContent = `Follow tail: ${follow ? 'on' : 'off'}`; if (follow) {output.scrollTop = output.scrollHeight; read();}});
  output.onscroll = () => {if (output.scrollHeight - output.scrollTop - output.clientHeight > 24) {follow = false; followButton.textContent = 'Follow tail: off';}};
  const next = button('Load more', () => read(false, true));
  async function read(reset = false, manual = false) {
    if (busy && !reset) return;
    if (reset) {token++; buffer = {text: '', offset: 0}; eof = false; output.textContent = ''; follow = true; followButton.textContent = 'Follow tail: on';}
    if (!manual && !follow) return;
    const pass = token, offset = buffer.offset; busy = true; next.disabled = true;
    try {
      const value = await request(`${casePath(job, row.instance_id)}/log?${new URLSearchParams({stream, attempt, offset, limit: 65536})}`);
      if (pass !== token) return;
      const scroll = output.scrollTop;
      if (value.available === false) {eof = true; output.textContent = buffer.text || logAvailability(value,row.state); note.textContent = logAvailability(value,row.state); next.hidden = false; next.textContent = 'Retry log'; return;}
      buffer = appendLog(buffer, value, offset); eof = value.eof === true;
      output.textContent = buffer.text || logAvailability(value, row.state);
      note.textContent = `${value.scope || 'unknown'} scope${value.scope === 'step' ? ' · shared step output, not exclusive to this test' : ''} · attempt ${attempt} · ${stream} · bytes ${buffer.start || 0}–${buffer.offset}${value.truncated ? ' · service truncated' : ''}${buffer.trimmed ? ' · earlier bytes trimmed from this view' : ''}`;
      next.textContent = 'Load more'; next.hidden = eof; output.scrollTop = follow ? output.scrollHeight : scroll;
    } catch (error) {if (pass !== token) return; note.textContent = `Disconnected · ${error.message} · bytes retained through ${buffer.offset}`; next.hidden = false; next.textContent = 'Retry log';}
    finally {if (pass === token) {busy = false; next.disabled = false; if (panel.isConnected && (!eof || ['queued', 'running'].includes(row.state))) setTimeout(() => {if (panel.isConnected) read();}, 1200);}}
  }
  panel.append(el('h4', {}, 'Test log'), selector('Log stream', ['stdout', 'stderr', 'logging', 'traceback'].map(value => [value, value]), value => {stream = value; read(true);}),
    selector('Log attempt', (row.attempts || [attempt]).map(value => [String(value), `Attempt ${value}`]), value => {attempt = value; read(true);}), note, output,
    el('div', {class: 'test-log-actions'}, followButton, next,
      button('Copy', async () => {try {await navigator.clipboard.writeText(buffer.text); note.textContent = `Loaded bytes ${buffer.start || 0}–${buffer.offset} copied.`;} catch {note.textContent = 'Clipboard unavailable. Select the log to copy.';}}),
      button('Download loaded log', () => {const href = URL.createObjectURL(new Blob([buffer.text], {type: 'text/plain'})), anchor = el('a', {href, download: `${row.instance_id}-${stream}.log`.replace(/[\\/:*?"<>|]/g, '_')}); anchor.click(); setTimeout(() => URL.revokeObjectURL(href), 1000);}))); read(true); return panel;
}
const reportCache = new Map();
export function reportReader() {
  const parts = location.hash.slice('#report/'.length).split('/'); let job, instance;
  try {[job, instance] = parts.map(decodeURIComponent);} catch {return empty('Invalid report link', 'The job and instance IDs could not be read.');}
  if (!job || !instance || parts.length !== 2) return empty('Invalid report link', 'Both job and instance IDs are required.');
  const key = JSON.stringify([job, instance]); if (reportCache.has(key)) return reportCache.get(key);
  const root = el('div', {class: 'report-reader'}), content = el('div', {class: 'report-content'}, empty('Loading report', 'Requesting protected HTML from Forge…'));
  const notice = el('div', {class: 'notice', role: 'status', hidden: true}); reportCache.set(key, root);
  root.append(pageHeader('Test report', `${job} · ${instance}`, [], {label: 'Back to run', href: `#job/${encodeURIComponent(job)}`}), notice, content);
  let revision = 0, lastGood = false;
  async function load(attempt) {
    const pass = ++revision; notice.hidden = true;
    try {
      const caps = await capabilities(); if (pass !== revision) return;
      const query = attempt ? `?attempt=${encodeURIComponent(attempt)}` : '';
      const [detail, result] = await Promise.all([request(casePath(job, instance)), request(`${casePath(job, instance)}/report${query}`)]); if (pass !== revision) return;
      const row = detail.test_case, report = result.report, view = reportPresentation(report), href = safeHref(report.html_href || report.href);
      const historical = attempt && Number(attempt) !== Number(row.attempt || 1);
      const tools = el('div', {class: 'report-toolbar'}, button('Refresh report', () => load(attempt)), el('div', {},
        historical && !report.outcome ? el('span', {}, `Attempt ${attempt} · outcome in HTML`) : status(report.outcome || row.outcome || row.state),
        el('span', {class: `report-phase ${view.kind}`}, view.title)),
        selector('Report attempt', (row.attempts || [row.attempt || 1]).map(value => [String(value), `Attempt ${value}`]), load));
      tools.querySelector('select').value = String(attempt || row.attempt || 1);
      if (href) tools.append(el('a', {href, target: '_blank', rel: 'noopener', class: 'button secondary'}, 'Open full HTML'), el('a', {href, download: `${instance}.html`, class: 'button secondary'}, 'Download HTML'));
      content.replaceChildren(el('h2', {class: 'report-test-title'}, row.title || row.native_id), tools);
      if (demoMode) content.append(el('p', {class: 'notice'}, 'Independent Demo · illustrative HTML, not local test evidence.'));
      if (row.reused_from) {
        const original = safeHref(report.original_html_href) || (row.reused_from.job_id ? reportRoute(row.reused_from.job_id, row.reused_from.instance_id) : null);
        content.append(original ? el('a', {class: 'notice', href: original}, `Reused · Open original evidence · attempt ${row.reused_from.attempt}`) : el('p', {class: 'notice'}, `Reused from run ${row.reused_from.run_id || 'unavailable'} · attempt ${row.reused_from.attempt}. Source evidence reference unavailable.`));
      }
      if (view.kind === 'failed') content.append(button('Rebuild report', async () => {
        try {await request(`${casePath(job, instance)}/report/rebuild`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}); await load(attempt);}
        catch (error) {notice.hidden = false; notice.replaceChildren(el('p', {}, `Could not rebuild report · ${error.message}`));}
      }));
      if (row.metadata_status === 'missing') content.append(el('p', {class: 'notice'}, 'Metadata missing · original identity and execution evidence remain available.'));
      if (view.kind !== 'ready') content.append(el('p', {class: 'notice'}, view.text));
      if (href) content.append(el('a', {class: 'report-skip', href: '#report-after-frame', onclick: event => {event.preventDefault(); content.querySelector('.report-after-frame').focus();}}, 'Skip embedded report'),
        el('iframe', {class: 'report-frame', title: `Full HTML report: ${row.title || row.native_id}`, src: href, sandbox: 'allow-downloads', referrerpolicy: 'no-referrer'}),
        el('div', {id: 'report-after-frame', class: 'report-after-frame', tabindex: -1}, link('Back to run', `#job/${encodeURIComponent(job)}`)));
      else content.append(empty(view.kind === 'ready' ? 'Report link rejected' : view.title, view.kind === 'ready' ? 'The HTML address is outside the allowed origin.' : view.text, button('Retry report', () => load(attempt))));
      lastGood = true;
    } catch (error) {
      if (pass !== revision) return;
      const view = reportPresentation({}, error);
      if (lastGood && (!error.status || error.status >= 500)) {notice.hidden = false; notice.replaceChildren(el('p', {}, `Disconnected · ${error.message}. Last report retained.`), button('Retry report', () => load(attempt)));}
      else content.replaceChildren(empty(view.title, view.text, button('Retry report', () => load(attempt))));
    }
  }
  load(); return root;
}
