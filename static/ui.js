const state = {
  runs: [],
  runId: null,
  clusters: [],
  selectedCluster: null,
  selectedFaceCluster: null,
  activeTags: new Set(),
  activePersons: new Set(),
  lightboxOpen: false,
  // current set of images in the workspace, used by the lightbox for navigation.
  viewIds: [],
  viewIndex: 0,
};

// Cache-bust thumb/preview URLs so a server-side regeneration (e.g. after the
// EXIF-orientation fix) actually shows up; pinned per page-load so a normal
// refresh evicts stale browser-cached images.
const ASSET_VERSION = String(Date.now());
const assetUrl = (kind, id) => `/${kind}/${id}?v=${ASSET_VERSION}`;

const $ = (id) => document.getElementById(id);
const html = (s) => { const d = document.createElement('div'); d.innerHTML = s.trim(); return d.firstElementChild; };
const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

async function loadRuns() {
  state.runs = await api('/api/runs');
  const sel = $('run-select');
  sel.innerHTML = '';
  state.runs.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.id;
    opt.textContent = `run ${r.id} — ${r.n_clusters}c · ${r.n_noise}n · ${r.total_images}img`;
    sel.appendChild(opt);
  });
  if (!state.runs.length) return;
  // Hash overrides default-latest-run.
  const hashState = parseHash();
  state.runId = hashState.run && state.runs.some(r => r.id === hashState.run)
    ? hashState.run : state.runs[0].id;
  sel.value = state.runId;
  await loadClusters();
  await loadTopTags();
  await loadFacesSummary();
  await applyHashState(hashState);
}

async function loadFacesSummary() {
  try {
    const s = await api('/api/faces/summary');
    if (s.images > 0) $('faces-count').textContent = `(${s.images} photos / ${s.faces} faces)`;
    else $('faces-count').textContent = '(none yet)';
  } catch (e) { /* faces feature not enabled */ }
}

async function loadClusters() {
  state.clusters = await api(`/api/runs/${state.runId}/clusters`);
  const root = $('cluster-list');
  root.innerHTML = '';
  state.clusters.forEach(c => {
    const isNoise = c.cluster_no === -1;
    const userLabel = c.label_user ? `<span class="user-label">${escape(c.label_user)}</span>` : '';
    const autoLabel = c.label_auto ? `<span class="auto-label">${escape(c.label_auto)}</span>` : '';
    const row = html(`<div class="cluster-row ${isNoise ? 'noise' : ''}" data-id="${c.id}">
      <div class="label">${userLabel}${userLabel && autoLabel ? '<br>' : ''}${autoLabel}</div>
      <div class="size">${c.size}</div>
    </div>`);
    row.onclick = () => selectCluster(c.id);
    root.appendChild(row);
  });
}

async function loadTopTags() {
  const tags = await api('/api/tags?limit=30');
  const root = $('top-tags');
  root.innerHTML = '';
  tags.forEach(t => {
    const el = html(`<span class="tag" data-tag="${escape(t.name)}">${escape(t.name)} <span class="count">${t.count}</span></span>`);
    el.onclick = () => toggleTag(t.name);
    root.appendChild(el);
  });
}

function selectCluster(id) {
  state.selectedCluster = id;
  document.querySelectorAll('.cluster-row').forEach(r => r.classList.toggle('selected', Number(r.dataset.id) === id));
  showCluster(id);
}

async function showCluster(id) {
  const ws = $('workspace');
  ws.innerHTML = '<div class="empty">loading…</div>';
  const c = await api(`/api/clusters/${id}?limit=120`);
  ws.innerHTML = '';
  ws.appendChild(html(`<h2>${c.label_user ? escape(c.label_user) : `cluster #${c.cluster_no}`} <button id="cluster-edit-toggle" class="pen-btn" title="edit label">✏️</button></h2>`));
  if (c.label_auto) ws.appendChild(html(`<div class="auto">auto: ${escape(c.label_auto)} · size ${c.size}</div>`));
  // rename — hidden until pen clicked
  const renameDiv = html(`<div class="rename" id="cluster-rename" style="display:none;">
    <input id="rename-input" type="text" placeholder="user label (blank to clear)" value="${escape(c.label_user || '')}">
    <button id="rename-btn">save</button>
  </div>`);
  ws.appendChild(renameDiv);
  $('cluster-edit-toggle').onclick = () => {
    const r = $('cluster-rename');
    r.style.display = r.style.display === 'none' ? 'flex' : 'none';
    if (r.style.display === 'flex') $('rename-input').focus();
  };
  $('rename-btn').onclick = async () => {
    const v = $('rename-input').value.trim();
    await api(`/api/clusters/${id}/rename`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ label_user: v || null })
    });
    await loadClusters();
    await showCluster(id);
  };
  // top tags
  if (c.top_tags?.length) {
    const tagDiv = html('<div class="tags"></div>');
    c.top_tags.forEach(t => {
      const el = html(`<span class="tag ${state.activeTags.has(t.name) ? 'active' : ''}" data-tag="${escape(t.name)}">${escape(t.name)} <span class="count">${t.count}</span></span>`);
      el.onclick = () => toggleTag(t.name);
      tagDiv.appendChild(el);
    });
    ws.appendChild(tagDiv);
  }
  // members
  const grid = html('<div class="grid"></div>');
  if (!c.members.length) grid.appendChild(html('<div class="empty">empty cluster</div>'));
  c.members.forEach(m => grid.appendChild(makeTile(m.image_id, m.path)));
  ws.appendChild(grid);
  state.viewIds = c.members.map(m => m.image_id);
}

