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

export function button(text, action, {variant = "secondary", glyph, disabled = false} = {}) {
  return el("button", {class: `button ${variant}`, type: "button", onclick: action, disabled},
    glyph && icon(glyph), text);
}

export function link(text, href, {variant = "text-link", glyph} = {}) {
  return el("a", {class: variant, href}, glyph && icon(glyph), text);
}

export function grid(...children) {
  return el("div", {class: "grid"}, children);
}

export function split(primary, sidebar) {
  return el("div", {class: "grid grid-split"}, cell(8, primary), cell(4, sidebar));
}

export function cell(span, ...children) {
  return el("div", {class: `span-${span} grid-cell`}, children);
}

export function pageHeader(title, description, actions = [], parent = null) {
  return el("header", {class: "page-header"}, grid(
    cell(8, parent && link(parent.label, parent.href, {glyph: "back", variant: "back-link"}),
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
  skipped: ["Skipped", "neutral"], blocked: ["Blocked", "warning"], ready: ["Ready", "good"],
  unavailable: ["Unavailable", "warning"], not_run: ["Not run", "neutral"]};

export function status(kind) {
  const [label, tone] = statuses[kind] || statuses.not_run;
  return el("span", {class: `status ${tone}`}, el("i", {"aria-hidden": "true"}), label);
}

export function recordCard({title, subtitle, name, href, state, detail, action}) {
  return el("article", {class: "record-card"},
    el("div", {class: "record-top"},
      el("div", {class: "tile-mark"}, icon(name)), state ? status(state) : action),
    el("div", {}, el("h3", {}, link(title, href)), el("p", {class: "breakable"}, subtitle)),
    el("div", {class: "record-footer"}, el("span", {}, detail),
      link("Open", href, {variant: "record-link", glyph: "arrow"})));
}

export function empty(title, detail, action = null) {
  return el("div", {class: "empty"}, el("div", {class: "tile-mark"}, icon("playlists")),
    el("strong", {}, title), detail && el("p", {}, detail), action);
}

export function table(columns, rows, label) {
  return el("div", {class: "table-scroll", role: "region", tabindex: "0", "aria-label": label},
    el("table", {"aria-label": label},
      el("thead", {}, el("tr", {}, columns.map(col => el("th", {scope: "col"}, col)))),
      el("tbody", {}, rows.map(row => el("tr", {}, row.map(value => el("td", {}, value)))))));
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
  return el("progress", {max: Math.max(1, total), value: done, "aria-label": label});
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
  }}, fields, note, el("div", {class: "form-footer"}, button("Back", () => dialog.close()), send));
  dialog.replaceChildren(el("h2", {id: "dialog-title"}, title), form);
  dialog.setAttribute("aria-labelledby", "dialog-title");
  dialog.showModal();
}

export function runBar(job, href) {
  const done = job.cases.filter(item => !["queued", "running"].includes(item.status)).length;
  return el("div", {class: "run-content"},
    el("div", {class: "run-info"}, status(job.status), el("strong", {}, job.label)),
    progress(done, job.cases.length, "Job progress"),
    el("span", {class: "subtext"}, `${done} / ${job.cases.length} cases`),
    link("View run", href, {glyph: "arrow"}));
}
