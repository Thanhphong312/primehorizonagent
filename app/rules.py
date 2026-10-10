"""Kiểm tra bằng code — các lỗi CHẮC CHẮN, không cần AI (nhanh, miễn phí, luôn đúng).

Mỗi phát hiện: ``{"index": int|None, "check": <CHECK_KEYS>|"general", "status": "warn"|"error", "reason": str}``.
``index`` là số thứ tự item ĐÃ GỬI (``sent_items[].index``); ``None`` = cả lượt fulfill.
"""

from __future__ import annotations

from typing import Any


CATALOG_TYPES = {"sellerwix", "simpleprint", "printbelle", "generic", "pineliner_pod"}


def _norm(s: str | None) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


# Đường dẫn ảnh mockup/ảnh listing — file in (design) KHÔNG bao giờ nằm ở đây.
# Hay gặp khi dán nhầm ô "file in" và ô "mockup" (vd Anprint: resource_url ↔ mockup_url).
_MOCKUP_HINTS = ("/media/mockups/", "etsystatic.com", "il_fullxfull", "ttcdn", "tiktokcdn")


def _looks_like_mockup(url: str | None) -> bool:
    u = (url or "").lower()
    return any(h in u for h in _MOCKUP_HINTS)


def run_rules(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    sent = bundle.get("sent_items") or []
    order_items = (bundle.get("order") or {}).get("items") or []

    if not sent:
        out.append({"index": None, "check": "general", "status": "error",
                    "reason": "Payload gửi nhà cung cấp không có item nào"})
        return out

    # Chỉ các nhà cung cấp backend tra được catalog mới cảnh báo "không tra được sku"; nhà khác (Onospod,
    # Pressify…) chưa tra catalog ⇒ cảnh báo này lượt nào cũng hiện, chỉ gây nhiễu — để AI tự đọc sku.
    has_catalog = ((bundle.get("supplier") or {}).get("type") or "") in CATALOG_TYPES
    seen: dict[tuple, int] = {}
    for it in sent:
        idx = it.get("index")
        dec = it.get("decoded")
        designs = it.get("designs") or []
        if not dec and has_catalog:
            out.append({"index": idx, "check": "product_color", "status": "warn",
                        "reason": f"Không tra được sku {it.get('sku') or '(trống)'} trong catalog nhà cung cấp"})
        if not designs:
            out.append({"index": idx, "check": "design", "status": "error",
                        "reason": "Item không có file thiết kế nào"})
        mockups = set(it.get("mockups") or [])
        for d in designs:
            if d.get("url") in mockups:
                out.append({"index": idx, "check": "design", "status": "error",
                            "reason": f"File in mặt {d.get('area')} trùng link với ảnh mockup — dán nhầm ô?"})
            elif _looks_like_mockup(d.get("url")):
                out.append({"index": idx, "check": "design", "status": "warn",
                            "reason": f"File in mặt {d.get('area')} có vẻ là ảnh mockup/ảnh listing, không phải file "
                                      "thiết kế — kiểm tra có dán nhầm ô file in và ô mockup không"})
        allowed = (dec or {}).get("allowed_areas")
        if allowed:
            allowed_n = {_norm(a) for a in allowed}
            bad = [d.get("area") for d in designs if _norm(d.get("area")) not in allowed_n]
            if bad:
                out.append({"index": idx, "check": "print_side", "status": "error",
                            "reason": f"Mặt in {', '.join(map(str, bad))} không có trong danh sách mặt in của sku "
                                      f"({', '.join(allowed)})"})
        if int(it.get("quantity") or 0) <= 0:
            out.append({"index": idx, "check": "general", "status": "error", "reason": "Số lượng ≤ 0"})
        key = (it.get("sku"), tuple(sorted((d.get("area") or "", d.get("url") or "") for d in designs)))
        if key in seen:
            out.append({"index": idx, "check": "general", "status": "warn",
                        "reason": f"Trùng hoàn toàn với item #{seen[key]} (cùng sku và file thiết kế) — có thể gửi lặp"})
        else:
            seen[key] = idx

    from app.garment import size_findings
    out += size_findings(sent, order_items)

    # Lượt fulfill có thể chỉ gồm một phần đơn (tách nhiều nhà cung cấp, gửi lại 1 item…)
    # nên lệch tổng số lượng là CẢNH BÁO, không phải lỗi.
    sent_qty = sum(int(it.get("quantity") or 0) for it in sent)
    order_qty = sum(int(it.get("quantity") or 0) for it in order_items)
    if order_items and sent_qty != order_qty:
        out.append({"index": None, "check": "general", "status": "warn",
                    "reason": f"Tổng số lượng gửi đi ({sent_qty}) khác tổng số lượng đơn ({order_qty})"})
    return out
