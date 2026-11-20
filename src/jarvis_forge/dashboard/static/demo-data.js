/* Illustrative acceptance suites. These descriptors never execute local commands. */
export const demoProjects = [
  {id: 'jarvis-plot', title: 'Jarvis-PLOT', icon: '📈', shelf: 'projects', available: true,
    description: 'Plotting, exports & web previews', theme: 'violet', sources: [{id: 'illustrative demo'}]},
  {id: 'sarah', title: 'SARAH-Jarvis', icon: '⚛️', shelf: 'examples', available: true,
    description: 'Model checks & numerical validation', theme: 'mint', sources: [{id: 'demo'}]},
  {id: 'jarvis-forge', title: 'Jarvis-Forge', icon: '🔧', shelf: 'projects', available: true,
    description: 'Environments & build workflows', theme: 'amber', sources: [{id: 'illustrative demo'}]}
];

const testCase = (project, module, name, title, kind = 'pytest', demoOutcome = 'passed') => ({
  id: `${project}/demo/${module}::${name}`, title, project, kind, tags: [module], demoOutcome,
  example: `${title}\n${kind === 'pytest' ? '1 passed (simulated)' : 'Demo check completed.'}`,
  ...(demoOutcome === 'failed' && {failure: 'Simulated assertion: exported axis labels differ from the reference.'})
});
export const demoCases = [
  testCase('jarvis-plot', 'render', 'axes', 'Axes and legend layout'),
  testCase('jarvis-plot', 'render', 'series', 'Multiple series rendering'),
  testCase('jarvis-plot', 'export', 'png', 'PNG export dimensions'),
  testCase('jarvis-plot', 'export', 'svg', 'SVG axis labels', 'pytest', 'failed'),
  testCase('jarvis-plot', 'web', 'routes', 'Preview page navigation', 'check'),
  testCase('jarvis-plot', 'web', 'resize', 'Responsive plot viewport', 'check'),
  ...[[2, 3], [5, 7]].map(([a, b]) => ({id: `sarah/demo/demo.arithmetic::${a},${b}`,
    title: `Sum and product · ${a} and ${b}`, project: 'sarah', kind: 'command', tags: ['arithmetic'],
    example: `${a} + ${b} = ${a + b}\n${a} × ${b} = ${a * b}`})),
  testCase('sarah', 'model', 'parameters', 'Model parameter validation'),
  testCase('sarah', 'model', 'constraints', 'Physical constraint checks'),
  testCase('jarvis-forge', 'environment', 'python', 'Selected Python environment', 'command'),
  testCase('jarvis-forge', 'environment', 'dependencies', 'Dependency availability', 'command'),
  testCase('jarvis-forge', 'build', 'recipe', 'Build recipe validation'),
  testCase('jarvis-forge', 'build', 'artifacts', 'Expected build artifacts')
];
const album = (id, project, title, description, module, artwork, coverLabel, engine = 'pytest') => {
  const members = demoCases.filter(row => row.project === project && row.tags.includes(module)).map(row => row.id);
  return {id, project, title, description, artwork, coverLabel, engine,
    shelf: project === 'sarah' ? 'examples' : 'projects', stored: true,
    members, entries: members.map(key => ({case: key})), digest: `demo-${id}`, frozen: null};
};
export const demoAlbums = [
  album('plot-rendering', 'jarvis-plot', 'Plot rendering', 'Axes, legends, and multi-series plots.', 'render', 'curves', 'FORM / FUNCTION'),
  album('plot-exports', 'jarvis-plot', 'Export pipeline', 'Image dimensions and vector output.', 'export', 'frames', 'BEYOND / THE CANVAS'),
  album('plot-web-preview', 'jarvis-plot', 'Web preview', 'Navigation and responsive viewports.', 'web', 'windows', 'IN / THE BROWSER', 'Web checks'),
  album('sarah-arithmetic', 'sarah', 'Arithmetic checks', 'Sum and product reference cases.', 'arithmetic', 'orbits', 'SUM / OF THE PARTS', 'Command'),
  album('sarah-model', 'sarah', 'Model validation', 'Parameters and physical constraints.', 'model', 'matrix', 'MODEL / IN BALANCE'),
  album('forge-environment', 'jarvis-forge', 'Environment checks', 'Python selection and dependencies.', 'environment', 'steps', 'READY / TO RUN', 'Command'),
  album('forge-build', 'jarvis-forge', 'Build recipes', 'Recipe validation and build artifacts.', 'build', 'blocks', 'BUILT / TOGETHER')
];
// Larger illustrative libraries exercise the six-wide and thirty-preview boundaries.
const plotSelections = [
  ['plot-axis-layout', 'Axis layout', 'render', 'curves'],
  ['plot-series-overlays', 'Series overlays', 'render', 'curves'],
  ['plot-legend-layout', 'Legend layout', 'render', 'matrix'],
  ['plot-multi-panel', 'Multi-panel plots', 'render', 'frames'],
  ['plot-style-regression', 'Style regression', 'render', 'matrix'],
  ['plot-dense-labels', 'Dense labels', 'render', 'curves'],
  ['plot-small-figures', 'Small figures', 'render', 'frames'],
  ['plot-large-figures', 'Large figures', 'render', 'frames'],
  ['plot-reference-curves', 'Reference curves', 'render', 'curves'],
  ['plot-numeric-series', 'Numeric series', 'render', 'orbits'],
  ['plot-png-output', 'PNG output', 'export', 'frames'],
  ['plot-vector-output', 'Vector output', 'export', 'frames'],
  ['plot-axis-label-export', 'Axis label export', 'export', 'matrix'],
  ['plot-publication-output', 'Publication output', 'export', 'frames'],
  ['plot-export-smoke', 'Export smoke', 'export', 'steps'],
  ['plot-output-dimensions', 'Output dimensions', 'export', 'frames'],
  ['plot-format-checks', 'Format checks', 'export', 'blocks'],
  ['plot-asset-regression', 'Asset regression', 'export', 'matrix'],
  ['plot-download-flow', 'Download flow', 'export', 'steps'],
  ['plot-preview-layout', 'Preview layout', 'web', 'windows'],
  ['plot-page-navigation', 'Page navigation', 'web', 'steps'],
  ['plot-viewport-checks', 'Viewport checks', 'web', 'windows'],
  ['plot-browser-smoke', 'Browser smoke', 'web', 'windows'],
  ['plot-workspace-preview', 'Workspace preview', 'web', 'blocks'],
  ['plot-preview-resize', 'Preview resize', 'web', 'windows'],
  ['plot-navigation-regression', 'Navigation regression', 'web', 'steps'],
  ['plot-interactive-preview', 'Interactive preview', 'web', 'orbits'],
  ['plot-visual-smoke', 'Visual smoke', 'web', 'windows'],
  ['plot-full-module-check', 'Full module check', 'render', 'blocks']
];
for (const [id, title, module, artwork] of plotSelections) {
  const description = module === 'render' ? 'Axes, legends, and series reference cases.' :
    module === 'export' ? 'Image size and SVG label reference cases.' : 'Page navigation and viewport reference cases.';
  demoAlbums.push(album(id, 'jarvis-plot', title, description, module, artwork, title, module === 'web' ? 'Web checks' : 'pytest'));
}
for (const [id, title, module, artwork] of [
  ['sarah-parameter-checks', 'Parameter checks', 'model', 'matrix'],
  ['sarah-physical-constraints', 'Physical constraints', 'model', 'orbits'],
  ['sarah-sum-checks', 'Sum checks', 'arithmetic', 'orbits'],
  ['sarah-product-checks', 'Product checks', 'arithmetic', 'matrix'],
  ['sarah-model-smoke', 'Model smoke', 'model', 'blocks']
]) {
  demoAlbums.push(album(id, 'sarah', title, module === 'model' ? 'Model parameters and constraint reference cases.' : 'Numerical sum and product reference cases.',
    module, artwork, title, module === 'arithmetic' ? 'Command' : 'pytest'));
}

export const demoJobs = ['plot-rendering', 'plot-exports', 'sarah-arithmetic', 'forge-environment'].map((id, index) => {
  const item = demoAlbums.find(row => row.id === id);
  const rows = item.members.map(key => ({...demoCases.find(row => row.id === key),
    status: demoCases.find(row => row.id === key).demoOutcome || 'passed', seconds: .12}));
  return {id: id === 'sarah-arithmetic' ? 'demo-arithmetic' : `demo-${id}`, playlist: id,
    project: item.project, label: item.title, simulated: true,
    status: rows.some(row => row.status === 'failed') ? 'failed' : 'passed',
    submitted_at: `2026-11-19T15:${36 - index * 3}:00Z`, finished_at: `2026-11-19T15:${36 - index * 3}:03Z`, cases: rows};
});
