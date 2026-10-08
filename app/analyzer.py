"""Phân tích một lượt fulfill: kiểm bằng code → gom ảnh → hỏi AI → ghép kết quả."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app import schema
from app.images import ImageLoadError, load_image
from app.providers.base import Part, Provider, ProviderError
from app.rules import run_rules

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Bạn là nhân viên QC của xưởng POD (print-on-demand). Nhiệm vụ: kiểm tra một lượt FULFILL \
(đơn đã gửi sang nhà cung cấp in) có đúng với đơn của khách không, để kịp huỷ trước khi hàng được in.

Dữ liệu gồm:
- ĐƠN CỦA KHÁCH: tên sản phẩm, biến thể (màu, size, kiểu áo), cá nhân hoá, ghi chú, ảnh listing/mockup của đơn.
- ITEM ĐÃ GỬI: sku, số lượng, phần "decoded" là sku đã được tra ra (sản phẩm, màu, size, kiểu in, các mặt in \
được phép) — tin phần này hơn là tự đoán từ chuỗi sku; các file thiết kế theo từng mặt in; mockup đã gửi.
- TICKET: yêu cầu chỉnh sửa / lưu ý của khách hoặc nội bộ cho đơn này (kể cả ticket đã đóng).

Với MỖI item đã gửi, chấm 4 mục:
1. product_color — loại sản phẩm (áo thun / hoodie / sweatshirt / mug…), màu và size của sku có khớp đơn không. \
Size trên Etsy thường ghi kèm loại áo (vd "Comfort Tshirt 2XL", "Sweatshirt Crew XL"). Màu gần nghĩa (vd \
"Sand" ~ "Tan") là warn, khác hẳn là error.
2. design — file thiết kế có đúng mẫu so với mockup/ảnh đơn không: cùng hình, cùng chữ; nội dung cá nhân hoá \
(tên, năm, chữ…) phải xuất hiện ĐÚNG CHÍNH TẢ trong file thiết kế. File thiết kế là hình in phẳng (thường nền \
trong suốt); nếu "file thiết kế" lại là ảnh chụp sản phẩm/mockup (có áo, cốc, ốp, người mẫu…) còn "mockup" lại là \
hình phẳng ⇒ hai ô bị dán ngược ⇒ error.
3. ticket — mọi yêu cầu trong ticket đã được thực hiện chưa (đổi màu chữ, sửa tên, đổi size…). Không có ticket \
⇒ ok với reason "Không có ticket".
4. print_side — các mặt có in trên mockup (trước / sau / tay áo…) có khớp các mặt có file thiết kế không; mặt \
in phải thuộc danh sách mặt in được phép của sku nếu có.

Mức chấm: ok = khớp; warn = nghi ngờ / cần người xem lại; error = sai chắc chắn, phải huỷ hoặc sửa; \
unknown = thiếu dữ liệu để kết luận (vd không có mockup, ảnh không tải được). KHÔNG đoán — thiếu dữ liệu thì \
unknown. reason viết tiếng Việt, ngắn, nêu cụ thể sai gì (vd "Mockup in mặt sau nhưng không có file Back").
summary: 1–2 câu tiếng Việt tóm tắt cả lượt. Trả về đúng index của từng item đã gửi."""


def _strip_images(bundle: dict[str, Any], labels: dict[str, str]) -> dict[str, Any]:
    """Bản dữ liệu dạng chữ cho AI: thay mọi URL ảnh bằng nhãn "[Ảnh #n]"."""

    def lab(url: str) -> str:
        return labels.get(url, "[ảnh không tải được]")

    order = dict(bundle.get("order") or {})
    order["items"] = [{**{k: v for k, v in it.items() if k != "images"},
                       "images": [lab(u) for u in it.get("images") or []]} for it in order.get("items") or []]
    sent = [{**{k: v for k, v in it.items() if k not in ("designs", "mockups")},
             "designs": [{"area": d.get("area"), "file": lab(d.get("url") or "")} for d in it.get("designs") or []],
             "mockups": [lab(u) for u in it.get("mockups") or []]} for it in bundle.get("sent_items") or []]
    tickets = []
    for t in bundle.get("tickets") or []:
        tickets.append({**{k: v for k, v in t.items() if k not in ("attachments", "comments")},
                        "attachments": [lab(u) for u in t.get("attachments") or []],
                        "comments": [{**{k: v for k, v in c.items() if k != "attachments"},
                                      "attachments": [lab(u) for u in c.get("attachments") or []]}
                                     for c in t.get("comments") or []]})
    return {"platform": bundle.get("platform"), "supplier": bundle.get("supplier"),
            "order": order, "sent_items": sent, "tickets": tickets}


