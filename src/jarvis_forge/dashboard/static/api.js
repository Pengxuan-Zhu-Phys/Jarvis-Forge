// Live requests stay on this origin. Demo is an explicit, browser-local mode.
export const demoMode = new URLSearchParams(location.search).get("mode") === "demo";

function catalog(data) {
  const themes = ["mint", "blue", "peach", "lavender"];
  data.projects = data.projects.map((row, index) => ({
    ...row, theme: row.theme || themes[index % themes.length],
    icon: row.icon || (row.id === "plot" ? "📊" : "📦")
  }));
  data.cases = data.cases.map(row => ({...row, title: row.title || row.id.split("/").at(-1)}));
  return data;
}

export async function apiFetch(path, options = {}) {
  if (demoMode) return (await import("./preview-api.js")).previewFetch(path, options);
  // Project is UI context; Forge resolves ownership from the entry closure.
  if (path === "/api/playlists" && options.body) {
    const body = JSON.parse(options.body);
    delete body.project;
    options = {...options, body: JSON.stringify(body)};
  }
  const response = await fetch(path, {...options, credentials: "same-origin", cache: "no-store"});
  let data;
  try { data = await response.json(); } catch { data = {ok: false, error: {message: `The service returned HTTP ${response.status} without a JSON response.`}}; }
  if (data.ok && data.cases && data.projects) catalog(data);
  return new Response(JSON.stringify(data), {status: response.status, statusText: response.statusText,
    headers: {"Content-Type": "application/json"}});
}
