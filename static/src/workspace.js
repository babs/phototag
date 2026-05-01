// Workspace = the right-hand main panel. Every "view" the user can land
// on (single cluster, search results, single person merged across clusters,
// triage queue, noise/orphan, the "all photos with faces" grid, the orphan
// re-cluster dry-run preview) is rendered here.

import { state } from './state.js';
import { $, html, escape, api } from './api.js';
import {
  makeTile, openLightbox, showCurrentLightbox,
} from './lightbox.js';
import { loadClusters, showFacesPanel, toggleTag } from './sidebar.js';

// ---- single cluster -----------------------------------------------------

export async function showCluster(id) {
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

// ---- tag/person filter search -------------------------------------------

export async function runSearch() {
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

// ---- viewPerson — used by the lightbox face popover G hotkey ------------
// Switch the underlying workspace to the person's view (faces panel +
// sidebar selection + grid + URL hash) and re-anchor the open lightbox at
// the first photo. Earlier versions only swapped `state.viewIds` inline,
// which left the workspace, sidebar, and hash stale — closing the lightbox
// dropped the user back on the previous view, mismatched.
export async function viewPerson(clusterId, name) {
  state.view = 'faces';
  document.querySelectorAll('.view-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.view === 'faces');
  });
  if ($('cluster-pane-title').textContent !== 'people') await showFacesPanel();
  if (name) {
    await showPersonByName(name);
  } else if (clusterId != null) {
    await showPersonInWorkspace(clusterId);
  } else {
    return;
  }
  if (!state.viewIds.length) return;
  state.viewIndex = 0;
  await showCurrentLightbox();
  // Update the `image=` segment of the hash now that the lightbox points
  // at the first photo of the new person; showPerson*() already wrote
  // run/view/who-or-face, but did so before viewIndex moved.
  writeHashRef();
}

// ---- noise / orphan workspace -------------------------------------------

export async function showUnidentifiedInWorkspace() {
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

export async function showTriageInWorkspace() {
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

export async function showFacesGrid() {
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
    // #12: expanders show ~5 sample faces per proposed cluster (the
    // `sample_face_ids` the backend already computes — nearest to centroid).
    // Lets the user sanity-check a dry-run before committing it.
    const table = html('<table style="border-collapse:collapse; font-size:12px; width:100%; max-width:720px;"></table>');
    table.appendChild(html(`<thead><tr>
      <th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border);"></th>
      <th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border);">cluster</th>
      <th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border);">size</th>
      <th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border);">matched identity</th>
    </tr></thead>`));
    const tbody = html('<tbody></tbody>');
    for (const c of result.clusters) {
      const samples = Array.isArray(c.sample_face_ids) ? c.sample_face_ids : [];
      const expandable = samples.length > 0;
      const tr = html(`<tr class="recluster-row" data-cluster="${c.cluster_no}">
        <td style="padding:4px 8px;border-bottom:1px solid #f0f0f0; width:24px;">
          ${expandable ? '<button class="recluster-toggle" type="button" style="background:none;border:0;cursor:pointer;font-family:inherit;font-size:12px;padding:0;color:var(--muted);">▸</button>' : ''}
        </td>
        <td style="padding:4px 8px;border-bottom:1px solid #f0f0f0;">#${c.cluster_no}</td>
        <td style="padding:4px 8px;border-bottom:1px solid #f0f0f0;">${c.size}</td>
        <td style="padding:4px 8px;border-bottom:1px solid #f0f0f0;">${c.label_user ? `<b>${escape(c.label_user)}</b>` : '<span style="color:var(--muted);">(new)</span>'}</td>
      </tr>`);
      tbody.appendChild(tr);
      if (expandable) {
        // Hidden expander row — strip of face thumbs, lazy-loaded so a
        // 50-cluster preview doesn't fan out 250 image requests up front.
        const expander = html(`<tr class="recluster-expander" style="display:none;">
          <td></td>
          <td colspan="3" style="padding:6px 8px;border-bottom:1px solid #f0f0f0;">
            <div class="recluster-thumbs" style="display:flex;gap:6px;flex-wrap:wrap;"></div>
          </td>
        </tr>`);
        tbody.appendChild(expander);
        const btn = tr.querySelector('.recluster-toggle');
        btn.addEventListener('click', () => {
          const open = expander.style.display !== 'none';
          expander.style.display = open ? 'none' : '';
          btn.textContent = open ? '▸' : '▾';
          if (!open) {
            const strip = expander.querySelector('.recluster-thumbs');
            if (!strip.dataset.loaded) {
              for (const fid of samples) {
                const img = document.createElement('img');
                img.loading = 'lazy';
                img.src = `/face-thumb/${fid}`;
                img.alt = `face ${fid}`;
                img.title = `face ${fid}`;
                img.style.width = '52px';
                img.style.height = '52px';
                img.style.objectFit = 'cover';
                img.style.borderRadius = '3px';
                img.style.border = '1px solid var(--border)';
                strip.appendChild(img);
              }
              strip.dataset.loaded = '1';
            }
          }
        });
      }
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

// ---- person workspace (single cluster) ----------------------------------

export async function showPersonInWorkspace(clusterId) {
  state.selectedFaceCluster = clusterId;
  state.selectedPersonName = null;
  writeHashRef();
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

// ---- person workspace (merged across all clusters of the same name) -----

export async function showPersonByName(name) {
  state.selectedPersonName = name;
  state.selectedFaceCluster = null;
  writeHashRef();
  const ws = $('workspace');
  ws.innerHTML = '<div class="empty">loading…</div>';
  const data = await api(`/api/people/by-name/${encodeURIComponent(name)}?limit=500`);
  ws.innerHTML = '';
  const cBadge = data.n_clusters > 1
    ? ` <span style="font-size:12px;color:var(--muted);">(${data.n_clusters} clusters)</span>`
    : '';
  ws.appendChild(html(`<h2>👤 ${escape(name)} <button id="person-edit-toggle" class="pen-btn" title="edit">✏️</button> <button id="person-fringe-toggle" class="pen-btn" title="show 9 most-uncertain faces">fringe</button>${cBadge}</h2>`));
  ws.appendChild(html(`<div class="auto">${data.n_photos} photos${data.n_clusters > 1 ? ` across ${data.n_clusters} clusters` : ''}</div>`));

  // Fringe row (hidden by default): the N farthest-from-centroid faces of
  // this person, across every cluster sharing the label. Click → lightbox.
  const fringeRow = html('<div id="person-fringe" class="fringe-row" style="display:none;"></div>');
  ws.appendChild(fringeRow);
  let fringeLoaded = false;
  $('person-fringe-toggle').addEventListener('click', async () => {
    const row = $('person-fringe');
    const showing = row.style.display !== 'none';
    if (showing) { row.style.display = 'none'; return; }
    row.style.display = 'flex';
    if (fringeLoaded) return;
    row.innerHTML = '<div class="empty">loading…</div>';
    try {
      const faces = await api(`/api/people/by-name/${encodeURIComponent(name)}/edge?limit=9`);
      row.innerHTML = '';
      if (!faces.length) {
        row.appendChild(html('<div class="empty">no faces</div>'));
      } else {
        for (const f of faces) {
          // Tag the distance with its kind — Euclidean (UMAP) and
          // cosine-derived distances are different scales, so the bare
          // number is misleading without context. (#16 — distance_kind)
          const kind = f.distance_kind === 'cosine_dist' ? 'cos'
                     : f.distance_kind === 'euclidean_umap' ? 'umap'
                     : '';
          const cell = html(`<div class="fringe-cell">
            <img loading="lazy" src="/face-thumb/${f.face_id}" alt="">
            <div class="fringe-meta">d=${Number(f.distance).toFixed(2)}${kind ? ' (' + kind + ')' : ''}</div>
          </div>`);
          cell.addEventListener('click', () => openLightbox(f.image_id));
          row.appendChild(cell);
        }
      }
      fringeLoaded = true;
    } catch (e) {
      row.innerHTML = `<div class="empty">failed: ${escape(e.message)}</div>`;
    }
  });

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

// ---- categories detail (#23 UI) -----------------------------------------
// Renders the rule editor for one category: tag-bind autocomplete, cluster-
// bind via a free-form id field, and an unmap button per existing rule.
export async function showCategoryDetail(name) {
  const ws = $('workspace');
  ws.innerHTML = '<div class="empty">loading…</div>';
  let data;
  try {
    data = await api(`/api/categories/${encodeURIComponent(name)}`);
  } catch (e) {
    ws.innerHTML = `<div class="empty">failed: ${escape(e.message)}</div>`;
    return;
  }
  ws.innerHTML = '';
  ws.appendChild(html(`<h2>📁 ${escape(data.name)} <button id="category-delete" class="pen-btn" title="delete this category">🗑️</button></h2>`));
  ws.appendChild(html(`<div class="auto">XMP sidecars get <code>${escape(data.name)}|&lt;subject&gt;</code> as a hierarchical keyword.</div>`));

  $('category-delete').addEventListener('click', async () => {
    if (!confirm(`Delete category "${name}" and all ${data.tag_rules.length + data.cluster_rules.length} rule(s)?`)) return;
    try {
      await api(`/api/categories/${encodeURIComponent(name)}`, {method: 'DELETE'});
    } catch (e) { alert('delete failed: ' + e.message); return; }
    // Refresh sidebar; clear workspace.
    const m = await import('./sidebar.js');
    await m.showCategoriesPanel();
    ws.innerHTML = '<div class="empty">category deleted</div>';
  });

  // ---- tag rules ---------------------------------------------------------
  ws.appendChild(html('<h3 style="margin:18px 0 6px; font-size:14px;">tag rules</h3>'));
  const tagWrap = html('<div style="display:flex;flex-direction:column;gap:4px;max-width:520px;"></div>');
  for (const r of data.tag_rules) {
    const row = html(`<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 8px;border:1px solid var(--border);border-radius:4px;">
      <span><code>${escape(r.tag)}</code></span>
      <button type="button" class="pen-btn" title="unmap" style="font-size:14px;">×</button>
    </div>`);
    row.querySelector('button').addEventListener('click', async () => {
      try { await api(`/api/categories/rules/tag/${encodeURIComponent(r.tag)}`, {method: 'DELETE'}); }
      catch (e) { alert('unmap failed: ' + e.message); return; }
      showCategoryDetail(name);
    });
    tagWrap.appendChild(row);
  }
  if (!data.tag_rules.length) tagWrap.appendChild(html('<div class="empty">no tag rules yet</div>'));
  ws.appendChild(tagWrap);

  // Tag bind form: free-form input + datalist sourced from /api/tags so the
  // user can either type-ahead or paste any known tag name.
  const tagDatalistId = `category-tag-bind-${Math.random().toString(36).slice(2, 8)}`;
  const tagAdd = html(`<div style="display:flex;gap:6px;margin-top:6px;max-width:520px;">
    <input id="category-tag-bind-input" type="text" list="${tagDatalistId}" placeholder="bind tag…"
           style="flex:1;padding:4px 7px;border:1px solid var(--border);border-radius:4px;font-size:12px;">
    <datalist id="${tagDatalistId}"></datalist>
    <button id="category-tag-bind-add" type="button"
            style="padding:4px 10px;border:1px solid var(--border);border-radius:4px;background:#fff;cursor:pointer;font-size:12px;">bind</button>
  </div>`);
  ws.appendChild(tagAdd);
  // Lazy-load tag list on first focus so we don't hit /api/tags every panel open.
  let tagsLoaded = false;
  $('category-tag-bind-input').addEventListener('focus', async () => {
    if (tagsLoaded) return;
    tagsLoaded = true;
    try {
      const tags = await api('/api/tags?limit=500');
      const dl = document.getElementById(tagDatalistId);
      if (dl) for (const t of tags) {
        const opt = document.createElement('option');
        opt.value = t.name;
        dl.appendChild(opt);
      }
    } catch (e) { /* ignore — input still works as free-form */ }
  });
  const submitTagBind = async () => {
    const tag = $('category-tag-bind-input').value.trim();
    if (!tag) return;
    try {
      await api(`/api/categories/${encodeURIComponent(name)}/rules/tag`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tag}),
      });
    } catch (e) { alert('bind failed: ' + e.message); return; }
    showCategoryDetail(name);
  };
  $('category-tag-bind-add').onclick = submitTagBind;
  $('category-tag-bind-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submitTagBind(); }
  });

  // ---- cluster rules -----------------------------------------------------
  ws.appendChild(html('<h3 style="margin:18px 0 6px; font-size:14px;">face-cluster rules</h3>'));
  const clWrap = html('<div style="display:flex;flex-direction:column;gap:4px;max-width:520px;"></div>');
  for (const r of data.cluster_rules) {
    const lbl = r.label_user ? `<b>${escape(r.label_user)}</b>` : `<span style="color:var(--muted);">cluster #${r.cluster_no}</span>`;
    const row = html(`<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 8px;border:1px solid var(--border);border-radius:4px;">
      <span>${lbl} <span style="color:var(--muted);font-size:11px;">id ${r.cluster_id}</span></span>
      <button type="button" class="pen-btn" title="unmap" style="font-size:14px;">×</button>
    </div>`);
    row.querySelector('button').addEventListener('click', async () => {
      try { await api(`/api/categories/rules/cluster/${r.cluster_id}`, {method: 'DELETE'}); }
      catch (e) { alert('unmap failed: ' + e.message); return; }
      showCategoryDetail(name);
    });
    clWrap.appendChild(row);
  }
  if (!data.cluster_rules.length) clWrap.appendChild(html('<div class="empty">no cluster rules yet</div>'));
  ws.appendChild(clWrap);

  // TODO: when this UI grows beyond bookkeeping, swap the raw cluster-id
  // input for a label-search datalist sourced from `/api/people/names`.
  // For now, single-user admin use justifies the integer-id field —
  // the user copies the id from the people sidebar.
  const clAdd = html(`<div style="display:flex;gap:6px;margin-top:6px;max-width:520px;">
    <input id="category-cluster-bind-input" type="number" min="1" placeholder="bind face-cluster id…"
           style="flex:1;padding:4px 7px;border:1px solid var(--border);border-radius:4px;font-size:12px;">
    <button id="category-cluster-bind-add" type="button"
            style="padding:4px 10px;border:1px solid var(--border);border-radius:4px;background:#fff;cursor:pointer;font-size:12px;">bind</button>
  </div>`);
  ws.appendChild(clAdd);
  const submitClusterBind = async () => {
    const raw = $('category-cluster-bind-input').value.trim();
    const cluster_id = parseInt(raw, 10);
    if (!cluster_id || cluster_id <= 0) { alert('cluster id must be a positive integer'); return; }
    try {
      await api(`/api/categories/${encodeURIComponent(name)}/rules/cluster`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cluster_id}),
      });
    } catch (e) { alert('bind failed: ' + e.message); return; }
    showCategoryDetail(name);
  };
  $('category-cluster-bind-add').onclick = submitClusterBind;
  $('category-cluster-bind-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); submitClusterBind(); }
  });
}

// ---- late-bound writeHash ref (set by runs.js to break the import cycle) -
let writeHashRef = () => {};

export function bindWorkspaceRefs(refs) {
  if (refs.writeHash) writeHashRef = refs.writeHash;
}
