import {scenarioFromSearch, scenarioCatalog} from './demo-scenarios.js';
import {demoTestFetch} from './demo-test-api.js';
/* Browser-local demo data for the design workspace. No connection to Forge. */
const storageKey = "forge-playlist-design-baseline-v1";
import {demoProjects, demoCases as cases, demoAlbums, demoJobs} from "./demo-data.js";
const selectedScenario = scenarioFromSearch(location.search);
const scenarioData = selectedScenario && scenarioCatalog(selectedScenario);
const projectData = scenarioData?.projects || demoProjects;
const caseData = scenarioData?.cases || cases;
const initial = scenarioData || {playlists: demoAlbums, jobs: demoJobs};
let state = structuredClone(initial);
try {
  const snapshot = JSON.parse(localStorage.getItem(storageKey) || "null");
  if (!selectedScenario && Array.isArray(snapshot?.playlists) && Array.isArray(snapshot?.jobs)) {
    // Preserve saved selections and run history while adding new demo project albums.
    state.playlists = initial.playlists.map(seed => ({...seed, ...snapshot.playlists.find(row => row.id === seed.id)}));
    state.playlists.push(...snapshot.playlists.filter(row => !initial.playlists.some(seed => seed.id === row.id)));
    state.jobs = [...snapshot.jobs, ...initial.jobs.filter(seed => !snapshot.jobs.some(row => row.id === seed.id))];
  }
} catch { /* A blocked storage policy still permits an in-memory preview. */ }

function remember() {
  if (selectedScenario) return;
  try { localStorage.setItem(storageKey, JSON.stringify(state)); }
  catch { /* Preview state can stay in memory. */ }
}

function response(value, code = 200) {
  return new Response(JSON.stringify(value), {
    status: code, headers: {"Content-Type": "application/json"}
  });
}
const ok = value => response({ok: true, ...value});
const failure = (message, status = 400) => response({ok: false, error: {message}}, status);
const caseIds = new Set(caseData.map(row => row.id));

const queueRank = job => job.queueOrder ?? (Date.parse(job.submitted_at) || 0);
let nextQueueOrder = Math.max(0, ...state.jobs.map(queueRank)) + 1;
const inQueueOrder = (a, b) => queueRank(a) - queueRank(b) || state.jobs.indexOf(b) - state.jobs.indexOf(a);
function beginJob(job, started) {
  Object.assign(job, {status: "running", demoStartedAt: started,
    started_at: new Date(started).toISOString(), finishAt: started + job.cases.length * 1500});
  job.cases.forEach((row, index) => { row.status = index === 0 ? "running" : "queued"; delete row.seconds; });
}
function advanceJobs() {
  if (selectedScenario) return;
  const now = Date.now();
  // Older previews allowed concurrent jobs. Keep the oldest current run and queue the rest.
  const concurrent = state.jobs.filter(job => job.status === "running").sort(inQueueOrder);
  for (const job of concurrent.slice(1)) {
    job.status = "queued";
    delete job.demoStartedAt; delete job.finishAt; delete job.started_at;
    job.cases.forEach(row => { row.status = "queued"; delete row.seconds; });
  }
  let availableAt = now;
  while (true) {
    let job = state.jobs.find(row => row.status === "running");
    if (!job) {
      job = state.jobs.filter(row => row.status === "queued").sort(inQueueOrder)[0];
      if (!job) break;
      beginJob(job, Math.max(availableAt, Date.parse(job.submitted_at) || availableAt));
    }
    const started = job.demoStartedAt ?? (job.finishAt - job.cases.length * 1500);
    const complete = Math.min(job.cases.length, Math.max(0, Math.floor((now - started) / 1500)));
    job.cases.forEach((row, index) => {
      row.status = index < complete ? (row.demoOutcome || "passed") : index === complete ? "running" : "queued";
      if (index < complete) row.seconds = .12;
    });
    if (complete < job.cases.length) break;
    availableAt = started + job.cases.length * 1500;
    Object.assign(job, {status: job.cases.some(row => row.status === "failed") ? "failed" : "passed",
      finished_at: new Date(availableAt).toISOString()});
  }
}

function catalog() {
  return {
    library: {id: "design-preview", title: "Research Workshop"}, host: {system: "Demo", machine: "Browser"},
    shelves: [{id: "projects", title: "Projects"}, {id: "examples", title: "Examples"}],
    projects: projectData,
    playlists: state.playlists, jobs: state.jobs, messages: selectedScenario ? [`Independent Demo scenario · ${selectedScenario.title}. ${selectedScenario.description} No local tests executed.`] : [],
    cases: caseData.map(row => {
      const latest = state.jobs.find(job => job.cases.some(item => item.id === row.id));
      const result = latest?.cases.find(item => item.id === row.id);
      return {...row, last: result ? {...result, job: latest.id} : null};
    })
  };
}

