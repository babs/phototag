# 08 — XMP sidecars & user categories (v2)

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

Three sources, in order:

1. **Cluster → category** — copy validated cluster `label_user` as a category, all images in that cluster get that category.
2. **Tag → category** — explicit map (`tag_category_map`), e.g., `{x-ray, mri, ultrasound} → medical`.
3. **Manual override** — `phototag tag <image> --category medical` for individual fixes.

Conflict resolution: cluster wins over tag rule, manual wins over both.

### CLI

```
phototag category add medical
phototag category map --tag x-ray --category medical
phototag category map --cluster 7 --category medical
phototag category list
phototag category apply           # rebuild image→category from rules
```

## Idempotence

XMP write must be idempotent: same DB state → same sidecar bytes. Skip writes when sidecar mtime > image mtime AND sidecar content matches DB-derived expected content.