function makeTile(image_id, path, score) {
  const name = path.split('/').pop();
  const scoreLabel = score != null ? `<b>${score.toFixed(2)}</b> · ` : '';
  const tile = html(`<div class="tile" data-id="${image_id}">
    <img loading="lazy" src="${assetUrl('thumb', image_id)}" alt="">
    <div class="meta">${scoreLabel}${escape(name)}</div>
  </div>`);
  tile.onclick = () => openLightbox(image_id);
  return tile;
}

function openLightbox(image_id) {
  // Seed viewIds from the current grid if needed (e.g. user clicked outside any tracked view).
  if (!state.viewIds.length) state.viewIds = [image_id];
  let idx = state.viewIds.indexOf(image_id);
  if (idx < 0) { state.viewIds = [image_id, ...state.viewIds]; idx = 0; }
  state.viewIndex = idx;
  state.lightboxOpen = true;
  $('lightbox').classList.add('show');
  showCurrentLightbox();
  writeHash();
}

let lightboxToken = 0;

async function showCurrentLightbox() {
  if (!state.viewIds.length) return;
  const id = state.viewIds[state.viewIndex];
  const myToken = ++lightboxToken;
  const img = $('lightbox-img');
  img.src = assetUrl('preview', id);
  img.dataset.imageId = id;
  $('face-layer').innerHTML = '';
  $('lightbox-counter').textContent = `${state.viewIndex + 1} / ${state.viewIds.length}`;
  $('lightbox-info').innerHTML = '<div>loading…</div>';
  preloadNeighbors(state.viewIndex);
  const [info, faces] = await Promise.all([
    api(`/api/images/${id}`),
    api(`/api/images/${id}/faces`).catch(() => []),
  ]);
  if (myToken !== lightboxToken) return;  // user navigated again before fetch returned
  const tagHtml = info.tags.map(t =>
    `<span class="tag" onclick="toggleTag('${escape(t.name)}'); closeLightbox();">${escape(t.name)} <span class="count">${t.score.toFixed(2)}</span></span>`
  ).join('');
  const facesActions = [];
  if (faces && faces.length > 0) {
    facesActions.push(`<a href="#" onclick="event.preventDefault(); deleteAllFacesOnImage(${id});" style="color:#fca5a5;">drop ${faces.length} faces</a>`);
  }
  facesActions.push(`<a href="#" onclick="event.preventDefault(); redetectFaces(${id});" style="color:#9cf;">re-detect faces</a>`);
  const facesActionStr = facesActions.length ? ' · ' + facesActions.join(' · ') : '';
  $('lightbox-info').innerHTML = `<div>${escape(info.path)} · <a href="${assetUrl('raw', id)}" target="_blank" style="color:#9cf;">open original</a>${facesActionStr}</div>${formatExif(info.exif)}<div class="tags" style="margin-top:6px;">${tagHtml}</div>`;
  state.lightboxFaces = faces;
  state.lightboxImage = info;
  // Render once the displayed size is known.
  if (img.complete && img.naturalWidth > 0) renderFaceOverlays();
  else img.addEventListener('load', renderFaceOverlays, { once: true });
}

function formatExif(exif) {
  if (!exif) return '';
  const parts = [];
  if (exif.datetime_original) parts.push(escape(exif.datetime_original.replace('T', ' ')));
  const camera = [exif.make, exif.model].filter(Boolean).join(' ');
  if (camera) parts.push(escape(camera));
  if (exif.lens) parts.push(escape(exif.lens));
  const expo = [];
  if (exif.f_number) expo.push(`f/${Number(exif.f_number).toFixed(1)}`);
  if (exif.exposure_time) {
    const t = Number(exif.exposure_time);
    expo.push(t >= 1 ? `${t.toFixed(1)}s` : `1/${Math.round(1/t)}s`);
  }
  if (exif.iso) expo.push(`ISO ${exif.iso}`);
  if (exif.focal_length) expo.push(`${Number(exif.focal_length).toFixed(0)}mm`);
  if (expo.length) parts.push(escape(expo.join(' · ')));
  if (exif.gps) {
    const { lat, lon } = exif.gps;
    parts.push(
      `<a href="https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}&zoom=15" target="_blank" style="color:#9cf;">${lat.toFixed(5)}, ${lon.toFixed(5)}</a>`
    );
  }
  if (!parts.length) return '';
  return `<div style="margin-top:4px;opacity:0.8;">${parts.join(' · ')}</div>`;
}

