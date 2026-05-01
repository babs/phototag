// Entry point. esbuild bundles the whole module graph into static/ui.js
// (single IIFE). Load order matches the original monolithic ui.js: wire
// late-bound cross-module refs, attach DOM handlers, render the empty
// active-filter strip, then kick off loadRuns().

import { renderActiveFilters, wireSidebarHandlers, bindSidebarRefs } from './sidebar.js';
import {
  wireLightboxFormHandlers, bindLightboxRefs,
  closeLightbox, navLightbox,
} from './lightbox.js';
import { bindWorkspaceRefs, viewPerson } from './workspace.js';
import { wireKeyboardHandlers, bindKeyboardRefs, toggleHelp } from './keyboard.js';
import { writeHash, wireHashChange, loadRuns } from './runs.js';
import { toggleTag } from './sidebar.js';

// Inject writeHash into modules that need it without a hard import cycle.
bindSidebarRefs({ writeHash });
bindWorkspaceRefs({ writeHash });
bindLightboxRefs({ writeHash, toggleTag });
bindKeyboardRefs({ writeHash });

wireSidebarHandlers();
wireLightboxFormHandlers(viewPerson);
wireKeyboardHandlers();
wireHashChange();

// Inline-onclick handlers in templates/ui.html call these directly off
// `window`. Keep the surface tiny — everything else is an addEventListener.
window.toggleHelp = toggleHelp;
window.closeLightbox = closeLightbox;
window.navLightbox = navLightbox;

renderActiveFilters();
loadRuns();
