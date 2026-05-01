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

  // #27 — per-image manual category override chip. Click opens a tiny
  // popover with a datalist autocomplete sourced from /api/categories.
  // Submit sets the override; "clear" drops it. The chip text reflects
  // the *current* override state, falling back to "category…" when none.
  sep();
  const catChip = document.createElement('a');
  catChip.href = '#';
  catChip.id = 'info-image-category';
  catChip.style.color = '#fcd34d';
  const updateCatChip = (val) => {
    catChip.textContent = val ? `📁 ${val}` : '📁 category…';
    catChip.title = val
      ? `manual category override (#27) — click to change or clear`
      : 'no manual override; rules apply (click to set one)';
  };
  updateCatChip(info.manual_category);
  catChip.addEventListener('click', (e) => { e.preventDefault(); openImageCategoryForm(id, info.manual_category, updateCatChip); });
  infoBar.appendChild(catChip);

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
  // Exit any in-progress bbox edit BEFORE swapping the image — otherwise
  // the editBox keeps its old coords against the new photo, the user
  // would submit a bbox in the wrong coord space, and the endpoint would
  // 422 with "no face found" (or, worse, accidentally rewrite the wrong
  // face if the coords happen to align).
  if (bboxEditState) exitBboxRedrawMode(true);
  const n = state.viewIds.length;
  state.viewIndex = ((state.viewIndex + delta) % n + n) % n;
  showCurrentLightbox();
  writeHashRef();
}

export function closeLightbox() {
  // Same reasoning as navLightbox: tear edit mode down so its toolbar +
  // global keydown listener don't leak past the lightbox close.
  if (bboxEditState) exitBboxRedrawMode(true);
  state.lightboxOpen = false;
  $('lightbox').classList.remove('show');
  closeFaceNameForm();
  writeHashRef();
}

// ---- face overlays in the lightbox ---------------------------------------

