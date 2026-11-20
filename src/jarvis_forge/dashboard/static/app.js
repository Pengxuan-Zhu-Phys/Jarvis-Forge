import { apiFetch, demoMode } from "./api.js";
import {albumCard, albumPreview, breadcrumbs, button, cell, checkbox, designGrid, dialogForm, el, empty, feedback, field, formGrid, grid, gridTable, link, navigation, navigationShortcut,
  pageHeader, playerCapsule, playerNotice, progress, projectMark, projectShelf, recordCard, runBar, section, shellGuides, sidebarToggle, split, status, table} from "./components.js?v=1.0.0";

const APP_VERSION = "1.0.0";
const $ = id => document.getElementById(id);
const workspace = $("workspace");
const dialog = $("dialog");

// Shelf headers track navigation height; the floating player never changes page geometry.
const topbar = document.querySelector(".topbar");
const rail = document.querySelector(".rail");
topbar.append(...shellGuides(12, "topbar"));
rail.append(...shellGuides(4, "rail"));
const shellBars = new ResizeObserver(() => {
  workspace.style.setProperty("--shelf-sticky-top", `${topbar.getBoundingClientRect().height}px`);
  measureRunBar();
  measureShellChrome();
  measureWorkspaceGrid();
});
shellBars.observe(topbar);
shellBars.observe(rail);
shellBars.observe($("run-bar"));
function measureShellChrome() {
  const size = node => `${Math.round(node.getBoundingClientRect().width)}×${Math.round(node.getBoundingClientRect().height)}`;
  for (const [node, label] of [[$("breadcrumbs"), "4c"], [document.querySelector(".search"), "3c"],
      [$("refresh"), "1c"], [$("quick-jump"), "2c"], [$("design-toggle"), "2c"]]) {
    node.classList.add("shell-slot");
    node.dataset.measure = `${label} · ${size(node)}`;
  }
  const topStyles = getComputedStyle(topbar);
  topbar.querySelector(".shell-design-caption").textContent = `TOP · ${Math.round(topbar.getBoundingClientRect().height)}px H · 12×${Math.round(parseFloat(topStyles.gridTemplateColumns))}px · gap 16px · pad Y 16px`;
  for (const node of rail.querySelectorAll(".brand, .nav-link, .rail-heading, .host, .rail-shortcut, .rail-create, .rail-toggle")) {
    node.dataset.measure = railCollapsed ? size(node) : `${size(node)} px`;
    if (node.matches(".nav-link")) node.setAttribute("aria-label", node.querySelector(".nav-label").textContent);
    if (node.matches(".nav-link, .rail-toggle, .rail-create")) node.title = railCollapsed ? "" : node.getAttribute("aria-label");
  }
  rail.querySelector(".shell-design-caption").textContent = railCollapsed ? "80 / 1c" : `RAIL · ${size(rail)} px / 4×32 / g16`;
}

function measureRunBar() {
  const bar = $("run-bar");
  const content = bar.querySelector(".run-content");
  if (bar.hidden || !content) return;
  if (bar.classList.contains("is-compact")) {
    const {width, height} = bar.getBoundingClientRect();
    bar.dataset.measure = `CAPSULE · ${Math.round(width)}×${Math.round(height)} px · 4 cols`;
    const ring = bar.querySelector(".capsule-perimeter");
    if (ring && width && height) {
      const half = height / 2, radius = half - 1;
      // A real capsule path keeps its end circles concentric with the icons at every width.
      const path = `M ${width / 2} 1 H ${width - half} A ${radius} ${radius} 0 0 1 ${width - 1} ${half} A ${radius} ${radius} 0 0 1 ${width - half} ${height - 1} H ${half} A ${radius} ${radius} 0 0 1 1 ${half} A ${radius} ${radius} 0 0 1 ${half} 1 Z`;
      ring.setAttribute("viewBox", `0 0 ${width} ${height}`);
      ring.querySelectorAll("path").forEach(node => node.setAttribute("d", path));
    }
    return;
  }
  const size = node => `${Math.round(node.getBoundingClientRect().width)}×${Math.round(node.getBoundingClientRect().height)}`;
  for (const slot of content.querySelectorAll(".player-slot")) slot.dataset.measure = `${slot.dataset.gridLabel} · ${size(slot)} px`;
  const styles = getComputedStyle(content);
  content.querySelector(".player-design-caption").textContent = `PLAYER · ${Math.round(content.getBoundingClientRect().width)}×${Math.round(bar.getBoundingClientRect().height)} px · 12×${Math.round(parseFloat(styles.gridTemplateColumns))} px · gap ${parseFloat(styles.columnGap)} px · pad Y ${parseFloat(getComputedStyle(bar).paddingTop)} px`;
}
const active = new Set(["queued", "running"]);
const state = {catalog: null, selected: new Set(), filter: "", kind: "", result: "", log: null, playerJob: null, expandedShelves: new Set(), shelfPositions: new Map()};
const nav = [["overview", "Home"], ["playlists", "Albums"],
  ["library", "Case library"], ["runs", "Runs"]];
let toastTimer;
let generation = 0;
let refreshing = false;
const startingAlbums = new Set();
const artifactCache = new Map();
let designOn = false;
let railCollapsed = false;
let playerExpanded = true;
let playerIdleTimer;
let playerHoverTimer;
let playerExitTimer;
let playerExitJobId = null;
try { designOn = localStorage.getItem("forge-playlists-design-grid") === "on";
  railCollapsed = localStorage.getItem("forge-playlists-rail-collapsed") === "yes"; } catch { /* In-memory toggle is sufficient. */ }

function applyDesignMode() {
  document.body.classList.toggle("design-mode", designOn);
  $("design-toggle").setAttribute("aria-checked", String(designOn));
  if ($("design-grid")) $("design-grid").hidden = !designOn;
}
$("design-toggle").onclick = () => {
  designOn = !designOn;
  applyDesignMode();
  try { localStorage.setItem("forge-playlists-design-grid", designOn ? "on" : "off"); } catch { /* Keep session state. */ }
};
applyDesignMode();
document.body.classList.toggle("rail-collapsed", railCollapsed);

