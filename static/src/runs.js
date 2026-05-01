// Initial loadRuns() bootstrap and the URL-hash <-> state sync. The hash
// captures everything the user sees (run, view, cluster, filters, image)
// so refresh / share / back-button works.

import { state } from './state.js';
import { $, api } from './api.js';
import {
  loadClusters, loadTopTags, loadFacesSummary, renderActiveFilters,
  selectCluster, showFacesPanel,
} from './sidebar.js';
import {
  runSearch, showPersonByName, showPersonInWorkspace,
} from './workspace.js';

export async function loadRuns() {
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

// ---- URL hash sync ------------------------------------------------------

export function parseHash() {
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

export function writeHash() {
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

export async function applyHashState(h) {
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
      // dynamic import keeps the runs<-> lightbox cycle as runtime-only
      import('./lightbox.js').then(m => m.showCurrentLightbox());
    }
  } finally {
    hashWriteSuspended = false;
  }
}

export function wireHashChange() {
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
}
