import {button, cell, checkbox, dialogForm, el, empty, feedback, field, grid, icon, link, metric,
  pageHeader, progress, recordCard, runBar, section, split, status, table} from "./components.js";

const $ = id => document.getElementById(id);
const workspace = $("workspace");
const dialog = $("dialog");
const active = new Set(["queued", "running"]);
const state = {catalog: null, selected: new Set(), filter: "", kind: "", result: "", log: null};
const nav = [["overview", "Overview"], ["library", "Case library"],
  ["playlists", "Playlists"], ["runs", "Runs"]];
let toastTimer;
let generation = 0;
let refreshing = false;

const route = () => {
  const [view, ...parts] = location.hash.slice(1).split("/");
  let id = "";
  try { id = decodeURIComponent(parts.join("/")); } catch { /* invalid deep link */ }
  return [view || "overview", id];
};
const to = (view, id) => `#${view}${id ? "/" + encodeURIComponent(id) : ""}`;
const nameOf = row => row.title || row.id.split("/").at(-1);
const count = (amount, noun) => `${amount} ${noun}${amount === 1 ? "" : "s"}`;
const duration = seconds => seconds == null ? "—" : seconds < 1 ? `${Math.round(seconds * 1000)} ms` :
  seconds < 60 ? `${seconds.toFixed(1)} s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
const timestamp = value => value ? new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric",
  hour: "numeric", minute: "2-digit"}).format(new Date(value)) : "—";

async function api(path, method = "GET", payload) {
  const options = method === "GET" ? {} : {method, headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload || {})};
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok || !data.ok) throw Error(data.error?.message || response.statusText);
  return data;
}

function toast(text) {
  clearTimeout(toastTimer);
  $("toast").textContent = text;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => $("toast").hidden = true, 6000);
}

async function refresh(sync = false) {
  if (refreshing) return;
  refreshing = true;
  $("refresh").disabled = true;
  try {
    state.catalog = await api(sync ? "/api/sync" : "/api/catalog", sync ? "POST" : "GET");
    const current = new Set(state.catalog.cases.map(row => row.id));
    for (const id of state.selected) if (!current.has(id)) state.selected.delete(id);
    render();
  } catch (error) {
    if (state.catalog) toast(error.message);
    else workspace.replaceChildren(empty("The library could not be loaded", error.message,
      button("Try again", () => refresh(true))));
  } finally {
    refreshing = false;
    $("refresh").disabled = false;
    workspace.setAttribute("aria-busy", "false");
  }
}

function currentJob(data) {
  return data.jobs.find(job => job.status === "running") || data.jobs.find(job => job.status === "queued");
}

function shell(data, view, id) {
  const navState = {project: "overview", shelf: "overview", playlist: "playlists", job: "runs"};
  $("navigation").replaceChildren(...nav.map(([value, label]) =>
    el("a", {class: "nav-link", href: to(value), "aria-current": (navState[view] || view) === value && "page"},
      icon(value), el("span", {class: "nav-label"}, label))));
  $("shelves").replaceChildren(...(data.shelves.length ? [el("div", {class: "rail-heading"}, "Shelves"),
    ...data.shelves.map(shelf => el("a", {class: "nav-link", href: to("shelf", shelf.id),
      "aria-current": view === "shelf" && id === shelf.id && "page"},
    icon("shelf"), el("span", {class: "nav-label"}, shelf.title)))] : []));
  $("host").textContent = `Local · ${data.host.system} ${data.host.machine}`;
  $("breadcrumbs").replaceChildren(el("span", {}, "Workspace"), el("span", {class: "separator", "aria-hidden": "true"}, "/"),
    el("strong", {title: data.library.title}, data.library.title));
  const job = currentJob(data);
  const bar = $("run-bar");
  bar.hidden = !job;
  if (job) bar.replaceChildren(runBar(job, to("job", job.id)));
}

function overview(data, shelfId) {
  const shelf = shelfId && data.shelves.find(item => item.id === shelfId);
  if (shelfId && !shelf) return missing("Shelf");
  const projects = data.projects.filter(row => !shelf || row.shelf === shelf.id);
  const cases = data.cases.filter(row => !shelf || row.shelf === shelf.id);
  const saved = data.playlists.filter(row => row.stored && (!shelf || row.shelf === shelf.id));
  const recent = shelf ? data.jobs.filter(job => job.cases.some(row => projects.some(item => item.id === row.project))) : data.jobs;
  const passed = cases.filter(row => ["passed", "reused"].includes(row.last?.status)).length;
  const waiting = recent.filter(job => active.has(job.status)).length;
  return [pageHeader(shelf ? shelf.title : "Playlists", shelf ? "Projects and playlists on this shelf." :
    "Organize research checks, run selected cases, and follow the results.",
  [button("New playlist", () => editDialog({shelf: shelf?.id}), {variant: "primary", glyph: "plus"})]),
  data.messages.length ? feedback(data.messages) : null,
  grid(cell(3, metric("Projects", projects.length, "project", `${projects.filter(row => row.available).length} available locally`)),
    cell(3, metric("Cases", cases.length, "cases", `${passed} passed on the latest run`)),
    cell(3, metric("Saved playlists", saved.length, "playlists", "Reusable selections")),
    cell(3, metric("Runs", recent.length, "runs", waiting ? `${waiting} active or queued` : "No active jobs"))),
  split(section("Projects", projects.length ? grid(projects.map(row =>
    cell(projects.length === 1 ? 12 : 6, projectCard(row, cases)))) :
    empty("No projects on this shelf", "Register a project in the playlist library."),
  link("Browse cases", to("library"), {glyph: "arrow"})),
  section("Recent runs", recent.length ? el("div", {class: "panel"},
    recent.slice(0, 4).map(job => jobLink(job))) : empty("No runs yet", "Select cases in the library or run a saved playlist."),
  link("All runs", to("runs"), {glyph: "arrow"}))),
  section("Saved playlists", saved.length ? grid(saved.map(row => cell(saved.length === 1 ? 6 : 4, playlistCard(row)))) :
    empty("No saved playlists", "Save the cases you expect to run together.",
      button("Create playlist", () => editDialog({shelf: shelf?.id}), {glyph: "plus"})),
  saved.length ? link("All playlists", to("playlists"), {glyph: "arrow"}) : null)];
}

function projectCard(row, cases) {
  const own = cases.filter(item => item.project === row.id);
  const sources = row.sources.map(source => source.id).join(", ");
  return recordCard({title: row.title, subtitle: sources, name: "project", href: to("project", row.id),
    state: row.available ? "ready" : "unavailable", detail: count(own.length, "case")});
}

function playlistCard(row) {
  return recordCard({title: row.title, subtitle: row.id, name: "playlists", href: to("playlist", row.id),
    state: row.problem ? "blocked" : null, detail: count(row.members.length, "case")});
}

function jobLink(job, withCount = false) {
  return el("a", {class: "list-row", href: to("job", job.id)},
    el("div", {}, el("strong", {}, job.label), el("p", {}, withCount ? count(job.cases.length, "case") :
      timestamp(job.finished_at || job.submitted_at))), status(job.status));
}

function select(label, values, value, onchange) {
  return el("select", {"aria-label": label, onchange: event => onchange(event.currentTarget.value)},
    values.map(([key, caption]) => el("option", {value: key, selected: key === value}, caption)));
}

function selections() {
  const amount = state.selected.size;
  return el("div", {class: "filters"},
    el("span", {class: "subtext"}, amount ? `${amount} selected` : "Select cases to run or save"),
    button("Run selected", () => confirmRun({cases: [...state.selected]}, amount), {glyph: "run", disabled: !amount}),
    button("Save selection", () => editDialog({entries: [...state.selected].map(caseId => ({case: caseId}))}),
      {glyph: "plus", disabled: !amount}));
}

function caseTable(rows, label = "Cases") {
  if (!rows.length) return empty("No matching cases", "Clear the filters or check the project catalog.");
  return table(["", "Case", "Kind", "Latest status", "Time"], rows.map(row => {
    const check = checkbox(`Select ${nameOf(row)}`, state.selected.has(row.id), checked => {
      if (checked) state.selected.add(row.id);
      else state.selected.delete(row.id);
      const [view] = route();
      const slot = $("case-actions");
      if (slot && ["library", "project"].includes(view)) slot.replaceChildren(selections());
    });
    return [check, el("div", {class: "case-cell"}, el("span", {class: "case-name"}, nameOf(row)),
      el("code", {class: "case-id"}, row.id)), row.kind, row.last?.job ?
      el("a", {href: to("job", row.last.job), title: "View latest run"}, status(row.last.status)) :
      status("not_run"), duration(row.last?.seconds)];
  }), label);
}

function library(data, projectId) {
  const project = projectId && data.projects.find(row => row.id === projectId);
  if (projectId && !project) return missing("Project");
  const search = $("search").value.trim().toLowerCase();
  const rows = data.cases.filter(row => (!project || row.project === project.id) &&
    (project || !state.filter || row.project === state.filter) &&
    (!state.kind || row.kind === state.kind) && (!state.result || (row.last?.status || "not_run") === state.result) &&
    (!search || [nameOf(row), row.id, ...(row.tags || [])].join(" ").toLowerCase().includes(search)));
  const profiles = data.playlists.filter(row => !row.stored && row.project === project?.id);
  return [pageHeader(project?.title || "Case library", project ? "Cases and derived profiles for this project." :
    "Browse test groups and command cases across your workspace.", [], project ? {label: "Overview", href: to("overview")} : null),
  project && !project.available ? feedback(["Register this project’s checkout to browse and run its cases."]) : null,
  profiles.length ? section("Profiles", grid(profiles.map(row => cell(4, playlistCard(row))))) : null,
  section(count(rows.length, "case"), [
    el("div", {class: "filters"},
      !project && select("Project filter", [["", "All projects"], ...data.projects.map(row => [row.id, row.title])],
        state.filter, value => { state.filter = value; render(); }),
      select("Kind filter", [["", "All kinds"], ["pytest", "Pytest"], ["check", "Check"], ["command", "Command"]],
        state.kind, value => { state.kind = value; render(); }),
      select("Status filter", [["", "All statuses"], ["not_run", "Not run"], ["passed", "Passed"],
        ["reused", "Reused"], ["running", "Running"], ["queued", "Queued"], ["failed", "Failed"],
        ["skipped", "Skipped"], ["interrupted", "Interrupted"]],
      state.result, value => { state.result = value; render(); })),
    el("div", {id: "case-actions"}, selections()), caseTable(rows)])];
}

function playlists(data) {
  const stored = data.playlists.filter(row => row.stored);
  const derived = data.playlists.filter(row => !row.stored);
  return [pageHeader("Playlists", "Run a live profile or save a group of cases you use together.",
    [button("New playlist", () => editDialog({}), {variant: "primary", glyph: "plus"})]),
  section("Saved", stored.length ? grid(stored.map(row => cell(4, playlistCard(row)))) :
    empty("No saved playlists", "Create a playlist or save a selection from the case library.",
      link("Open case library", to("library"), {glyph: "arrow"}))),
  section("Derived profiles", derived.length ? grid(derived.map(row => cell(4, playlistCard(row)))) :
    empty("No derived profiles", "Test cards with named profiles appear here when their checkouts are available."))];
}

function playlist(data, id) {
  const item = data.playlists.find(row => row.id === id);
  if (!item) return missing("Playlist");
  const members = item.members.map(member => data.cases.find(row => row.id === member)).filter(Boolean);
  const actions = [button("Run playlist", () => confirmRun({playlist: item.id}, members.length),
    {variant: "primary", glyph: "run", disabled: !members.length})];
  if (item.stored) actions.unshift(button("Add entries", () => editDialog({item}), {glyph: "plus"}));
  const summary = item.shelf ? data.shelves.find(shelf => shelf.id === item.shelf)?.title : null;
  const entries = item.stored ? section("Saved entries", item.entries.length ? table(["Entry", "Type", ""],
    item.entries.map(entry => [el("code", {class: "breakable"}, entry.case || entry.profile),
      entry.case ? "Case" : "Profile", button("Remove", () => removeDialog(item, entry), {variant: "subtle"})]), "Playlist entries") :
    empty("No entries", "Add cases or live profiles to this playlist.")) : null;
  const metadata = [
    ["Storage", item.stored ? "Saved playlist" : "Derived profile"], ["Shelf", summary || "Library"],
    ["Environment", item.environment || "Project default"], ["Resolved members", members.length],
  ];
  const details = el("div", {class: "panel"}, metadata.map(([label, value]) =>
    el("div", {class: "list-row"}, el("span", {class: "subtext"}, label),
      el("strong", {class: "breakable"}, value))));
  const resolved = members.length ?
    table(["Case", "Kind", "Latest status"], members.map(row => [
      el("div", {class: "case-cell"}, el("span", {}, nameOf(row)), el("code", {class: "case-id"}, row.id)),
      row.kind, status(row.last?.status),
    ]), "Resolved cases") : empty("No runnable cases", "Check the project catalog or add entries.");
  return [pageHeader(item.title, item.goal || item.id, actions, {label: "All playlists", href: to("playlists")}),
    item.problem ? feedback([item.problem]) : null,
    split([entries, section("Resolved cases", resolved)], section("Details", details))];
}

function runs(data) {
  const ongoing = data.jobs.filter(job => active.has(job.status));
  const finished = data.jobs.filter(job => !active.has(job.status));
  return [pageHeader("Runs", "Track queued work and open case results.",
    [link("Browse cases", to("library"), {variant: "button secondary", glyph: "arrow"})]),
  section("In progress", ongoing.length ? el("div", {class: "panel"}, ongoing.map(job => jobLink(job, true))) :
    empty("Queue is empty", "New runs appear here until they finish.")),
  section("Run history", finished.length ? table(["Run", "Started", "Cases", "Status"],
    finished.map(job => [link(job.label, to("job", job.id)), timestamp(job.started_at || job.submitted_at),
      job.cases.length, status(job.status)]), "Run history") :
    empty("No finished runs", "Finished runs and their logs appear here."))];
}

function jobPage(data, id) {
  const job = data.jobs.find(item => item.id === id);
  if (!job) return missing("Run");
  const complete = job.cases.filter(item => !active.has(item.status)).length;
  const logId = state.log?.job === id ? state.log.case : job.cases.find(row => !active.has(row.status))?.id;
  const cancel = button("Cancel run", () => confirmDialog("Cancel run?", "This interrupts the active cases.",
    "Cancel run", async () => { await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, "POST"); await refresh(); }),
    {variant: "danger"});
  const logs = el("pre", {class: "log", id: "job-log", tabindex: "0"},
    state.log?.job === id && state.log.case === logId ? state.log.text || "Waiting for output…" : "Select a case to view output.");
  if (logId) updateLog(id, logId, generation);
  return [pageHeader(job.label, job.id, active.has(job.status) ? [cancel] : [],
    {label: "All runs", href: to("runs")}),
  job.reason ? feedback([job.reason]) : null,
  el("div", {class: "panel"},
    el("div", {class: "panel-head"}, status(job.status), el("span", {class: "subtext"}, `${complete} of ${job.cases.length} complete`)),
    el("div", {class: "form-row"}, progress(complete, job.cases.length, "Case completion"))),
  split(section("Case results", table(["Case", "Status", "Time", ""], job.cases.map(row => [
    el("div", {class: "case-cell"}, el("span", {}, nameOf(row)), el("code", {class: "case-id"}, row.id),
      row.reason && el("p", {class: "subtext breakable"}, row.reason)), status(row.status),
    duration(row.seconds), button("Log", () => {
      state.log = {job: id, case: row.id, text: ""};
      render();
    }, {variant: "subtle"})]), "Case results")),
  section("Output", el("div", {class: "panel"}, logs)))];
}

async function updateLog(job, caseId, pass) {
  try {
    const data = await api(`/api/jobs/${encodeURIComponent(job)}/log?case=${encodeURIComponent(caseId)}`);
    if (pass !== generation) return;
    state.log = {job, case: caseId, text: data.text};
    if ($("job-log")) $("job-log").textContent = data.text || "Waiting for output…";
  } catch (error) {
    if (pass === generation && $("job-log")) $("job-log").textContent = error.message;
  }
}

function missing(label) {
  return empty(`${label} unavailable`, "It may have been removed from the library.",
    link("Return to overview", to("overview"), {glyph: "back"}));
}

function render() {
  if (!state.catalog) return;
  const data = state.catalog;
  const [view, id] = route();
  generation += 1;
  shell(data, view, id);
  const views = {overview: () => overview(data), shelf: () => overview(data, id),
    library: () => library(data), project: () => library(data, id), playlists: () => playlists(data),
    playlist: () => playlist(data, id), runs: () => runs(data), job: () => jobPage(data, id)};
  workspace.replaceChildren(...[Object.hasOwn(views, view) ? views[view]() : missing("View")].flat(Infinity).filter(Boolean));
}

function confirmDialog(title, text, label, work) {
  dialogForm(dialog, title, el("p", {}, text), label, work);
}

function confirmRun(choice, size) {
  dialogForm(dialog, "Run verification", [el("p", {}, count(size, "selected case")),
    field("Parallel projects", el("input", {name: "jobs", type: "number", min: 1, max: 8, value: 1})),
    el("label", {class: "form-row run-info"}, el("input", {type: "checkbox", name: "fresh"}), "Run fresh")],
  "Queue run", async data => {
    const {job} = await api("/api/jobs", "POST", {...choice, jobs: Number(data.get("jobs")), fresh: data.has("fresh")});
    state.selected.clear();
    state.catalog.jobs.unshift(job);
    location.hash = to("job", job.id);
    await refresh();
    toast("Run queued");
  });
}

function removeDialog(item, entry) {
  const fields = [el("p", {class: "breakable"}, entry.case || entry.profile)];
  if (item.frozen) fields.push(field("Reason", el("input", {name: "reason", required: true})));
  dialogForm(dialog, "Remove entry", fields, "Remove", async form => {
    await api(`/api/playlists/${encodeURIComponent(item.id)}`, "PUT",
      {op: "remove", entries: [entry], digest: item.digest, ...(item.frozen && {reason: form.get("reason")})});
    await refresh();
    toast("Playlist updated");
  });
}

function editDialog({item, shelf, entries = []}) {
  const data = state.catalog;
  const matches = data.cases.filter(row => !item?.entries.some(entry => entry.case === row.id) &&
    !entries.some(entry => entry.case === row.id));
  const profiles = data.playlists.filter(row => !row.stored && !item?.entries.some(entry => entry.profile === row.id));
  const choice = (name, caption, values, mode = false) => el("select", {name, multiple: mode, size: mode ? 4 : null},
    !mode && el("option", {value: ""}, caption), values);
  const inputs = [
    !item && grid(cell(6, field("Title", el("input", {name: "title", required: true, placeholder: "e.g. Arithmetic checks"}))),
      cell(6, field("ID", el("input", {name: "id", required: true, pattern: "[A-Za-z0-9][A-Za-z0-9_.\\-]{0,127}",
        placeholder: "arithmetic-checks"})))),
    !item && field("Shelf", choice("shelf", "No shelf", data.shelves.map(row =>
      el("option", {value: row.id, selected: shelf === row.id}, row.title)))),
    entries.length && el("p", {class: "form-row subtext"}, `${count(entries.length, "case")} from the current selection`),
    matches.length > 0 && field("Cases", choice("case", "", matches.map(row => el("option", {value: row.id}, nameOf(row))), true)),
    profiles.length > 0 && field("Live profile", choice("profile", "Select a profile", profiles.map(row =>
      el("option", {value: row.id}, row.id)))),
    !item && el("details", {class: "form-row"}, el("summary", {}, "Combined playlist settings"),
      field("Goal", el("textarea", {name: "goal", rows: 2})),
      field("Named environment", el("input", {name: "environment", placeholder: "Environment name"}))),
    item?.frozen && field("Reason", el("input", {name: "reason", required: true}))
  ].filter(Boolean);
  dialogForm(dialog, item ? "Add playlist entries" : "New playlist", inputs, item ? "Add entries" : "Create playlist", async form => {
    const cases = form.getAll("case").map(value => ({case: value}));
    const profile = form.get("profile");
    const joined = [...entries, ...cases, ...(profile ? [{profile}] : [])];
    if (item) {
      await api(`/api/playlists/${encodeURIComponent(item.id)}`, "PUT",
        {op: "add", digest: item.digest, entries: joined, ...(item.frozen && {reason: form.get("reason")})});
    } else {
      const fields = Object.fromEntries(["id", "title", "shelf", "goal", "environment"].map(key => [key, form.get(key)])
        .filter(([, value]) => value?.trim()));
      const result = await api("/api/playlists", "POST", {...fields, entries: joined});
      location.hash = to("playlist", result.playlist.id);
    }
    await refresh();
    toast(item ? "Playlist updated" : "Playlist created");
  });
}

document.querySelector(".skip-link").addEventListener("click", event => {
  event.preventDefault();
  workspace.focus();
});
window.addEventListener("hashchange", () => {
  state.filter = state.kind = state.result = "";
  render();
});
$("refresh").onclick = () => refresh(true);
$("search").addEventListener("input", () => {
  if (!["library", "project"].includes(route()[0])) location.hash = to("library");
  else render();
});
document.addEventListener("keydown", event => {
  if (event.key === "/" && !dialog.open && !event.target.matches("input, select, textarea")) {
    event.preventDefault();
    $("search").focus();
  }
});
refresh();
setInterval(() => {
  if (state.catalog && currentJob(state.catalog) && !dialog.open &&
      !workspace.contains(document.activeElement)) refresh();
}, 1200);
setInterval(() => {
  if (state.catalog && !currentJob(state.catalog) && !dialog.open &&
      !workspace.contains(document.activeElement)) refresh();
}, 8000);
