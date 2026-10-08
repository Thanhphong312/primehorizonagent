"""Giao diện chung cho nhà cung cấp AI — đổi Claude ⇄ GPT chỉ bằng ``AI_PROVIDER``."""

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
