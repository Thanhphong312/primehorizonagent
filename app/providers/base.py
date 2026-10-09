"""Giao diện chung cho nhà cung cấp AI — đổi Claude ⇄ GPT trên trang quản lý (hoặc ``AI_PROVIDER``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.images import LoadedImage


@dataclass
class Part:
    """Một mảnh nội dung gửi AI: chữ HOẶC ảnh, theo đúng thứ tự trình bày."""

    text: str | None = None
    image: LoadedImage | None = None


@dataclass
class ProviderResult:
    data: dict[str, Any]
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None


def schema_instruction(schema: dict[str, Any]) -> str:
    """Gateway/proxy không ép được JSON schema ⇒ ghi schema vào prompt."""
    import json

    return ("\n\nĐỊNH DẠNG TRẢ LỜI (bắt buộc): chỉ trả về ĐÚNG MỘT object JSON, không markdown, không giải thích, "
            "đúng tên khoá theo JSON Schema sau:\n" + json.dumps(schema, ensure_ascii=False))


def parse_json_loose(text: str) -> dict[str, Any]:
    """JSON có thể bọc ```json … ``` hoặc kèm chữ trước/sau ⇒ lấy object ngoài cùng."""
    import json

    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            raise
        data = json.loads(t[a:b + 1])
    if not isinstance(data, dict):
        raise json.JSONDecodeError("không phải object", t, 0)
    return data


def check_result_shape(data: dict[str, Any]) -> None:
    """Kiểm tra tối thiểu khi không có structured output ép schema."""
    if not isinstance(data.get("items"), list) or not isinstance(data.get("summary", ""), str):
        raise ProviderError("AI trả JSON sai khuôn (thiếu items/summary)")
    for it in data["items"]:
        if not isinstance(it, dict) or not isinstance(it.get("checks"), dict):
            raise ProviderError("AI trả JSON sai khuôn (item thiếu checks)")


class ProviderError(Exception):
    """Lỗi không phục hồi được ở lượt này (từ chối, hết token, JSON hỏng…)."""


class Provider(Protocol):
    name: str

    def analyze(self, system: str, parts: list[Part], schema: dict[str, Any]) -> ProviderResult: ...


def get_provider(settings) -> Provider:
    if settings.ai_provider == "openai":
        from app.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(settings)
    from app.providers.anthropic_provider import AnthropicProvider

    return AnthropicProvider(settings)


def check_key(settings) -> dict[str, Any]:
    """Thử key bằng lệnh liệt kê model (không tốn token). Trả ``{ok, message, models?}``."""
    try:
        p = get_provider(settings)
        page = p.client.models.list()
        ids = [m.id for m in getattr(page, "data", [])][:50]
        return {"ok": True, "message": f"Key hợp lệ ({p.name})", "models": ids}
    except ProviderError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 — sai key / mạng / gateway không hỗ trợ
        return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:300]}"}
