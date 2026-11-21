// Consumer-side query, paging and presentation logic. No framework-log inference.
export const rowIdentity = row => row.instance_id || row.case_id;
export function matchesTest(row, query = {}) {
  return (!query.q || [row.title, row.native_id, row.definition_id, row.file, row.group?.file].join(' ').toLowerCase().includes(query.q.toLowerCase())) &&
    (!query.component || row.component === query.component) && (!query.tag || row.tags?.includes(query.tag)) &&
    (!query.metadata || row.metadata_status === query.metadata) &&
    ['state', 'outcome', 'execution_mode'].every(key => !query[key] || row[key] === query[key]);
}
const cancelled = () => Object.assign(Error('Superseded inventory request'), {name: 'AbortError'});
export class InventoryPager {
  constructor(fetchPage, pageSize = 100) { this.fetchPage = fetchPage; this.pageSize = pageSize; this.invalidate(); }
  invalidate() { this.pages = new Map(); this.scans = new Map(); }
  async page(query = {}, index = 0, {current = () => true, progress = () => {}, complete = false} = {}) {
    const check = () => { if (!current()) throw cancelled(); };
    const server = Object.fromEntries(['q', 'component', 'tag', 'metadata', 'state', 'outcome', 'execution_mode'].filter(key => query[key]).map(key => [key, query[key]]));
    const key = JSON.stringify(Object.fromEntries(Object.entries(server).sort(([a],[b])=>a.localeCompare(b)))), client = complete || this.scans.has('{}');
    const fetch = async (cursor, limit) => {
      check(); const params = new URLSearchParams({...server, limit: String(limit)}); if (cursor) params.set('cursor', cursor);
      const page = await this.fetchPage(params); check();
      if (page.api_version !== 1 || !Array.isArray(page.items)) throw Error('Unsupported inventory contract.');
      return page;
    };
    if (client) {
      let scan = this.scans.get('{}') || this.scans.get(key);
      if (!scan) {
        const rows = new Map(), cursors = new Set(); let cursor = null, revision, total, confirmed = true;
        do {
          const value = await fetch(cursor, 500);
          if (revision !== undefined && revision !== value.inventory_revision) throw Object.assign(Error('Inventory changed while filtering. Retry with the new revision.'), {status: 409});
          revision = value.inventory_revision; total = value.total ?? null; confirmed &&= value.total_confirmed !== false && total != null;
          value.items.forEach(row => rows.set(rowIdentity(row), row));
          progress(rows.size, total); cursor = value.next_cursor;
          if (cursor && cursors.has(cursor)) throw Error('The inventory returned a repeated cursor.');
          if (cursor) cursors.add(cursor);
        } while (cursor);
        check(); scan = {rows: [...rows.values()], revision, confirmed}; this.scans.set(key, scan);
      }
      const rows = scan.rows.filter(row => matchesTest(row, query));
      index = Math.min(index, Math.max(0, Math.ceil(rows.length / this.pageSize) - 1));
      const facets = {component:[...new Set(scan.rows.map(row=>row.component).filter(Boolean))].sort(),tag:[...new Set(scan.rows.flatMap(row=>row.tags||[]))].sort(),metadata:[...new Set(scan.rows.map(row=>row.metadata_status).filter(Boolean))].sort()};
      return {items: rows.slice(index * this.pageSize, (index + 1) * this.pageSize), total: scan.confirmed ? rows.length : null,
        inventory_revision: scan.revision, hasNext: (index + 1) * this.pageSize < rows.length, index, facets, scanTotal: scan.rows.length};
    }
    const pages = this.pages.get(key) || []; this.pages.set(key, pages);
    for (let position = 0; position <= index; position++) {
      if (pages[position]) continue;
      if (position > 0 && !pages[position - 1]?.next_cursor) { index = position - 1; break; }
      const value = await fetch(position ? pages[position - 1].next_cursor : null, this.pageSize);
      if (position && pages[0].inventory_revision !== value.inventory_revision) throw Object.assign(Error('Inventory revision changed.'), {status: 409});
      check(); pages[position] = value;
    }
    const value = pages[index];
    return {...value, total: value.total_confirmed === false ? null : value.total, hasNext: !!value.next_cursor, index};
  }
  patch(updates) {
    const change = rows => rows.forEach(row => { const update = updates.find(item => rowIdentity(item) === rowIdentity(row)); if (update) Object.assign(row, update); });
    for (const pages of this.pages.values()) pages.forEach(page => change(page.items));
    for (const scan of this.scans.values()) change(scan.rows);
  }
}
export function reportPresentation(report = {}, error = null) {
  if (error?.status === 410 || error?.code === 'evidence_cleaned') return {kind: 'cleaned', title: 'Evidence no longer available', text: error?.message || 'The service reports that this report has been removed.'};
  if (error) return {kind: 'unavailable', title: 'Report request failed', text: error.message};
  if (report.status === 'failed') return {kind: 'failed', title: 'Report generation failed', text: report.generation_error || 'The renderer failed. The test outcome and acceptance evidence are unchanged.'};
  if (report.status === 'provisional' || report.status === 'pending') return {kind: 'provisional', title: 'Provisional · results pending', text: 'The test is queued or running. Final observations are not available yet.'};
  if (report.status === 'ready' && (report.html_href || report.href)) return {kind: 'ready', title: 'HTML report ready', text: ''};
  if (report.status === 'ready') return {kind: 'unavailable', title: 'Report link missing', text: 'The service marked this report ready but supplied no HTML link.'};
  return {kind: 'unavailable', title: 'Report unavailable', text: 'No report availability or removal reason was supplied. No report was inferred.'};
}
export function logAvailability(chunk, state) {
  if (chunk.available === false) return chunk.reason === 'cleaned' ? 'Log evidence was cleaned.' : 'Log unavailable from this service.';
  return chunk.eof && !['queued', 'running'].includes(state) ? 'No additional output.' : 'No output yet.';
}
export function appendLog(buffer, chunk, requestedOffset, maxBytes = 1048576) {
  if (!Number.isSafeInteger(chunk.next_offset) || chunk.next_offset < requestedOffset) throw Error('Invalid log offset. Last bytes retained.');
  if (chunk.text && chunk.next_offset === requestedOffset) throw Error('Log cursor did not advance. Last bytes retained.');
  let bytes = new TextEncoder().encode((buffer.text || '') + (chunk.text || '')), trimmed = buffer.trimmed || false;
  if (bytes.length > maxBytes) { let start = bytes.length - maxBytes; while (start < bytes.length && (bytes[start] & 0xc0) === 0x80) start++; bytes = bytes.slice(start); trimmed = true; }
  return {text: new TextDecoder().decode(bytes), offset: chunk.next_offset, start: chunk.next_offset - bytes.length, trimmed};
}

export const inventoryNeedsReload = (events, query, hasCompleteSnapshot = false) => events.some(event => event.event_type === 'collection_finished') || !!events.length && ['state','outcome','execution_mode'].some(key=>query[key]) && !hasCompleteSnapshot;