const railTooltip = el("div", {id: "rail-tooltip", class: "rail-tooltip", "aria-hidden": "true", hidden: true});
document.body.append(railTooltip);
function hideRailTooltip() { railTooltip.hidden = true; }
function showRailTooltip(event) {
  const target = event.target.closest(".nav-link, .brand, .rail-shortcut, .rail-create, .rail-toggle");
  if (!railCollapsed || !target || dialog.open) return hideRailTooltip();
  const box = target.getBoundingClientRect();
  railTooltip.textContent = target.getAttribute("aria-label") || target.title;
  railTooltip.style.left = `${rail.getBoundingClientRect().right + 12}px`;
  railTooltip.style.top = `${box.top + box.height / 2}px`;
  railTooltip.hidden = false;
}
rail.addEventListener("pointerover", showRailTooltip);
rail.addEventListener("focusin", showRailTooltip);
rail.addEventListener("pointerout", event => {
  if (!event.relatedTarget || !event.target.closest("a, button")?.contains(event.relatedTarget)) hideRailTooltip();
});
rail.addEventListener("focusout", hideRailTooltip);
document.querySelector(".rail-body").addEventListener("scroll", hideRailTooltip);
function toggleSidebar() {
  railCollapsed = !railCollapsed;
  document.body.classList.toggle("rail-collapsed", railCollapsed);
  hideRailTooltip();
  try { localStorage.setItem("forge-playlists-rail-collapsed", railCollapsed ? "yes" : "no"); } catch { /* Session fallback. */ }
  render();
  $("rail-toggle").focus({preventScroll: true});
}

const playerBar = $("run-bar");
function setPlayerExpanded(expanded, focus = false) {
  playerExpanded = expanded;
  clearTimeout(playerIdleTimer);
  clearTimeout(playerHoverTimer);
  playerBar.classList.toggle("is-compact", !expanded);
  const focused = playerBar.contains(document.activeElement);
  if (focus || focused) playerBar.querySelector(expanded ? ".player-minimize" : ".player-capsule")?.focus({preventScroll: true});
  measureRunBar();
  if (expanded) schedulePlayerCompact();
}
function schedulePlayerCompact() {
  clearTimeout(playerIdleTimer);
  if (!playerExpanded || playerBar.hidden) return;
  playerIdleTimer = setTimeout(() => {
    if (playerBar.matches(":hover") || playerBar.contains(document.activeElement) || dialog.open) return schedulePlayerCompact();
    setPlayerExpanded(false);
  }, 500);
}

function clearPlayerExit() {
  clearTimeout(playerExitTimer);
  playerExitTimer = null;
  playerExitJobId = null;
}
function schedulePlayerExit(job) {
  if (active.has(job.status) || upcomingJobs(state.catalog).length) { clearPlayerExit(); return; }
  if (playerExitJobId === job.id) return;
  clearPlayerExit();
  playerExitJobId = job.id;
  playerExitTimer = setTimeout(() => {
    if (state.playerJob !== job.id || currentJob(state.catalog)) return;
    const focused = playerBar.contains(document.activeElement);
    clearTimeout(playerIdleTimer);
    clearTimeout(playerHoverTimer);
    clearPlayerExit();
    state.playerJob = null;
    playerBar.hidden = true;
    playerBar.replaceChildren();
    if (focused) workspace.focus({preventScroll: true});
    updateQueueDialog(state.catalog);
  }, 1500);
}
playerBar.addEventListener("pointerenter", () => {
  clearTimeout(playerIdleTimer);
  if (!playerExpanded) playerHoverTimer = setTimeout(() => setPlayerExpanded(true), 250);
});
playerBar.addEventListener("pointerleave", () => { clearTimeout(playerHoverTimer); schedulePlayerCompact(); });
playerBar.addEventListener("focusin", () => clearTimeout(playerIdleTimer));
playerBar.addEventListener("focusout", schedulePlayerCompact);
dialog.addEventListener("close", () => {
  if (!dialog.open) {
    const wasQueue = dialog.dataset.view === "queue";
    dialog.classList.remove("album-detail-dialog", "queue-dialog");
    delete dialog.dataset.view;
    dialog.removeAttribute("aria-describedby");
    if (wasQueue && state.catalog) render();
  }
  schedulePlayerCompact();
});

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
  const response = await apiFetch(path, options);
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

async function refreshPlayer() {
  if (refreshing) return;
  refreshing = true;
  try {
    const [view, id] = route();
    const previous = state.catalog.jobs.find(row => row.id === id);
    state.catalog = await api("/api/catalog");
    updatePlayer(state.catalog);
    if (view === "job" && !dialog.open) {
      const job = state.catalog.jobs.find(row => row.id === id);
      if (JSON.stringify(previous) !== JSON.stringify(job)) {
        const log = $("job-log"), scroll = log?.scrollTop, focused = document.activeElement === log;
        render();
        if ($("job-log")) {
          $("job-log").scrollTop = scroll || 0;
          if (focused) $("job-log").focus({preventScroll: true});
        }
      } else if (state.log?.job === id) updateLog(id, state.log.case, generation);
    }
  } catch { /* Retry on the next poll without interrupting the open view. */ }
  finally { refreshing = false; }
}

function upcomingJobs(data) {
  return data.jobs.filter(job => job.status === "queued").sort((a, b) =>
    (a.queueOrder ?? Date.parse(a.submitted_at)) - (b.queueOrder ?? Date.parse(b.submitted_at)));
}
function currentJob(data) {
  return data.jobs.find(job => job.status === "running") || upcomingJobs(data)[0];
}

function shell(data, view, id) {
  const navState = {project: "library", shelf: "overview", collection: "playlists", playlist: "playlists", job: "runs"};
  const running = data.jobs.filter(row => active.has(row.status)).length;
  const counts = {playlists: data.playlists.filter(row => row.stored).length, library: data.cases.length,
    runs: running || data.jobs.length};
  $("navigation").replaceChildren(el("div", {class: "rail-heading"}, el("span", {}, "Library"), sidebarToggle(railCollapsed, toggleSidebar)),
    ...navigation(nav.map(([value, label]) => ({id: value, label, href: to(value), count: counts[value],
      context: view !== value, live: value === "runs" && running > 0})), navState[view] || view));
  const album = view === "playlist" && data.playlists.find(row => row.id === id);
  const projectId = view === "collection" || view === "project" ? id : album ? albumProject(data, album) || "shared" : null;
  const create = button("+", () => editDialog(projectId && projectId !== "shared" ?
    {project: projectId, shelf: data.projects.find(row => row.id === projectId)?.shelf} : {}), {variant: "rail-create"});
  create.setAttribute("aria-label", "New album");
  create.setAttribute("title", "New album");
  const projects = [...data.projects];
  if (albumsForProject(data, "shared").length) projects.push(collectionProject(data, "shared"));
  $("collections").replaceChildren(el("div", {class: "rail-heading"}, el("span", {}, "Collections"), create),
    ...navigation(projects.map(project => ({id: project.id, label: project.title, href: to("collection", project.id),
      emoji: project.icon || "📦", theme: project.theme, count: albumsForProject(data, project.id).length,
      context: view !== "collection"})), projectId));
  $("shelves").replaceChildren(...(data.shelves.length ? [el("div", {class: "rail-heading"}, el("span", {}, "Shelves")),
    ...navigation(data.shelves.map(shelf => ({id: shelf.id, label: shelf.title,
      href: to("shelf", shelf.id), glyph: "shelf"})), view === "shelf" ? id : null)] : []));
  $("shelves").hidden = !data.shelves.length;
  $("host").replaceChildren(navigationShortcut("Quick jump", openQuickJump),
    el("div", {class: "rail-demo"}, el("span", {class: "demo-tag", title: `Design Studio V${APP_VERSION}`}, `V${APP_VERSION}`), link(demoMode ? "Demo · Switch to local" : "Local · Open Demo", demoMode ? "/" : "/?mode=demo")));
  $("breadcrumbs").replaceChildren(breadcrumbs(routeBreadcrumbs(data, view, id)));
  $("quick-jump").disabled = false;
  measureShellChrome();
  updatePlayer(data);
}

