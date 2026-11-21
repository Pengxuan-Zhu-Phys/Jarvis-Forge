import {apiFetch} from './api.js';
import {progressSnapshots} from './progress-model.js';
const pending = new Map();
let capabilitiesPromise;
export async function request(path, options = {}) {
  const key = `${options.method || 'GET'} ${path}`;
  const deduplicate = (options.method || 'GET') === 'GET' && !options.signal;
  if (deduplicate && pending.has(key)) return pending.get(key);
  const work = (async () => {
    const response = await apiFetch(path, options);
    let value;
    try { value = await response.json(); } catch { throw Error(`Invalid API response (${response.status}).`); }
    if (!response.ok || value.ok === false) {
      const error = Error(value.error?.message || `Request failed (${response.status}).`);
      error.status = response.status; error.code = value.error?.code; error.details = value.error?.details; throw error;
    }
    return value;
  })();
  if (deduplicate) pending.set(key, work);
  try { return await work; } finally { if (deduplicate) pending.delete(key); }
}
export function capabilities(retry = false) {
  if (retry) capabilitiesPromise = null;
  return capabilitiesPromise ||= request('/api/capabilities').then(caps => {
    if (caps.api_version !== 1 || caps.features?.test_cases !== true) throw Error('Forge test-report API v1 is required. Update jforge and restart the dashboard.');
    return caps;
  }).catch(error => {capabilitiesPromise = null; throw error;});
}

export const jobPath = job => `/api/jobs/${encodeURIComponent(job)}`;
export const casePath = (job, instance) => `${jobPath(job)}/test-cases/${encodeURIComponent(instance)}`;
export function reportRoute(job, instance) { return `#report/${encodeURIComponent(job)}/${encodeURIComponent(instance)}`; }
export function safeHref(value) {
  if (!value) return null;
  try { const url = new URL(value, location.origin);
    return url.origin === location.origin && url.pathname.startsWith('/') && !url.username && !url.password && ['http:', 'https:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}
export async function loadProgress(job) {
  const id = typeof job === 'string' ? job : job.id;
  const caps = await capabilities();
  if (typeof job === 'object') job._testCapabilities = caps;
  const value = await request(`${jobPath(id)}/progress`);
  if (value.api_version !== 1 || !Array.isArray(value.summaries)) throw Error('Unsupported progress contract.');
  if (typeof job === 'object') {
    if (['queued','running','passed','failed','interrupted'].includes(value.status)) job.status = value.status;
    for (const step of value.steps || []) {const target = job.cases?.find(row=>row.id===step.id); if (target) Object.assign(target, step);}
  }
  value._capabilities = caps;
  progressSnapshots.set(id, value);
  return value;
}
