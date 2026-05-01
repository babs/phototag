"""Tests for the EXIF extractor."""

from pathlib import Path

import pytest
from PIL import Image

from phototag.exif import _parse_exif_dt, _to_decimal, _to_jsonable, extract_exif


def test_to_jsonable_basics() -> None:
    assert _to_jsonable(None) is None
    assert _to_jsonable(1) == 1
    assert _to_jsonable("x") == "x"
    assert _to_jsonable(b"abc\x00") == "abc"
    assert _to_jsonable([1, 2, b"hi"]) == [1, 2, "hi"]
    assert _to_jsonable({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}


def test_to_decimal_gps() -> None:
    # 48° 53' 6.6" N → 48.885166...
    assert _to_decimal((48, 53, 6.6), "N") == 48 + 53 / 60 + 6.6 / 3600
    # Negative for S/W
    assert _to_decimal((48, 53, 6.6), "S") == -(48 + 53 / 60 + 6.6 / 3600)
    # Degenerate
    assert _to_decimal(None, "N") is None
    assert _to_decimal((1,), "N") is None


def test_parse_exif_dt() -> None:
    assert _parse_exif_dt("2024:01:15 13:45:30") == "2024-01-15T13:45:30"
    assert _parse_exif_dt("2024-01-15T13:45:30") == "2024-01-15T13:45:30"
    assert _parse_exif_dt("garbage") == "garbage"
    assert _parse_exif_dt(None) is None
    assert _parse_exif_dt("") is None


def test_extract_exif_no_metadata(tmp_path: Path) -> None:
    p = tmp_path / "plain.jpg"
    Image.new("RGB", (10, 10), (255, 0, 0)).save(p, format="JPEG")
    # Plain JPEG has no useful EXIF — should return None.
    assert extract_exif(p) is None


def test_extract_exif_with_piexif(tmp_path: Path) -> None:
    piexif = pytest.importorskip("piexif")
    p = tmp_path / "tagged.jpg"
    img = Image.new("RGB", (20, 20), (0, 255, 0))
    exif_dict = {
        "0th": {
            piexif.ImageIFD.Make: b"TestCam",
            piexif.ImageIFD.Model: b"Model 1",
            piexif.ImageIFD.Orientation: 1,
        },
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: b"2024:06:15 10:00:00",
            piexif.ExifIFD.FNumber: (28, 10),
            piexif.ExifIFD.ISOSpeedRatings: 100,
        },
        "GPS": {},
    }
    exif_bytes = piexif.dump(exif_dict)
    img.save(p, format="JPEG", exif=exif_bytes)

    exif = extract_exif(p)
    assert exif is not None
    assert exif["make"] == "TestCam"
    assert exif["model"] == "Model 1"
    assert exif["datetime_original"] == "2024-06-15T10:00:00"
    assert exif["f_number"] == 2.8
    assert exif["iso"] == 100
