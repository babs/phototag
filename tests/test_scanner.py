from pathlib import Path

from PIL import Image

from phototag.scanner import hash_file, iter_images


def _make_jpg(path: Path, color: tuple[int, int, int] = (255, 0, 0)) -> None:
    Image.new("RGB", (8, 8), color).save(path, format="JPEG")


def test_iter_images_filters_extensions(tmp_path: Path) -> None:
    _make_jpg(tmp_path / "a.jpg")
    _make_jpg(tmp_path / "b.JPG")
    (tmp_path / "c.txt").write_text("not an image")
    sub = tmp_path / "sub"
    sub.mkdir()
    _make_jpg(sub / "d.jpg")

    found = sorted(s.path.name.lower() for s in iter_images(tmp_path))
    assert found == ["a.jpg", "b.jpg", "d.jpg"]


def test_hash_file_deterministic(tmp_path: Path) -> None:
    p = tmp_path / "x.jpg"
    _make_jpg(p, (0, 255, 0))
    h1 = hash_file(p)
    h2 = hash_file(p)
    assert h1 == h2
    assert len(h1) == 16

    q = tmp_path / "y.jpg"
    _make_jpg(q, (0, 0, 255))
    assert hash_file(q) != h1
