import {coverVariant, coverImage, coverShelf, resolvedCoverTheme} from './cover-themes.js?v=1.0.9';
import {jobProgress} from './progress-model.js';
/* DOM components consume data; all views share the same grid, type, and states. */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (typeof value === "boolean") node.toggleAttribute(key, value);
    else if (value != null) node.setAttribute(key, value);
  }
  const append = (child) => {
    if (Array.isArray(child)) child.forEach(append);
    else if (child instanceof Node) node.append(child);
    else if (child != null && typeof child !== "boolean") node.append(document.createTextNode(String(child)));
  };
  children.forEach(append);
  return node;
}

const glyphs = {overview: "◈", library: "▤", playlists: "≡", runs: "↻", shelf: "◇",
  project: "⌘", command: "▸", cases: "▦", run: "▷", arrow: "↗", back: "←", plus: "+", close: "×"};

export function icon(name) {
  return el("span", {class: "icon", "aria-hidden": "true"}, glyphs[name] || "·");
}

export function alignmentIcon() {
  return svgNode('svg', {viewBox: '0 0 24 24', class: 'nav-icon design-icon', 'aria-hidden': 'true', focusable: 'false'},
    ...[[3, 3], [14, 3], [3, 14], [14, 14]].map(([x, y]) => svgNode('rect', {x, y, width: 7, height: 7, rx: 1})));
}

export function button(text, action, {variant = "secondary", glyph, disabled = false} = {}) {
  return el("button", {class: `button ${variant}`, type: "button", onclick: action, disabled},
    glyph && icon(glyph), text);
}

export function link(text, href, {variant = "text-link", glyph} = {}) {
  return el("a", {class: variant, href}, glyph && icon(glyph), text);
}

function navigationIcon(name) {
  const paths = {
    overview: ['m3 11 9-8 9 8', 'M5 10v11h5v-7h4v7h5V10'],
    playlists: ['M4 4h7v7H4z', 'M15 4h5v7h-5z', 'M4 15h7v5H4z', 'M15 15h5v5h-5z'],
    library: ['M4 4h16v16H4z', 'M8 4v16', 'M12 9h5m-5 5h5'],
    runs: ['M4 8V3m0 5h5', 'M4 8a9 9 0 1 1-1 7', 'M12 7v5l3 2'],
    shelf: ['M3 20h18', 'M5 5h5v12H5z', 'm13 6 4-1 3 11-4 1z'],
    search: ['M20 20l-5-5', 'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0']
  };
  return svgNode('svg', {viewBox: '0 0 24 24', class: 'nav-icon', 'aria-hidden': 'true', focusable: 'false'},
    ...(paths[name] || paths.shelf).map(d => svgNode('path', {d})));
}

export function navigation(items, current) {
  return items.map(item => el("a", {class: `nav-link${item.theme ? ` theme-${item.theme}` : ""}${item.live ? " has-live-count" : ""}`,
    href: item.href, title: item.label, "aria-label": item.label, onclick: () => {
      if (location.hash === item.href) window.scrollTo({top: 0, behavior: "instant"});
    },
    "aria-current": item.id === current ? item.context ? "location" : "page" : null},
    item.emoji ? el("span", {class: "nav-project-mark", "aria-hidden": "true"}, item.emoji) : navigationIcon(item.glyph || item.id),
    el("span", {class: "nav-label"}, item.label), item.count != null && el("span", {class: "nav-count", "aria-hidden": "true"},
      item.count > 99 ? "99+" : item.count)));
}

export function sidebarToggle(collapsed, action) {
  const control = button("", action, {variant: "rail-toggle"});
  control.id = "rail-toggle";
  control.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
  control.setAttribute("title", collapsed ? "Expand sidebar" : "Collapse sidebar");
  control.setAttribute("aria-expanded", String(!collapsed));
  control.setAttribute("aria-controls", "rail-groups");
  control.append(svgNode("svg", {viewBox: "0 0 24 24", class: "nav-icon", "aria-hidden": "true", focusable: "false"},
    svgNode("path", {d: "M3 4h18v16H3z M9 4v16"}),
    svgNode("path", {d: collapsed ? "m13 9 3 3-3 3" : "m16 9-3 3 3 3"})));
  return control;
}