function preloadNeighbors(idx) {
  // Pre-fetch a small window in both directions so ← / → feels instant.
  const n = state.viewIds.length;
  if (n <= 1) return;
  for (const o of [1, -1, 2, -2]) {
    const i = ((idx + o) % n + n) % n;
    if (i === idx) continue;
    const im = new Image();
    im.src = assetUrl('preview', state.viewIds[i]);
  }
}

function navLightbox(delta) {
  if (!state.viewIds.length) return;
  const n = state.viewIds.length;
  state.viewIndex = ((state.viewIndex + delta) % n + n) % n;
  showCurrentLightbox();
  writeHash();
}

function closeLightbox() {
  state.lightboxOpen = false;
  $('lightbox').classList.remove('show');
  closeFaceNameForm();
  writeHash();
}

// ---- face overlays in the lightbox ---------------------------------------

state.lightboxFaces = [];
state.lightboxImage = null;
state.facesVisible = true;

function renderFaceOverlays() {
  const img = $('lightbox-img');
  const layer = $('face-layer');
  layer.innerHTML = '';
  if (!state.facesVisible || !state.lightboxFaces.length) return;
  if (!img.naturalWidth) return;
  // The face layer has the same size as the rendered image, but the image is
  // rendered at clientWidth/clientHeight while bboxes are in naturalWidth/Height.
  const sx = img.clientWidth / img.naturalWidth;
  const sy = img.clientHeight / img.naturalHeight;
  layer.style.left = img.offsetLeft + 'px';
  layer.style.top = img.offsetTop + 'px';
  layer.style.width = img.clientWidth + 'px';
  layer.style.height = img.clientHeight + 'px';
  for (const f of state.lightboxFaces) {
    if (!f.bbox || f.bbox.length !== 4) continue;
    const [x, y, w, h] = f.bbox;
    const box = document.createElement('div');
    const suspect = f.verified === 0 || (f.det_score != null && f.det_score < 0.65);
    box.className = 'face-box' + (f.named ? '' : ' unnamed') + (suspect ? ' suspect' : '');
    box.style.setProperty('--c', f.color);
    box.style.left = (x * sx) + 'px';
    box.style.top = (y * sy) + 'px';
    box.style.width = (w * sx) + 'px';
    box.style.height = (h * sy) + 'px';
    box.title = f.label || 'unnamed face';
    const lbl = document.createElement('div');
    lbl.className = 'face-label';
    lbl.textContent = f.label || 'name…';
    box.appendChild(lbl);
    box.onclick = (e) => { e.stopPropagation(); onFaceClicked(f, e.clientX, e.clientY); };
    layer.appendChild(box);
  }
}

window.addEventListener('resize', renderFaceOverlays);

function toggleFaceOverlays() {
  state.facesVisible = !state.facesVisible;
  $('lightbox-toggle-faces').style.opacity = state.facesVisible ? '1' : '0.4';
  renderFaceOverlays();
}

$('lightbox-toggle-faces').onclick = (e) => { e.stopPropagation(); toggleFaceOverlays(); };

async function onFaceClicked(face, clickX, clickY) {
  // Always open the menu — gives access to wrong/delete even on named faces.
  openFaceNameForm(face, clickX, clickY);
}

let pendingFace = null;

function openFaceNameForm(face, x, y) {
  pendingFace = face;
  const form = $('face-name-form');
  form.style.left = Math.min(window.innerWidth - 480, x) + 'px';
  form.style.top = Math.min(window.innerHeight - 60, y) + 'px';
  form.style.display = 'flex';
  $('face-name-input').value = face.label || '';
  $('face-name-input').placeholder = face.named ? 'rename this cluster…' : 'name this person…';
  // Toggle context-sensitive buttons.
  $('face-view-person').style.display = (face.named && face.cluster_id != null) ? 'inline-block' : 'none';
  if (face.named) $('face-view-person').textContent = `view 👤 ${face.label}`;
  $('face-wrong').style.display = (face.cluster_id != null) ? 'inline-block' : 'none';
  $('face-name-input').focus();
  $('face-name-input').select();
}

function closeFaceNameForm() {
  $('face-name-form').style.display = 'none';
  pendingFace = null;
}