function updatePlayer(data) {
  const job = currentJob(data) || data.jobs.find(row => row.id === state.playerJob);
  const waiting = upcomingJobs(data);
  if (job) state.playerJob = job.id;
  const bar = $("run-bar");
  const appeared = bar.hidden && !!job;
  bar.hidden = !job;
  if (job) {
    const focusLabel = bar.contains(document.activeElement) ? document.activeElement.getAttribute("aria-label") || document.activeElement.textContent.trim() : null;
    const item = data.playlists.find(row => row.id === job.playlist);
    const minimize = button("⌄", () => setPlayerExpanded(false, true), {variant: "player-minimize"});
    minimize.setAttribute("aria-label", "Minimize player");
    minimize.setAttribute("title", "Minimize player");
    bar.classList.toggle("is-compact", !playerExpanded);
    bar.replaceChildren(runBar(job, to("job", job.id), item && !active.has(job.status) ? () => startAlbum(item) : null, waiting, openQueueDialog),
      playerCapsule(job, () => setPlayerExpanded(true, true), item && collectionProject(data, albumProject(data, item) || "shared"), waiting.length), minimize, playerNotice(job));
    if (focusLabel) {
      const control = [...bar.querySelectorAll("button, a")].find(node => (node.getAttribute("aria-label") || node.textContent.trim()) === focusLabel);
      (control || bar.querySelector(playerExpanded ? ".player-minimize" : ".player-capsule"))?.focus({preventScroll: true});
    }
    measureRunBar();
    if (appeared) schedulePlayerCompact();
    schedulePlayerExit(job);
  } else {
    clearPlayerExit();
    clearTimeout(playerIdleTimer);
    clearTimeout(playerHoverTimer);
    state.playerJob = null;
    bar.replaceChildren();
  }
  updateQueueDialog(data);
}

function openQueueDialog() {
  if (dialog.open) return;
  hideRailTooltip();
  dialog.classList.remove("album-detail-dialog");
  dialog.classList.add("queue-dialog");
  dialog.dataset.view = "queue";
  dialog.removeAttribute("aria-describedby");
  const close = button("×", () => dialog.close(), {variant: "subtle"});
  close.classList.add("dialog-close");
  close.setAttribute("aria-label", "Close play queue");
  dialog.replaceChildren(el("div", {class: "dialog-header"}, el("h2", {id: "dialog-title"}, "Play queue"), close),
    el("div", {class: "queue-body", id: "queue-body"}),
    el("div", {class: "queue-footer subtext"}, "Albums run one at a time, in the order you add them."));
  dialog.setAttribute("aria-labelledby", "dialog-title");
  dialog.showModal();
  updateQueueDialog(state.catalog);
  close.focus();
}
function updateQueueDialog(data) {
  if (!dialog.open || dialog.dataset.view !== "queue") return;
  const body = $("queue-body");
  const scroll = body.scrollTop;
  const focusId = document.activeElement?.dataset?.queueRemove;
  const playing = data.jobs.find(job => job.status === "running") || data.jobs.find(job => job.id === state.playerJob);
  const waiting = upcomingJobs(data);
  const row = (job, index = null) => {
    const item = data.playlists.find(album => album.id === job.playlist);
    const project = item && collectionProject(data, albumProject(data, item) || "shared");
    const remove = index != null && button("×", () => removeFromQueue(job), {variant: "queue-remove"});
    if (remove) {
      remove.dataset.queueRemove = job.id;
      remove.setAttribute("aria-label", `Remove ${job.label} from queue`);
      remove.setAttribute("title", "Remove from queue");
    }
    return el("div", {class: `queue-row theme-${project?.theme || "mint"}`, "data-queued-album": job.playlist || ""},
      projectMark(project || {title: "Selected cases", icon: "▦"}),
      el("div", {class: "queue-row-copy"}, el("strong", {}, job.label),
        el("span", {class: "subtext"}, `${index != null ? `${index === 0 ? "Next" : index + 1} · ` : ""}${project?.title || "Acceptance run"} · ${job.cases.length} cases`)),
      remove || status(job.status));
  };
  body.replaceChildren(el("section", {}, el("h3", {}, playing && active.has(playing.status) ? "Now running" : "Last run"),
      playing ? row(playing) : empty("Nothing playing", "Play an album to begin.")),
    el("section", {}, el("h3", {}, `Up next · ${waiting.length}`), waiting.length ?
      el("div", {class: "queue-list"}, waiting.map((job, index) => row(job, index))) :
      empty("Queue is clear", "Play another album to add it here.")));
  body.scrollTop = scroll;
  if (focusId) ([...body.querySelectorAll("[data-queue-remove]")].find(node => node.dataset.queueRemove === focusId) ||
    dialog.querySelector(".dialog-close"))?.focus({preventScroll: true});
}
async function removeFromQueue(job) {
  try { await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, "POST", {only_if_queued: true}); }
  catch (error) { toast(error.message); }
  await refresh();
}

