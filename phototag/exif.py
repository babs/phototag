"""EXIF / metadata extraction.

Returns a JSON-friendly dict with the fields that are actually useful for
clustering / search / report (date, camera, lens, exposure, GPS).
Unknown or empty fields are omitted.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image
from PIL.ExifTags import GPSTAGS

# IFD pointers within the main EXIF block.
_EXIF_IFD = 0x8769  # ExifIFDPointer
_GPS_IFD = 0x8825  # GPSInfoIFDPointer


def _to_jsonable(v: Any) -> Any:
    if isinstance(v, bytes):
        try:
            return v.decode("ascii", "ignore").strip("\x00").strip() or None
        except Exception:
            return None
    if isinstance(v, str):
        return v.strip("\x00").strip() or None
    if isinstance(v, bool | int | float) or v is None:
        return v
    if isinstance(v, list | tuple):
        return [_to_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _to_jsonable(x) for k, x in v.items()}
    # Pillow IFDRational (and similar) — coerce to float.
    try:
        return float(v)  # type: ignore[arg-type]
    except Exception:
        return str(v)


def _rational(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _to_decimal(coord: Any, ref: Any) -> float | None:
    if not coord or len(coord) != 3:
        return None
    d, m, s = (_rational(x) for x in coord)
    if d is None or m is None or s is None:
        return None
    val = d + m / 60.0 + s / 3600.0
    if isinstance(ref, str) and ref.upper() in ("S", "W"):
        val = -val
    return val


def _parse_exif_dt(s: Any) -> str | None:
    if not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return s or None


def extract_exif(path: Path) -> dict[str, Any] | None:
    """Return useful EXIF fields, or None if the image has no EXIF."""
    try:
        img = Image.open(path)
    except Exception:
        return None
    try:
        exif = img.getexif()
    except Exception:
        return None
    if not exif:
        return None

    named: dict[str, Any] = {}
    for tag_id, val in exif.items():
        name = ExifTags.TAGS.get(tag_id, str(tag_id))
        named[name] = _to_jsonable(val)

    # ExifIFD (where DateTimeOriginal lives, plus exposure data).
    try:
        exif_ifd = exif.get_ifd(_EXIF_IFD)
    except Exception:
        exif_ifd = None
    if exif_ifd:
        for tag_id, val in exif_ifd.items():
            name = ExifTags.TAGS.get(tag_id, str(tag_id))
            named[name] = _to_jsonable(val)

    gps: dict[str, Any] = {}
    try:
        gps_ifd = exif.get_ifd(_GPS_IFD)
    except Exception:
        gps_ifd = None
    if gps_ifd:
        for tag_id, val in gps_ifd.items():
            gps[GPSTAGS.get(tag_id, str(tag_id))] = _to_jsonable(val)

    out: dict[str, Any] = {}
    out["make"] = named.get("Make")
    out["model"] = named.get("Model")
    out["software"] = named.get("Software")
    out["orientation"] = named.get("Orientation")
    out["datetime_original"] = _parse_exif_dt(named.get("DateTimeOriginal") or named.get("DateTime"))
    out["exposure_time"] = named.get("ExposureTime")
    out["f_number"] = named.get("FNumber")
    out["iso"] = named.get("ISOSpeedRatings") or named.get("PhotographicSensitivity")
    out["focal_length"] = named.get("FocalLength")
    out["lens"] = named.get("LensModel")

    if gps:
        lat = _to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
        lon = _to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
        alt = _rational(gps.get("GPSAltitude"))
        if lat is not None and lon is not None:
            entry: dict[str, Any] = {"lat": lat, "lon": lon}
            if alt is not None:
                entry["altitude"] = alt
            out["gps"] = entry

    cleaned = {k: v for k, v in out.items() if v not in (None, "", [])}
    return cleaned or None
