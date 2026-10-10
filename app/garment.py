"""Kiểm màu thiết kế có bị TRÙNG màu áo không (in ra bị chìm) — chỉ cho các dòng áo, làm bằng code (không tốn token).

Cách làm: lấy màu áo từ sku đã tra ("decoded".color, hoặc tên màu nằm trong chuỗi sku) → bảng mã màu bên dưới;
trên file thiết kế (PNG nền trong suốt) đo bao nhiêu % phần CÓ IN có màu gần màu áo (ΔE Lab < ``NEAR_DE``).
File không có nền trong suốt ⇒ bỏ qua (không tách được đâu là hình in, đâu là nền).
"""

from __future__ import annotations

import re
from typing import Any

from PIL import Image

NEAR_DE = 15.0           # ΔE76 dưới ngưỡng này ⇒ mắt thường gần như không phân biệt được trên vải
WARN_SHARE = 0.30        # ≥ 30% diện tích hình in trùng màu áo ⇒ cảnh báo
SAMPLE_PX = 160          # đo trên ảnh thu nhỏ — đủ cho tỉ lệ diện tích, rẻ RAM / CPU
MAX_SAMPLES = 8000

# Tên màu (chữ thường) → RGB gần đúng. Gildan / Bella+Canvas / Comfort Colors / Next Level hay gặp.
COLOR_HEX: dict[str, str] = {
    "black": "25282a", "vintage black": "3a3a3a", "white": "ffffff", "navy": "263147", "true navy": "2b3446",
    "heather navy": "333f48", "red": "c8102e", "cherry red": "ac2b37", "antique cherry red": "971b2f",
    "cardinal": "8a1538", "cardinal red": "8a1538", "crimson": "a6333f", "maroon": "5b2b42", "royal": "224d8f",
    "royal blue": "224d8f", "true royal": "005bbb", "sport grey": "97999b", "sports grey": "97999b",
    "heather grey": "b5b5b5", "heather gray": "b5b5b5", "athletic heather": "b4b4b4", "deep heather": "8c8c8c",
    "ash": "c8c9c7", "ash grey": "c8c9c7", "ice grey": "d7d2cb", "silver": "c0c0c0", "dark heather": "425563",
    "dark grey heather": "4a4a4a", "dark grey": "4a4a4a", "charcoal": "4b4b4b", "graphite heather": "707372",
    "graphite": "4c4c4c", "granite": "7a7a76", "pepper": "5f605b", "asphalt": "52514f", "grey": "8d8d8d",
    "gray": "8d8d8d", "forest": "273b33", "forest green": "273b33", "military green": "5e7461", "army": "6a6b4c",
    "olive": "5e5b3a", "moss": "6d6e4e", "hemp": "6c6b48", "sage": "a2a98c", "bay": "b5bfa5", "irish green": "00a74a",
    "kelly": "00805e", "kelly green": "00805e", "safety green": "c6d219", "light green": "b5c9a3", "mint": "a0dab3",
    "chalky mint": "a5d8c6", "seafoam": "8fbcae", "island reef": "8ed1b9", "sand": "dcd2be", "natural": "e8e2d0",
    "ivory": "f3ead7", "cream": "f3ead7", "tan": "bd9a7a", "khaki": "c2b28f", "heather dust": "e5d9c9",
    "brown": "4a3a32", "dark chocolate": "382f2d", "espresso": "4a3a32", "light blue": "a4c8e1", "sky": "71c5e8",
    "carolina blue": "7ba4db", "chambray": "b8cbdc", "blue jean": "6a7f99", "washed denim": "8aa0b8",
    "denim": "5d7393", "indigo blue": "486d87", "lagoon blue": "6cc3d5", "flo blue": "7a8ed1", "light pink": "e4c6d4",
    "pink": "f2b9cd", "blossom": "f2c9d2", "azalea": "dd74a1", "heliconia": "db3e79", "safety pink": "ec80b4",
    "berry": "8e3a5c", "watermelon": "e46f78", "coral": "ff7f6f", "purple": "464e7e", "violet": "9a86b4",
    "orchid": "c9a7c7", "lavender": "a7a2c8", "orange": "f4633a", "safety orange": "f05a28", "burnt orange": "d9733a",
    "yam": "c3672b", "terracotta": "c2694a", "brick": "8f3b2b", "gold": "ffb81c", "mustard": "d9a441",
    "daisy": "fed141", "yellow": "fbe122", "butter": "f6e4a6", "banana": "f6e59a", "maize yellow": "f6d36b",
}

