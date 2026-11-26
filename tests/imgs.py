from __future__ import annotations

import io

from PIL import Image


def image_bytes(fmt: str = "PNG", size: tuple[int, int] = (40, 30), exif: bool = False) -> bytes:
    img = Image.new("RGB", size, (200, 80, 40))
    out = io.BytesIO()
    kwargs = {}
    if exif and fmt == "JPEG":
        e = Image.Exif()
        e[0x010F] = "SecretCameraMaker"
        e[0x8825] = {1: "S", 2: (1.0, 2.0, 3.0)}
        kwargs["exif"] = e
    img.save(out, format=fmt, **kwargs)
    return out.getvalue()
