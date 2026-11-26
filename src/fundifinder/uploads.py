"""Upload validation and storage.

Checks run in this order: size, magic bytes, extension agrees with content, pixel budget,
the image really decodes. Accepted images are re-encoded, which drops EXIF/GPS metadata, and
stored under a random name. Private files (ID documents, quote photos) live in a directory
that is never mapped to a public URL.
"""

from __future__ import annotations

import io
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PIXELS = 25_000_000
MAX_SIDE = 1600
NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp)$")


def _is_png(b: bytes) -> bool:
    return b.startswith(b"\x89PNG\r\n\x1a\n")


def _is_jpg(b: bytes) -> bool:
    return b.startswith(b"\xff\xd8\xff")


def _is_webp(b: bytes) -> bool:
    return b[:4] == b"RIFF" and b[8:12] == b"WEBP"


_MAGIC: dict[str, Callable[[bytes], bool]] = {"png": _is_png, "jpg": _is_jpg, "webp": _is_webp}
_EXT_ALIASES = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp"}
_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
_PIL_FORMAT = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}


class UploadError(ValueError):
    """Raised with a message that is safe to show to the user."""


@dataclass(frozen=True)
class ValidatedImage:
    kind: str  # png | jpg | webp
    width: int
    height: int

    @property
    def mime(self) -> str:
        return _MIME[self.kind]


def sniff_kind(data: bytes) -> str | None:
    for kind, check in _MAGIC.items():
        if check(data):
            return kind
    return None


def validate_image(data: bytes, filename: str | None, max_bytes: int) -> ValidatedImage:
    if not data:
        raise UploadError("The file is empty.")
    if len(data) > max_bytes:
        raise UploadError(f"The file is too large (limit {max_bytes // (1024 * 1024)} MB).")
    kind = sniff_kind(data)
    if kind is None:
        raise UploadError("Only PNG, JPEG or WebP images are accepted.")
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if ext and _EXT_ALIASES.get(ext) != kind:
        raise UploadError("The file extension does not match the file contents.")
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            if width * height > MAX_PIXELS:
                raise UploadError("The image has too many pixels.")
            img.verify()
    except UploadError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise UploadError("The file is not a valid image.") from None
    return ValidatedImage(kind=kind, width=width, height=height)


def sanitise_image(data: bytes, kind: str) -> bytes:
    """Re-encode: applies EXIF rotation, drops all metadata, caps the longest side."""
    with Image.open(io.BytesIO(data)) as opened:
        img: Image.Image = ImageOps.exif_transpose(opened)
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        if kind == "jpg" and img.mode != "RGB":
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format=_PIL_FORMAT[kind])
        return out.getvalue()


class Storage:
    """File storage with two zones: ``public`` (served by the app) and ``private``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._zones = {"public": root / "public", "private": root / "private"}
        for zone in self._zones.values():
            zone.mkdir(parents=True, exist_ok=True)
        self._zones["private"].chmod(0o700)

    def save(self, zone: str, data: bytes, kind: str) -> str:
        name = f"{secrets.token_hex(16)}.{kind}"
        (self._zones[zone] / name).write_bytes(data)
        return name

    def path(self, zone: str, name: str) -> Path | None:
        """Resolve a stored name; None unless it matches the strict generated-name pattern."""
        if not NAME_RE.match(name):
            return None
        candidate = self._zones[zone] / name
        return candidate if candidate.is_file() else None

    def delete(self, zone: str, name: str) -> None:
        path = self.path(zone, name)
        if path:
            path.unlink(missing_ok=True)


def mime_for(name: str) -> str:
    return _MIME.get(name.rsplit(".", 1)[-1], "application/octet-stream")


def store_upload(
    storage: Storage, zone: str, data: bytes, filename: str | None, max_bytes: int
) -> str:
    """Validate, sanitise and store an uploaded image; returns the stored name."""
    info = validate_image(data, filename, max_bytes)
    return storage.save(zone, sanitise_image(data, info.kind), info.kind)
