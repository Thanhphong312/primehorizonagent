"""Tải ảnh (mockup, file thiết kế, ảnh ticket) và thu nhỏ trước khi gửi AI."""

from __future__ import annotations

import base64
import io
import logging
import re
import threading
from dataclasses import dataclass

import requests
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# File in POD rất lớn (vd 4500×5400 RGBA ≈ 100 MB khi giải nén, chuyển mode lại ×2). Nhiều ảnh giải nén CÙNG LÚC
# (2 lượt phân tích + ảnh thu nhỏ trên trang quản lý) từng làm service vượt MemoryMax ⇒ bị OOM-kill giữa lượt.
# ⇒ giải nén tuần tự từng ảnh; tải mạng vẫn song song.
_DECODE_LOCK = threading.BoundedSemaphore(1)
MAX_PIXELS = 120_000_000          # > 120 MP coi như file lỗi / bom giải nén
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

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
               session: requests.Session | None = None, jpeg_only: bool = False) -> LoadedImage:
    """``jpeg_only``: luôn nén JPEG (nền trong suốt ⇒ nền xám nhạt) — cho gateway tính tiền theo dung lượng ảnh."""
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

    raw = buf.getvalue()
    del buf
    with _DECODE_LOCK:
        return _encode(url, raw, max_px=max_px, jpeg_only=jpeg_only)


def _encode(url: str, raw: bytes, *, max_px: int, jpeg_only: bool) -> LoadedImage:
    try:
        img = Image.open(io.BytesIO(raw))
        if img.width * img.height > MAX_PIXELS:
            raise ImageLoadError(f"Ảnh quá lớn ({img.width}×{img.height})")
        if img.format == "JPEG":
            img.draft("RGB", (max_px, max_px))      # JPEG: giải nén thẳng ở cỡ nhỏ, tốn ít RAM
        img.thumbnail((max_px, max_px))             # thu nhỏ TRƯỚC khi xoay / đổi mode để đỡ RAM
        img = ImageOps.exif_transpose(img)
    except ImageLoadError:
        raise
    except Exception as exc:  # noqa: BLE001 — mọi lỗi decode đều là "không phải ảnh"
        raise ImageLoadError(f"File không phải ảnh hợp lệ: {exc}") from exc

    out = io.BytesIO()
    # PNG giữ nền trong suốt của file thiết kế (nền trong suốt quyết định "in gì");
    # JPEG cho mockup / ảnh chụp để nhẹ.
    if jpeg_only:
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (225, 225, 225))   # xám nhạt: chữ trắng / đen đều còn thấy
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
        img.convert("RGB").save(out, format="JPEG", quality=70, optimize=True)
        media = "image/jpeg"
    elif img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        img.save(out, format="PNG", optimize=True)
        media = "image/png"
    else:
        img.convert("RGB").save(out, format="JPEG", quality=85)
        media = "image/jpeg"
    return LoadedImage(url=url, media_type=media, data_b64=base64.standard_b64encode(out.getvalue()).decode(),
                       width=img.width, height=img.height)
