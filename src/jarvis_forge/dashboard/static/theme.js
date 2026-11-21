// Apply the saved palette before CSS paints; theme choice is independent of Demo data.
(() => {
  const key = 'forge-playlists-theme';
  const root = document.documentElement;
  let theme = 'dark';
  try { if (localStorage.getItem(key) === 'light') theme = 'light'; } catch { /* Keep the in-memory default. */ }
  function apply(value) {
    theme = value === 'light' ? 'light' : 'dark';
    root.dataset.theme = theme;
    document.querySelector('meta[name="color-scheme"]')?.setAttribute('content', theme);
    const toggle = document.getElementById('theme-toggle');
    if (toggle) {
      const action = `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`;
      toggle.setAttribute('aria-label', action);
      toggle.setAttribute('title', action);
    }
  }
  apply(theme);
  document.addEventListener('DOMContentLoaded', () => {
    apply(theme);
    document.getElementById('theme-toggle')?.addEventListener('click', () => {
      apply(theme === 'dark' ? 'light' : 'dark');
      try { localStorage.setItem(key, theme); } catch { /* Switching still works without storage. */ }
    });
  });
  window.addEventListener('storage', event => { if (event.key === key) apply(event.newValue); });
})();
