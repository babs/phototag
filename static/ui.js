const state = {
  runs: [],
  runId: null,
  clusters: [],
  selectedCluster: null,
  selectedFaceCluster: null,
  selectedPersonName: null,
  activeTags: new Set(),
  activePersons: new Set(),
  lightboxOpen: false,
  facesVisible: true,
  tagsVisible: false,
  // current set of images in the workspace, used by the lightbox for navigation.
  viewIds: [],
  viewIndex: 0,
};

// Cache-bust thumb/preview URLs so a server-side regeneration (e.g. after the
// EXIF-orientation fix) actually shows up; pinned per page-load so a normal
// refresh evicts stale browser-cached images.
const ASSET_VERSION = String(Date.now());
// Optional shared-secret token (APP_API_TOKEN env). When non-empty, every
// fetch carries an X-API-Token header and every <img> URL gets a ?token=
// query string (browsers can't set headers on native asset loads).
const API_TOKEN = (typeof window !== 'undefined' && window.PHOTOTAG_API_TOKEN) || '';
const tokenQuery = (sep) => API_TOKEN ? `${sep}token=${encodeURIComponent(API_TOKEN)}` : '';
const assetUrl = (kind, id) => `/${kind}/${id}?v=${ASSET_VERSION}${tokenQuery('&')}`;

const $ = (id) => document.getElementById(id);
const html = (s) => { const d = document.createElement('div'); d.innerHTML = s.trim(); return d.firstElementChild; };
const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, opts) {
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
  // Build tag chips as DOM nodes — never as templated `onclick` attributes.
  // Inline-onclick interpolation HTML-decodes attribute values before JS
  // execution, so `&#39;` round-trips back to `'` and breaks out of any
  // string a tag/label can land inside (XSS surface).
  const tagsBlock = document.createElement('div');
  tagsBlock.className = 'tags';
  tagsBlock.id = 'info-tags';
  tagsBlock.style.marginTop = '6px';
  tagsBlock.style.display = state.tagsVisible ? 'flex' : 'none';
  for (const t of info.tags) {
    const chip = document.createElement('span');
    chip.className = 'tag';
    chip.textContent = `${t.name} `;
    const cnt = document.createElement('span');
    cnt.className = 'count';
    cnt.textContent = t.score.toFixed(2);
    chip.appendChild(cnt);
    chip.addEventListener('click', () => { toggleTag(t.name); closeLightbox(); });
    tagsBlock.appendChild(chip);
  }

  const infoBar = document.createElement('div');
  const pathSpan = document.createElement('span');
  pathSpan.textContent = info.path + ' · ';
  infoBar.appendChild(pathSpan);

  const rawLink = document.createElement('a');
  rawLink.href = assetUrl('raw', id);
  rawLink.target = '_blank';
  rawLink.style.color = '#9cf';
  rawLink.textContent = 'open original';
  infoBar.appendChild(rawLink);

  const sep = () => infoBar.appendChild(document.createTextNode(' · '));

  if (info.tags && info.tags.length > 0) {
    sep();
    const a = document.createElement('a');
    a.href = '#';
    a.id = 'info-toggle-tags';
    a.style.color = '#9cf';
    a.style.opacity = state.tagsVisible ? '1' : '0.5';
    a.textContent = `${info.tags.length} ${info.tags.length === 1 ? '(T)ag' : '(T)ags'}`;
    a.addEventListener('click', (e) => { e.preventDefault(); toggleTagsCloud(); });
    infoBar.appendChild(a);
  }
  if (faces && faces.length > 0) {
    sep();
    const a = document.createElement('a');
    a.href = '#';
    a.id = 'info-toggle-faces';
    a.style.color = '#9cf';
    a.style.opacity = state.facesVisible ? '1' : '0.5';
    a.textContent = `${faces.length} ${faces.length === 1 ? '(F)ace' : '(F)aces'}`;
    a.addEventListener('click', (e) => { e.preventDefault(); toggleFaceOverlays(); });
    infoBar.appendChild(a);

    // Hint the user when a name shows up multiple times on this image.
    // Validated faces don't count — once one occurrence is confirmed, the
    // others are still suspicious but the validated one is ground truth.
    const namedCounts = new Map();
    for (const f of faces) {
      if (f.named && !f.user_verified && f.label) {
        namedCounts.set(f.label, (namedCounts.get(f.label) || 0) + 1);
      }
    }
    const dups = [...namedCounts.entries()].filter(([, n]) => n > 1);
    if (dups.length) {
      sep();
      const warn = document.createElement('span');
      warn.style.color = '#f59e0b';
      warn.title = 'Same person clustered onto multiple faces of the same photo — almost always a false positive (mirror/montage edge cases excepted).';
      warn.textContent = `⚠ ${dups.map(([n, c]) => `${n}×${c}`).join(', ')}`;
      infoBar.appendChild(warn);
    }

    sep();
    const drop = document.createElement('a');
    drop.href = '#';
    drop.style.color = '#fca5a5';
    drop.textContent = `drop ${faces.length} face${faces.length === 1 ? '' : 's'}`;
    drop.addEventListener('click', (e) => { e.preventDefault(); deleteAllFacesOnImage(id); });
    infoBar.appendChild(drop);

    // Bulk-validate every named-but-not-yet-validated face on this image.
    const namedUnvalidated = faces.filter(f => f.named && !f.user_verified).length;
    if (namedUnvalidated > 0) {
      sep();
      const valAll = document.createElement('a');
      valAll.href = '#';
      valAll.style.color = '#86efac';
      valAll.textContent = `validate ${namedUnvalidated} named`;
      valAll.title = 'Trust all current name assignments on this photo and mark them verified';
      valAll.addEventListener('click', async (e) => {
        e.preventDefault();
        try {
          await api(`/api/images/${id}/faces/validate-named`, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
          });
        } catch (err) { alert('failed: ' + err.message); return; }
        showCurrentLightbox();
      });
      // Hover preview: highlight the boxes that *would* be validated.
      valAll.addEventListener('mouseenter', () => {
        document.querySelectorAll('#face-layer .face-box').forEach((box, i) => {
          const f = state.lightboxFaces[i];
          if (f && f.named && !f.user_verified) box.classList.add('validate-preview');
        });
      });
      valAll.addEventListener('mouseleave', () => {
        document.querySelectorAll('#face-layer .face-box.validate-preview')
          .forEach(box => box.classList.remove('validate-preview'));
      });
      infoBar.appendChild(valAll);
    }

    // Drop only the unidentified faces (no name + still showing as auto "person N").
    const unident = faces.filter(f => !f.named).length;
    if (unident > 0 && unident < faces.length) {
      sep();
      const dropU = document.createElement('a');
      dropU.href = '#';
      dropU.style.color = '#fca5a5';
      dropU.textContent = `drop ${unident} unidentified`;
      dropU.addEventListener('click', (e) => { e.preventDefault(); deleteUnidentifiedFacesOnImage(id); });
      // Hover preview: paint the boxes that *would* be dropped in red, and
      // hide the named/verified faces so only the at-risk ones are visible.
      const previewOn = () => {
        document.querySelectorAll('#face-layer .face-box').forEach((box, i) => {
          const f = state.lightboxFaces[i];
          if (!f) return;
          if (!f.named) box.classList.add('drop-preview');
          else box.classList.add('drop-preview-hide');
        });
      };
      const previewOff = () => {
        document.querySelectorAll('#face-layer .face-box').forEach(box => {
          box.classList.remove('drop-preview', 'drop-preview-hide');
        });
      };
      dropU.addEventListener('mouseenter', previewOn);
      dropU.addEventListener('mouseleave', previewOff);
      infoBar.appendChild(dropU);
    }
  }
  sep();
  const redet = document.createElement('a');
  redet.href = '#';
  redet.style.color = '#9cf';
  redet.textContent = 're-detect faces';
  redet.addEventListener('click', (e) => { e.preventDefault(); redetectFaces(id); });
  infoBar.appendChild(redet);

  const exifBlock = formatExifNode(info.exif);
  const root = $('lightbox-info');
  root.innerHTML = '';
  root.appendChild(infoBar);
  if (exifBlock) root.appendChild(exifBlock);
  root.appendChild(tagsBlock);
  state.lightboxFaces = faces;
  state.lightboxImage = info;
  // Render now if the image is already decoded; otherwise wait for it. Use
  // .decode() which resolves regardless of whether `load` fires (cached images
  // sometimes skip the event). We also retry once on the next animation frame
  // because clientWidth can be stale right after a swap.
  scheduleFaceOverlayRender(myToken);
}