function routeBreadcrumbs(data, view, id) {
  const home = {label: "Home", href: to("overview")};
  const pages = {playlists: "Albums", library: "Case library", runs: "Runs"};
  if (view === "overview") return [{label: "Home"}];
  if (pages[view]) return [home, {label: pages[view]}];
  if (view === "shelf") return [home, {label: data.shelves.find(row => row.id === id)?.title || "Unavailable shelf"}];
  if (view === "project") return [home, {label: "Case library", href: to("library")},
    {label: data.projects.find(row => row.id === id)?.title || "Unavailable project"}];
  if (view === "collection") return [home, {label: "Albums", href: to("playlists")},
    {label: collectionProject(data, id)?.title || "Unavailable collection"}];
  if (view === "playlist") {
    const item = data.playlists.find(row => row.id === id);
    const project = item && collectionProject(data, albumProject(data, item) || "shared");
    return [home, {label: "Albums", href: to("playlists")},
      ...(project ? [{label: project.title, href: to("collection", project.id)}] : []),
      {label: item?.title || "Unavailable album"}];
  }
  if (view === "job") return [home, {label: "Runs", href: to("runs")},
    {label: data.jobs.find(row => row.id === id)?.label || "Unavailable run"}];
  return [home, {label: "Page unavailable"}];
}

function openQuickJump() {
  if (!state.catalog || dialog.open) return;
  const data = state.catalog;
  const destinations = [
    ...nav.map(([view, label]) => ({label, group: "Pages", detail: "Workspace page", href: to(view)})),
    ...data.projects.map(row => ({label: row.title, group: "Collections", detail: "Album collection", href: to("collection", row.id)})),
    ...(albumsForProject(data, "shared").length ? [{label: "Shared albums", group: "Collections", detail: "Album collection", href: to("collection", "shared")}] : []),
    ...data.shelves.map(row => ({label: row.title, group: "Shelves", detail: "Project shelf", href: to("shelf", row.id)})),
    ...data.playlists.map(row => ({label: row.title, group: "Albums",
      detail: data.projects.find(project => project.id === albumProject(data, row))?.title || "Shared album", href: to("playlist", row.id)})),
    ...data.jobs.slice(0, 5).map(row => ({label: row.label, group: "Recent runs", detail: timestamp(row.submitted_at), href: to("job", row.id)}))
  ];
  const input = el("input", {class: "quick-filter", type: "search", placeholder: "Pages, projects, albums…",
    "aria-label": "Filter destinations", "aria-controls": "quick-results", autofocus: true});
  const results = el("div", {id: "quick-results", class: "quick-results"});
  const update = () => {
    const query = input.value.trim().toLowerCase();
    const matches = destinations.filter(item => `${item.label} ${item.group} ${item.detail}`.toLowerCase().includes(query));
    results.replaceChildren(...["Pages", "Collections", "Shelves", "Albums", "Recent runs"].flatMap(group => {
      const items = matches.filter(item => item.group === group);
      return items.length ? [el("section", {class: "quick-group"}, el("h3", {}, group), items.map(item =>
        el("a", {class: "quick-destination", href: item.href, onclick: event => {
          $("search").value = "";
          dialog.close();
          if (item.group === "Albums") {
            event.preventDefault();
            const album = data.playlists.find(row => to("playlist", row.id) === item.href);
            if (album) openAlbumDialog(album);
            return;
          }
          if (location.hash === item.href) render();
          window.scrollTo({top: 0, behavior: "instant"});
        }}, el("span", {}, item.label), el("small", {}, item.detail))))] : [];
    }));
    if (!matches.length) results.append(el("p", {class: "quick-empty", role: "status"}, "No destinations found. Try a page, project, or album name."));
  };
  input.addEventListener("input", update);
  input.addEventListener("keydown", event => {
    if (["ArrowDown", "Enter"].includes(event.key)) {
      event.preventDefault();
      const first = results.querySelector("a");
      if (event.key === "Enter") first?.click();
      else first?.focus();
    }
  });
  results.addEventListener("keydown", event => {
    if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
    event.preventDefault();
    const links = [...results.querySelectorAll("a")];
    const index = links.indexOf(document.activeElement);
    const next = index + (event.key === "ArrowDown" ? 1 : -1);
    if (next < 0) input.focus();
    else links[Math.min(next, links.length - 1)]?.focus();
  });
  const close = button("×", () => dialog.close(), {variant: "subtle"});
  close.classList.add("dialog-close");
  close.setAttribute("aria-label", "Close quick jump");
  update();
  dialog.replaceChildren(el("div", {class: "dialog-header"}, el("h2", {id: "dialog-title"}, "Jump to"), close),
    el("div", {class: "quick-jump"}, input, results),
    el("p", {class: "quick-help"}, "Type to filter · Enter to open · Esc to close"));
  dialog.setAttribute("aria-labelledby", "dialog-title");
  dialog.showModal();
  input.focus();
}
$("quick-jump").onclick = openQuickJump;

function drawAlbum(data, item, project = null) {
  project ||= data.projects.find(row => row.id === albumProject(data, item)) || {title: "Shared album", theme: "mint"};
  const latest = latestAlbumJob(data, item);
  const running = data.jobs.find(job => job.playlist === item.id && job.status === "running");
  const waiting = upcomingJobs(data);
  const queued = waiting.find(job => job.playlist === item.id);
  const summary = item.description || item.goal || item.members.map(id => data.cases.find(row => row.id === id)?.title).filter(Boolean).join(" · ");
  return albumCard({item, project, latest, running, queued, queuePosition: waiting.indexOf(queued) + 1, summary, pending: startingAlbums.has(item.id),
    onOpen: () => openAlbumDialog(item), lastRunHref: latest && to("job", running?.id || queued?.id || latest.id), onRun: () => startAlbum(item)});
}

