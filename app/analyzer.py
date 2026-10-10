"""Phân tích một lượt fulfill: kiểm bằng code → gom ảnh → hỏi AI → ghép kết quả."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app import garment, schema
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
CHỈ so CHỮ: "decoded" của sku (hoặc mã sku nếu không tra được) với biến thể khách chọn trong đơn (size / màu / \
kiểu áo). Size trên Etsy thường ghi kèm loại áo (vd "Comfort Tshirt 2XL", "Hoodie 2XL", "Sweatshirt Crew XL"). \
Mã kiểu Gildan hay gặp: 5000/64000/3001/1717 = áo thun, 18000 = sweatshirt, 18500 = hoodie. \
⚠️ KHÔNG dùng ảnh mockup / ảnh listing để kết luận loại áo hay màu: shop thường dùng CHUNG một mockup (vd áo thun \
trắng) cho mọi biến thể của listing, nên mockup khác loại / khác màu với đơn là BÌNH THƯỜNG. Màu gần nghĩa (vd \
"Sand" ~ "Tan") là warn, khác hẳn là error. SIZE khác size khách đặt (vd đơn L mà sku M) là error, không phải warn.
2. design — file thiết kế có đúng mẫu so với mockup/ảnh đơn không: cùng HÌNH IN, cùng chữ (bỏ qua loại áo / màu \
áo trên mockup). Khác mockup đúng ở chỗ ticket yêu cầu sửa (vd đã xoá dòng chữ theo ticket) ⇒ vẫn ok; nội dung cá nhân hoá \
(tên, năm, chữ…) phải xuất hiện ĐÚNG CHÍNH TẢ trong file thiết kế. File thiết kế là hình in phẳng (thường nền \
trong suốt); nếu "file thiết kế" lại là ảnh chụp sản phẩm/mockup (có áo, cốc, ốp, người mẫu…) còn "mockup" lại là \
hình phẳng ⇒ hai ô bị dán ngược ⇒ error.
3. ticket — mọi yêu cầu trong ticket đã được thực hiện chưa (đổi màu chữ, sửa tên, xoá chữ, đổi size…). Không có \
ticket ⇒ ok với reason "Không có ticket". Yêu cầu về NỘI DUNG IN (xoá / thêm / sửa chữ, đổi màu chữ…) ⇒ chỉ kết luận \
bằng cách NHÌN LẠI CHÍNH ẢNH FILE THIẾT KẾ (ảnh có nhãn "File thiết kế"), đọc từng dòng chữ có trên đó. TUYỆT ĐỐI \
không suy từ mockup, ảnh listing hay tên sản phẩm: mockup listing là bản GỐC trước khi sửa nên vẫn còn nội dung cũ \
— điều đó là bình thường. Chỉ chấm error khi chính ảnh file thiết kế còn nội dung lẽ ra phải bỏ (hoặc thiếu nội \
dung lẽ ra phải thêm); reason ghi rõ đã thấy gì trên file thiết kế.
4. print_side — các mặt có in trên mockup (trước / sau / tay áo…) có khớp các mặt có file thiết kế không; mặt \
in phải thuộc danh sách mặt in được phép của sku nếu có.

Mức chấm: ok = khớp; warn = nghi ngờ / cần người xem lại; error = sai CHẮC CHẮN, phải huỷ hoặc sửa — nếu \
reason của bạn có chữ "nghi vấn", "có thể", "cần xem lại" hoặc chính dữ liệu chữ lại khớp thì KHÔNG được chấm \
error (tối đa warn); \
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


def _final_instruction(bundle: dict[str, Any]) -> str:
    idx = [it.get("index") for it in bundle.get("sent_items") or []]
    return (f"Chấm 4 mục cho từng item đã gửi theo đúng mẫu JSON. Có {len(idx)} item đã gửi (index: "
            f"{', '.join(map(str, idx))}) ⇒ mảng items phải có ĐÚNG {len(idx)} phần tử, mỗi index đúng một lần — "
            "kể cả khi các item trông giống hệt nhau (cùng sản phẩm / cùng mockup) thì vẫn chấm riêng từng item.")


def analyze(bundle: dict[str, Any], provider: Provider, settings, *, loader=load_image) -> dict[str, Any]:
    """Trả về payload kết quả gửi về backend (``POST /api/agent/fulfillments/<id>/result``)."""
    t0 = time.monotonic()
    findings = run_rules(bundle)

    plan = _image_plan(bundle)
    skipped = plan[settings.max_images:]
    labels: dict[str, str] = {}
    parts_images: list[Part] = []
    failed: list[dict[str, str]] = []
    base_url = settings.openai_base_url if settings.ai_provider == "openai" else settings.anthropic_base_url
    img_kw: dict[str, Any] = {"max_px": settings.image_max_px}
    if base_url and settings.gateway_image_px:
        img_kw = {"max_px": min(settings.image_max_px, settings.gateway_image_px), "jpeg_only": True}
    # File thiết kế của các item là ÁO đã biết màu ⇒ đo màu phần có in ngay lúc giải nén (không tải lại).
    contrast_urls = {d.get("url") for it in bundle.get("sent_items") or [] if garment.garment_of(it)
                     for d in it.get("designs") or [] if d.get("url")}
    design_stats: dict[str, Any] = {}
    for url, desc in plan[: settings.max_images]:
        kw = dict(img_kw, inspect=garment.design_colors) if url in contrast_urls else img_kw
        try:
            img = loader(url, max_bytes=settings.max_download_mb * 1024 * 1024, timeout=settings.http_timeout,
                         **kw)
        except ImageLoadError as exc:
            failed.append({"url": url, "desc": desc, "error": str(exc)})
            continue
        if getattr(img, "stats", None):
            design_stats[url] = img.stats
        n = len(parts_images) + 1
        labels[url] = f"[Ảnh #{n}]"
        parts_images.append(Part(text=f"[Ảnh #{n}] {desc}"))
        parts_images.append(Part(image=img))
    findings += garment.contrast_findings(bundle.get("sent_items") or [], design_stats)
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
             Part(text=_final_instruction(bundle))]

    res = provider.analyze(SYSTEM_PROMPT, parts, schema.AI_RESULT_SCHEMA)
    sent_by_idx = {it.get("index"): it for it in bundle.get("sent_items") or []}
    items = []
    got: set = set()
    for it in res.data.get("items") or []:
        if it.get("index") in got or it.get("index") not in sent_by_idx:
            continue       # trùng / lạ ⇒ bỏ (item thiếu sẽ bị báo bên dưới)
        got.add(it.get("index"))
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
