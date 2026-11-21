// Progress snapshots are facts; steps never impersonate leaf test counts.
export const progressSnapshots = new Map();
const active = job => ['queued', 'running', 'collecting'].includes(job.status);
function finalOnly(job, row) {
  const snapshot = job.progress_snapshot;
  const steps = snapshot?.steps?.length ? snapshot.steps : job.cases || [];
  const runningSteps = steps.filter(step => ['queued','running'].includes(step.state || step.status));
  const adapters = new Set(row.adapter ? [row.adapter] : (runningSteps.length ? runningSteps : steps).map(step => step.adapter || step.kind).filter(Boolean));
  const relevant = (snapshot?._capabilities?.adapters || job._testCapabilities?.adapters || []).filter(cap => adapters.has(cap.adapter));
  return row.progress === 'final_only' || row.capabilities?.progress === 'final_only' || relevant.some(cap => cap.progress === 'final_only');
}
function groupProgress(job, row) {
  const collecting = job.collection_status === 'running' || job.progress_snapshot?.collection_status === 'running';
  const waiting = active(job) && finalOnly(job, row);
  const collectionFailed = job.collection_status === 'failed' || job.progress_snapshot?.collection_status === 'failed';
  const valid = Number.isSafeInteger(row.total) && row.total >= 0 && Number.isSafeInteger(row.completed) && row.completed >= 0 && row.completed <= row.total;
  const known = valid && row.total_confirmed !== false && !waiting && !collecting && !collectionFailed;
  const completed = Number.isSafeInteger(row.completed) && row.completed >= 0 ? row.completed : 0;
  const noun = row.unit === 'test_case' ? 'tests' : row.unit === 'step' ? 'steps' : row.unit || 'items';
  return {...row, total: known ? row.total : null, completed, known, noun,
    label: collecting ? 'Collecting' : collectionFailed ? 'Collection failed' : waiting ? 'Waiting for final report' : known ? `${completed} / ${row.total} ${noun}` : active(job) ? 'Waiting for test total' : 'Test total unavailable'};
}
export function jobProgress(job) {
  const summaries = job.progress_snapshot?.summaries || [], tests = summaries.filter(row => row.unit === 'test_case');
  if (tests.length === 1) return {...groupProgress(job, tests[0]), current: job.progress_snapshot.current_test?.title || '',
    groups: summaries.filter(row => row.unit !== 'test_case').map(row => groupProgress(job, row))};
  if (tests.length > 1) return {total: null, completed: 0, known: false, noun: 'tests', label: 'Multiple test groups · View run', groups: summaries.map(row => groupProgress(job, row))};
  if (summaries.length === 1) return {...groupProgress(job, summaries[0]), current: job.progress_snapshot?.current_test?.title || ''};
  return {total: null, completed: 0, known: false, noun: 'tests',
    label: summaries.length ? 'Multiple result groups · View run' : job.collection_status === 'running' ? 'Collecting' : active(job) ? 'Waiting for test total' : 'Test progress unavailable', groups: summaries.map(row => groupProgress(job, row))};
}
export function attachProgress(data) {
  for (const job of data.jobs || []) if (progressSnapshots.has(job.id)) job.progress_snapshot = progressSnapshots.get(job.id);
  return data;
}