function openAlbumDialog(item) {
  if (dialog.open) return;
  const data = state.catalog;
  const project = collectionProject(data, albumProject(data, item) || "shared") || {title: "Shared album", theme: "mint"};
  const latest = latestAlbumJob(data, item);
  const running = data.jobs.find(job => job.playlist === item.id && job.status === "running");
  const waiting = upcomingJobs(data);
  const queued = waiting.find(job => job.playlist === item.id);
  const members = item.members.map(id => data.cases.find(row => row.id === id)).filter(Boolean);
  const close = button("×", () => dialog.close(), {variant: "subtle"});
  close.classList.add("dialog-close");
  close.setAttribute("aria-label", "Close album details");
  const play = button(running ? "Running" : queued ? `Queued · ${waiting.indexOf(queued) + 1}` : currentJob(data) ? "Add to queue" : "Play album", () => { dialog.close(); startAlbum(item); },
    {variant: "primary", glyph: "run", disabled: !!running || !!queued || !members.length});
  const edit = item.stored && button("Add cases", () => { dialog.close(); editDialog({item}); });
  const manage = link("Manage entries", to("playlist", item.id), {variant: "text-link"});
  manage.addEventListener("click", () => dialog.close());
  const result = latest && link("View latest run", to("job", running?.id || latest.id), {variant: "text-link"});
  result?.addEventListener("click", () => dialog.close());
  dialog.classList.remove("queue-dialog");
  delete dialog.dataset.view;
  dialog.classList.add("album-detail-dialog");
  dialog.replaceChildren(el("div", {class: "dialog-header"}, el("h2", {id: "dialog-title"}, item.title), close),
    el("div", {class: "album-dialog-body"},
      el("div", {class: "album-dialog-hero"}, albumPreview(item, project),
        el("div", {class: "album-dialog-summary"}, el("span", {class: "album-dialog-project"}, project.icon || "📦", " ", project.title),
          el("p", {id: "album-dialog-description"}, item.description || item.goal || "A saved selection of acceptance cases."),
          el("div", {class: "album-dialog-facts"}, el("span", {}, `${members.length} cases`),
            el("span", {}, item.engine || "Mixed checks"), status(queued ? "queued" : running ? "running" : latest?.status || "not_run")),
          el("p", {class: "subtext"}, `Environment · ${item.environment || "Project default"}`),
          result)),
      el("section", {class: "album-dialog-cases"}, el("h3", {}, "Acceptance cases"),
        members.length ? table(["Case", "Kind", "Latest status"], members.map(row => [el("span", {}, nameOf(row)), row.kind,
          status(latest?.cases.find(entry => entry.id === row.id)?.status || row.last?.status)]), "Album acceptance cases") :
          empty("No cases yet", "Add cases to this album to run acceptance."))),
    el("div", {class: "album-dialog-footer"}, manage, el("div", {}, edit, play)));
  dialog.setAttribute("aria-labelledby", "dialog-title");
  dialog.setAttribute("aria-describedby", "album-dialog-description");
  hideRailTooltip();
  dialog.showModal();
  close.focus();
}

function albumProject(data, item) {
  const projects = new Set(item.members.map(id => data.cases.find(row => row.id === id)?.project).filter(Boolean));
  return projects.size === 1 ? [...projects][0] : projects.size === 0 ? item.project : null;
}

function latestAlbumJob(data, item) {
  return data.jobs.find(job => job.playlist === item.id || (!job.playlist && item.members.length > 0 &&
    job.cases.length === item.members.length && job.cases.every(row => item.members.includes(row.id))));
}

async function startAlbum(item) {
  if (startingAlbums.has(item.id)) return;
  startingAlbums.add(item.id);
  render();
  try {
    const {job} = await api("/api/jobs", "POST", {playlist: item.id});
    if (job.status === "running" || !currentJob(state.catalog)) state.playerJob = job.id;
    await refresh();
  } catch (error) {
    toast(error.message);
  } finally {
    startingAlbums.delete(item.id);
    render();
  }
}

function collectionProject(data, id) {
  return data.projects.find(row => row.id === id) || (id === "shared" ?
    {id: "shared", title: "Shared albums", icon: "📚", theme: "mint"} : null);
}

function albumsForProject(data, projectId) {
  return data.playlists.filter(item => item.stored && (projectId === "shared" ?
    !data.projects.some(project => project.id === albumProject(data, item)) : albumProject(data, item) === projectId));
}

function toggleShelf(projectId) {
  const collapsing = state.expandedShelves.has(projectId);
  if (collapsing) state.expandedShelves.delete(projectId);
  else state.expandedShelves.add(projectId);
  render();
  const section = document.getElementById(`project-shelf-${projectId}`);
  if (collapsing) {
    section?.scrollIntoView({block: "start", behavior: "instant"});
    section?.querySelector(".shelf-toggle")?.focus({preventScroll: true});
  } else section?.querySelector(".album-cover")?.focus({preventScroll: true});
}

function renderShelf(data, project, items) {
  return projectShelf(project, items.map(item => drawAlbum(data, item, project)), {
    collectionHref: to("collection", project.id),
    expanded: state.expandedShelves.has(project.id), startIndex: state.shelfPositions.get(project.id) || 0,
    onToggle: () => toggleShelf(project.id), onPosition: index => state.shelfPositions.set(project.id, index)
  });
}

function collection(data, id) {
  const project = collectionProject(data, id);
  if (!project) return missing("Collection");
  const search = $("search").value.trim().toLowerCase();
  const all = albumsForProject(data, id);
  const items = all.filter(item => !search || [item.title, item.description, item.engine,
    ...item.members.map(key => data.cases.find(row => row.id === key)?.title)].filter(Boolean).join(" ").toLowerCase().includes(search));
  const caseHref = id === "shared" ? to("library") : to("project", id);
  return [pageHeader(project.title, search ? `${items.length} / ${count(all.length, "album")}` : count(all.length, "album"),
    [link("Cases", caseHref, {variant: "button secondary"}),
      button("New album", () => editDialog({shelf: project.shelf, project: id === "shared" ? null : id}), {variant: "primary", glyph: "plus"})],
    {label: "Home", href: to("overview")}),
    items.length ? el("div", {class: "album-grid collection-grid", "aria-label": `${project.title} complete collection`},
      items.map(item => drawAlbum(data, item, project))) : empty("No matching albums", "Try another album or case name.")];
}

function overview(data, shelfId) {
  const shelf = shelfId && data.shelves.find(item => item.id === shelfId);
  if (shelfId && !shelf) return missing("Shelf");
  const search = $("search").value.trim().toLowerCase();
  const projects = data.projects.filter(row => !shelf || row.shelf === shelf.id);
  const saved = data.playlists.filter(row => row.stored && (!shelf || row.shelf === shelf.id));
  const matches = (item, project) => !search || [item.title, item.description, item.engine, project.title,
    ...item.members.map(id => data.cases.find(row => row.id === id)?.title)].filter(Boolean).join(" ").toLowerCase().includes(search);
  const shelves = projects.map(project => {
    const own = saved.filter(item => albumProject(data, item) === project.id);
    const filtered = own.filter(item => matches(item, project));
    return search && !filtered.length ? null : renderShelf(data, project, filtered);
  }).filter(Boolean);
  const sharedProject = collectionProject(data, "shared");
  const shared = saved.filter(item => !data.projects.some(project => project.id === albumProject(data, item)) && matches(item, sharedProject));
  if (shared.length) shelves.push(renderShelf(data, sharedProject, shared));
  return [el("div", {class: "library-intro"},
    pageHeader(shelf ? shelf.title : "Albums", null,
      [button("New album", () => editDialog({shelf: shelf?.id}), {variant: "secondary", glyph: "plus"})]),
    el("p", {class: "shelf-summary"}, `${count(projects.length, "project")} · ${count(saved.length, "album")}`)),
    data.messages.length ? feedback(data.messages) : null,
    shelves.length ? el("div", {class: "project-shelves"}, shelves) :
      empty("No matching albums", "Try a project, module, or test name.")];
}


