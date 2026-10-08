"""Cấu hình PrimeAgent — đọc toàn bộ từ biến môi trường (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Server PrimeHorizon (backend-etsy) — nơi lấy gói dữ liệu và gửi kết quả về.
    prime_api_base: str = field(default_factory=lambda: os.getenv("PRIME_API_BASE", "").rstrip("/"))
    # Key PrimeAgent dùng khi GỌI backend (header X-Service-Key).
    prime_service_key: str = field(default_factory=lambda: os.getenv("PRIME_SERVICE_KEY", ""))
    # Key backend dùng khi GỌI PrimeAgent (header X-Agent-Key). Hai key tách riêng:
    # lộ một chiều không mở được chiều kia.
    agent_inbound_key: str = field(default_factory=lambda: os.getenv("AGENT_INBOUND_KEY", ""))

    # "anthropic" | "openai" — đổi qua lại để so sánh độ chính xác trên đơn thật.
    ai_provider: str = field(default_factory=lambda: (os.getenv("AI_PROVIDER") or "anthropic").strip().lower())
    anthropic_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5"))
    anthropic_effort: str = field(default_factory=lambda: os.getenv("ANTHROPIC_EFFORT", "medium"))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", ""))

    # Số đơn phân tích cùng lúc — giữ nhỏ để không dồn tải / chi phí.
    concurrency: int = field(default_factory=lambda: _int("AGENT_CONCURRENCY", 2))
    # Cạnh dài tối đa của ảnh gửi AI (px). Ảnh thiết kế gốc 13–21 MB được thu nhỏ trước khi gửi.
    image_max_px: int = field(default_factory=lambda: _int("IMAGE_MAX_PX", 1568))
    # Giới hạn số ảnh / lượt và dung lượng tải mỗi ảnh.
    max_images: int = field(default_factory=lambda: _int("MAX_IMAGES", 16))
    max_download_mb: int = field(default_factory=lambda: _int("MAX_DOWNLOAD_MB", 40))
    http_timeout: int = field(default_factory=lambda: _int("HTTP_TIMEOUT", 60))
    ai_timeout: int = field(default_factory=lambda: _int("AI_TIMEOUT", 300))


def get_settings() -> Settings:
    return Settings()
