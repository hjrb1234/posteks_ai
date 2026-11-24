// Preview builds use hash routing so the same UI can be opened from a local HTML file.
// The production entry never sets this attribute and keeps normal server routes.
export function currentPath(): string {
  return document.documentElement.dataset.preview === 'true'
    ? (window.location.hash.slice(1) || '/')
    : window.location.pathname;
}

export function pushPath(path: string): void {
  window.history.pushState({}, '', document.documentElement.dataset.preview === 'true' ? `#${path}` : path);
}