# Chỉ các dòng ÁO (vải) — cốc, ốp, poster… không kiểm.
_APPAREL_RE = re.compile(
    r"t-?shirt|\btees?\b|\bshirts?\b|hoodie|sweatshirt|crew ?neck|\btank\b|long ?sleeve|\bpolo\b|jersey|sweater"
    r"|onesie|bodysuit"
    # mã kiểu áo (Gildan / Comfort Colors / Bella) — ranh giới tự đặt vì "_" trong sku (vd G500_BLACK) dính \b
    r"|(?<![a-z0-9])(?:g?5000|g500|g?64000|g640|3001|3001cvc|c?1717|g?18000|g180|g?18500|g185|6400|1566|1567|2000"
    r"|3413|3480)(?![a-z0-9])", re.I)
_NON_APPAREL_RE = re.compile(r"\bmug\b|tumbler|phone ?case|poster|canvas|ornament|sticker|blanket|pillow|card\b",
                             re.I)


def _rgb(hex6: str) -> tuple[int, int, int]:
    return int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


def lookup_color(name: str | None) -> tuple[str, tuple[int, int, int]] | None:
    """Tên màu → (tên chuẩn, RGB). Không khớp chính xác thì lấy tên DÀI NHẤT nằm trong chuỗi (vd "Black Heather")."""
    n = _norm(name or "")
    if not n:
        return None
    if n in COLOR_HEX:
        return n, _rgb(COLOR_HEX[n])
    padded = f" {n} "
    best = max((k for k in COLOR_HEX if f" {k} " in padded), key=len, default=None)
    return (best, _rgb(COLOR_HEX[best])) if best else None


def is_apparel(item: dict[str, Any]) -> bool:
    dec = item.get("decoded") or {}
    text = " ".join(str(x) for x in (dec.get("product"), dec.get("variant_title"), dec.get("category"),
                                     item.get("sku")) if x)
    return bool(_APPAREL_RE.search(text)) and not _NON_APPAREL_RE.search(text)


def garment_of(item: dict[str, Any]) -> tuple[str, tuple[int, int, int]] | None:
    """Màu áo của một item đã gửi, hoặc None nếu không phải áo / không biết màu."""
    dec = item.get("decoded") or {}
    if not is_apparel(item):
        return None
    if dec.get("color"):
        return lookup_color(str(dec["color"]))
    # Chưa tra được sku: thử đọc tên màu ngay trong sku (vd "C1717-Black-2XL", "G500_SPORT_GREY_L").
    toks = [t for t in re.split(r"[^A-Za-z]+", str(item.get("sku") or "")) if t]
    for size in (3, 2, 1):
        for i in range(len(toks) - size + 1):
            hit = lookup_color(" ".join(toks[i:i + size]))
            if hit and _norm(" ".join(toks[i:i + size])) == hit[0]:
                return hit
    return None


# ─────────────────────────── đo màu trên file thiết kế ───────────────────────────