function playlistCard(row) {
  const card = recordCard({title: row.title, subtitle: row.id, name: "playlists", href: to("playlist", row.id),
    state: row.problem ? "blocked" : null, detail: count(row.members.length, "case")});
  card.querySelectorAll("a").forEach(anchor => anchor.addEventListener("click", event => { event.preventDefault(); openAlbumDialog(row); }));
  return card;
}

function runRow(job) {
  return el("a", {class: "run-grid-row", href: to("job", job.id)},
    el("div", {class: "run-row-copy run-grid-slot", "data-grid-span": 8}, el("strong", {}, job.label),
      el("small", {}, timestamp(job.started_at || job.submitted_at))),
    el("div", {class: "run-grid-slot", "data-grid-span": 2}, el("div", {class: "grid-value number-slot", "data-grid-content": ""}, count(job.cases.length, "case"))),
    el("div", {class: "run-grid-slot", "data-grid-span": 2}, el("div", {class: "grid-value status-slot", "data-grid-content": ""}, status(job.status))));
}

function measureWorkspaceGrid() {
  // Give every cell in a row the same 16px-rounded frame so centered 32px values stay on the 8px baseline, including wrapped case names.
  const rows = [...workspace.querySelectorAll(".grid-table tbody tr")];
  rows.forEach(row => row.style.removeProperty("--run-row-height"));
  const heights = rows.map(row => Math.ceil(Math.max(...[...row.querySelectorAll(".grid-table-cell")]
    .map(cell => cell.getBoundingClientRect().height)) / 16) * 16);
  rows.forEach((row, index) => row.style.setProperty("--run-row-height", `${heights[index]}px`));
  for (const node of workspace.querySelectorAll("[data-grid-span]")) {
    const {width, height} = node.getBoundingClientRect();
    node.dataset.measure = node.dataset.gridSpan === "1" ? `1c ${Math.round(width)}×${Math.round(height)}` :
      `${node.dataset.gridSpan}c · ${Math.round(width)}×${Math.round(height)} px`;
  }
  for (const node of workspace.querySelectorAll("[data-grid-content]")) {
    const {width, height} = node.getBoundingClientRect();
    node.dataset.measure = node.dataset.contentSpan === "1" ? `1c ${Math.round(width)}×${Math.round(height)}` :
      `${Math.round(width)}×${Math.round(height)} px`;
  }
}

function select(label, values, value, onchange) {
  return el("select", {"aria-label": label, onchange: event => onchange(event.currentTarget.value)},
    values.map(([key, caption]) => el("option", {value: key, selected: key === value}, caption)));
}

function selections() {
  const amount = state.selected.size;
  const action = (label, glyph, onclick) => {
    const node = button(label, onclick, {glyph, disabled: !amount});
    node.classList.add("case-action-button");
    node.setAttribute("aria-label", label);
    node.title = label;
    node.dataset.gridSpan = 1;
    return node;
  };
  return el("div", {class: "case-action-grid"},
    el("span", {class: "subtext", "data-grid-span": 10}, amount ? `${amount} selected` : "Select cases to run or save"),
    action("Run selected", "run", () => confirmRun({cases: [...state.selected]}, amount)),
    action("Save selection", "plus", () => editDialog({entries: [...state.selected].map(caseId => ({case: caseId}))})));
}

function caseTable(rows, label = "Cases") {
  if (!rows.length) return empty("No matching cases", "Clear the filters or check the project catalog.");
  return gridTable([{label: "Select", span: 1, type: "select"}, {label: "Case", span: 6}, {label: "Kind", span: 2, type: "kind"},
    {label: "Latest status", span: 2, type: "status"}, {label: "Time", span: 1, type: "number"}], rows.map(row => {
    const check = checkbox(`Select ${nameOf(row)}`, state.selected.has(row.id), checked => {
      if (checked) state.selected.add(row.id);
      else state.selected.delete(row.id);
      const [view] = route();
      const slot = $("case-actions");
      if (slot && ["library", "project"].includes(view)) { slot.replaceChildren(selections()); measureWorkspaceGrid(); }
    });
    return [check, el("div", {class: "case-cell"}, el("span", {class: "case-name"}, nameOf(row)),
      el("code", {class: "case-id"}, row.id)), row.kind, row.last?.job ?
      el("a", {href: to("job", row.last.job), title: "View latest run"}, status(row.last.status)) :
      status("not_run"), duration(row.last?.seconds)];
  }), label);
}

function caseFilter(label, control) {
  const slot = el("div", {class: "case-filter-control", "data-grid-span": 4}, control);
  // A wrapper carries the measurement because native select elements do not render pseudo-elements.
  return el("div", {class: "case-filter-field"}, el("span", {class: "filter-caption"}, label), slot);
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
    "Individual acceptance checks across projects. Select cases to build an album and run them together.", [], project ? {label: "Home", href: to("overview")} : null),
  project && !project.available ? feedback(["Register this project’s checkout to browse and run its cases."]) : null,
  profiles.length ? section("Profiles", grid(profiles.map(row => cell(4, playlistCard(row))))) : null,
  section(count(rows.length, "case"), [
    el("div", {class: "case-filter-grid"},
      caseFilter("Project", project ? el("div", {class: "filter-project"}, project.title) :
        select("Project filter", [["", "All projects"], ...data.projects.map(row => [row.id, row.title])],
          state.filter, value => { state.filter = value; render(); })),
      caseFilter("Kind", select("Kind filter", [["", "All kinds"], ["pytest", "Pytest"], ["check", "Check"], ["command", "Command"]],
        state.kind, value => { state.kind = value; render(); })),
      caseFilter("Status", select("Status filter", [["", "All statuses"], ["not_run", "Not run"], ["passed", "Passed"],
        ["reused", "Reused"], ["running", "Running"], ["queued", "Queued"], ["failed", "Failed"],
        ["skipped", "Skipped"], ["interrupted", "Interrupted"]],
        state.result, value => { state.result = value; render(); }))),
    el("div", {id: "case-actions"}, selections()), caseTable(rows)])];
}