// Render face boxes through every async trigger we can: ResizeObserver
// (covers layout-after-load), img.decode() (cached images), and a RAF
// fallback (consecutive same-size images where the observer never fires).
// All paths read state.lightboxFaces / state.lightboxImage so they paint
// the current image, not a stale one. The observer is attached ONCE and
// reads `lightboxToken` (the live module var) directly, never a closed-over
// stale one — that was the bug where overlays disappeared after a few nav
// steps even though both images had detected faces.
let _faceObserver = null;

function _renderIfReady() {
  const img = $('lightbox-img');
  if (img.naturalWidth > 0 && img.clientWidth > 0) renderFaceOverlays();
}

function scheduleFaceOverlayRender(token) {
  const img = $('lightbox-img');
  if (!_faceObserver) {
    _faceObserver = new ResizeObserver(_renderIfReady);
    _faceObserver.observe(img);
  }
  const tryRender = () => {
    if (token !== lightboxToken) return;  // navigation moved on
    _renderIfReady();
  };
  // Direct attempt for the fast path (image already decoded and laid out).
  tryRender();
  // Cached/freshly-set images: decode resolves even when `load` won't fire.
  if (img.decode) {
    img.decode().then(tryRender, tryRender);
  } else {
    img.addEventListener('load', tryRender, { once: true });
    img.addEventListener('error', tryRender, { once: true });
  }
  // Same-size consecutive images: ResizeObserver never fires; this RAF
  // triggers a render once layout settles for the new src.
  requestAnimationFrame(tryRender);
}

function formatExifNode(exif) {
  // Returns a DOM node (or null). Building a node avoids HTML interpolation
  // for EXIF strings that ride directly from camera firmware.
  if (!exif) return null;
  const wrap = document.createElement('div');
  wrap.style.marginTop = '4px';
  wrap.style.opacity = '0.8';
  const parts = [];
  if (exif.datetime_original) parts.push(document.createTextNode(exif.datetime_original.replace('T', ' ')));
  const camera = [exif.make, exif.model].filter(Boolean).join(' ');
  if (camera) parts.push(document.createTextNode(camera));
  if (exif.lens) parts.push(document.createTextNode(exif.lens));
  const expo = [];
  if (exif.f_number) expo.push(`f/${Number(exif.f_number).toFixed(1)}`);
  if (exif.exposure_time) {
    const t = Number(exif.exposure_time);
    expo.push(t >= 1 ? `${t.toFixed(1)}s` : `1/${Math.round(1/t)}s`);
  }
  if (exif.iso) expo.push(`ISO ${exif.iso}`);
  if (exif.focal_length) expo.push(`${Number(exif.focal_length).toFixed(0)}mm`);
  if (expo.length) parts.push(document.createTextNode(expo.join(' · ')));
  if (exif.gps) {
    const { lat, lon } = exif.gps;
    const a = document.createElement('a');
    a.href = `https://www.openstreetmap.org/?mlat=${encodeURIComponent(lat)}&mlon=${encodeURIComponent(lon)}&zoom=15`;
    a.target = '_blank';
    a.style.color = '#9cf';
    a.textContent = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
    parts.push(a);
  }
  if (!parts.length) return null;
  parts.forEach((p, i) => {
    if (i > 0) wrap.appendChild(document.createTextNode(' · '));
    wrap.appendChild(p);
  });
  return wrap;
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
  // A named person appearing twice on the same photo is almost always a
  // clustering false positive — flag those faces so the user can split or
  // unassign quickly. Validated faces are excluded from the duplicate count
  // (the user has confirmed them, they are the ground truth) and never carry
  // the dup warning themselves.
  const nameCount = new Map();
  for (const f of state.lightboxFaces) {
    if (f.named && f.label && !f.user_verified) {
      nameCount.set(f.label, (nameCount.get(f.label) || 0) + 1);
    }
  }
  for (const f of state.lightboxFaces) {
    if (!f.bbox || f.bbox.length !== 4) continue;
    const [x, y, w, h] = f.bbox;
    const box = document.createElement('div');
    const suspect = f.verified === 0 || (f.det_score != null && f.det_score < 0.65);
    const userVerified = !!f.user_verified;
    // dup warning never fires on a validated face: the user has signed off
    // on it. Other un-validated faces sharing the same name still flash ⚠.
    const dupName = !userVerified && f.named && f.label && nameCount.get(f.label) > 1;
    box.className = 'face-box'
      + (f.named ? '' : ' unnamed')
      + (suspect ? ' suspect' : '')
      + (dupName ? ' dup' : '');
    box.style.setProperty('--c', f.color);
    box.style.left = (x * sx) + 'px';
    box.style.top = (y * sy) + 'px';
    box.style.width = (w * sx) + 'px';
    box.style.height = (h * sy) + 'px';
    // Marker logic for the title and label prefix:
    //   ⚠  duplicate name on this photo → almost certainly false positive
    //   ✓  user-verified (drawn via CSS ::before, kept out of textContent here)
    //   ?  named but never user-confirmed (model said "Alex", you didn't agree yet)
    box.title = dupName
      ? `${f.label} appears ${nameCount.get(f.label)}× on this photo — likely false positive`
      : userVerified
        ? `${f.label} (verified)`
        : f.named
          ? `${f.label} — not yet verified, click to confirm or correct`
          : 'unnamed face';
    const lbl = document.createElement('div');
    lbl.className = 'face-label';
    let prefix = '';
    if (dupName) prefix = '⚠ ';
    else if (f.named && !userVerified) prefix = '? ';
    // Show the auto-attach confidence (rounded) next to the label when the
    // face came from an identity match — helps spot marginal attachments
    // (sim 0.5–0.7) that the user should sanity-check.
    let suffix = '';
    if (f.named && typeof f.attach_sim === 'number') {
      suffix = ` · ${f.attach_sim.toFixed(2)}`;
    }
    lbl.textContent = prefix + (f.label || 'name…') + suffix;
    box.appendChild(lbl);
    box.onclick = (e) => { e.stopPropagation(); onFaceClicked(f, e.clientX, e.clientY); };
    layer.appendChild(box);
  }
}

