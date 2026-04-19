"""
Подготовка фото перед отправкой на сервер: ориентация EXIF, уменьшение по длинной стороне, JPEG.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

DEFAULT_MAX_SIDE = 1024
DEFAULT_JPEG_QUALITY = 88


def _resample_lanczos() -> int:
    try:
        return Image.Resampling.LANCZOS  # Pillow 10+
    except AttributeError:
        return Image.LANCZOS


def prepare_photo_for_upload(
    data: bytes,
    max_side: int = DEFAULT_MAX_SIDE,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> bytes:
    """
    Поворачивает по EXIF, приводит к RGB, уменьшает так, чтобы max(w,h) <= max_side,
    сохраняет JPEG с оптимизацией.
    """
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)

    if im.mode == "P":
        im = im.convert("RGBA")
    if im.mode == "RGBA":
        bg = Image.new("RGB", im.size, (255, 255, 255))
        alpha = im.split()[3] if len(im.split()) == 4 else None
        bg.paste(im, mask=alpha)
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")

    w, h = im.size
    if w > 0 and h > 0 and max(w, h) > max_side:
        im.thumbnail((max_side, max_side), _resample_lanczos())

    out = io.BytesIO()
    im.save(
        out,
        format="JPEG",
        quality=jpeg_quality,
        optimize=True,
        progressive=True,
    )
    return out.getvalue()
