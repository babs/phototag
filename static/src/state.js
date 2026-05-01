// Shared mutable UI state. Imported by every other module so they all see
// the same object — JS module exports are live bindings, but mutating
// `state.foo = …` from elsewhere works because everyone holds the same
// object reference.
export const state = {
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
  // populated by the lightbox; referenced by overlay/popover code.
  lightboxFaces: [],
  lightboxImage: null,
  // active workspace view: 'clusters' | 'faces'
  view: 'clusters',
};
