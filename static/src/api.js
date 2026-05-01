// fetch + asset-URL helpers and a handful of tiny DOM utilities reused
// everywhere. Kept in one module so other modules don't all reach into the
// global scope for `$` / `html` / `escape`.

// Cache-bust thumb/preview URLs so a server-side regeneration (e.g. after the
// EXIF-orientation fix) actually shows up; pinned per page-load so a normal
// refresh evicts stale browser-cached images.
export const ASSET_VERSION = String(Date.now());

// Optional shared-secret token (APP_API_TOKEN env). When non-empty, every
// fetch carries an X-API-Token header and every <img> URL gets a ?token=
// query string (browsers can't set headers on native asset loads).
export const API_TOKEN = (typeof window !== 'undefined' && window.PHOTOTAG_API_TOKEN) || '';

const tokenQuery = (sep) => API_TOKEN ? `${sep}token=${encodeURIComponent(API_TOKEN)}` : '';

export const assetUrl = (kind, id) => `/${kind}/${id}?v=${ASSET_VERSION}${tokenQuery('&')}`;

export const $ = (id) => document.getElementById(id);

export const html = (s) => {
  const d = document.createElement('div');
  d.innerHTML = s.trim();
  return d.firstElementChild;
};

export const escape = (s) =>
  String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

export async function api(path, opts) {
  // no-store: the UI mutates state via POST/DELETE then immediately re-reads
  // — without this, browsers happily serve a cached GET of /api/images/{id}/faces
  // and the user sees pre-mutation state ("wrong didn't unassign").
  const merged = Object.assign({ cache: 'no-store' }, opts || {});
  if (API_TOKEN) {
    const headers = new Headers(merged.headers || {});
    headers.set('X-API-Token', API_TOKEN);
    merged.headers = headers;
  }
  const r = await fetch(path, merged);
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}