export function navigationShortcut(label, action) {
  const control = button("", action, {variant: "rail-shortcut"});
  control.append(navigationIcon("search"), el("span", {class: "rail-shortcut-label"}, label),
    el("kbd", {"aria-hidden": "true"}, "⌘ K"));
  control.setAttribute("aria-label", label);
  return control;
}

export function breadcrumbs(items) {
  return el("ol", {class: "breadcrumb-list"}, items.map((item, index) =>
    el("li", {}, index > 0 && el("span", {class: "crumb-separator", "aria-hidden": "true"}, "//"),
      item.href ? el("a", {href: item.href, title: item.label}, item.label) :
        el("span", {"aria-current": "page", title: item.label}, item.label))));
}

export function designGrid(visible) {
  return el("div", {id: "design-grid", class: "design-grid", "aria-hidden": "true", hidden: !visible},
    Array.from({length: 12}, () => el("span")));
}

export function shellGuides(columns, variant) {
  return [el("div", {class: `shell-design-grid ${variant}-design-grid`, "aria-hidden": "true"},
    Array.from({length: columns}, () => el("span"))),
    el("div", {class: `shell-design-caption ${variant}-design-caption`, "aria-hidden": "true"})];
}

export function formGrid(...children) {
  return el("div", {class: "grid form-grid"}, children);
}

export function grid(...children) {
  return el("div", {class: "grid"}, children);
}

export function split(primary, sidebar, primarySpan = 8) {
  return el("div", {class: "grid grid-split"}, cell(primarySpan, primary), cell(12 - primarySpan, sidebar));
}

export function cell(span, ...children) {
  return el("div", {class: `span-${span} grid-cell`, "data-grid-span": span}, children);
}

export function pageHeader(title, description, actions = [], parent = null) {
  return el("header", {class: "page-header"}, el("div", {class: "grid header-grid"},
    cell(8, parent && link(parent.label, parent.href, {variant: "back-link"}),
      el("h1", {}, title), description && el("p", {}, description)),
    cell(4, el("div", {class: "page-actions"}, actions)),
  ));
}

export function section(title, content, action = null, description = null) {
  return el("section", {class: "section"},
    el("div", {class: "section-head"},
      el("div", {}, el("h2", {}, title), description && el("p", {}, description)), action), content);
}

export function metric(label, value, name, detail) {
  return el("div", {class: "metric"},
    el("div", {class: "metric-head"}, el("span", {}, label), el("span", {class: "metric-icon"}, icon(name))),
    el("strong", {}, value),
    detail && el("small", {}, detail));
}

const statuses = {passed: ["Passed", "good"], reused: ["Reused", "good"], failed: ["Failed", "bad"],
  running: ["Running", "active"], queued: ["Queued", "active"], interrupted: ["Interrupted", "warning"],
  error: ["Error", "bad"], xfailed: ["Expected failure", "neutral"], xpassed: ["Unexpected pass", "warning"], collecting: ["Collecting", "active"], finished: ["Finished", "neutral"], unknown: ["Unknown", "warning"], skipped: ["Skipped", "neutral"], blocked: ["Blocked", "warning"], ready: ["Ready", "good"],
  unavailable: ["Unavailable", "warning"], not_run: ["Not run", "neutral"]};

export function status(kind) {
  const [label, tone] = statuses[kind] || statuses.not_run;
  return el("span", {class: `status ${tone}`, "data-state": kind || "not_run", title: label, "aria-label": label},
    el("i", {"aria-hidden": "true"}), ({xfailed: "XFAIL", xpassed: "XPASS"})[kind] || label);
}

export function recordCard({title, subtitle, name, href, state, detail, action}) {
  return el("article", {class: "record-card"},
    el("div", {class: "record-top"},
      el("div", {class: "tile-mark"}, icon(name)), state ? status(state) : action),
    el("div", {}, el("h3", {}, link(title, href)), el("p", {class: "breakable"}, subtitle)),
    el("div", {class: "record-footer"}, el("span", {}, detail),
      link("Open", href, {variant: "record-link"})));
}

export function empty(title, detail, action = null) {
  return el("div", {class: "empty"}, el("div", {class: "tile-mark"}, icon("playlists")),
    el("strong", {}, title), detail && el("p", {}, detail), action);
}

