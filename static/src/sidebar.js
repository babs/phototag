// Left sidebar — cluster/people list, top-tags cloud, view switcher, the
// AND-token cluster filter, the active-filters chip strip and the
// tag/people search dropdown.

import { state } from './state.js';
import { $, html, escape, api } from './api.js';
import {
  showCluster, runSearch, showFacesGrid, showUnidentifiedInWorkspace,
  showTriageInWorkspace, showPersonByName, showPersonInWorkspace,
} from './workspace.js';

export async function loadClusters() {
  state.clusters = await api(`/api/runs/${state.runId}/clusters`);
  renderClusters();
}

export function renderClusters() {
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

export async function loadTopTags() {
  const tags = await api('/api/tags?limit=30');
  const root = $('top-tags');
  root.innerHTML = '';
  tags.forEach(t => {
    const el = html(`<span class="tag" data-tag="${escape(t.name)}">${escape(t.name)} <span class="count">${t.count}</span></span>`);
    el.onclick = () => toggleTag(t.name);
    root.appendChild(el);
  });
}

export async function loadFacesSummary() {
  try {
    const s = await api('/api/faces/summary');
    if (s.images > 0) $('faces-count').textContent = `(${s.images} photos / ${s.faces} faces)`;
    else $('faces-count').textContent = '(none yet)';
  } catch (e) { /* faces feature not enabled */ }
}

export function selectCluster(id) {
  state.selectedCluster = id;
  document.querySelectorAll('.cluster-row').forEach(r => r.classList.toggle('selected', Number(r.dataset.id) === id));
  showCluster(id);
  // writeHash is wired in runs.js — late-bound below.
  writeHashRef();
}

export function renderActiveFilters() {
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

export async function toggleTag(name) {
  if (state.activeTags.has(name)) state.activeTags.delete(name);
  else state.activeTags.add(name);
  renderActiveFilters();
  if (state.activeTags.size + state.activePersons.size > 0) await runSearch();
  else if (state.selectedCluster) await showCluster(state.selectedCluster);
  writeHashRef();
}

export async function togglePerson(name) {
  if (state.activePersons.has(name)) state.activePersons.delete(name);
  else state.activePersons.add(name);
  renderActiveFilters();
  if (state.activeTags.size + state.activePersons.size > 0) await runSearch();
  else if (state.selectedCluster) await showCluster(state.selectedCluster);
}

// ---- tag/people autocomplete dropdown -----------------------------------

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

// ---- view switching: clusters vs faces ----------------------------------

export function switchView(view) {
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
  writeHashRef();
}

export async function showFacesPanel() {
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

// ---- top-of-sidebar wiring (run select / clear / min-score / view btns) -

export function wireSidebarHandlers() {
  $('run-select').addEventListener('change', async (e) => {
    state.runId = Number(e.target.value);
    state.selectedCluster = null;
    await loadClusters();
    await loadTopTags();
    writeHashRef();
  });
  $('min-score').addEventListener('change', () => {
    if (state.activeTags.size > 0) runSearch();
    writeHashRef();
  });
  $('clear-filters').addEventListener('click', () => {
    state.activeTags.clear();
    state.activePersons.clear();
    renderActiveFilters();
    $('workspace').innerHTML = '<div class="empty">filters cleared</div>';
    writeHashRef();
  });
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

  setupClusterFilter();
}

// Quick filter for the cluster/people list. Pure client-side.
//   - whitespace-delimited tokens act as AND filters (all must match)
//   - matches are bolded in-place via <b> wrappers in the row label
//   - case-insensitive substring; original markup preserved across passes
//     by snapshotting innerHTML on first sight (data-orig-html)
function setupClusterFilter() {
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
}

// ---- late-bound writeHash ref (set by runs.js to break the import cycle) -
let writeHashRef = () => {};

export function bindSidebarRefs(refs) {
  if (refs.writeHash) writeHashRef = refs.writeHash;
}