def _lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    def lin(c: float) -> float:
        c /= 255
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = (lin(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def design_colors(img: Image.Image) -> dict[str, Any]:
    """Gọi trong lúc giải nén ảnh (images._encode): rút gọn phần CÓ IN thành danh sách màu (RGB) để so sau."""
    small = img.copy()
    small.thumbnail((SAMPLE_PX, SAMPLE_PX))
    if small.mode not in ("RGBA", "LA", "P") and "transparency" not in small.info:
        return {"transparent": False}
    rgba = small.convert("RGBA")
    data = rgba.tobytes()                   # RGBA liền nhau (getdata() sắp bị bỏ ở Pillow 14)
    total = len(data) // 4
    opaque = [(data[i], data[i + 1], data[i + 2]) for i in range(0, len(data), 4) if data[i + 3] >= 128]
    if not total or len(opaque) > 0.98 * total:
        return {"transparent": False}        # "PNG" nhưng nền đặc ⇒ không tách được hình in
    step = max(1, len(opaque) // MAX_SAMPLES)
    return {"transparent": True, "pixels": opaque[::step]}


def near_share(stats: dict[str, Any] | None, garment_rgb: tuple[int, int, int]) -> float | None:
    """Tỉ lệ phần có in gần trùng màu áo; None = không đo được."""
    if not stats or not stats.get("transparent") or not stats.get("pixels"):
        return None
    gl = _lab(garment_rgb)
    cache: dict[tuple[int, int, int], bool] = {}
    near = 0
    for p in stats["pixels"]:
        q = (p[0] >> 2, p[1] >> 2, p[2] >> 2)         # gộp màu gần nhau để đỡ tính Lab lặp lại
        hit = cache.get(q)
        if hit is None:
            lab = _lab((q[0] << 2, q[1] << 2, q[2] << 2))
            hit = ((lab[0] - gl[0]) ** 2 + (lab[1] - gl[1]) ** 2 + (lab[2] - gl[2]) ** 2) ** 0.5 < NEAR_DE
            cache[q] = hit
        near += hit
    return near / len(stats["pixels"])


def contrast_findings(sent_items: list[dict[str, Any]], stats_by_url: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for it in sent_items:
        g = garment_of(it)
        if not g:
            continue
        name, rgb = g
        for d in it.get("designs") or []:
            share = near_share(stats_by_url.get(d.get("url") or ""), rgb)
            if share is not None and share >= WARN_SHARE:
                out.append({"index": it.get("index"), "check": "design", "status": "warn",
                            "reason": f"File in mặt {d.get('area')}: ~{round(share * 100)}% hình in gần trùng màu "
                                      f"áo {name.title()} — in ra dễ bị chìm, kiểm tra lại màu thiết kế / màu áo"})
    return out


# ─────────────────────────── size áo: gửi đi phải đúng size khách đặt ───────────────────────────

_SIZE_ALIASES = {
    "xs": "XS", "xsmall": "XS", "extrasmall": "XS", "s": "S", "small": "S", "m": "M", "medium": "M",
    "l": "L", "large": "L", "xl": "XL", "xlarge": "XL", "extralarge": "XL", "1x": "XL",
    "2xl": "2XL", "xxl": "2XL", "2x": "2XL", "xxlarge": "2XL", "2xlarge": "2XL",
    "3xl": "3XL", "xxxl": "3XL", "3x": "3XL", "xxxlarge": "3XL", "3xlarge": "3XL",
    "4xl": "4XL", "xxxxl": "4XL", "4x": "4XL", "5xl": "5XL", "5x": "5XL", "6xl": "6XL",
}


def norm_size(text: str | None) -> str | None:
    """"Comfort Tshirt L US letter" → "L", "2XL" → "2XL", "X-Large" → "XL". Không có / nhiều hơn 1 size ⇒ None."""
    t = (text or "").lower().replace("x-large", "xlarge").replace("extra large", "extralarge") \
        .replace("extra small", "extrasmall").replace("x-small", "xsmall")
    found = {_SIZE_ALIASES[w] for w in re.split(r"[^a-z0-9]+", t) if w in _SIZE_ALIASES}
    return found.pop() if len(found) == 1 else None


def size_findings(sent_items: list[dict[str, Any]], order_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mỗi size áo: số áo GỬI ĐI không được vượt số áo KHÁCH ĐẶT size đó ⇒ vượt = gửi sai size (lỗi chắc chắn).

    So theo số lượng từng size (không ghép từng dòng) nên đúng cả khi lượt fulfill chỉ gửi một phần đơn.
    Chỉ chạy khi mọi item áo đã gửi đều đọc được size (sku đã tra) và đơn có size đọc được.
    """
    sent = [(it, norm_size(str((it.get("decoded") or {}).get("size") or ""))) for it in sent_items if is_apparel(it)]
    if not sent or any(sz is None for _, sz in sent):
        return []
    ordered: dict[str, int] = {}
    for it in order_items:
        sz = norm_size(str(it.get("size") or ""))
        if sz:
            ordered[sz] = ordered.get(sz, 0) + int(it.get("quantity") or 0)
    if not ordered:
        return []
    sent_qty: dict[str, list] = {}
    for it, sz in sent:
        sent_qty.setdefault(sz, []).append(it)
    out = []
    order_txt = ", ".join(f"{k}×{v}" for k, v in sorted(ordered.items()))
    for sz, items in sent_qty.items():
        qty = sum(int(i.get("quantity") or 0) for i in items)
        if qty > ordered.get(sz, 0):
            idx = ", ".join(f"#{i.get('index')}" for i in items)
            out.append({"index": items[-1].get("index") if len(items) == 1 else None, "check": "product_color",
                        "status": "error",
                        "reason": f"Sai size: gửi {qty} áo size {sz} (item {idx}) nhưng đơn chỉ đặt "
                                  f"{ordered.get(sz, 0)} áo size {sz} — size khách đặt: {order_txt}"})
    return out