export function table(columns, rows, label) {
  const columnClass = name => name === "Select" ? "column-select" :
    name === "Actions" ? "column-actions" : ["Time", "Cases"].includes(name) ? "column-number" : null;
  return el("div", {class: "table-scroll", role: "region", tabindex: "0", "aria-label": label},
    el("table", {"aria-label": label},
      el("thead", {}, el("tr", {}, columns.map(col => el("th", {scope: "col", class: columnClass(col)},
        ["Select", "Actions"].includes(col) ? el("span", {class: "sr-only"}, col) : col)))),
      el("tbody", {}, rows.map(row => el("tr", {}, row.map((value, i) =>
        el("td", {class: columnClass(columns[i])}, value)))))));
}

// Native table semantics with explicit shared-grid spans, including the inter-column gutters.
export function gridTable(columns, rows, label) {
  const slot = (value, column, header = false) => el("div", {class: "grid-table-cell", "data-grid-span": column.span},
    el("div", {class: `grid-value ${column.type || "text"}-slot${header ? " header-value" : ""}`,
      "data-grid-content": "", "data-content-span": header || ["status", "number", "select", "kind"].includes(column.type) ? 1 : column.span}, value));
  return el("div", {class: "table-scroll grid-table-frame", role: "region", tabindex: "0", "aria-label": label},
    el("table", {class: "grid-table", "aria-label": label},
      el("colgroup", {}, columns.map((column, index) => el("col", {style:
        `width:calc(var(--column-size) * ${column.span} + var(--gap) * ${column.span - (index === columns.length - 1 ? 1 : 0)})`}))),
      el("thead", {}, el("tr", {}, columns.map(column => el("th", {scope: "col"}, slot(column.label, column, true))))),
      el("tbody", {}, rows.map(row => el("tr", {}, row.map((value, index) => el("td", {}, slot(value, columns[index]))))))));
}

export function field(label, control) {
  return el("label", {class: "field"}, el("span", {}, label), control);
}

export function feedback(messages) {
  return el("div", {class: "notice", role: "status"}, messages.map(message => el("p", {}, message)));
}

export function checkbox(label, checked, action) {
  const input = el("input", {type: "checkbox", checked, "aria-label": label,
    onchange: event => action(event.currentTarget.checked)});
  return input;
}

export function progress(done, total, label) {
  return el("progress", {max: Math.max(1, total || 1), value: total == null ? null : done, "aria-label": label});
}

export function dialogForm(dialog, title, fields, submit, work) {
  const note = el("p", {class: "form-error", role: "alert", hidden: true});
  const send = el("button", {class: "button primary", type: "submit"}, submit);
  const form = el("form", {onsubmit: async event => {
    event.preventDefault();
    send.disabled = true;
    note.hidden = true;
    try {
      await work(new FormData(event.currentTarget));
      dialog.close();
    } catch (error) {
      note.textContent = error.message;
      note.hidden = false;
    } finally {
      send.disabled = false;
    }
  }}, el("div", {class: "form-body"}, fields, note),
    el("div", {class: "form-footer"}, button("Back", () => dialog.close()), send));
  const close = button("×", () => dialog.close(), {variant: "subtle"});
  close.classList.add("dialog-close");
  close.setAttribute("aria-label", "Close dialog");
  dialog.classList.remove("album-detail-dialog", "queue-dialog");
  delete dialog.dataset.view;
  dialog.removeAttribute("aria-describedby");
  dialog.replaceChildren(el("div", {class: "dialog-header"},
    el("h2", {id: "dialog-title"}, title), close), form);
  dialog.setAttribute("aria-labelledby", "dialog-title");
  dialog.showModal();
}

