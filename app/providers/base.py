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
