"""GPT (OpenAI SDK, Responses API) — để so sánh với Claude trên đơn thật.

⚠️ Tên model đặt bằng ``OPENAI_MODEL`` (bắt buộc), không đoán mặc định.
"""

from __future__ import annotations

import json
from typing import Any

from app.providers.base import Part, ProviderError, ProviderResult


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings, client=None):
        if not settings.openai_model:
            raise ProviderError("Thiếu OPENAI_MODEL")
        self.settings = settings
        if client is None:
            if not settings.openai_api_key:
                raise ProviderError("Chưa có OpenAI API key — nhập trên trang quản lý PrimeAgent")
            import openai

            client = openai.OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url or None,
                                   timeout=float(settings.ai_timeout), max_retries=2)
        self.client = client

    def _content(self, parts: list[Part]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in parts:
            if p.image is not None:
                out.append({"type": "input_image",
                            "image_url": f"data:{p.image.media_type};base64,{p.image.data_b64}"})
            elif p.text:
                out.append({"type": "input_text", "text": p.text})
        return out

    def analyze(self, system: str, parts: list[Part], schema: dict[str, Any]) -> ProviderResult:
        resp = self.client.responses.create(
            model=self.settings.openai_model,
            instructions=system,
            input=[{"role": "user", "content": self._content(parts)}],
            text={"format": {"type": "json_schema", "name": "fulfill_check", "schema": schema, "strict": True}},
        )
        text = getattr(resp, "output_text", "") or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"AI trả JSON không hợp lệ: {exc}") from exc
        usage = getattr(resp, "usage", None)
        return ProviderResult(
            data=data,
            model=getattr(resp, "model", self.settings.openai_model),
            usage={"input_tokens": getattr(usage, "input_tokens", None),
                   "output_tokens": getattr(usage, "output_tokens", None)} if usage else {},
            request_id=getattr(resp, "id", None),
        )