export function runBar(job, href, replay = null, queue = [], onQueue = null) {
  const model = jobProgress(job);
  const done = model.completed;
  const again = replay && button("", replay, {glyph: "run"});
  if (again) {
    again.setAttribute("aria-label", "Run again");
    again.setAttribute("title", "Run again");
  }
  const queueButton = button("", onQueue, {variant: "secondary player-queue-button"});
  queueButton.setAttribute("aria-label", "Play queue");
  queueButton.setAttribute("aria-haspopup", "dialog");
  queueButton.setAttribute("title", `${queue.length} albums waiting`);
  queueButton.append(svgNode("svg", {viewBox: "0 0 24 24", class: "nav-icon", "aria-hidden": "true"},
    svgNode("path", {d: "M4 6h16 M4 12h16 M4 18h9"})),
    queue.length > 0 && el("span", {class: "player-queue-count", "aria-hidden": "true"}, queue.length));
  return el("div", {class: "run-content", id: "player-full"},
    el("div", {class: "run-info player-slot", "data-grid-label": "NAME · 4c"},
      el("span", {class: "player-mark", "aria-hidden": "true"}, ["running", "queued"].includes(job.status) ? "▥" : "▷"),
      el("div", {class: "player-title"},
        el("span", {class: "player-label"}, ["running", "queued"].includes(job.status) ? `Now running · ${queue.length ? `${queue.length} next` : job.simulated ? "Demo" : "Local"}` : `Last run · ${job.simulated ? "Demo" : "Local"}`),
        el("strong", {title: job.label}, job.label))),
    el("div", {class: "player-progress player-slot", "data-grid-label": "PROGRESS · 3c"},
      progress(done, model.total, "Job progress"), el("span", {class: "subtext", title: model.current || model.label}, model.current ? `${model.label} · ${model.current}` : model.label)),
    el("div", {class: "player-state player-slot", "data-grid-label": "STATE · 2c"}, status(job.status)),
    el("div", {class: "player-actions player-slot", "data-grid-label": "ACTIONS · 3c"}, again, queueButton, link("View run", href)),
    el("div", {class: "player-design-grid", "aria-hidden": "true"}, Array.from({length: 12}, () => el("span"))),
    el("div", {class: "player-design-caption", "aria-hidden": "true"}));
}

export function playerNotice(job) {
  const label = (statuses[job.status] || statuses.not_run)[0];
  return el("p", {class: "player-notice", "aria-hidden": "true"}, `${label} ${job.label} · ${job.simulated ? "demo" : "local"}`);
}

export function playerCapsule(job, expand, project = null, waiting = 0) {
  const model = jobProgress(job);
  const total = model.total;
  const done = model.completed;
  const failed = model.outcomes ? (model.outcomes.failed || 0) + (model.outcomes.error || 0) : job.cases.filter(item => item.status === "failed").length;
  const label = (statuses[job.status] || statuses.not_run)[0];
  const outcome = job.status === "failed" && failed ? `${failed} failed` : label;
  const projectTitle = project?.title || "Acceptance run";
  const control = button("", expand, {variant: `player-capsule theme-${project?.theme || "mint"}`});
  control.setAttribute("aria-label", `Expand player: ${job.label}, ${projectTitle}, ${label}, ${model.label}${waiting ? `, ${waiting} albums next` : ""}${failed ? `, ${failed} failed` : ""}`);
  control.setAttribute("aria-expanded", "false");
  control.setAttribute("aria-controls", "player-full");
  control.setAttribute("title", `${projectTitle} · ${job.label} · ${outcome} · ${model.label}${waiting ? ` · ${waiting} next` : ""}`);
  control.dataset.state = job.status;
  const percent = total ? Math.round(done / total * 10000) / 100 : 0;
  control.dataset.progress = String(percent);
  const startedAt = Date.parse(job.started_at || job.submitted_at);
  const phase = Number.isFinite(startedAt) ? Math.max(0, Date.now() - startedAt) % 1600 : 0;
  const cometTiming = `animation-delay: -${phase}ms`;
  const meter = progress(done, total, "Job progress");
  meter.classList.add("sr-only");
  control.append(el("span", {class: "capsule-mark", "aria-hidden": "true"},
      project ? projectMark(project) : el("span", {}, "▷")),
    el("span", {class: "capsule-title"}, el("strong", {}, job.label), el("span", {class: "capsule-project"}, `${projectTitle}${waiting ? ` · +${waiting} next` : ""}`)),
    el("span", {class: "capsule-result"}, el("span", {class: "capsule-outcome"}, outcome),
      el("span", {class: "capsule-count"}, model.label)),
    svgNode("svg", {viewBox: "0 0 24 24", class: "nav-icon", "aria-hidden": "true"}, svgNode("path", {d: "m6 14 6-6 6 6"})),
    svgNode("svg", {class: "capsule-perimeter", "aria-hidden": "true", focusable: "false", preserveAspectRatio: "none"},
      svgNode("path", {class: "capsule-perimeter-track", pathLength: 100}),
      svgNode("path", {class: "capsule-perimeter-fill", pathLength: 100, "stroke-dasharray": `${percent} 100`}),
      svgNode("path", {class: "capsule-comet capsule-comet-glow", pathLength: 100, style: cometTiming}),
      svgNode("path", {class: "capsule-comet capsule-comet-tail", pathLength: 100, style: cometTiming}),
      svgNode("path", {class: "capsule-comet capsule-comet-head", pathLength: 100, style: cometTiming})), meter);
  return control;
}