async function saveFaceName() {
  if (!pendingFace) return;
  const name = $('face-name-input').value.trim();
  if (!name) { closeFaceNameForm(); return; }
  // Two paths: rename an existing cluster, or create a manual cluster on the fly
  // (when face detection has run but face clustering hasn't yet).
  const url = pendingFace.cluster_id != null
    ? `/api/people/${pendingFace.cluster_id}/name`
    : `/api/faces/${pendingFace.id}/name`;
  try {
    await api(url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name }),
    });
  } catch (e) {
    alert('save failed: ' + e.message);
  }
  closeFaceNameForm();
  // Reload faces for current image so this overlay updates immediately.
  showCurrentLightbox();
}

$('face-name-save').onclick = saveFaceName;
$('face-name-cancel').onclick = closeFaceNameForm;
$('face-view-person').onclick = () => {
  if (pendingFace?.cluster_id != null) viewPerson(pendingFace.cluster_id);
  closeFaceNameForm();
};
$('face-wrong').onclick = async () => {
  if (!pendingFace) return;
  try {
    await api(`/api/faces/${pendingFace.id}/unassign`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
  } catch (e) { alert('failed: ' + e.message); return; }
  closeFaceNameForm();
  showCurrentLightbox();
};
$('face-delete').onclick = async () => {
  if (!pendingFace) return;
  if (!confirm('Delete this face permanently? (use for false positives or non-faces)')) return;
  try {
    await api(`/api/faces/${pendingFace.id}`, { method: 'DELETE' });
  } catch (e) { alert('failed: ' + e.message); return; }
  closeFaceNameForm();
  showCurrentLightbox();
};
$('face-name-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') saveFaceName();
  else if (e.key === 'Escape') closeFaceNameForm();
});

async function deleteAllFacesOnImage(imageId) {
  if (!confirm('Drop every detected face from this photo? Useful for crowd shots where individual recognition is unhelpful.')) return;
  try {
    await api(`/api/images/${imageId}/faces`, { method: 'DELETE' });
  } catch (e) {
    alert('failed: ' + e.message); return;
  }
  showCurrentLightbox();
}