def _image_plan(bundle: dict[str, Any]) -> list[tuple[str, str]]:
    """(url, mô tả) theo thứ tự ưu tiên — cắt theo MAX_IMAGES thì mất phần ít quan trọng nhất."""
    plan: list[tuple[str, str]] = []
    for it in bundle.get("sent_items") or []:
        for u in it.get("mockups") or []:
            plan.append((u, f"Mockup đã gửi — item #{it.get('index')}"))
        for d in it.get("designs") or []:
            if d.get("url"):
                plan.append((d["url"], f"File thiết kế mặt {d.get('area')} — item #{it.get('index')}"))
    for it in (bundle.get("order") or {}).get("items") or []:
        for u in (it.get("images") or [])[:1]:
            plan.append((u, f"Ảnh đơn của khách — dòng {it.get('index')}"))
    for t in bundle.get("tickets") or []:
        for u in t.get("attachments") or []:
            plan.append((u, f"Ảnh đính kèm ticket #{t.get('id')}"))
        for c in t.get("comments") or []:
            for u in c.get("attachments") or []:
                plan.append((u, f"Ảnh trong trao đổi ticket #{t.get('id')}"))
    seen: set[str] = set()
    uniq = []
    for u, d in plan:
        if u and u not in seen:
            seen.add(u)
            uniq.append((u, d))
    return uniq


def analyze(bundle: dict[str, Any], provider: Provider, settings, *, loader=load_image) -> dict[str, Any]:
    """Trả về payload kết quả gửi về backend (``POST /api/agent/fulfillments/<id>/result``)."""
    t0 = time.monotonic()
    findings = run_rules(bundle)

    plan = _image_plan(bundle)
    skipped = plan[settings.max_images:]
    labels: dict[str, str] = {}
    parts_images: list[Part] = []
    failed: list[dict[str, str]] = []
    for url, desc in plan[: settings.max_images]:
        try:
            img = loader(url, max_px=settings.image_max_px, max_bytes=settings.max_download_mb * 1024 * 1024,
                         timeout=settings.http_timeout)
        except ImageLoadError as exc:
            failed.append({"url": url, "desc": desc, "error": str(exc)})
            continue
        n = len(parts_images) + 1
        labels[url] = f"[Ảnh #{n}]"
        parts_images.append(Part(text=f"[Ảnh #{n}] {desc}"))
        parts_images.append(Part(image=img))
    if failed:
        findings.append({"index": None, "check": "general", "status": "warn",
                         "reason": f"{len(failed)} ảnh không tải được — AI kiểm thiếu dữ liệu"})
    if skipped:
        findings.append({"index": None, "check": "general", "status": "warn",
                         "reason": f"Bỏ qua {len(skipped)} ảnh vì vượt giới hạn {settings.max_images} ảnh/lượt"})

    text = _strip_images(bundle, labels)
    rules_text = json.dumps(findings, ensure_ascii=False) if findings else "(không có)"
    parts = [Part(text="DỮ LIỆU LƯỢT FULFILL (JSON):\n" + json.dumps(text, ensure_ascii=False, indent=1)),
             Part(text="Phát hiện của bước kiểm bằng code (đã chắc chắn, không cần chấm lại):\n" + rules_text),
             *parts_images,
             Part(text="Chấm 4 mục cho từng item đã gửi theo đúng mẫu JSON.")]

    res = provider.analyze(SYSTEM_PROMPT, parts, schema.AI_RESULT_SCHEMA)
    sent_by_idx = {it.get("index"): it for it in bundle.get("sent_items") or []}
    items = []
    for it in res.data.get("items") or []:
        checks = {k: it["checks"][k] for k in schema.CHECK_KEYS if k in (it.get("checks") or {})}
        items.append({"index": it.get("index"), "sku": (sent_by_idx.get(it.get("index")) or {}).get("sku"),
                      "checks": checks})
    missing = [i for i in sent_by_idx if i not in {x["index"] for x in items}]
    if missing:
        findings.append({"index": None, "check": "general", "status": "warn",
                         "reason": f"AI không trả kết quả cho item {', '.join(map(str, missing))}"})

    return {
        "status_code": schema.overall_code(items, findings),
        "provider": provider.name,
        "model": res.model,
        "summary": res.data.get("summary") or "",
        "items": items,
        "rule_findings": findings,
        "images_used": len(parts_images) // 2,
        "images_failed": failed,
        "usage": res.usage,
        "request_id": res.request_id,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }


def failure_payload(error: str, provider_name: str | None = None) -> dict[str, Any]:
    return {"status_code": schema.FAILED, "provider": provider_name, "error": error[:1000]}


__all__ = ["analyze", "failure_payload", "SYSTEM_PROMPT", "ProviderError"]