window.addEventListener('resize', renderFaceOverlays);

function toggleFaceOverlays() {
  state.facesVisible = !state.facesVisible;
  const btn = document.getElementById('info-toggle-faces');
  if (btn) btn.style.opacity = state.facesVisible ? '1' : '0.5';
  renderFaceOverlays();
}

function toggleTagsCloud() {
  state.tagsVisible = !state.tagsVisible;
  const tagsDiv = document.getElementById('info-tags');
  if (tagsDiv) tagsDiv.style.display = state.tagsVisible ? 'flex' : 'none';
  const btn = document.getElementById('info-toggle-tags');
  if (btn) btn.style.opacity = state.tagsVisible ? '1' : '0.5';
}

async function onFaceClicked(face, clickX, clickY) {
  // Always open the menu — gives access to wrong/delete even on named faces.
  openFaceNameForm(face, clickX, clickY);
}

let pendingFace = null;

function openFaceNameForm(face, x, y) {
  pendingFace = face;
  const form = $('face-name-form');
  form.style.left = Math.min(window.innerWidth - 540, x) + 'px';
  form.style.top = Math.min(window.innerHeight - 60, y) + 'px';
  form.style.display = 'flex';
  $('face-name-input').value = face.label || '';
  $('face-name-input').placeholder = face.named ? 'rename this cluster…' : 'name this person…';
  // Toggle context-sensitive buttons.
  $('face-view-person').style.display = (face.named && face.cluster_id != null) ? 'inline-block' : 'none';
  if (face.named)
    $('face-view-person').innerHTML = `go to 👤 ${escape(face.label)} <span class="key">G</span>`;
  $('face-wrong').style.display = (face.cluster_id != null) ? 'inline-block' : 'none';
  // Verify only meaningful for named faces (you're confirming a name is correct).
  // Build innerHTML (not textContent) so the V hotkey hint span stays.
  const verifyBtn = $('face-verify');
  if (face.named) {
    verifyBtn.style.display = 'inline-block';
    const label = face.user_verified ? '✓ validated' : 'validate';
    verifyBtn.innerHTML = `${label} <span class="key">V</span>`;
    verifyBtn.disabled = !!face.user_verified;
  } else {
    verifyBtn.style.display = 'none';
  }
  // Drop-dups only when there is more than one face on this image with the
  // same label (counts come from state.lightboxFaces). We keep this face.
  const dropBtn = $('face-drop-dups');
  if (face.named && face.label) {
    const sameName = (state.lightboxFaces || []).filter(f => f.named && f.label === face.label);
    if (sameName.length > 1) {
      dropBtn.style.display = 'inline-block';
      dropBtn.innerHTML = `drop ${sameName.length - 1} dup of ${escape(face.label)} <span class="key">X</span>`;
    } else {
      dropBtn.style.display = 'none';
    }
  } else {
    dropBtn.style.display = 'none';
  }
  $('face-name-input').focus();
  $('face-name-input').select();
  // Top-K identity suggestions: only meaningful for unnamed faces. The user
  // already validated named faces; suggestions there would be noise.
  const suggBox = $('face-suggestions');
  suggBox.innerHTML = '';
  suggBox.style.display = 'none';
  if (!face.named) {
    const reqFaceId = face.id;
    api(`/api/faces/${face.id}/suggest?k=3`).then(items => {
      // Bail if the popover moved on to another face while we were waiting.
      if (!pendingFace || pendingFace.id !== reqFaceId) return;
      if (!Array.isArray(items) || items.length === 0) return;
      // Backend returns the raw top-K (no threshold) so the caller can decide.
      // Drop chips below the attach threshold so we never *suggest* a match
      // the system itself wouldn't auto-attach. 0.5 mirrors the default
      // `attach_face_to_best_identity` threshold.
      const SUGGEST_MIN_SIM = 0.5;
      const usable = items.filter(it => Number(it.sim) >= SUGGEST_MIN_SIM);
      if (!usable.length) return;
      // When the top-2 are very close, the highest one is a coin-flip too —
      // collapse to a single chip so the user sees "ambiguous, pick one"
      // instead of two chips of equal weight. Mirrors the backend min_margin.
      const SUGGEST_MIN_MARGIN = 0.05;
      const top = usable[0];
      const ambiguous =
        usable.length >= 2 && (Number(top.sim) - Number(usable[1].sim)) < SUGGEST_MIN_MARGIN;
      const chips = ambiguous ? usable : usable;  // keep all for transparency
      for (const it of chips) {
        const chip = document.createElement('span');
        chip.className = 'chip' + (ambiguous && it !== top ? ' alt' : '');
        chip.title = ambiguous
          ? `attach to ${it.name} (${it.n_samples} samples) — ambiguous: top-2 within ${SUGGEST_MIN_MARGIN}`
          : `attach to ${it.name} (${it.n_samples} samples)`;
        const nm = document.createElement('span');
        nm.textContent = it.name;
        const sim = document.createElement('span');
        sim.className = 'sim';
        sim.textContent = '· ' + Number(it.sim).toFixed(2);
        chip.appendChild(nm);
        chip.appendChild(sim);
        chip.addEventListener('click', () => attachFaceToSuggestion(it.name));
        suggBox.appendChild(chip);
      }
      suggBox.style.display = 'flex';
    }).catch(() => { /* non-fatal: suggestions are best-effort */ });
  }
}