async function redetectFaces(imageId) {
  // Loads the InsightFace model on first call (~5 s), runs detection, replaces faces.
  $('lightbox-info').innerHTML = '<div>re-detecting faces…</div>';
  try {
    await api(`/api/images/${imageId}/redetect-faces`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
  } catch (e) {
    alert('failed: ' + e.message); return;
  }
  showCurrentLightbox();
}

async function viewPerson(clusterId) {
  // Pull all photos containing that person; replace the current view with them.
  const data = await api(`/api/people/${clusterId}?limit=500`);
  const ids = [];
  const seen = new Set();
  for (const m of data.members) {
    if (!seen.has(m.image_id)) { seen.add(m.image_id); ids.push(m.image_id); }
  }
  if (!ids.length) return;
  state.viewIds = ids;
  state.viewIndex = 0;
  showCurrentLightbox();
}

function renderActiveFilters() {
  const root = $('active-tags');
  if (state.activeTags.size === 0 && state.activePersons.size === 0) {
    root.innerHTML = '<span style="color:var(--muted);">none</span>';
    return;
  }
  root.innerHTML = '';
  state.activePersons.forEach(p => {
    const el = html(`<span class="tag active person">👤 ${escape(p)} <span class="x">×</span></span>`);
    el.querySelector('.x').onclick = (e) => { e.stopPropagation(); togglePerson(p); };
    root.appendChild(el);
  });
  state.activeTags.forEach(t => {
    const el = html(`<span class="tag active">${escape(t)} <span class="x">×</span></span>`);
    el.querySelector('.x').onclick = (e) => { e.stopPropagation(); toggleTag(t); };
    root.appendChild(el);
  });
}

async function toggleTag(name) {
  if (state.activeTags.has(name)) state.activeTags.delete(name);
  else state.activeTags.add(name);
  renderActiveFilters();
  if (state.activeTags.size + state.activePersons.size > 0) await runSearch();
  else if (state.selectedCluster) await showCluster(state.selectedCluster);
}

async function togglePerson(name) {
  if (state.activePersons.has(name)) state.activePersons.delete(name);
  else state.activePersons.add(name);
  renderActiveFilters();
  if (state.activeTags.size + state.activePersons.size > 0) await runSearch();
  else if (state.selectedCluster) await showCluster(state.selectedCluster);
}

async function runSearch() {
  const ws = $('workspace');
  ws.innerHTML = '<div class="empty">searching…</div>';
  const tags = Array.from(state.activeTags);
  const persons = Array.from(state.activePersons);
  const minScore = parseFloat($('min-score').value || '0');
  const params = new URLSearchParams();
  tags.forEach(t => params.append('tag', t));
  persons.forEach(p => params.append('person', p));
  params.set('min_score', String(minScore));
  params.set('limit', '200');
  if (state.runId != null) params.set('run_id', String(state.runId));
  const results = await api(`/api/search?${params.toString()}`);
  ws.innerHTML = '';
  const titleParts = [
    ...persons.map(p => `👤 ${escape(p)}`),
    ...tags.map(escape),
  ];
  ws.appendChild(html(`<h2>${titleParts.join(' + ')}</h2>`));
  ws.appendChild(html(`<div class="auto">${results.length} matches${tags.length ? ` (score ≥ ${minScore.toFixed(2)})` : ''}</div>`));
  if (!results.length) {
    ws.appendChild(html('<div class="empty">no matches</div>'));
    return;
  }
  // Group by cluster
  const groups = new Map();
  results.forEach(r => {
    const key = r.cluster_id ?? 'none';
    if (!groups.has(key)) groups.set(key, { cluster: r, items: [] });
    groups.get(key).items.push(r);
  });
  const flatIds = [];
  for (const { cluster, items } of groups.values()) {
    const lbl = cluster.label_user || cluster.label_auto || `cluster #${cluster.cluster_no ?? '?'}`;
    ws.appendChild(html(`<details open><summary><b>${escape(lbl)}</b> · ${items.length}</summary></details>`));
    const det = ws.lastElementChild;
    const grid = html('<div class="grid" style="margin-top:8px;"></div>');
    items.forEach(r => { grid.appendChild(makeTile(r.id, r.path, r.score)); flatIds.push(r.id); });
    det.appendChild(grid);
  }
  state.viewIds = flatIds;
}

$('run-select').addEventListener('change', async (e) => {
  state.runId = Number(e.target.value);
  state.selectedCluster = null;
  await loadClusters();
  await loadTopTags();
});
$('min-score').addEventListener('change', () => { if (state.activeTags.size > 0) runSearch(); });
$('clear-filters').addEventListener('click', () => {
  state.activeTags.clear();
  state.activePersons.clear();
  renderActiveFilters();
  $('workspace').innerHTML = '<div class="empty">filters cleared</div>';
});

// Custom dropdown for tag search: shows on focus, filters as you type, click to add.
const tagDropState = { items: [], active: -1 };
let tagDebounce = null;

async function refreshTagDropdown() {
  const q = $('tag-input').value.trim();
  const tagUrl = q.length
    ? `/api/tags?prefix=${encodeURIComponent(q)}&limit=15`
    : `/api/tags?limit=15`;
  const personUrl = q.length
    ? `/api/people/names?prefix=${encodeURIComponent(q)}&limit=8`
    : `/api/people/names?limit=8`;
  const [tags, people] = await Promise.all([
    api(tagUrl).catch(() => []),
    api(personUrl).catch(() => []),
  ]);
  // Combine: people first when q is set (more specific), tags first otherwise.
  const items = [
    ...people.map(p => ({ kind: 'person', name: p.name, count: p.count, n_clusters: p.n_clusters })),
    ...tags.map(t => ({ kind: 'tag', name: t.name, count: t.count })),
  ];
  tagDropState.items = items;
  tagDropState.active = items.length ? 0 : -1;
  renderTagDropdown();
}

function renderTagDropdown() {
  const drop = $('tag-drop');
  if (!tagDropState.items.length) {
    drop.innerHTML = '<div class="empty">no matches</div>';
    drop.classList.add('show');
    return;
  }
  drop.innerHTML = tagDropState.items.map((t, i) => {
    const cls = ['row'];
    if (t.kind === 'person') cls.push('person');
    if (i === tagDropState.active) cls.push('active');
    const inFilter = t.kind === 'person'
      ? state.activePersons.has(t.name)
      : state.activeTags.has(t.name);
    if (inFilter) cls.push('in-filter');
    const icon = t.kind === 'person' ? '👤' : '#';
    const clusterBadge = (t.kind === 'person' && t.n_clusters > 1)
      ? ` <span class="count" style="background:#fde6c4;color:#92400e;padding:1px 5px;border-radius:6px;">×${t.n_clusters}</span>`
      : '';
    return `<div class="${cls.join(' ')}" data-kind="${t.kind}" data-name="${escape(t.name)}">
      <span><span class="kind">${icon}</span>${escape(t.name)}${clusterBadge}</span><span class="count">${t.count}</span>
    </div>`;
  }).join('');
  drop.classList.add('show');
  drop.querySelectorAll('.row').forEach((row, i) => {
    row.onmousedown = (e) => {
      e.preventDefault();
      const it = tagDropState.items[i];
      if (it.kind === 'person') pickPerson(it.name);
      else pickTag(it.name);
    };
    row.onmouseenter = () => { tagDropState.active = i; updateActiveRow(); };
  });
}

function updateActiveRow() {
  document.querySelectorAll('#tag-drop .row').forEach((r, i) => {
    r.classList.toggle('active', i === tagDropState.active);
  });
}

function pickTag(name) {
  toggleTag(name);
  $('tag-input').value = '';
  refreshTagDropdown();  // keep dropdown open showing latest set with new in-filter highlight
}

async function pickPerson(name) {
  await togglePerson(name);
  $('tag-input').value = '';
  refreshTagDropdown();
}

function hideTagDropdown() {
  $('tag-drop').classList.remove('show');
}

$('tag-input').addEventListener('focus', () => refreshTagDropdown());
$('tag-input').addEventListener('blur', () => setTimeout(hideTagDropdown, 120));
$('tag-input').addEventListener('input', () => {
  clearTimeout(tagDebounce);
  tagDebounce = setTimeout(refreshTagDropdown, 120);
});
$('tag-input').addEventListener('keydown', (e) => {
  if (!$('tag-drop').classList.contains('show')) return;
  const max = tagDropState.items.length;
  if (e.key === 'ArrowDown') { e.preventDefault(); tagDropState.active = (tagDropState.active + 1) % max; updateActiveRow(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); tagDropState.active = (tagDropState.active - 1 + max) % max; updateActiveRow(); }
  else if (e.key === 'Enter') {
    e.preventDefault();
    if (tagDropState.active >= 0) {
      const it = tagDropState.items[tagDropState.active];
      if (it.kind === 'person') pickPerson(it.name); else pickTag(it.name);
    } else if (e.target.value.trim()) pickTag(e.target.value.trim());
  } else if (e.key === 'Escape') {
    e.target.value = '';
    hideTagDropdown();
    e.target.blur();
  }
});
document.addEventListener('keydown', (e) => {
  const open = $('lightbox').classList.contains('show');
  if (!open) return;
  // Don't intercept while typing in the face-name form.
  if (document.activeElement && document.activeElement.tagName === 'INPUT') return;
  if (e.key === 'Escape') closeLightbox();
  else if (e.key === 'f' || e.key === 'F') { e.preventDefault(); toggleFaceOverlays(); }
  else if (e.key === 'ArrowLeft' || e.key === 'PageUp' || e.key === 'k') { e.preventDefault(); navLightbox(-1); }
  else if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === 'j' || e.key === ' ') { e.preventDefault(); navLightbox(1); }
});

