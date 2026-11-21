// Local artwork and shelf preferences are independent of API records and interface palette.
const asset = name => new URL(`./assets/covers/${name}.jpg`, import.meta.url).href;
export const coverThemes = [
  {id: 'fluid', name: 'Fluid', description: 'Sculpted lavender glass', images: ["fluid-violet", "fluid-knot", "fluid-fins", "fluid-loop", "fluid-shell", "fluid-arc", "fluid-fold", "fluid-sphere", "fluid-ribbon", "fluid-lattice"].map(asset)},
  {id: 'paper', name: 'Paper', description: 'Warm folded paper', images: ["sculpted-paper", "paper-fan", "paper-arch", "paper-spiral", "paper-terrace", "paper-petal", "paper-accordion", "paper-tower", "paper-radial", "paper-wave"].map(asset)},
  {id: 'orbit', name: 'Orbit', description: 'Luminous emerald metal', images: ["emerald-orbits", "orbit-gyro", "orbit-twin", "orbit-lattice", "orbit-halo", "orbit-ellipses", "orbit-spiral", "orbit-satellite", "orbit-crescent", "orbit-pendulum"].map(asset)},
  {id: 'terrain', name: 'Terrain', description: 'Cinematic mountain photography', images: ["terrain-ridge", "terrain-dunes", "terrain-glacier", "terrain-sea", "terrain-canyon", "terrain-volcano", "terrain-river", "terrain-forest", "terrain-island", "terrain-cloud"].map(asset)},
  {id: 'botanical', name: 'Botanical', description: 'Botanical cyanotype prints', images: ["botanical-fern", "botanical-ginkgo", "botanical-kelp", "botanical-poppy", "botanical-eucalyptus", "botanical-grasses", "botanical-monstera", "botanical-algae", "botanical-seedpods", "botanical-magnolia"].map(asset)},
  {id: 'prism', name: 'Prism', description: 'Modernist geometric screenprints', images: ["prism-sun", "prism-stairs", "prism-balance", "prism-portal", "prism-circles", "prism-diagonal", "prism-fan", "prism-arch", "prism-grid", "prism-wave"].map(asset)},
 ];
// v2 defaults to shelf-specific artwork, including browsers upgraded from the global-only picker.
const storageKey = 'forge-playlists-cover-preference-v2';
const valid = value => value === 'auto' || coverThemes.some(theme => theme.id === value);
let selected = 'auto';
try { const saved = localStorage.getItem(storageKey); if (valid(saved)) selected = saved; } catch { /* Memory-only choice works too. */ }
document.documentElement.dataset.coverTheme = selected;
export const currentCoverTheme = () => selected;
const shelfDefaults = {'jarvis-plot': 'fluid', sarah: 'botanical', 'jarvis-forge': 'prism', shared: 'terrain', projects: 'paper', examples: 'orbit'};
let assignedShelves = new Map();
export function configureCoverShelves(projects = []) {
  assignedShelves = new Map();
  const ids = [...new Set(projects.map(project => project.id).filter(Boolean))].sort();
  const used = new Set(ids.map(id => shelfDefaults[id]).filter(Boolean));
  for (const id of ids) {
    const theme = shelfDefaults[id] || coverThemes.find(row => !used.has(row.id))?.id || coverThemes[assignedShelves.size % coverThemes.length].id;
    assignedShelves.set(id, theme); used.add(theme);
  }
}
function hash(value) {
  let result = 2166136261;
  for (const char of String(value)) result = Math.imul(result ^ char.charCodeAt(0), 16777619) >>> 0;
  return result;
}
export function coverShelf(item, project = {}) { return project.id || item.project || item.shelf || project.shelf || 'shared'; }
export function defaultCoverTheme(shelf) {
  return assignedShelves.get(shelf) || shelfDefaults[shelf] || coverThemes[hash(shelf) % coverThemes.length].id;
}
export const resolvedCoverTheme = shelf => selected === 'auto' ? defaultCoverTheme(shelf) : selected;
function apply(value) {
  selected = valid(value) ? value : 'auto';
  document.documentElement.dataset.coverTheme = selected;
  window.dispatchEvent(new Event('cover-theme-change'));
}
export function setCoverTheme(value) {
  if (!valid(value)) return;
  apply(value);
  try { localStorage.setItem(storageKey, selected); } catch { /* Keep the session choice. */ }
}
window.addEventListener('storage', event => {
  if (event.key === storageKey || event.key === null) apply(event.newValue);
});
export const coverVariant = item => hash(item.id || item.title || 'album') % 10;
export function coverImage(variant, shelf = 'shared') {
  const theme = coverThemes.find(row => row.id === resolvedCoverTheme(shelf)) || coverThemes[0];
  return theme.images[variant] || theme.images[0];
}
