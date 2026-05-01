# 08 — XMP sidecars & user categories

**Status — shipped.** XMP writer (`phototag xmp write/clean`) +
category CLI (`phototag category add/rm/list/map/unmap`) + in-app
categories view (sidebar third tab). v11 schema migration adds
`categories`, `tag_category_map`, `cluster_categories` tables.
`phototag xmp write --apply` emits both `dc:Subject` (flat keywords)
and `lr:HierarchicalSubject` (`category|subject` paths) and is
idempotent against the existing sidecar (mtime + content gate).

## Goal

Tags stay attached to photos even if the SQLite DB is lost. Tags become readable in digiKam, Lightroom, Capture One, etc. — anything that reads XMP.

## XMP sidecar format

- File: `<image>.xmp` next to the original.
- Field: `dc:subject` (Dublin Core subject) — multi-value, used by virtually all photo apps for keywords.
- Optionally also `lr:hierarchicalSubject` (Lightroom hierarchical keywords) for `category|sub-category` paths once user categories exist.

Example:

```xml
<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
  <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
    <rdf:Description rdf:about=''
        xmlns:dc='http://purl.org/dc/elements/1.1/'
        xmlns:lr='http://ns.adobe.com/lightroom/1.0/'>
      <dc:subject>
        <rdf:Bag>
          <rdf:li>x-ray</rdf:li>
          <rdf:li>bone</rdf:li>
        </rdf:Bag>
      </dc:subject>
      <lr:hierarchicalSubject>
        <rdf:Bag>
          <rdf:li>medical|x-ray</rdf:li>
        </rdf:Bag>
      </lr:hierarchicalSubject>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>
```

## Implementation

`exiftool` subprocess by default — most reliable, handles all formats. `pyexiv2` as fallback. Always write sidecar (`.xmp`), never modify the original (preserves bit-exact archives).

```
phototag xmp write PATH [--threshold 0.7] [--include-people] [--apply]
phototag xmp clean PATH [--apply]
```

Write only tags above threshold (default 0.7, configurable via `--threshold`). `--include-people` also adds the `label_user` of every `user_verified=1` face on the image as a keyword. Don't pollute keywords with low-confidence noise.

**Status — XMP write portion: shipped (#22).** `phototag/xmp.py` shells
out to `exiftool` (system binary; no Python dep). Default is dry-run;
`--apply` actually writes. Skips images whose sidecar mtime is newer
than the source AND whose `dc:Subject` set already matches what we
would write (mtime + content check, per the idempotence contract
below). Sidecars are written atomically via `<sidecar>.tmp.<token>` →
`os.replace`. The `lr:HierarchicalSubject` field stays empty until
**#23 (categories + tag/cluster mapping)** lands — see the "User
categories" section below.

## User categories

`categories` table + `tag_category_map` (see `03-data-model.md`). Drives the hierarchical XMP field.

### Mapping rules

Two rule sources, in precedence order:

1. **Cluster → category** — `cluster_categories` row binds a face_cluster
   to a category. Every image carrying a face from that cluster picks up
   the category in its hierarchical keyword set.
2. **Tag → category** — `tag_category_map` row binds a tag (RAM++ or
   manual) to a category. Every image carrying that tag picks it up.

Cluster rules win over tag rules when both apply (the cluster signal is
human-confirmed, the tag is a model prediction). Per-image manual
overrides are intentionally NOT shipped — the two-rule model covers the
real-world case and the schema can be extended later if the need shows
up. UNIQUE on `(tag_id)` and `(cluster_id)` enforces "one rule per
target": a tag/cluster maps to at most one category.

### CLI

```
phototag category add medical
phototag category rm medical
phototag category list                       # JSON: categories + rules
phototag category map --tag x-ray --category medical
phototag category map --cluster 7 --category family
phototag category unmap --tag x-ray
phototag category unmap --cluster 7
```

Rules are read on every `phototag xmp write --apply` — there is no
"apply" / cache step to invalidate. `Store.categories_for_image()` does
the union resolve at write time.

### UI

Sidebar third view **categories** (next to clusters / faces). Inline
"+ new" form to create a category. Clicking a category opens the
workspace rule editor: bound tag rows + bound cluster rows (each with
an unmap × button), tag-bind input with `/api/tags`-sourced datalist
autocomplete, cluster-id-bind numeric input, and a 🗑️ button to drop
the category (cascades through both rule tables via FK ON DELETE).
Endpoints: `GET/POST /api/categories`,
`DELETE /api/categories/{name}`, `GET /api/categories/{name}`,
`POST /api/categories/{name}/rules/{tag,cluster}`,
`DELETE /api/categories/rules/{tag,cluster}/{key}`.

## Idempotence

XMP write must be idempotent: same DB state → same sidecar bytes. Skip writes when sidecar mtime > image mtime AND sidecar content matches DB-derived expected content.