// ---- URL hash sync (so refresh / share preserves view) ----------------------

function parseHash() {
  const raw = (location.hash || '').replace(/^#/, '');
  const p = new URLSearchParams(raw);
  return {
    view: p.get('view'),
    run: p.has('run') ? Number(p.get('run')) : null,
    cluster: p.has('cluster') ? Number(p.get('cluster')) : null,
    face: p.has('face') ? Number(p.get('face')) : null,
    tags: p.getAll('tag'),
    persons: p.getAll('person'),
    minScore: p.has('score') ? parseFloat(p.get('score')) : null,
    image: p.has('image') ? Number(p.get('image')) : null,
  };
}

let hashWriteSuspended = false;

function writeHash() {
  if (hashWriteSuspended) return;
  const p = new URLSearchParams();
  if (state.runId != null) p.set('run', String(state.runId));
  if (state.view && state.view !== 'clusters') p.set('view', state.view);
  // Filters (tags + persons) take precedence over a selected cluster.
  if (state.activeTags.size > 0 || state.activePersons.size > 0) {
    state.activePersons.forEach(n => p.append('person', n));
    state.activeTags.forEach(t => p.append('tag', t));
    const ms = parseFloat($('min-score').value || '0');
    if (ms > 0 && state.activeTags.size > 0) p.set('score', ms.toFixed(2));
  } else if (state.view === 'faces' && state.selectedFaceCluster != null) {
    p.set('face', String(state.selectedFaceCluster));
  } else if (state.selectedCluster != null) {
    p.set('cluster', String(state.selectedCluster));
  }
  if (state.lightboxOpen && state.viewIds.length) {
    p.set('image', String(state.viewIds[state.viewIndex]));
  }
  const next = '#' + p.toString();
  if (location.hash !== next) history.replaceState(null, '', next || location.pathname);
}

async function applyHashState(h) {
  hashWriteSuspended = true;
  try {
    if (h.minScore != null && !Number.isNaN(h.minScore)) $('min-score').value = String(h.minScore);
    if (h.view === 'faces') {
      state.view = 'faces';
      document.querySelectorAll('.view-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.view === 'faces');
      });
      await showFacesPanel();
    }
    const hasFilter = (h.tags && h.tags.length) || (h.persons && h.persons.length);
    if (hasFilter) {
      state.activeTags = new Set(h.tags || []);
      state.activePersons = new Set(h.persons || []);
      renderActiveFilters();
      await runSearch();
    } else if (h.face != null) {
      state.view = 'faces';
      document.querySelectorAll('.view-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.view === 'faces');
      });
      // Ensure people sidebar is rendered before selecting.
      if ($('cluster-pane-title').textContent !== 'people') await showFacesPanel();
      await showPersonInWorkspace(h.face);
    } else if (h.cluster != null) {
      const exists = state.clusters.some(c => c.id === h.cluster);
      if (exists) selectCluster(h.cluster);
    }
    // Re-open the lightbox if the hash had an image= entry. Suspended-write
    // means openLightbox doesn't try to write a hash mid-restore.
    if (h.image != null && state.viewIds.includes(h.image)) {
      state.viewIndex = state.viewIds.indexOf(h.image);
      state.lightboxOpen = true;
      $('lightbox').classList.add('show');
      showCurrentLightbox();
    }
  } finally {
    hashWriteSuspended = false;
  }
}