export async function previewFetch(path, {method = "GET", body} = {}) {
  const url = new URL(path, location.origin);
  const [, , resource, rawId, action] = url.pathname.split("/");
  const id = rawId && decodeURIComponent(rawId);
  const payload = body ? JSON.parse(body) : {};
  advanceJobs();
  if (selectedScenario && resource === "jobs" && method === "POST" && !id) return failure("This is a controlled scenario snapshot. Return to ordinary Demo to submit runs.", 409);
  if (["capabilities", "test-discoveries"].includes(resource) || (resource === "jobs" && ["progress", "test-cases", "events"].includes(action))) {
    const testResponse = await demoTestFetch(url, {method, body}, state);
    if (testResponse) return testResponse;
  }

  if (resource === "catalog" || resource === "sync") {
    remember();
    return ok(catalog());
  }
  if (resource === "jobs") {
    if (method === "POST" && !id) {
      const playlist = state.playlists.find(row => row.id === payload.playlist);
      const ids = [...new Set(playlist?.members || payload.cases || [])];
      if (!ids.length || ids.some(key => !caseIds.has(key))) return failure("Choose at least one demo case.");
      const duplicate = payload.playlist && state.jobs.find(row => row.playlist === payload.playlist && ["running", "queued"].includes(row.status));
      if (duplicate) return failure(duplicate.status === "running" ? "This album is already running." : "This album is already in the queue.", 409);
      const submitted = Date.now();
      const job = {id: "demo-" + crypto.randomUUID(), label: playlist?.title || "Selected cases",
        playlist: playlist?.id || null, simulated: true, queueOrder: nextQueueOrder++,
        submitted_at: new Date(submitted).toISOString(), status: "queued",
        cases: ids.map(key => ({...caseData.find(row => row.id === key), status: "queued"}))};
      state.jobs.unshift(job);
      advanceJobs();
      remember();
      return ok({job});
    }
    const job = state.jobs.find(row => row.id === id);
    if (!job) return failure("Demo run not found.", 404);
    if (action === "log") {
      const row = job.cases.find(item => item.id === url.searchParams.get("case"));
      if (!row) return failure("Case not found in this demo run.", 404);
      const heading = "Design demo output (simulated; no local tests executed)\n";
      return ok({text: heading + (row.status === "passed" ? row.example :
        row.status === "failed" ? (row.failure || "Simulated check failed.") :
        row.status === "interrupted" ? "Demo run cancelled." : "Running demo case…")});
    }
    if (method === "POST" && action === "cancel") {
      if (payload.only_if_queued && job.status !== "queued") return failure("This album has started. Open its run to stop it.", 409);
      if (["running", "queued"].includes(job.status)) {
        Object.assign(job, {status: "interrupted", finished_at: new Date(Date.now()).toISOString()});
        for (const row of job.cases) if (["queued", "running"].includes(row.status)) row.status = "interrupted";
      }
      advanceJobs();
      remember();
      return ok({job});
    }
  }
  if (resource === "playlists") {
    if (method === "POST" && !id) {
      if (!payload.id || !payload.title) return failure("Enter a playlist title and ID.");
      if (state.playlists.some(row => row.id === payload.id)) return failure("That playlist ID already exists.", 409);
      const playlist = {...payload, stored: true, frozen: null, digest: crypto.randomUUID(),
        members: [...new Set(payload.entries.map(entry => entry.case))].filter(key => caseIds.has(key))};
      state.playlists.push(playlist);
      remember();
      return ok({playlist});
    }
    if (method === "PUT") {
      const playlist = state.playlists.find(row => row.id === id);
      if (!playlist) return failure("Playlist not found.", 404);
      if (playlist.digest !== payload.digest) return failure("Playlist changed. Reload and try again.", 409);
      const entries = new Map(playlist.entries.map(entry => [entry.case, entry]));
      const keys = payload.entries.map(entry => entry.case);
      if (!keys.length) return failure("Select at least one case.");
      for (const key of keys) {
        if (payload.op === "remove") entries.delete(key);
        else if (caseIds.has(key)) entries.set(key, {case: key});
      }
      Object.assign(playlist, {entries: [...entries.values()], members: [...entries.keys()], digest: crypto.randomUUID()});
      remember();
      return ok({playlist});
    }
  }
  return failure("This action is unavailable in the design preview.", 404);
}