function playlists(data) {
  const stored = data.playlists.filter(row => row.stored);
  const derived = data.playlists.filter(row => !row.stored);
  return [pageHeader("Albums", "Acceptance suites and saved groups of cases, ready to run.",
    [button("New album", () => editDialog({}), {variant: "primary", glyph: "plus"})]),
  section("Saved albums", stored.length ? el("div", {class: "album-grid"}, stored.map(row => drawAlbum(data, row))) :
    empty("No saved playlists", "Create a playlist or save a selection from the case library.",
      link("Open case library", to("library")))),
  section("Derived profiles", derived.length ? grid(derived.map(row => cell(4, playlistCard(row)))) :
    empty("No derived profiles", "Test cards with named profiles appear here when their checkouts are available."))];
}

function playlist(data, id) {
  const item = data.playlists.find(row => row.id === id);
  if (!item) return missing("Playlist");
  const members = item.members.map(member => data.cases.find(row => row.id === member)).filter(Boolean);
  const actions = [button(currentJob(data) ? "Add to queue" : "Run album", () => confirmRun({playlist: item.id}, members.length),
    {variant: "primary", glyph: "run", disabled: !members.length || data.jobs.some(job => job.playlist === item.id && active.has(job.status))})];
  if (item.stored) actions.unshift(button("Add entries", () => editDialog({item}), {glyph: "plus"}));
  const summary = item.shelf ? data.shelves.find(shelf => shelf.id === item.shelf)?.title : null;
  const entries = item.stored ? section("Saved entries", item.entries.length ? table(["Entry", "Type", "Actions"],
    item.entries.map(entry => [el("code", {class: "breakable"}, entry.case || entry.profile),
      entry.case ? "Case" : "Profile", button("Remove", () => removeDialog(item, entry), {variant: "subtle"})]), "Playlist entries") :
    empty("No entries", "Add cases or live profiles to this playlist.")) : null;
  const metadata = [
    ["Execution", demoMode ? "Simulated demo" : "Local tests"], ["Runner", item.engine || "Mixed checks"],
    ["Project", data.projects.find(row => row.id === albumProject(data, item))?.title || "Multiple projects"],
    ["Storage", item.stored ? "Saved album" : "Derived profile"], ["Shelf", summary || "Library"],
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
  const parent = collectionProject(data, albumProject(data, item) || "shared");
  return [pageHeader(item.title, item.goal || item.id, actions,
    parent ? {label: `${parent.title} albums`, href: to("collection", parent.id)} : {label: "All albums", href: to("playlists")}),
    item.problem ? feedback([item.problem]) : null,
    split([entries, section("Resolved cases", resolved)], section("Details", details))];
}

function runs(data) {
  const ongoing = data.jobs.filter(job => job.status === "running");
  const waiting = upcomingJobs(data);
  const finished = data.jobs.filter(job => !active.has(job.status));
  return [pageHeader("Runs", "Track queued work and open case results.",
    [link("Browse cases", to("library"), {variant: "button secondary"})]),
  section("Now running", ongoing.length ? el("div", {class: "run-list"}, ongoing.map(runRow)) :
    empty("Queue is empty", "New runs appear here until they finish.")),
  waiting.length ? section(`Up next · ${waiting.length}`, el("div", {class: "run-list"}, waiting.map(runRow))) : null,
  section("Run history", finished.length ? gridTable([{label: "Run", span: 6}, {label: "Started", span: 3}, {label: "Cases", span: 1, type: "number"}, {label: "Status", span: 2, type: "status"}],
    finished.map(job => [link(job.label, to("job", job.id)), timestamp(job.started_at || job.submitted_at),
      job.cases.length, status(job.status)]), "Run history") :
    empty("No finished runs", "Finished runs and their logs appear here."))];
}

function jobPage(data, id) {
  const job = data.jobs.find(item => item.id === id);
  if (!job) return missing("Run");
  const complete = job.cases.filter(item => !active.has(item.status)).length;
  const logId = state.log?.job === id ? state.log.case : (job.cases.find(row => row.status === "running") || job.cases.find(row => !active.has(row.status)) || job.cases[0])?.id;
  const cancel = button("Cancel run", () => confirmDialog("Cancel run?", "This interrupts the active cases.",
    "Cancel run", async () => { await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, "POST"); await refresh(); }),
    {variant: "danger"});
  const logs = el("pre", {class: "log", id: "job-log", tabindex: "0"},
    state.log?.job === id && state.log.case === logId ? state.log.text || "Waiting for output…" : "Select a case to view output.");
  if (logId) updateLog(id, logId, generation);
  if (!demoMode) updateArtifacts(id, generation);
  return [pageHeader(job.label, job.id, active.has(job.status) ? [cancel] : [],
    {label: "All runs", href: to("runs")}),
  job.reason ? feedback([job.reason]) : null,
  job.warnings?.length ? feedback(job.warnings) : null,
  job.acceptance ? feedback([`Acceptance evidence: ${job.acceptance.status}. ${(job.acceptance.reasons || []).join(" ")}`]) : null,
  el("div", {class: "run-progress-grid"},
    el("div", {class: "run-grid-slot", "data-grid-span": 2}, el("div", {class: "grid-value status-slot", "data-grid-content": ""}, status(job.status))),
    el("div", {class: "run-grid-slot", "data-grid-span": 8}, progress(complete, job.cases.length, "Case completion")),
    el("div", {class: "run-grid-slot", "data-grid-span": 2}, el("div", {class: "grid-value number-slot", "data-grid-content": ""}, `${complete} / ${job.cases.length} complete`))),
  split(section("Case results", gridTable([{label: "Case", span: 4}, {label: "Status", span: 2, type: "status"}, {label: "Time", span: 1, type: "number"}, {label: "Log", span: 1}], job.cases.map(row => [
    el("div", {class: "case-cell"}, el("span", {}, nameOf(row)), el("code", {class: "case-id"}, row.id),
      row.reason && el("p", {class: "subtext breakable"}, row.reason),
      row.counts && el("small", {class: "subtext"}, Object.entries(row.counts).filter(([, value]) => typeof value === "number").map(([key, value]) => `${value} ${key}`).join(" · "))), status(row.status),
    duration(row.seconds), button("Log", () => {
      state.log = {job: id, case: row.id, text: ""};
      render();
    }, {variant: "subtle"})]), "Case results")),
  section("Output", el("div", {class: "panel"}, logs))),
  !demoMode && section("Artifacts", el("div", {class: "panel", id: "job-artifacts"}, artifactCache.has(id) ? artifactGallery(artifactCache.get(id)) : "Loading artifacts…"))];
}