function svgNode(tag, attrs = {}, ...children) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  node.append(...children);
  return node;
}

export function projectMark(project, variant = "") {
  return el("span", {class: `project-icon ${variant}`, title: project.title, "aria-hidden": "true"},
    project.iconUrl ? el("img", {src: project.iconUrl, alt: ""}) : project.icon || "📦");
}

function coverContents(item, project) {
  const variant = coverVariant(item);
  const shelf = coverShelf(item, project);
  return [
    el('img', {class: 'cover-image', 'data-cover-variant': variant, 'data-cover-shelf': shelf, 'data-cover-theme': resolvedCoverTheme(shelf), src: coverImage(variant, shelf), alt: '', loading: 'lazy', decoding: 'async', width: 768, height: 768}),
    el('span', {class: 'cover-shade', 'aria-hidden': 'true'}),
    el('span', {class: 'cover-engine'}, item.engine || 'Mixed checks'),
    el('span', {class: 'cover-caption'}, item.title),
    el('span', {class: 'cover-imprint'}, project.title)
  ];
}

// Update artwork in place to preserve focus, shelf position, dialogs and active playback.
export function refreshAlbumCovers() {
  for (const image of document.querySelectorAll('img[data-cover-variant]')) {
    image.src = coverImage(Number(image.dataset.coverVariant), image.dataset.coverShelf);
    image.dataset.coverTheme = resolvedCoverTheme(image.dataset.coverShelf);
  }
}

export function albumPreview(item, project) {
  return el('div', {class: `album-preview theme-${project.theme || 'mint'} cover-study`, 'aria-hidden': 'true'}, coverContents(item, project));
}

export function albumCard({item, project, latest, running, pending, queued, queuePosition, onOpen, onRun, lastRunHref, summary}) {
  const cover = button("", onOpen, {variant: "album-cover cover-study"});
  cover.setAttribute("aria-label", `View ${item.title} album details`);
  cover.setAttribute("aria-haspopup", "dialog");
  cover.append(...coverContents(item, project));
  const play = button("", onRun, {variant: "album-play", disabled: pending || !!running || !!queued || !item.members.length});
  play.setAttribute("aria-label", `Run ${item.title} acceptance for ${project.title}`);
  play.setAttribute("title", running ? "Running" : queued ? `Queued · ${queuePosition} next` : `Play ${item.title}`);
  play.setAttribute("data-album-control", item.id);
  play.append(queued ? el("span", {"aria-hidden": "true"}, "≡") : running ? el("span", {class: "equalizer", "aria-hidden": "true"}, el("i"), el("i"), el("i")) :
    el("span", {"aria-hidden": "true"}, "▶"));
  const result = latest ? link("", lastRunHref, {variant: "album-result"}) : status("not_run");
  if (latest) {
    const badge = status(queued ? "queued" : running ? "running" : latest.status);
    if (queued) badge.append(` · ${queuePosition}`);
    result.append(badge);
    result.setAttribute("aria-label", `View ${item.title} ${queued ? "queued" : running ? "running" : latest.status} result`);
  }
  const description = summary || item.description || item.goal || item.title;
  return el("article", {class: `album-card theme-${project.theme || "mint"}`, "data-album-id": item.id, onclick: event => {
      if (!event.target.closest("button, a")) onOpen();
    }}, el("h3", {class: "sr-only"}, item.title),
    el("div", {class: "album-cover-wrap"}, cover, play),
    el("div", {class: "album-info"},
      el("button", {class: "album-summary", type: "button", onclick: onOpen, "aria-haspopup": "dialog",
        "aria-label": `View ${item.title} details`, title: description}, description),
      el("div", {class: "album-facts"}, el("span", {class: "album-case-count", title: latest ? jobProgress(latest).label : `${item.members.length} execution steps`},
        latest ? jobProgress(latest).label : `${item.members.length} execution steps`), result)));
}