window.addEventListener('hashchange', async () => {
  const h = parseHash();
  // Switch run if the hash points elsewhere.
  if (h.run != null && h.run !== state.runId && state.runs.some(r => r.id === h.run)) {
    state.runId = h.run;
    $('run-select').value = h.run;
    await loadClusters();
    await loadTopTags();
  }
  // Reset filters/cluster then re-apply.
  state.activeTags.clear();
  state.activePersons.clear();
  state.selectedCluster = null;
  document.querySelectorAll('.cluster-row').forEach(r => r.classList.remove('selected'));
  renderActiveFilters();
  await applyHashState(h);
});

// Wrap state-mutating functions so the hash always reflects the visible state.
const _origSelectCluster = selectCluster;
selectCluster = (id) => { _origSelectCluster(id); writeHash(); };
const _origToggleTag = toggleTag;
toggleTag = async (name) => { await _origToggleTag(name); writeHash(); };
$('clear-filters').addEventListener('click', writeHash);
$('min-score').addEventListener('change', writeHash);
$('run-select').addEventListener('change', writeHash);

// ---- view switching: clusters vs faces ----------------------------------

state.view = 'clusters';

function switchView(view) {
  state.view = view;
  document.querySelectorAll('.view-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.view === view);
  });
  if (view === 'clusters') {
    $('cluster-pane-title').textContent = 'clusters';
    $('cluster-list').innerHTML = '';
    if (state.clusters.length) renderClusters();
    else loadClusters();
  } else if (view === 'faces') {
    showFacesPanel();
  }
  writeHash();
}

function renderClusters() {
  const root = $('cluster-list');
  root.innerHTML = '';
  state.clusters.forEach(c => {
    const isNoise = c.cluster_no === -1;
    const userLabel = c.label_user ? `<span class="user-label">${escape(c.label_user)}</span>` : '';
    const autoLabel = c.label_auto ? `<span class="auto-label">${escape(c.label_auto)}</span>` : '';
    const row = html(`<div class="cluster-row ${isNoise ? 'noise' : ''}" data-id="${c.id}">
      <div class="label">${userLabel}${userLabel && autoLabel ? '<br>' : ''}${autoLabel}</div>
      <div class="size">${c.size}</div>
    </div>`);
    row.onclick = () => selectCluster(c.id);
    root.appendChild(row);
  });
}

async function showFacesPanel() {
  // Sidebar list: face clusters (people) if any; otherwise just an empty message.
  $('cluster-pane-title').textContent = 'people';
  const root = $('cluster-list');
  root.innerHTML = '<div class="empty" style="padding:12px;">loading…</div>';
  let people = [];
  try { people = await api('/api/people'); } catch (e) { people = []; }
  root.innerHTML = '';
  if (!people.length) {
    root.appendChild(html(`<div class="empty" style="padding:12px;font-size:12px;">
      no face clusters yet. run <code>phototag faces cluster</code> after detection.
    </div>`));
  } else {
    people.forEach(p => {
      const lbl = p.name
        ? `<span class="user-label">${escape(p.name)}</span>`
        : `<span class="auto-label">${escape(p.auto || `person ${p.cluster_no}`)}</span>`;
      const row = html(`<div class="cluster-row" data-id="${p.cluster_id}">
        <div class="label">${lbl}</div>
        <div class="size">${p.size}</div>
      </div>`);
      row.onclick = () => showPersonInWorkspace(p.cluster_id);
      root.appendChild(row);
    });
  }
  // Workspace grid: all photos with at least one detected face.
  await showFacesGrid();
}

async function showFacesGrid() {
  const ws = $('workspace');
  ws.innerHTML = '<div class="empty">loading…</div>';
  const items = await api('/api/faces/images?limit=500');
  ws.innerHTML = '';
  ws.appendChild(html(`<h2>Photos with detected faces</h2>`));
  ws.appendChild(html(`<div class="auto">${items.length} photos · sorted by face count</div>`));
  if (!items.length) {
    ws.appendChild(html('<div class="empty">no faces detected yet</div>'));
    state.viewIds = [];
    return;
  }
  const grid = html('<div class="grid"></div>');
  items.forEach(it => {
    const tile = makeTile(it.id, it.path, null);
    const meta = tile.querySelector('.meta');
    meta.innerHTML = `<b>${it.face_count}</b> · ${meta.textContent}`;
    grid.appendChild(tile);
  });
  ws.appendChild(grid);
  state.viewIds = items.map(it => it.id);
}