export function renderFaceOverlays() {
  const img = $('lightbox-img');
  const layer = $('face-layer');
  // Bbox edit mode owns the face layer until it exits — don't trample its
  // editBox + handles. The ResizeObserver on `<img>` fires every reflow
  // (info-bar swap, image swap, window resize…), and an unconditional
  // `layer.innerHTML = ''` here would nuke the in-progress edit overlay.
  if (bboxEditState) {
    _relayoutEditBox();
    return;
  }
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

window.addEventListener('resize', () => {
  // When in edit mode, the bbox edit overlay needs the same realign math
  // the regular overlays get. Both branches are no-ops when their state
  // is empty.
  renderFaceOverlays();
  _relayoutEditBox();
});

// ---- bbox redraw mode (#13) ---------------------------------------------
// Enters a modal-ish mode where ONE face's bbox shows draggable corner
// handles + body grab. Save → POST /api/faces/{id}/redraw-bbox. Cancel/Esc
// rebuilds overlays from server state. Bbox lives in DETECT_MAX_SIDE coords;
// we convert to/from screen pixels using the same sx/sy as renderFaceOverlays.
let bboxEditState = null;

function _bboxClamp(b, naturalW, naturalH) {
  let [x, y, w, h] = b.map(n => Math.round(n));
  x = Math.max(0, Math.min(x, naturalW - 1));
  y = Math.max(0, Math.min(y, naturalH - 1));
  w = Math.max(24, Math.min(w, naturalW - x));
  h = Math.max(24, Math.min(h, naturalH - y));
  return [x, y, w, h];
}

export function enterBboxRedrawMode(face) {
  if (!face || !Array.isArray(face.bbox) || face.bbox.length !== 4) return;
  const img = $('lightbox-img');
  if (!img.naturalWidth) return;
  const layer = $('face-layer');
  layer.innerHTML = '';
  // Hide all OTHER overlays so the user can focus on the box they're editing.
  state.facesVisible = false;

  // Toolbar replaces the info bar in the lightbox flex flow. Both share
  // the bottom slot (`flex: 0 0 auto`) so the image area's dimensions
  // stay the same — we just swap content. Building the toolbar with the
  // `.info` class keeps the same CSS sizing and padding.
  const infoBar = $('lightbox-info');
  const previousInfoDisplay = infoBar ? infoBar.style.display : null;
  if (infoBar) infoBar.style.display = 'none';

  const toolbar = document.createElement('div');
  toolbar.id = 'bbox-edit-toolbar';
  toolbar.className = 'info';
  toolbar.style.background = 'rgba(0,0,0,0.85)';
  toolbar.style.display = 'flex';
  toolbar.style.gap = '10px';
  toolbar.style.alignItems = 'center';
  toolbar.style.justifyContent = 'center';
  toolbar.innerHTML = `
    <span>redraw bbox — drag corners to resize, drag inside to move</span>
    <button id="bbox-edit-save" type="button" style="background:#22c55e;color:#fff;border:0;border-radius:4px;padding:3px 10px;cursor:pointer;">save (Enter)</button>
    <button id="bbox-edit-cancel" type="button" style="background:#52525b;color:#fff;border:0;border-radius:4px;padding:3px 10px;cursor:pointer;">cancel (Esc)</button>
  `;
  // Insert as a sibling of the info bar (same parent → same flex slot).
  if (infoBar && infoBar.parentNode) {
    infoBar.parentNode.insertBefore(toolbar, infoBar);
  } else {
    $('lightbox').appendChild(toolbar);
  }

  // Force a layout flush right now so `img.client{Width,Height}` and
  // `img.offset{Left,Top}` reflect the post-swap geometry. Without this,
  // a subsequent `_relayoutEditBox` call inside `requestAnimationFrame`
  // would still be racing whatever else may schedule on the same frame
  // (and on some browsers the box would render at 0×0 momentarily).
  void img.getBoundingClientRect();

  const editBox = document.createElement('div');
  editBox.className = 'face-box';
  editBox.style.borderColor = '#22c55e';
  editBox.style.background = 'rgba(34,197,94,0.18)';
  // Make the box obvious so a misalignment is still spottable.
  editBox.style.borderWidth = '3px';
  editBox.style.cursor = 'move';
  editBox.style.zIndex = '50';

  // Eight handles (4 corners + 4 edges). Only corners are functional for
  // resize; the rest are visual cues.
  const handlePositions = [
    {key: 'nw', x: 0,  y: 0,  cursor: 'nwse-resize'},
    {key: 'ne', x: 1,  y: 0,  cursor: 'nesw-resize'},
    {key: 'sw', x: 0,  y: 1,  cursor: 'nesw-resize'},
    {key: 'se', x: 1,  y: 1,  cursor: 'nwse-resize'},
  ];
  for (const h of handlePositions) {
    const dot = document.createElement('div');
    dot.className = 'bbox-handle';
    dot.dataset.handle = h.key;
    dot.style.position = 'absolute';
    dot.style.width = '14px';
    dot.style.height = '14px';
    dot.style.background = '#22c55e';
    dot.style.border = '2px solid #fff';
    dot.style.borderRadius = '50%';
    dot.style.boxSizing = 'border-box';
    dot.style.left = (h.x * 100) + '%';
    dot.style.top = (h.y * 100) + '%';
    dot.style.transform = 'translate(-50%, -50%)';
    dot.style.cursor = h.cursor;
    dot.style.zIndex = '51';
    editBox.appendChild(dot);
  }
  layer.appendChild(editBox);

  bboxEditState = {
    faceId: face.id,
    bbox: face.bbox.slice(),
    naturalW: img.naturalWidth,
    naturalH: img.naturalHeight,
    sx: 1,
    sy: 1,
    editBox,
    toolbar,
    infoBar,
    previousInfoDisplay,
    drag: null,
  };
  // Inline measurement after the layout flush above. Also re-measure on
  // the next two animation frames as a defensive belt-and-braces — some
  // browsers settle flex layout asynchronously after a child display
  // toggle even though `clientWidth` reads the current value.
  _relayoutEditBox();
  requestAnimationFrame(_relayoutEditBox);
  requestAnimationFrame(() => requestAnimationFrame(_relayoutEditBox));

  editBox.addEventListener('pointerdown', onEditPointerDown);
  for (const dot of editBox.querySelectorAll('.bbox-handle')) {
    dot.addEventListener('pointerdown', onEditPointerDown);
  }
  $('bbox-edit-save').onclick = () => commitBboxRedraw().catch(e => alert('save failed: ' + e.message));
  $('bbox-edit-cancel').onclick = () => exitBboxRedrawMode(true);
  // Capture phase + stopImmediatePropagation: keyboard.js attached its own
  // global keydown listener at startup; without capture, its handler runs
  // first and Esc closes the entire lightbox before we can intercept.
  document.addEventListener('keydown', onBboxEditKey, true);
}

function drawEditBox() {
  if (!bboxEditState) return;
  const {bbox, sx, sy, editBox} = bboxEditState;
  const [x, y, w, h] = bbox;
  editBox.style.left = (x * sx) + 'px';
  editBox.style.top = (y * sy) + 'px';
  editBox.style.width = (w * sx) + 'px';
  editBox.style.height = (h * sy) + 'px';
}

// Recompute sx/sy + face-layer geometry from the *current* image rect, then
// redraw. Called once on entry (after layout reflows) and on window resize.
function _relayoutEditBox() {
  if (!bboxEditState) return;
  const img = $('lightbox-img');
  const layer = $('face-layer');
  if (!img || !img.naturalWidth) return;
  const sx = img.clientWidth / img.naturalWidth;
  const sy = img.clientHeight / img.naturalHeight;
  layer.style.left = img.offsetLeft + 'px';
  layer.style.top = img.offsetTop + 'px';
  layer.style.width = img.clientWidth + 'px';
  layer.style.height = img.clientHeight + 'px';
  bboxEditState.sx = sx;
  bboxEditState.sy = sy;
  drawEditBox();
}

function onEditPointerDown(e) {
  if (!bboxEditState) return;
  e.preventDefault();
  e.stopPropagation();
  const handle = e.target.dataset && e.target.dataset.handle;
  bboxEditState.drag = {
    mode: handle ? `resize-${handle}` : 'move',
    startX: e.clientX,
    startY: e.clientY,
    startBbox: bboxEditState.bbox.slice(),
  };
  // Capture so the drag survives leaving the box element.
  if (e.target.setPointerCapture) {
    try { e.target.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
  }
  document.addEventListener('pointermove', onEditPointerMove);
  document.addEventListener('pointerup', onEditPointerUp, {once: true});
}

function onEditPointerMove(e) {
  if (!bboxEditState || !bboxEditState.drag) return;
  const {drag, sx, sy, naturalW, naturalH} = bboxEditState;
  const dxPx = e.clientX - drag.startX;
  const dyPx = e.clientY - drag.startY;
  // Convert screen-pixel delta to natural-coord delta.
  const dx = dxPx / sx;
  const dy = dyPx / sy;
  let [x, y, w, h] = drag.startBbox;
  if (drag.mode === 'move') {
    x += dx; y += dy;
  } else if (drag.mode === 'resize-nw') {
    x += dx; y += dy; w -= dx; h -= dy;
  } else if (drag.mode === 'resize-ne') {
    y += dy; w += dx; h -= dy;
  } else if (drag.mode === 'resize-sw') {
    x += dx; w -= dx; h += dy;
  } else if (drag.mode === 'resize-se') {
    w += dx; h += dy;
  }
  bboxEditState.bbox = _bboxClamp([x, y, w, h], naturalW, naturalH);
  drawEditBox();
}

function onEditPointerUp() {
  if (!bboxEditState) return;
  bboxEditState.drag = null;
  document.removeEventListener('pointermove', onEditPointerMove);
}

function onBboxEditKey(e) {
  if (!bboxEditState) return;
  // Capture phase + stopImmediatePropagation: keyboard.js's global keydown
  // listener was registered earlier and would otherwise fire next; on
  // Escape that would closeLightbox(), tearing down the edit mode under us.
  if (e.key === 'Escape') {
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    exitBboxRedrawMode(true);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    commitBboxRedraw().catch(err => alert('save failed: ' + err.message));
  }
}

async function commitBboxRedraw() {
  if (!bboxEditState) return;
  const {faceId, bbox, toolbar} = bboxEditState;
  // Surface a "submitting / processing" indicator while the request runs:
  // the redraw endpoint does up to 7 detector forwards (multi-margin +
  // offset sweep) so a one-shot save can take 1–3 s on CPU. Without
  // feedback the user has no idea whether the click registered.
  let savedToolbarHTML = null;
  let saveBtn = null;
  let cancelBtn = null;
  if (toolbar) {
    savedToolbarHTML = toolbar.innerHTML;
    saveBtn = toolbar.querySelector('#bbox-edit-save');
    cancelBtn = toolbar.querySelector('#bbox-edit-cancel');
    if (saveBtn) saveBtn.disabled = true;
    if (cancelBtn) cancelBtn.disabled = true;
    toolbar.innerHTML = `
      <span style="display:inline-block;width:14px;height:14px;border:2px solid #fff;border-top-color:transparent;border-radius:50%;animation:bbox-spin 0.8s linear infinite;"></span>
      <span>submitted — refining face position (this can take a few seconds)…</span>
    `;
    // One-time keyframes injection so the spinner has something to animate.
    if (!document.getElementById('bbox-spin-keyframes')) {
      const style = document.createElement('style');
      style.id = 'bbox-spin-keyframes';
      style.textContent = '@keyframes bbox-spin { to { transform: rotate(360deg); } }';
      document.head.appendChild(style);
    }
  }
  // Edit box stays visible (locked) so the user can see what they submitted
  // while the model runs. Pointer events are disabled so they can't drag mid-flight.
  if (bboxEditState.editBox) {
    bboxEditState.editBox.style.pointerEvents = 'none';
    bboxEditState.editBox.style.opacity = '0.7';
  }
  let result;
  try {
    result = await api(`/api/faces/${faceId}/redraw-bbox`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({bbox}),
    });
  } catch (e) {
    // Surface the URL + bbox we attempted in the console so a 404 / 422
    // failure is debuggable from devtools without re-instrumenting.
    console.warn('redraw-bbox failed', {faceId, bbox, error: String(e)});
    // Restore the toolbar so the user can re-try or cancel without
    // re-entering edit mode from scratch.
    if (toolbar && savedToolbarHTML !== null) {
      toolbar.innerHTML = savedToolbarHTML;
      if (bboxEditState && bboxEditState.editBox) {
        bboxEditState.editBox.style.pointerEvents = '';
        bboxEditState.editBox.style.opacity = '';
      }
      // Re-bind the save/cancel handlers — the new buttons are different
      // DOM nodes after innerHTML restore.
      const sBtn = toolbar.querySelector('#bbox-edit-save');
      const cBtn = toolbar.querySelector('#bbox-edit-cancel');
      if (sBtn) sBtn.onclick = () => commitBboxRedraw().catch(err => alert('save failed: ' + err.message));
      if (cBtn) cBtn.onclick = () => exitBboxRedrawMode(true);
    }
    throw e;
  }
  // Update the local face record so the overlay redraws at the new spot
  // immediately. The cached thumb on the server side is already evicted by
  // the endpoint; force a reload of any visible <img src="/face-thumb/..">
  // by appending a cache-buster query string.
  const idx = (state.lightboxFaces || []).findIndex(f => f.id === faceId);
  if (idx >= 0) {
    state.lightboxFaces[idx] = {
      ...state.lightboxFaces[idx],
      bbox: result.bbox,
      det_score: result.det_score,
      // Server forced verified=1 on redraw — pull it through so the
      // overlay drops the dashed "suspect" border immediately.
      verified: result.verified ?? state.lightboxFaces[idx].verified,
    };
  }
  exitBboxRedrawMode(false);
  state.facesVisible = true;
  renderFaceOverlays();
}

function exitBboxRedrawMode(restoreOverlays) {
  if (!bboxEditState) return;
  // Tear down the toolbar (lives on document.body now, not the face-layer).
  if (bboxEditState.toolbar && bboxEditState.toolbar.parentNode) {
    bboxEditState.toolbar.parentNode.removeChild(bboxEditState.toolbar);
  }
  // Restore the lightbox info bar's display state.
  if (bboxEditState.infoBar) {
    bboxEditState.infoBar.style.display = bboxEditState.previousInfoDisplay || '';
  }
  // Listener was added with capture: true → must be removed with the same flag.
  document.removeEventListener('keydown', onBboxEditKey, true);
  document.removeEventListener('pointermove', onEditPointerMove);
  // Defensive: pointerup is `{once: true}` so it normally self-removes,
  // but if the user is still holding pointerdown when edit mode tears
  // down (e.g. Cancel button click while dragging) the listener would
  // outlive its purpose. Removing here is harmless if it already fired.
  document.removeEventListener('pointerup', onEditPointerUp);
  bboxEditState = null;
  if (restoreOverlays) {
    state.facesVisible = true;
    renderFaceOverlays();
  }
}

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

// #27 — per-image manual category override popover. A lightweight form
// (no global state) that lets the user pin / re-pin / clear a single
// photo's category. Sourced datalist from /api/categories so they don't
// need to remember exact spelling.
function openImageCategoryForm(imageId, currentValue, onChanged) {
  // Tear down any pre-existing popover first (rapid re-clicks).
  const existing = document.getElementById('image-category-form');
  if (existing && existing.parentNode) existing.parentNode.removeChild(existing);

  const form = document.createElement('div');
  form.id = 'image-category-form';
  Object.assign(form.style, {
    position: 'fixed', zIndex: '200', background: '#222', color: '#fff',
    padding: '8px', borderRadius: '4px', boxShadow: '0 6px 20px rgba(0,0,0,0.5)',
    minWidth: '320px', display: 'flex', flexDirection: 'column', gap: '6px',
  });
  // Anchor near the chip click; the user just clicked it so it's near top-bottom.
  form.style.left = '50%';
  form.style.top = '50%';
  form.style.transform = 'translate(-50%, -50%)';
  const datalistId = `image-cat-list-${Math.random().toString(36).slice(2, 8)}`;
  form.innerHTML = `
    <div>📁 manual category for this photo (overrides rules)</div>
    <div style="display:flex; gap:6px;">
      <input id="image-cat-input" list="${datalistId}" type="text" placeholder="category name…"
             style="flex:1; padding:4px 6px; background:#111; color:#fff; border:1px solid #555; border-radius:3px; font-size:13px;"
             value="${escape(currentValue || '')}">
      <datalist id="${datalistId}"></datalist>
      <button id="image-cat-save" type="button"
              style="padding:4px 10px; background:#22c55e; color:#fff; border:0; border-radius:3px; cursor:pointer; font-size:12px;">save</button>
    </div>
    <div style="display:flex; gap:6px; justify-content:space-between;">
      <button id="image-cat-clear" type="button" title="drop the override (rules will apply)"
              style="padding:4px 10px; background:#52525b; color:#fff; border:0; border-radius:3px; cursor:pointer; font-size:12px;">clear override</button>
      <button id="image-cat-cancel" type="button"
              style="padding:4px 10px; background:#52525b; color:#fff; border:0; border-radius:3px; cursor:pointer; font-size:12px;">close (Esc)</button>
    </div>
  `;
  document.body.appendChild(form);

  // Lazy-load the category list into the datalist on first focus.
  const inp = $('image-cat-input');
  let listLoaded = false;
  inp.addEventListener('focus', async () => {
    if (listLoaded) return;
    listLoaded = true;
    try {
      const cats = await api('/api/categories');
      const dl = document.getElementById(datalistId);
      if (dl) for (const c of cats) {
        const opt = document.createElement('option');
        opt.value = c.name;
        dl.appendChild(opt);
      }
    } catch (_) { /* free-form input still works */ }
  });
  inp.focus();
  inp.select();

  const close = () => { if (form.parentNode) form.parentNode.removeChild(form); };
  const submit = async (categoryOrNull) => {
    try {
      const result = await api(`/api/images/${imageId}/category`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({category: categoryOrNull}),
      });
      onChanged(result.category);
      close();
    } catch (e) {
      alert('failed: ' + e.message);
    }
  };
  $('image-cat-save').onclick = () => {
    const v = inp.value.trim();
    submit(v || null);
  };
  $('image-cat-clear').onclick = () => submit(null);
  $('image-cat-cancel').onclick = close;
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submit(inp.value.trim() || null); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
  });
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
  $('face-redraw').onclick = () => {
    if (!pendingFace) return;
    const target = pendingFace;
    closeFaceNameForm();
    enterBboxRedrawMode(target);
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