export function projectShelf(project, albums, {
  collectionHref, expanded = false, startIndex = 0, onToggle, onPosition
}) {
  const preview = albums.slice(0, 30);
  const overflowing = albums.length > 6;
  const limited = albums.length > 30;
  const regionId = `album-shelf-${project.id}`;
  const region = el("div", {id: regionId, class: `shelf-viewport${expanded ? " is-expanded" : ""}`,
    role: "region", "aria-label": `${project.title} album preview`, tabindex: overflowing && !expanded ? "0" : null},
    el("div", {class: expanded ? "album-grid shelf-grid" : "shelf-strip"}, preview));
  const position = el("span", {class: "shelf-position"});
  let previous, next;
  const pitch = () => {
    const first = region.querySelector(".album-card");
    return first ? first.getBoundingClientRect().width + parseFloat(getComputedStyle(region.firstElementChild).columnGap) : 0;
  };
  const update = () => {
    const maximum = Math.max(0, region.scrollWidth - region.clientWidth);
    const index = pitch() ? Math.round(region.scrollLeft / pitch()) : 0;
    if (previous) previous.disabled = region.scrollLeft <= 1;
    if (next) next.disabled = region.scrollLeft >= maximum - 1;
    position.textContent = expanded ? `${preview.length} / ${albums.length}` :
      `${index + 1}–${Math.min(index + 6, preview.length)} / ${albums.length}`;
    if (!expanded) onPosition?.(index);
  };
  const move = direction => region.scrollBy({left: direction * (region.clientWidth + parseFloat(getComputedStyle(region.firstElementChild).columnGap)),
    behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth"});
  const navigation = el("div", {class: "shelf-navigation"});
  if (overflowing && !expanded) {
    previous = button("", () => move(-1), {disabled: true});
    next = button("", () => move(1));
    for (const [control, label] of [[previous, "Previous"], [next, "Next"]]) {
      control.setAttribute("aria-label", `${label} ${project.title} albums`);
      control.setAttribute("aria-controls", regionId);
      control.setAttribute("title", `${label} albums`);
      control.append(svgNode("svg", {viewBox: "0 0 24 24", class: "shelf-control-icon", "aria-hidden": "true", focusable: "false"},
        svgNode("path", {d: label === "Previous" ? "m14 6-6 6 6 6" : "m10 6 6 6-6 6"})));
    }
    navigation.append(previous, next);
  }
  const toggle = button("", onToggle);
  toggle.append(svgNode("svg", {viewBox: "0 0 24 24", class: "shelf-control-icon", "aria-hidden": "true", focusable: "false"},
    svgNode("path", {d: expanded ? "m6 14 6-6 6 6" : "m6 10 6 6 6-6"})));
  toggle.classList.add("shelf-toggle");
  toggle.setAttribute("aria-expanded", String(expanded));
  toggle.setAttribute("aria-controls", regionId);
  toggle.setAttribute("title", expanded ? "Collapse albums" : limited ? "Expand the first 30 albums" : "Expand all albums");
  toggle.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} ${project.title} albums`);
  const footer = overflowing ? el("div", {class: "shelf-footer"}, position,
    el("div", {class: "shelf-footer-actions"}, toggle,
      limited && el("a", {class: "shelf-more button secondary", href: collectionHref,
        "aria-label": `View all ${albums.length} ${project.title} albums`, title: `View all ${albums.length} albums`},
        svgNode("svg", {viewBox: "0 0 24 24", class: "shelf-control-icon", "aria-hidden": "true", focusable: "false"},
          ...[[4, 4], [14, 4], [4, 14], [14, 14]].map(([x, y]) => svgNode("rect", {x, y, width: 6, height: 6, rx: 1})))))) : null;
  region.addEventListener("scroll", update, {passive: true});
  const section = el("section", {id: `project-shelf-${project.id}`,
    class: `project-shelf theme-${project.theme || "mint"}${expanded ? " shelf-expanded" : ""}`, "aria-label": `${project.title} albums`},
    el("header", {class: "project-shelf-header"},
      el("div", {class: "project-identity"}, el("h2", {},
        el("a", {class: "project-collection-link", href: collectionHref, title: "Open album collection"}, projectMark(project), project.title))),
      el("span", {class: "shelf-count"}, `${albums.length} ${albums.length === 1 ? "album" : "albums"}`), navigation),
    albums.length ? region : empty("No albums yet", "Save cases from this project to add an album."), footer);
  queueMicrotask(() => {
    if (!region.isConnected || !preview.length) return;
    if (!expanded) region.scrollLeft = startIndex * pitch();
    update();
  });
  return section;
}