async function showPersonInWorkspace(clusterId) {
  state.selectedFaceCluster = clusterId;
  writeHash();
  const ws = $('workspace');
  ws.innerHTML = '<div class="empty">loading…</div>';
  const data = await api(`/api/people/${clusterId}?limit=500`);
  const seen = new Set(); const ids = [];
  for (const m of data.members) if (!seen.has(m.image_id)) { seen.add(m.image_id); ids.push(m.image_id); }
  // Find sibling clusters sharing this label_user (if any).
  let siblings = [];
  if (data.label_user) {
    siblings = await api(`/api/people/by-name/${encodeURIComponent(data.label_user)}/clusters`)
      .catch(() => []);
  }
  ws.innerHTML = '';
  const titleText = data.label_user || data.label_auto || `person ${data.cluster_no}`;
  const titleSuffix = siblings.length > 1 ? ` <span style="font-size:12px;color:var(--muted);">(${siblings.length} clusters share this name)</span>` : '';
  ws.appendChild(html(`<h2>${escape(titleText)} <button id="person-edit-toggle" class="pen-btn" title="edit name">✏️</button>${titleSuffix}</h2>`));

  // Inline rename for *this cluster only* — hidden until pen is clicked.
  const renameDiv = html(`<div class="rename" id="person-rename" style="margin:6px 0 8px; display:none;">
    <input id="person-rename-input" type="text" placeholder="rename this cluster…" value="${escape(data.label_user || '')}">
    <button id="person-rename-save">save</button>
    <button id="person-rename-clear" title="unname this cluster">clear</button>
  </div>`);
  ws.appendChild(renameDiv);
  $('person-edit-toggle').onclick = () => {
    const r = $('person-rename');
    r.style.display = r.style.display === 'none' ? 'flex' : 'none';
    if (r.style.display === 'flex') $('person-rename-input').focus();
  };
  $('person-rename-save').onclick = () => savePersonRename(clusterId, $('person-rename-input').value.trim());
  $('person-rename-clear').onclick = () => savePersonRename(clusterId, '');
  $('person-rename-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); savePersonRename(clusterId, e.target.value.trim()); }
  });

  // Group operations: only meaningful when 2+ clusters share the name.
  if (siblings.length > 1 && data.label_user) {
    const grpDiv = html(`<div class="rename" style="margin-bottom:12px; padding:6px 8px; background:#fff7ed; border-radius:4px;">
      <span style="font-size:12px;">all <b>${siblings.length}</b> clusters of "${escape(data.label_user)}":</span>
      <input id="group-rename-input" type="text" placeholder="rename all to…" style="flex:1;">
      <button id="group-rename-save">rename all</button>
      <button id="group-split">split into ${siblings.length}</button>
    </div>`);
    ws.appendChild(grpDiv);
    $('group-rename-save').onclick = () => groupRename(data.label_user, $('group-rename-input').value.trim());
    $('group-split').onclick = () => groupSplit(data.label_user);
    $('group-rename-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); groupRename(data.label_user, e.target.value.trim()); }
    });
  }

  ws.appendChild(html(`<div class="auto">${ids.length} photos · ${data.size} faces in cluster ${data.cluster_no}</div>`));
  const grid = html('<div class="grid"></div>');
  ids.forEach(id => grid.appendChild(makeTile(id, `image-${id}`, null)));
  ws.appendChild(grid);
  state.viewIds = ids;
}

async function savePersonRename(clusterId, name) {
  try {
    await api(`/api/people/${clusterId}/name`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name: name || null }),
    });
  } catch (e) {
    alert('rename failed: ' + e.message);
    return;
  }
  await showFacesPanel();
  await showPersonInWorkspace(clusterId);
}

async function groupRename(oldName, newName) {
  if (!newName) { alert('new name required'); return; }
  try {
    await api(`/api/people/by-name/${encodeURIComponent(oldName)}/rename`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name: newName }),
    });
  } catch (e) {
    alert('rename failed: ' + e.message);
    return;
  }
  await showFacesPanel();
}

async function groupSplit(name) {
  if (!confirm(`Split clusters of "${name}" into "${name} 1", "${name} 2", …?`)) return;
  try {
    await api(`/api/people/by-name/${encodeURIComponent(name)}/split`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
  } catch (e) {
    alert('split failed: ' + e.message);
    return;
  }
  await showFacesPanel();
}

document.querySelectorAll('.view-btn').forEach(b => {
  b.onclick = () => switchView(b.dataset.view);
});

renderActiveFilters();
loadRuns();
