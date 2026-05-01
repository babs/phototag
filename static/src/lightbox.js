// Lightbox + face popover + overlay rendering + per-photo bulk-face actions.
// Most of the face-management UX lives here because every entry point
// (sidebar tile click, search result, triage queue, …) eventually opens a
// photo in the lightbox.

import { state } from './state.js';
import { $, html, escape, api, assetUrl } from './api.js';

// ---- tile factory --------------------------------------------------------
// Lives here because every tile click opens the lightbox; importing it from
// workspace.js would create a workspace→lightbox→workspace import dance.
export function makeTile(image_id, path, score) {
  const name = path.split('/').pop();
  const scoreLabel = score != null ? `<b>${score.toFixed(2)}</b> · ` : '';
  const tile = html(`<div class="tile" data-id="${image_id}">
    <img loading="lazy" src="${assetUrl('thumb', image_id)}" alt="">
    <div class="meta">${scoreLabel}${escape(name)}</div>
  </div>`);
  tile.onclick = () => openLightbox(image_id);
  return tile;
}

// ---- lightbox open/close/nav --------------------------------------------

export function openLightbox(image_id) {
  // Seed viewIds from the current grid if needed (e.g. user clicked outside any tracked view).
  if (!state.viewIds.length) state.viewIds = [image_id];
  let idx = state.viewIds.indexOf(image_id);
  if (idx < 0) { state.viewIds = [image_id, ...state.viewIds]; idx = 0; }
  state.viewIndex = idx;
  state.lightboxOpen = true;
  $('lightbox').classList.add('show');
  showCurrentLightbox();
  writeHashRef();
}

let lightboxToken = 0;

export async function showCurrentLightbox() {
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
    chip.addEventListener('click', () => { toggleTagRef(t.name); closeLightbox(); });
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

export function navLightbox(delta) {
  if (!state.viewIds.length) return;
  const n = state.viewIds.length;
  state.viewIndex = ((state.viewIndex + delta) % n + n) % n;
  showCurrentLightbox();
  writeHashRef();
}

export function closeLightbox() {
  state.lightboxOpen = false;
  $('lightbox').classList.remove('show');
  closeFaceNameForm();
  writeHashRef();
}

// ---- face overlays in the lightbox ---------------------------------------

export function renderFaceOverlays() {
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
    //   ?  named but never user-confirmed (model proposed a name, you didn't agree yet)
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
      suffix = ` · ${f.attach_sim.toFixed(2)}`;
    }
    lbl.textContent = prefix + (f.label || 'name…') + suffix;
    box.appendChild(lbl);
    box.onclick = (e) => { e.stopPropagation(); onFaceClicked(f, e.clientX, e.clientY); };
    layer.appendChild(box);
  }
}

window.addEventListener('resize', renderFaceOverlays);

export function toggleFaceOverlays() {
  state.facesVisible = !state.facesVisible;
  const btn = document.getElementById('info-toggle-faces');
  if (btn) btn.style.opacity = state.facesVisible ? '1' : '0.5';
  renderFaceOverlays();
}

export function toggleTagsCloud() {
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

export function closeFaceNameForm() {
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

// ---- popover form button wiring -----------------------------------------
// Called once on DOM ready by main.js so we attach to elements that exist.
export function wireLightboxFormHandlers(viewPersonRef) {
  $('face-name-save').onclick = saveFaceName;
  $('face-name-cancel').onclick = closeFaceNameForm;
  $('face-view-person').onclick = () => {
    if (pendingFace?.cluster_id != null) {
      viewPersonRef(pendingFace.cluster_id, pendingFace.named ? pendingFace.label : null);
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
}

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

// ---- late-bound cross-module references ---------------------------------
// Avoids a hard import cycle with sidebar.js (writeHash + toggleTag).
// main.js calls bindLightboxRefs() once everything else is loaded.
let writeHashRef = () => {};
let toggleTagRef = () => {};

export function bindLightboxRefs(refs) {
  if (refs.writeHash) writeHashRef = refs.writeHash;
  if (refs.toggleTag) toggleTagRef = refs.toggleTag;
}

// Test-only popover-state introspection used by keyboard.js if needed.
export function isFaceFormOpen() {
  return $('face-name-form')?.style.display !== 'none';
}
