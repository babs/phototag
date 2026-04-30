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
phototag xmp write ~/Photos              # write all
phototag xmp write ~/Photos --modified   # only since last write
phototag xmp clean ~/Photos              # remove sidecars
```

Write only tags above threshold (default 0.68, configurable via `--xmp-threshold`). Don't pollute keywords with low-confidence noise.

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
