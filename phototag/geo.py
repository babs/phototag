"""Offline reverse geocoding.

Uses `reverse_geocoder` which ships with a ~150 MB cities-1000 dataset and
resolves any (lat, lon) to the nearest known place in milliseconds. No
network calls.
"""

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def _searcher() -> Any:
    # Built-in cities CSV is loaded lazily; first call costs a few seconds and
    # ~250 MB RAM, then queries are O(log N) on a kd-tree.
    import reverse_geocoder

    return reverse_geocoder.RGeocoder(mode=1, verbose=False)


def reverse_lookup(lat: float, lon: float) -> dict[str, str] | None:
    """Return {city, country_code} for the nearest known place, or None."""
    try:
        rg = _searcher()
        results = rg.query([(lat, lon)])
    except Exception:
        return None
    if not results:
        return None
    r = results[0]
    out: dict[str, str] = {}
    name = (r.get("name") or "").strip()
    cc = (r.get("cc") or "").strip().upper()
    region = (r.get("admin1") or "").strip()
    if name:
        out["city"] = name
    if region:
        out["region"] = region
    if cc:
        out["country_code"] = cc
    return out or None


def to_tags(geo: dict[str, str]) -> list[tuple[str, float]]:
    """Geo dict → tag tuples ready for `Store.replace_image_tags`.

    Score is 1.0 — these are facts, not predictions.
    """
    tags: list[tuple[str, float]] = []
    if city := geo.get("city"):
        tags.append((city.lower(), 1.0))
    if cc := geo.get("country_code"):
        tags.append((cc.lower(), 1.0))
    return tags