async function attachFaceToSuggestion(name) {
  if (!pendingFace) return;
  // Reuse the manual-name path: same detach-from-noise + auto-validate logic.
  try {
    await api(`/api/faces/${pendingFace.id}/name`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name }),
    });
  } catch (e) { alert('attach failed: ' + e.message); return; }
  closeFaceNameForm();
  showCurrentLightbox();
}

function closeFaceNameForm() {
  $('face-name-form').style.display = 'none';
  const sugg = document.getElementById('face-suggestions');
  if (sugg) { sugg.innerHTML = ''; sugg.style.display = 'none'; }
  pendingFace = null;
}

// Compute screen coords for a face's bbox using the current lightbox image
// scale — same math `renderFaceOverlays` uses, hoisted so the V auto-advance
// can re-open the popover on the next face without going through a click.
function faceScreenAnchor(face) {
  const img = $('lightbox-img');
  if (!img || !img.naturalWidth || !face.bbox || face.bbox.length !== 4) {
    return { x: window.innerWidth / 2, y: window.innerHeight / 2 };
  }
  const [x, y, w, h] = face.bbox;
  const sx = img.clientWidth / img.naturalWidth;
  const sy = img.clientHeight / img.naturalHeight;
  const r = img.getBoundingClientRect();
  // Anchor at the face center so the popover lands somewhere reasonable.
  return { x: r.left + (x + w / 2) * sx, y: r.top + (y + h / 2) * sy };
}

