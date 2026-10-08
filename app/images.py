"""Tải ảnh (mockup, file thiết kế, ảnh ticket) và thu nhỏ trước khi gửi AI."""

from __future__ import annotations

import base64
import io
import logging
import re
from dataclasses import dataclass

import requests
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

_DRIVE_ID_RE = re.compile(r"(?:/file/d/|[?&]id=)([A-Za-z0-9_-]{10,})")


def drive_thumbnail(url: str, size_px: int) -> str:
    """Link Google Drive → link thumbnail ``sz=w<size>`` (vài trăm KB thay vì file gốc 13–21 MB).

    Link khác giữ nguyên.
    """
    if "drive.google.com" not in url and "docs.google.com" not in url:
        return url
    m = _DRIVE_ID_RE.search(url)
    if not m:
        return url
    return f"https://drive.google.com/thumbnail?id={m.group(1)}&sz=w{size_px}"


@dataclass
class LoadedImage:
    url: str
    media_type: str
    data_b64: str
    width: int
    height: int


class ImageLoadError(Exception):
    pass


def load_image(url: str, *, max_px: int, max_bytes: int, timeout: int,
               session: requests.Session | None = None) -> LoadedImage:
    src = drive_thumbnail(url, max_px)
    http = session or requests
    try:
        resp = http.get(src, timeout=timeout, stream=True, headers={"User-Agent": "PrimeAgent/1.0"})
        resp.raise_for_status()
        buf = io.BytesIO()
        for chunk in resp.iter_content(64 * 1024):
            buf.write(chunk)
            if buf.tell() > max_bytes:
                raise ImageLoadError(f"Ảnh quá lớn (> {max_bytes // (1024 * 1024)} MB)")
    except requests.RequestException as exc:
        raise ImageLoadError(f"Không tải được ảnh: {exc}") from exc

    try:
        img = Image.open(io.BytesIO(buf.getvalue()))
        img = ImageOps.exif_transpose(img)
    except Exception as exc:  # noqa: BLE001 — mọi lỗi decode đều là "không phải ảnh"
        raise ImageLoadError(f"File không phải ảnh hợp lệ: {exc}") from exc

    img.thumbnail((max_px, max_px))
    out = io.BytesIO()
    # PNG giữ nền trong suốt của file thiết kế (nền trong suốt quyết định "in gì");
    # JPEG cho mockup / ảnh chụp để nhẹ.
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        img.save(out, format="PNG", optimize=True)
        media = "image/png"
    else:
        img.convert("RGB").save(out, format="JPEG", quality=85)
        media = "image/jpeg"
    return LoadedImage(url=url, media_type=media, data_b64=base64.standard_b64encode(out.getvalue()).decode(),
                       width=img.width, height=img.height)
