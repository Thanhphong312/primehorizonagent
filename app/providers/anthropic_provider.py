"""Claude (Anthropic SDK) — ảnh base64 + structured output (``output_config.format``)."""

from __future__ import annotations

import json
from typing import Any

from app.providers.base import Part, ProviderError, ProviderResult

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings, client=None):
        self.settings = settings
        if client is None:
            import anthropic

            # ANTHROPIC_API_KEY đọc từ môi trường.
            client = anthropic.Anthropic(timeout=float(settings.ai_timeout), max_retries=2)
        self.client = client

    def _content(self, parts: list[Part]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in parts:
            if p.image is not None:
                out.append({"type": "image", "source": {"type": "base64", "media_type": p.image.media_type,
                                                        "data": p.image.data_b64}})
            elif p.text:
                out.append({"type": "text", "text": p.text})
        return out

    def analyze(self, system: str, parts: list[Part], schema: dict[str, Any]) -> ProviderResult:
        resp = self.client.beta.messages.create(
            model=self.settings.anthropic_model,
            max_tokens=16000,
            betas=[_FALLBACK_BETA],
            # Bị bộ lọc an toàn từ chối thì máy chủ tự chạy lại trên model dự phòng phù hợp.
            fallbacks="default",
            system=system,
            output_config={"effort": self.settings.anthropic_effort,
                           "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": self._content(parts)}],
        )
        if resp.stop_reason == "refusal":
            raise ProviderError("AI từ chối phân tích lượt này")
        if resp.stop_reason == "max_tokens":
            raise ProviderError("AI trả lời vượt giới hạn token")
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"AI trả JSON không hợp lệ: {exc}") from exc
        usage = getattr(resp, "usage", None)
        return ProviderResult(
            data=data,
            model=getattr(resp, "model", self.settings.anthropic_model),
            usage={"input_tokens": getattr(usage, "input_tokens", None),
                   "output_tokens": getattr(usage, "output_tokens", None)} if usage else {},
            request_id=getattr(resp, "_request_id", None),
        )
