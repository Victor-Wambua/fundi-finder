from __future__ import annotations

import io

import pytest
from PIL import Image

from fundifinder.uploads import (
    Storage,
    UploadError,
    mime_for,
    sanitise_image,
    sniff_kind,
    store_upload,
    validate_image,
)
from imgs import image_bytes

LIMIT = 5 * 1024 * 1024


@pytest.mark.parametrize(
    ("fmt", "name", "kind"),
    [
        ("PNG", "a.png", "png"),
        ("JPEG", "a.jpg", "jpg"),
        ("JPEG", "a.JPEG", "jpg"),
        ("WEBP", "a.webp", "webp"),
    ],
)
def test_valid_images_are_accepted(fmt, name, kind):
    info = validate_image(image_bytes(fmt), name, LIMIT)
    assert info.kind == kind and (info.width, info.height) == (40, 30)


def test_missing_filename_is_fine_when_content_is_valid():
    assert validate_image(image_bytes(), None, LIMIT).kind == "png"


def test_empty_file_rejected():
    with pytest.raises(UploadError, match="empty"):
        validate_image(b"", "a.png", LIMIT)


def test_oversize_rejected_before_anything_else():
    with pytest.raises(UploadError, match="too large"):
        validate_image(b"x" * 101, "a.png", 100)


@pytest.mark.parametrize(
    "payload",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"<?php echo 1;",
        b"%PDF-1.4 fake",
        b"GIF89a....",
        b"MZ\x90\x00",
    ],
)
def test_non_images_rejected_by_magic_bytes(payload):
    with pytest.raises(UploadError, match="PNG, JPEG or WebP"):
        validate_image(payload, "photo.png", LIMIT)


def test_extension_must_match_contents():
    with pytest.raises(UploadError, match="extension"):
        validate_image(image_bytes("PNG"), "photo.jpg", LIMIT)
    with pytest.raises(UploadError, match="extension"):
        validate_image(image_bytes("JPEG"), "photo.png", LIMIT)


def test_valid_header_with_garbage_body_rejected():
    with pytest.raises(UploadError, match="not a valid image"):
        validate_image(b"\x89PNG\r\n\x1a\n" + b"garbage" * 50, "a.png", LIMIT)


def test_truncated_image_rejected():
    data = image_bytes("PNG", (200, 200))
    with pytest.raises(UploadError):
        validate_image(data[: len(data) // 2], "a.png", LIMIT)


def test_pixel_bomb_rejected(monkeypatch):
    monkeypatch.setattr("fundifinder.uploads.MAX_PIXELS", 100)
    with pytest.raises(UploadError, match="too many pixels"):
        validate_image(image_bytes("PNG", (50, 50)), "a.png", LIMIT)


def test_sniffing():
    assert sniff_kind(image_bytes("WEBP")) == "webp"
    assert sniff_kind(b"nothing") is None


def test_sanitise_strips_exif_and_gps():
    original = image_bytes("JPEG", exif=True)
    assert b"SecretCameraMaker" in original
    cleaned = sanitise_image(original, "jpg")
    assert b"SecretCameraMaker" not in cleaned
    with Image.open(io.BytesIO(cleaned)) as img:
        assert not img.getexif()


def test_sanitise_caps_longest_side():
    big = image_bytes("PNG", (3200, 100))
    with Image.open(io.BytesIO(sanitise_image(big, "png"))) as img:
        assert max(img.size) == 1600


def test_storage_uses_random_names_and_strict_lookup(tmp_path):
    storage = Storage(tmp_path)
    name = store_upload(storage, "private", image_bytes(), "evil/../../name.png", LIMIT)
    assert "evil" not in name and name.endswith(".png")
    assert storage.path("private", name) is not None
    assert storage.path("public", name) is None


@pytest.mark.parametrize(
    "bad", ["../etc/passwd", "..%2f..%2fx.png", "a.png", "x" * 32 + ".svg", "/abs.png", ""]
)
def test_storage_rejects_traversal_and_unknown_names(tmp_path, bad):
    storage = Storage(tmp_path)
    assert storage.path("private", bad) is None


def test_private_zone_is_not_world_readable(tmp_path):
    Storage(tmp_path)
    assert (tmp_path / "private").stat().st_mode & 0o077 == 0


def test_delete_removes_the_file(tmp_path):
    storage = Storage(tmp_path)
    name = store_upload(storage, "public", image_bytes(), "a.png", LIMIT)
    storage.delete("public", name)
    assert storage.path("public", name) is None


def test_mime_for():
    assert mime_for("a.png") == "image/png" and mime_for("a.jpg") == "image/jpeg"
    assert mime_for("a.exe") == "application/octet-stream"
