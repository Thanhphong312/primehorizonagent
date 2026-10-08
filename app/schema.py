"""Mẫu JSON cố định AI phải trả về + quy đổi sang mã ``agent_analysis`` (int) của order."""

from __future__ import annotations

from typing import Any

# Mã trạng thái lưu trên order (``agent_analysis``) — PHẢI khớp backend-etsy.
PENDING = 1
OK = 2
WARN = 3
ERROR = 4
FAILED = 9

CHECK_KEYS = ("product_color", "design", "ticket", "print_side")
CHECK_LABELS = {
    "product_color": "Loại sản phẩm & màu/size",
    "design": "Design so với mockup",
    "ticket": "Yêu cầu trong ticket",
    "print_side": "Mặt in",
}
VERDICTS = ("ok", "warn", "error", "unknown")

_CHECK = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": list(VERDICTS)},
        "reason": {"type": "string"},
    },
    "required": ["status", "reason"],
    "additionalProperties": False,
}

AI_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "checks": {
                        "type": "object",
                        "properties": {k: _CHECK for k in CHECK_KEYS},
                        "required": list(CHECK_KEYS),
                        "additionalProperties": False,
                    },
                },
                "required": ["index", "checks"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["items", "summary"],
    "additionalProperties": False,
}

_RANK = {"ok": 0, "unknown": 1, "warn": 2, "error": 3}


def worst(verdicts: list[str]) -> str:
    return max(verdicts, key=lambda v: _RANK.get(v, 1)) if verdicts else "unknown"


def overall_code(items: list[dict[str, Any]], rule_findings: list[dict[str, Any]]) -> int:
    """Mã tổng của lượt fulfill.

    ``unknown`` (AI không đủ dữ liệu để kết luận) tính là CẢNH BÁO, không phải OK: "không
    kiểm được" mà hiện xanh thì người xem sẽ tưởng đã kiểm xong.
    """
    verdicts = [c["status"] for it in items for c in it.get("checks", {}).values()]
    verdicts += [f["status"] for f in rule_findings]
    w = worst(verdicts)
    if w == "error":
        return ERROR
    if w in ("warn", "unknown"):
        return WARN
    return OK