function artifactGallery(items) {
  return items.length ? el("div", {class: "artifact-grid"}, items.map(item => {
    const anchor = link(item.name, item.url, {variant: "artifact-link"});
    anchor.target = "_blank"; anchor.rel = "noopener";
    return el("article", {class: "artifact-card"},
      item.preview && el("a", {href: item.url, target: "_blank", rel: "noopener"},
        el("img", {src: item.url, alt: item.name, loading: "lazy"})), anchor,
      el("small", {class: "subtext breakable"}, `${item.case} · ${Math.round(item.bytes / 1024)} KB`));
  })) : empty("No artifacts yet", "Generated images, web pages and reports appear as the tests run.");
}

async function updateArtifacts(job, pass) {
  try {
    const data = await api(`/api/jobs/${encodeURIComponent(job)}/artifacts`);
    artifactCache.set(job, data.artifacts);
    if (pass !== generation || !$("job-artifacts")) return;
    $("job-artifacts").replaceChildren(artifactGallery(data.artifacts));
  } catch (error) {
    if (pass === generation && $("job-artifacts")) $("job-artifacts").textContent = error.message;
  }
}

async function updateLog(job, caseId, pass) {
  try {
    const data = await api(`/api/jobs/${encodeURIComponent(job)}/log?case=${encodeURIComponent(caseId)}`);
    if (pass !== generation) return;
    state.log = {job, case: caseId, text: data.text};
    if ($("job-log")) $("job-log").textContent = data.text || "Waiting for output…";
    measureWorkspaceGrid();
  } catch (error) {
    if (pass === generation && $("job-log")) $("job-log").textContent = error.message;
  }
}

function missing(label) {
  return empty(`${label} unavailable`, "It may have been removed from the library.",
    link("Return to overview", to("overview")));
}

function render() {
  if (!state.catalog) return;
  // In-place catalog/player updates must not let DOM replacement move the viewport.
  // Navigation and explicit shelf expansion apply their own scrolling after rendering.
  const scrollPosition = {left: window.scrollX, top: window.scrollY, behavior: "instant"};
  const data = state.catalog;
  const [view, id] = route();
  generation += 1;
  shell(data, view, id);
  const views = {overview: () => overview(data), shelf: () => overview(data, id),
    library: () => library(data), project: () => library(data, id), playlists: () => playlists(data),
    playlist: () => playlist(data, id), collection: () => collection(data, id), runs: () => runs(data), job: () => jobPage(data, id)};
  workspace.classList.toggle("case-library-view", ["library", "project"].includes(view));
  workspace.classList.toggle("grid-view", ["runs", "job", "library", "project"].includes(view));
  workspace.classList.toggle("runs-view", ["runs", "job"].includes(view));
  workspace.classList.toggle("shelf-view", ["overview", "shelf"].includes(view));
  workspace.replaceChildren(...[Object.hasOwn(views, view) ? views[view]() : missing("View")].flat(Infinity).filter(Boolean), designGrid(designOn));
  if (["runs", "job", "library", "project"].includes(view)) {
    for (const section of workspace.querySelectorAll(".section")) {
      const span = section.closest(".span-8") ? 8 : section.closest(".span-4") ? 4 : 12;
      section.querySelector(".section-head").dataset.gridSpan = span;
      const frame = section.querySelector(".empty, .panel");
      if (frame) frame.dataset.gridSpan = span;
    }
    measureWorkspaceGrid();
  }
  window.scrollTo(scrollPosition);
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
    state.playerJob = job.id;
    state.catalog.jobs.unshift(job);
    location.hash = to("job", job.id);
    await refresh();
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

function editDialog({item, shelf, entries = [], project = null}) {
  const data = state.catalog;
  const matches = data.cases.filter(row => (!project || row.project === project) && !item?.entries.some(entry => entry.case === row.id) &&
    !entries.some(entry => entry.case === row.id));
  const profiles = data.playlists.filter(row => (!project || row.project === project) && !row.stored && !item?.entries.some(entry => entry.profile === row.id));
  const choice = (name, caption, values, mode = false) => el("select", {name, multiple: mode, size: mode ? 4 : null},
    !mode && el("option", {value: ""}, caption), values);
  const inputs = [
    !item && formGrid(cell(6, field("Title", el("input", {name: "title", required: true, placeholder: "e.g. Arithmetic checks"}))),
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
  dialogForm(dialog, item ? "Add album cases" : "New album", inputs, item ? "Add entries" : "Create album", async form => {
    const cases = form.getAll("case").map(value => ({case: value}));
    const profile = form.get("profile");
    const joined = [...entries, ...cases, ...(profile ? [{profile}] : [])];
    if (item) {
      await api(`/api/playlists/${encodeURIComponent(item.id)}`, "PUT",
        {op: "add", digest: item.digest, entries: joined, ...(item.frozen && {reason: form.get("reason")})});
    } else {
      const fields = Object.fromEntries(["id", "title", "shelf", "goal", "environment"].map(key => [key, form.get(key)])
        .filter(([, value]) => value?.trim()));
      const result = await api("/api/playlists", "POST", {...fields, entries: joined, ...(project && {project})});
      location.hash = to("playlist", result.playlist.id);
    }
    await refresh();
    toast(item ? "Album updated" : "Album created");
  });
}

document.querySelector(".skip-link").addEventListener("click", event => {
  event.preventDefault();
  workspace.focus();
});
window.addEventListener("hashchange", () => {
  if (route()[0] === "collection") $("search").value = "";
  state.filter = state.kind = state.result = "";
  render();
  window.scrollTo({top: 0, behavior: "instant"});
});
$("refresh").onclick = () => refresh(true);
$("search").addEventListener("input", () => {
  if (!["overview", "shelf", "library", "project", "collection"].includes(route()[0])) location.hash = to("library");
  else render();
});
document.addEventListener("keydown", event => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    if (!dialog.open) { event.preventDefault(); openQuickJump(); }
    else if (dialog.querySelector(".quick-jump")) { event.preventDefault(); dialog.close(); }
    return;
  }
  if (event.key === "/" && !dialog.open && !event.target.matches("input, select, textarea")) {
    event.preventDefault();
    $("search").focus();
  }
});
refresh();
setInterval(() => {
  if (state.catalog && currentJob(state.catalog)) {
    if (dialog.open || workspace.contains(document.activeElement)) refreshPlayer();
    else refresh();
  }
}, 1200);
setInterval(() => {
  if (state.catalog && !currentJob(state.catalog) && !dialog.open) {
    if (route()[0] === "job" && workspace.contains(document.activeElement)) refreshPlayer();
    else if (!workspace.contains(document.activeElement)) refresh();
  }
}, 8000);