async function saveFaceName() {
  if (!pendingFace) return;
  const name = $('face-name-input').value.trim();
  if (!name) { closeFaceNameForm(); return; }
  // Three paths:
  //  1. face is in a real (non-noise) cluster → rename that cluster.
  //  2. face is in the noise cluster → noise must NEVER carry a label
  //     (it groups unrelated faces); fall back to manual-name path which
  //     creates a per-name cluster in the manual run.
  //  3. face is unclustered → manual-name path.
  const inRealCluster = pendingFace.cluster_id != null && pendingFace.cluster_no !== -1;
  const url = inRealCluster
    ? `/api/people/${pendingFace.cluster_id}/name`
    : `/api/faces/${pendingFace.id}/name`;
  const faceId = pendingFace.id;
  try {
    await api(url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name }),
    });
    // Naming a face is an explicit user assertion → also mark it validated
    // so the "?" prefix disappears and dup-drop spares this face. The
    // manual-name backend already does this server-side; the cluster-rename
    // path doesn't, so chain a verify call here for parity.
    if (inRealCluster) {
      try {
        await api(`/api/faces/${faceId}/verify`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
        });
      } catch (e) { /* non-fatal: rename already succeeded */ }
    }
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
  if (pendingFace?.cluster_id != null) {
    viewPerson(pendingFace.cluster_id, pendingFace.named ? pendingFace.label : null);
  }
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
$('face-verify').onclick = async () => {
  if (!pendingFace) return;
  const verifiedId = pendingFace.id;
  try {
    await api(`/api/faces/${pendingFace.id}/verify`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
  } catch (e) { alert('verify failed: ' + e.message); return; }
  // Validate-and-advance: find the next named-but-not-yet-verified face on
  // this image (skipping the one we just verified) so V can be hammered as a
  // bulk "yes, yes, yes" through every auto-attached candidate. Falls back
  // to plain close+refresh when the current photo has nothing left.
  const nextFace = (state.lightboxFaces || []).find(
    f => f.id !== verifiedId && f.named && !f.user_verified
  );
  closeFaceNameForm();
  await showCurrentLightbox();
  if (nextFace) {
    // Re-fetch the post-refresh face object so user_verified/attach_sim are
    // current; bbox is stable so the screen anchor math is the same.
    const fresh = (state.lightboxFaces || []).find(f => f.id === nextFace.id) || nextFace;
    const { x, y } = faceScreenAnchor(fresh);
    openFaceNameForm(fresh, x, y);
  }
};
$('face-drop-dups').onclick = async () => {
  if (!pendingFace || !pendingFace.label) return;
  const id = state.viewIds[state.viewIndex];
  const sameName = (state.lightboxFaces || []).filter(f => f.named && f.label === pendingFace.label);
  if (sameName.length <= 1) { closeFaceNameForm(); return; }
  if (!confirm(`Drop ${sameName.length - 1} other face${sameName.length - 1 === 1 ? '' : 's'} labelled "${pendingFace.label}" on this photo? Keeps the one you have selected.`)) return;
  try {
    await api(`/api/images/${id}/faces/dups-of/${encodeURIComponent(pendingFace.label)}?keep_face_id=${pendingFace.id}`, { method: 'DELETE' });
  } catch (e) { alert('drop dups failed: ' + e.message); return; }
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

async function deleteUnidentifiedFacesOnImage(imageId) {
  if (!confirm('Drop unidentified faces (still showing as "person N" or no name) from this photo? Named faces stay.')) return;
  try {
    await api(`/api/images/${imageId}/faces/unidentified`, { method: 'DELETE' });
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

async function viewPerson(clusterId, name) {
  // Pull all photos containing that person; replace the current view with them.
  // If `name` is supplied, walk every cluster sharing the same label_user so
  // the lightbox includes age-band siblings too.
  let ids = [];
  const seen = new Set();
  if (name) {
    const data = await api(`/api/people/by-name/${encodeURIComponent(name)}?limit=500`);
    for (const grp of data.groups) {
      for (const m of grp.members) {
        if (!seen.has(m.image_id)) { seen.add(m.image_id); ids.push(m.image_id); }
      }
    }
  } else {
    const data = await api(`/api/people/${clusterId}?limit=500`);
    for (const m of data.members) {
      if (!seen.has(m.image_id)) { seen.add(m.image_id); ids.push(m.image_id); }
    }
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
function toggleHelp() {
  const o = $('help-overlay');
  o.style.display = o.style.display === 'none' ? 'flex' : 'none';
}
window.toggleHelp = toggleHelp;

// Global ? to open help, Esc to close it. Don't fire when typing into an input.
document.addEventListener('keydown', (e) => {
  const t = e.target;
  const tag = t && t.tagName;
  const typing = tag === 'INPUT' || tag === 'TEXTAREA' || (t && t.isContentEditable);
  const helpOpen = $('help-overlay').style.display !== 'none';
  if (helpOpen && e.key === 'Escape') { e.preventDefault(); toggleHelp(); return; }
  if (!typing && (e.key === '?' || (e.shiftKey && e.key === '/'))) {
    e.preventDefault(); toggleHelp(); return;
  }
});

document.addEventListener('keydown', (e) => {
  const lightboxOpen = $('lightbox').classList.contains('show');
  const formOpen = $('face-name-form').style.display !== 'none';
  if (!lightboxOpen && !formOpen) return;

  // Typing in the rename input → only Esc/Enter, handled by the input listener.
  if (formOpen && document.activeElement === $('face-name-input')) return;

  if (e.key === 'Escape') {
    if (formOpen) { e.preventDefault(); closeFaceNameForm(); }
    else closeLightbox();
    return;
  }

  // Form-specific shortcuts (only when the action's button is visible).
  if (formOpen) {
    const click = (id) => {
      const btn = $(id);
      if (btn && btn.style.display !== 'none' && !btn.disabled) {
        e.preventDefault();
        btn.click();
      }
    };
    if (e.key === 'w' || e.key === 'W') { click('face-wrong'); return; }
    if (e.key === 'd' || e.key === 'D') { click('face-delete'); return; }
    if (e.key === 'g' || e.key === 'G') { click('face-view-person'); return; }
    if (e.key === 'v' || e.key === 'V') { click('face-verify'); return; }
    if (e.key === 'x' || e.key === 'X') { click('face-drop-dups'); return; }
    return;  // suppress lightbox shortcuts while form is open
  }

  // Lightbox shortcuts (form not open).
  if (e.key === 'f' || e.key === 'F') { e.preventDefault(); toggleFaceOverlays(); }
  else if (e.key === 't' || e.key === 'T') { e.preventDefault(); toggleTagsCloud(); }
  else if (e.key === 'ArrowLeft' || e.key === 'PageUp' || e.key === 'k') { e.preventDefault(); navLightbox(-1); }
  else if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === 'j' || e.key === ' ') { e.preventDefault(); navLightbox(1); }
  // N — jump to the next photo (after the current viewIndex) that has at
  // least one unidentified face. Wraps to the start. Empty → no-op.
  else if (e.key === 'n' || e.key === 'N') { e.preventDefault(); jumpToNextUnidentified(); }
});

async function jumpToNextUnidentified() {
  if (!state.viewIds.length) return;
  // Pull the photos with unidentified faces and intersect with current view.
  let candidates = [];
  try {
    const data = await api('/api/faces/unidentified/images?limit=2000');
    candidates = data.map(it => it.id);
  } catch (e) { return; }
  if (!candidates.length) return;
  const candSet = new Set(candidates);
  const viewIds = state.viewIds;
  const start = state.viewIndex;
  for (let off = 1; off <= viewIds.length; off++) {
    const i = (start + off) % viewIds.length;
    if (candSet.has(viewIds[i])) {
      state.viewIndex = i;
      showCurrentLightbox();
      writeHash();
      return;
    }
  }
  // None in current view — load the first orphan photo into the view.
  if (candidates.length) {
    state.viewIds = candidates;
    state.viewIndex = 0;
    showCurrentLightbox();
    writeHash();
  }
}

// ---- URL hash sync (so refresh / share preserves view) ----------------------

function parseHash() {
  const raw = (location.hash || '').replace(/^#/, '');
  const p = new URLSearchParams(raw);
  return {
    view: p.get('view'),
    run: p.has('run') ? Number(p.get('run')) : null,
    cluster: p.has('cluster') ? Number(p.get('cluster')) : null,
    face: p.has('face') ? Number(p.get('face')) : null,
    who: p.get('who'),
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
  } else if (state.view === 'faces' && state.selectedPersonName != null) {
    p.set('who', state.selectedPersonName);
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
    } else if (h.who) {
      state.view = 'faces';
      document.querySelectorAll('.view-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.view === 'faces');
      });
      if ($('cluster-pane-title').textContent !== 'people') await showFacesPanel();
      await showPersonByName(h.who);
    } else if (h.face != null) {
      state.view = 'faces';
      document.querySelectorAll('.view-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.view === 'faces');
      });
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
  // Sidebar list: deduplicated by label_user — one row per name even if the
  // person spans several underlying face_clusters. Unnamed clusters keep their
  // own per-cluster rows (each is its own "person N").
  $('cluster-pane-title').textContent = 'people';
  const root = $('cluster-list');
  root.innerHTML = '<div class="empty" style="padding:12px;">loading…</div>';
  let named = [];
  let unnamed = [];
  try {
    [named, unnamed] = await Promise.all([
      api('/api/people/names?limit=500'),
      api('/api/people?only_unnamed=true').catch(() => []),
    ]);
  } catch (e) { /* faces feature absent */ }
  root.innerHTML = '';
  // Pinned workflow entries (orphan + triage). Resolved up-front so we can
  // surface the triage count even on a fresh DB with no clusters yet.
  let unidentCount = 0;
  try {
    const sm = await api('/api/faces/unidentified/summary');
    unidentCount = sm.unidentified || 0;
  } catch (e) { /* endpoint optional */ }
  let triageCount = 0;
  try {
    const tr = await api('/api/faces/triage?limit=2000');
    triageCount = tr.length;
  } catch (e) { /* endpoint optional */ }
  const addPinnedRow = (label, count, onclick) => {
    const row = html(`<div class="cluster-row">
      <div class="label"><span class="auto-label" style="font-style:italic;">${escape(label)}</span></div>
      <div class="size">${count}</div>
    </div>`);
    row.addEventListener('click', onclick);
    root.appendChild(row);
  };
  if (!named.length && !unnamed.length) {
    // Even without any clusters yet, freshly-detected faces sit as orphans —
    // expose the qualify entry so the user can name them one by one.
    if (unidentCount > 0) {
      addPinnedRow('noise / orphan (qualify)', unidentCount, () => showUnidentifiedInWorkspace());
    }
    if (triageCount > 0) {
      addPinnedRow('triage queue', triageCount, () => showTriageInWorkspace());
    }
    root.appendChild(html(`<div class="empty" style="padding:12px;font-size:12px;">
      no face clusters yet. run <code>phototag faces cluster</code> after detection.
    </div>`));
  } else {
    // Pin the noise/orphan + triage entries to the TOP of the sidebar — these
    // are the actions the user comes here to perform. Always rendered so the
    // counts stay visible even at zero (confirms "nothing left to do").
    addPinnedRow('noise / orphan (qualify)', unidentCount, () => showUnidentifiedInWorkspace());
    addPinnedRow('triage queue', triageCount, () => showTriageInWorkspace());

    named.forEach(p => {
      const cBadge = p.n_clusters > 1 ? ` <span class="count" style="opacity:0.7;">×${p.n_clusters}</span>` : '';
      const row = html(`<div class="cluster-row" data-name="${escape(p.name)}">
        <div class="label"><span class="user-label">${escape(p.name)}</span>${cBadge}</div>
        <div class="size">${p.count}</div>
      </div>`);
      row.onclick = () => showPersonByName(p.name);
      root.appendChild(row);
    });
    unnamed.forEach(p => {
      const row = html(`<div class="cluster-row" data-id="${p.cluster_id}" style="padding-left:18px;">
        <div class="label"><span class="auto-label">${escape(p.auto || `person ${p.cluster_no}`)}</span></div>
        <div class="size">${p.size}</div>
      </div>`);
      row.onclick = () => showPersonInWorkspace(p.cluster_id);
      root.appendChild(row);
    });
  }
  // Workspace grid: all photos with at least one detected face.
  await showFacesGrid();
}

async function showUnidentifiedInWorkspace() {
  const ws = $('workspace');
  ws.innerHTML = '<div class="empty">loading…</div>';
  let summary = { unidentified: 0 };
  try { summary = await api('/api/faces/unidentified/summary'); }
  catch (e) { /* endpoint absent */ }
  // Photos containing at least one unidentified face. The face_count column
  // here is the unidentified count, not the total face count on the photo.
  const items = await api('/api/faces/unidentified/images?limit=2000');
  ws.innerHTML = '';
  ws.appendChild(html(`<h2>Noise / orphan faces — qualify</h2>`));
  ws.appendChild(html(`<div class="auto">${summary.unidentified} faces in the noise cluster or with no cluster at all. Open a photo, click a face and name it (or mark it as not-a-face).</div>`));
  const actions = html('<div style="margin:8px 0 14px; display:flex; gap:8px; flex-wrap:wrap;"></div>');

  // Recluster (dry-run preview) — try to recover names from already-known
  // identities by re-running UMAP+HDBSCAN on just the orphan pool.
  const reclusterBtn = document.createElement('button');
  reclusterBtn.textContent = 'preview re-cluster (dry-run)';
  reclusterBtn.style.background = '#2c5fa3';
  reclusterBtn.style.color = '#fff';
  reclusterBtn.style.padding = '6px 14px';
  reclusterBtn.style.border = '0';
  reclusterBtn.style.borderRadius = '4px';
  reclusterBtn.style.cursor = 'pointer';
  reclusterBtn.addEventListener('click', () => previewOrphanRecluster(true));
  actions.appendChild(reclusterBtn);

  const dropAll = document.createElement('button');
  dropAll.textContent = `drop all ${summary.unidentified} unidentified`;
  dropAll.style.background = '#dc2626';
  dropAll.style.color = '#fff';
  dropAll.style.padding = '6px 14px';
  dropAll.style.border = '0';
  dropAll.style.borderRadius = '4px';
  dropAll.style.cursor = 'pointer';
  dropAll.disabled = !summary.unidentified;
  dropAll.addEventListener('click', async () => {
    if (!confirm(`Drop ALL ${summary.unidentified} unidentified faces across the entire library? Named faces stay.`)) return;
    try { await api('/api/faces/unidentified?yes=true', { method: 'DELETE' }); }
    catch (e) { alert('failed: ' + e.message); return; }
    showFacesPanel();
  });
  actions.appendChild(dropAll);
  ws.appendChild(actions);

  // Pre-existing slot for the re-cluster preview output (filled async on click).
  ws.appendChild(html('<div id="orphan-recluster-out"></div>'));
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

async function showTriageInWorkspace() {
  const ws = $('workspace');
  ws.innerHTML = '<div class="empty">loading…</div>';
  // Photos needing attention: ≥1 unverified named face, or a duplicate-name
  // ⚠ on the overlay. Sorted server-side by score (dups weighted ×2).
  const items = await api('/api/faces/triage?limit=2000');
  ws.innerHTML = '';
  ws.appendChild(html(`<h2>Triage queue</h2>`));
  ws.appendChild(html(`<div class="auto">${items.length} photos · validate or fix the ⚠ duplicates · open one then walk with ←/→</div>`));
  if (!items.length) {
    ws.appendChild(html('<div class="empty">nothing to triage — every named face is verified and no duplicate names are left.</div>'));
    state.viewIds = [];
    return;
  }
  const grid = html('<div class="grid"></div>');
  items.forEach(it => {
    const tile = makeTile(it.id, it.path, null);
    const meta = tile.querySelector('.meta');
    // Compact badges: amber ⚠ for unverified count, red ⚠⚠ for duplicate-name
    // groups (likely false positives, hence the heavier weight in the score).
    const badges = [];
    if (it.n_unverified > 0) {
      badges.push(`<span style="color:#f59e0b;" title="${it.n_unverified} unverified named face${it.n_unverified === 1 ? '' : 's'}">⚠ ${it.n_unverified}</span>`);
    }
    if (it.n_dups > 0) {
      badges.push(`<span style="color:#dc2626;" title="${it.n_dups} duplicate-name group${it.n_dups === 1 ? '' : 's'}">⚠⚠ ${it.n_dups}</span>`);
    }
    const hover = `score ${it.score} · ${it.n_unverified} unverified · ${it.n_dups} dups`;
    meta.innerHTML = `${badges.join(' ')} · ${meta.textContent}`;
    meta.title = hover;
    grid.appendChild(tile);
  });
  ws.appendChild(grid);
  state.viewIds = items.map(it => it.id);
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

async function previewOrphanRecluster(dryRun) {
  const out = document.getElementById('orphan-recluster-out');
  if (!out) return;
  out.innerHTML = '<div class="empty">running…</div>';
  let result;
  try {
    result = await api(`/api/faces/recluster-orphan?dry_run=${dryRun}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
  } catch (e) {
    out.innerHTML = '';
    out.appendChild(html(`<div class="empty">failed: ${escape(e.message)}</div>`));
    return;
  }
  out.innerHTML = '';
  if (result.error) {
    out.appendChild(html(`<div class="empty">${escape(result.error)}</div>`));
    return;
  }
  const verb = dryRun ? 'would create' : 'created';
  const header = html(`<h3 style="margin:18px 0 4px; font-size:14px;">
    re-cluster of ${result.n_orphan} orphan faces — ${verb} ${result.n_clusters} cluster(s)
    <span style="color:var(--muted);font-weight:400;">
      · ${result.n_noise} still noise · ${result.named_via_identity} matched a known identity
    </span>
  </h3>`);
  out.appendChild(header);

  if (!result.clusters.length) {
    out.appendChild(html('<div class="empty">no clusters formed at these settings</div>'));
  } else {
    const table = html('<table style="border-collapse:collapse; font-size:12px; width:100%; max-width:720px;"></table>');
    table.appendChild(html('<thead><tr><th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border);">cluster</th><th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border);">size</th><th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border);">matched identity</th></tr></thead>'));
    const tbody = html('<tbody></tbody>');
    for (const c of result.clusters) {
      const tr = html(`<tr>
        <td style="padding:4px 8px;border-bottom:1px solid #f0f0f0;">#${c.cluster_no}</td>
        <td style="padding:4px 8px;border-bottom:1px solid #f0f0f0;">${c.size}</td>
        <td style="padding:4px 8px;border-bottom:1px solid #f0f0f0;">${c.label_user ? `<b>${escape(c.label_user)}</b>` : '<span style="color:var(--muted);">(new)</span>'}</td>
      </tr>`);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    out.appendChild(table);
  }

  if (dryRun && result.n_clusters > 0) {
    const persistBtn = document.createElement('button');
    persistBtn.textContent = 'persist this re-cluster (writes a new face_run)';
    persistBtn.style.background = '#16a34a';
    persistBtn.style.color = '#fff';
    persistBtn.style.padding = '6px 14px';
    persistBtn.style.border = '0';
    persistBtn.style.borderRadius = '4px';
    persistBtn.style.cursor = 'pointer';
    persistBtn.style.marginTop = '12px';
    persistBtn.addEventListener('click', async () => {
      if (!confirm('Persist this orphan re-cluster as a new face_run? Named clusters stay untouched; orphan faces get the new cluster assignments.')) return;
      await previewOrphanRecluster(false);
      // Refresh sidebar so the count drops.
      showFacesPanel();
    });
    out.appendChild(persistBtn);
  }
}

async function showPersonInWorkspace(clusterId) {
  state.selectedFaceCluster = clusterId;
  state.selectedPersonName = null;
  writeHash();
  const ws = $('workspace');
  ws.innerHTML = '<div class="empty">loading…</div>';
  const data = await api(`/api/people/${clusterId}?limit=500`);
  const seen = new Set(); const ids = [];
  for (const m of data.members) if (!seen.has(m.image_id)) { seen.add(m.image_id); ids.push(m.image_id); }
  ws.innerHTML = '';
  const titleText = data.label_user || data.label_auto || `person ${data.cluster_no}`;
  ws.appendChild(html(`<h2>${escape(titleText)} <button id="person-edit-toggle" class="pen-btn" title="edit name">✏️</button></h2>`));

  // Edit-only block: rename this cluster (hidden until pen is clicked).
  const editDiv = html(`<div id="person-edit" style="display:none; margin:6px 0 12px; padding:8px; background:#fff7ed; border-radius:4px;">
    <div class="rename">
      <input id="person-rename-input" type="text" placeholder="rename this cluster…" value="${escape(data.label_user || '')}">
      <button id="person-rename-save">save</button>
      <button id="person-rename-clear" title="unname this cluster">clear</button>
    </div>
  </div>`);
  ws.appendChild(editDiv);
  $('person-edit-toggle').onclick = () => {
    const r = $('person-edit');
    r.style.display = r.style.display === 'none' ? 'block' : 'none';
    if (r.style.display === 'block') $('person-rename-input').focus();
  };
  $('person-rename-save').onclick = () => savePersonRename(clusterId, $('person-rename-input').value.trim());
  $('person-rename-clear').onclick = () => savePersonRename(clusterId, '');
  $('person-rename-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); savePersonRename(clusterId, e.target.value.trim()); }
  });

  ws.appendChild(html(`<div class="auto">${ids.length} photos · ${data.size} faces in cluster ${data.cluster_no}</div>`));
  const grid = html('<div class="grid"></div>');
  ids.forEach(id => grid.appendChild(makeTile(id, `image-${id}`, null)));
  ws.appendChild(grid);
  state.viewIds = ids;
}

async function showPersonByName(name) {
  state.selectedPersonName = name;
  state.selectedFaceCluster = null;
  writeHash();
  const ws = $('workspace');
  ws.innerHTML = '<div class="empty">loading…</div>';
  const data = await api(`/api/people/by-name/${encodeURIComponent(name)}?limit=500`);
  ws.innerHTML = '';
  const cBadge = data.n_clusters > 1
    ? ` <span style="font-size:12px;color:var(--muted);">(${data.n_clusters} clusters)</span>`
    : '';
  ws.appendChild(html(`<h2>👤 ${escape(name)} <button id="person-edit-toggle" class="pen-btn" title="edit">✏️</button>${cBadge}</h2>`));
  ws.appendChild(html(`<div class="auto">${data.n_photos} photos${data.n_clusters > 1 ? ` across ${data.n_clusters} clusters` : ''}</div>`));

  // Edit block (hidden by default): rename-all + split + merge + clear.
  const editBlock = html(`<div id="person-edit" style="display:none; margin:8px 0 12px; padding:8px; background:#fff7ed; border-radius:4px;">
    <div class="rename" style="margin-bottom:6px;">
      <input id="group-rename-input" type="text" placeholder="rename all clusters of '${escape(name)}' to…" style="flex:1;">
      <button id="group-rename-save">rename all</button>
      <button id="group-clear" title="unname every cluster of this person">clear</button>
    </div>
    <div class="rename" style="margin-bottom:6px;">
      <input id="group-merge-input" type="text" list="group-merge-names" placeholder="merge '${escape(name)}' into…" style="flex:1;">
      <datalist id="group-merge-names"></datalist>
      <button id="group-merge-save" title="blend centroids, re-label clusters, drop the duplicate identity">merge</button>
    </div>
    ${data.n_clusters > 1 ? `<div style="margin-top:4px;">
      <button id="group-split">split into ${data.n_clusters} (${escape(name)} 1, ${escape(name)} 2…)</button>
    </div>` : ''}
  </div>`);
  ws.appendChild(editBlock);
  $('person-edit-toggle').onclick = () => {
    const r = $('person-edit');
    r.style.display = r.style.display === 'none' ? 'block' : 'none';
    if (r.style.display === 'block') {
      $('group-rename-input').focus();
      // Lazy-populate the merge datalist (excludes the current name).
      populateMergeDatalist(name).catch(() => {});
    }
  };
  $('group-rename-save').onclick = () => groupRename(name, $('group-rename-input').value.trim());
  $('group-clear').onclick = () => {
    if (confirm(`Clear name "${name}" from all its clusters?`)) groupRename(name, '');
  };
  $('group-rename-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); groupRename(name, e.target.value.trim()); }
  });
  $('group-merge-save').onclick = () => groupMerge(name, $('group-merge-input').value.trim());
  $('group-merge-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); groupMerge(name, e.target.value.trim()); }
  });
  if ($('group-split')) $('group-split').onclick = () => groupSplit(name);

  // Workspace grid: photos grouped by underlying cluster, one section per cluster.
  const allIds = [];
  for (const grp of data.groups) {
    const seen = new Set();
    const ids = [];
    for (const m of grp.members) {
      if (!seen.has(m.image_id)) { seen.add(m.image_id); ids.push(m.image_id); allIds.push(m.image_id); }
    }
    const header = html(`<h3 style="margin:18px 0 4px; font-size:14px;">
      cluster #${grp.cluster_no} <span style="color:var(--muted);font-weight:400;">· ${ids.length} photos · ${grp.size} faces</span>
    </h3>`);
    ws.appendChild(header);
    const grid = html('<div class="grid"></div>');
    ids.forEach(id => grid.appendChild(makeTile(id, `image-${id}`, null)));
    ws.appendChild(grid);
  }
  state.viewIds = allIds;
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

async function populateMergeDatalist(currentName) {
  // Datalist powers the merge-into autocomplete; filter out the current name
  // so the user can't accidentally pick a self-merge (server rejects it too).
  const dl = document.getElementById('group-merge-names');
  if (!dl) return;
  dl.innerHTML = '';
  const rows = await api('/api/people/names?limit=500');
  for (const p of rows) {
    if (!p.name || p.name === currentName) continue;
    const opt = document.createElement('option');
    opt.value = p.name;
    dl.appendChild(opt);
  }
}

async function groupMerge(loser, survivor) {
  if (!survivor) { alert('survivor name required'); return; }
  if (survivor === loser) { alert('survivor and loser must differ'); return; }
  if (!confirm(`Merge "${loser}" into "${survivor}"? Centroids are blended and the "${loser}" identity is dropped.`)) return;
  let result;
  try {
    result = await api('/api/face-identities/merge', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ survivor, loser }),
    });
  } catch (e) {
    alert('merge failed: ' + e.message);
    return;
  }
  alert(`merged: re-labelled ${result.renamed_clusters} cluster(s) → ${survivor}`);
  await showFacesPanel();
  await showPersonByName(survivor);
}

document.querySelectorAll('.view-btn').forEach(b => {
  b.onclick = () => switchView(b.dataset.view);
});

// Collapsible section headers (top-tags). Persists open/closed in localStorage
// so a refresh keeps your last view choice.
document.querySelectorAll('aside .section h3.collapsible').forEach(h => {
  const targetId = h.dataset.target;
  const body = document.getElementById(targetId);
  if (!body) return;
  const key = `phototag.section.${targetId}.open`;
  const setState = (open) => {
    h.classList.toggle('open', open);
    body.style.display = open ? '' : 'none';
    try { localStorage.setItem(key, open ? '1' : '0'); } catch (e) { /* private mode */ }
  };
  let initial = false;
  try { initial = localStorage.getItem(key) === '1'; } catch (e) { /* private mode */ }
  setState(initial);
  h.addEventListener('click', () => setState(!h.classList.contains('open')));
});

// Quick filter for the cluster/people list. Pure client-side.
//   - whitespace-delimited tokens act as AND filters (all must match)
//   - matches are bolded in-place via <b> wrappers in the row label
//   - case-insensitive substring; original markup preserved across passes
//     by snapshotting innerHTML on first sight (data-orig-html)
(function setupClusterFilter() {
  const input = document.getElementById('cluster-filter');
  if (!input) return;

  const snapshot = (row) => {
    if (row.dataset.origHtml === undefined) row.dataset.origHtml = row.innerHTML;
  };

  const restore = (row) => {
    if (row.dataset.origHtml !== undefined) row.innerHTML = row.dataset.origHtml;
  };

  // Wrap every occurrence of any token (case-insensitive) inside text nodes
  // beneath `root`, using <b>. Skips nodes already inside <b> to avoid nesting.
  const highlight = (root, tokens) => {
    if (!tokens.length) return;
    // Build a single regex with alternation; escape regex special chars.
    const escaped = tokens.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    const re = new RegExp(`(${escaped.join('|')})`, 'gi');
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => n.parentNode && n.parentNode.nodeName === 'B'
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT,
    });
    const targets = [];
    let n;
    while ((n = walker.nextNode())) targets.push(n);
    for (const node of targets) {
      const text = node.nodeValue;
      if (!re.test(text)) { re.lastIndex = 0; continue; }
      re.lastIndex = 0;
      const frag = document.createDocumentFragment();
      let last = 0;
      let m;
      while ((m = re.exec(text))) {
        if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        const b = document.createElement('b');
        b.textContent = m[0];
        frag.appendChild(b);
        last = m.index + m[0].length;
      }
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    }
  };

  const clearBtn = document.getElementById('cluster-filter-clear');

  const apply = () => {
    const tokens = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    if (clearBtn) clearBtn.style.display = input.value ? '' : 'none';
    document.querySelectorAll('#cluster-list .cluster-row').forEach(row => {
      snapshot(row);
      restore(row);  // wipe previous bold spans before re-decorating
      if (!tokens.length) { row.classList.remove('hidden'); return; }
      const text = (row.textContent || '').toLowerCase();
      const allMatch = tokens.every(t => text.includes(t));
      row.classList.toggle('hidden', !allMatch);
      if (allMatch) highlight(row, tokens);
    });
  };

  input.addEventListener('input', apply);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { input.value = ''; apply(); input.blur(); }
  });
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      input.value = '';
      apply();
      input.focus();
    });
  }
  // Re-apply after async list reloads (loadClusters / showFacesPanel rebuild
  // the DOM, wiping the previous hidden state). The list is rebuilt with
  // many sequential appendChild calls — coalesce them into one apply per
  // animation frame so the cost is O(n) not O(n²) per panel render.
  const list = document.getElementById('cluster-list');
  if (list) {
    let pending = false;
    const schedule = () => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => { pending = false; apply(); });
    };
    new MutationObserver(schedule).observe(list, { childList: true });
  }
})();

renderActiveFilters();
loadRuns();
