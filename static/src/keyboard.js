// Global keyboard shortcuts. Two listeners:
//  1. ? / Esc → help overlay (suppressed when typing into an input)
//  2. lightbox + face-popover shortcuts (only fire when one of those is open)

import { state } from './state.js';
import { $, api } from './api.js';
import {
  navLightbox, closeLightbox, closeFaceNameForm,
  toggleFaceOverlays, toggleTagsCloud, showCurrentLightbox,
} from './lightbox.js';

export function toggleHelp() {
  const o = $('help-overlay');
  o.style.display = o.style.display === 'none' ? 'flex' : 'none';
}

export function wireKeyboardHandlers() {
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
}

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
      writeHashRef();
      return;
    }
  }
  // None in current view — load the first orphan photo into the view.
  if (candidates.length) {
    state.viewIds = candidates;
    state.viewIndex = 0;
    showCurrentLightbox();
    writeHashRef();
  }
}

let writeHashRef = () => {};

export function bindKeyboardRefs(refs) {
  if (refs.writeHash) writeHashRef = refs.writeHash;
}
