"""Claude (Anthropic SDK) — ảnh base64 + structured output (``output_config.format``)."""

from __future__ import annotations

import json
from typing import Any

from app.providers.base import (Part, ProviderError, ProviderResult, check_result_shape, parse_json_loose,
                                schema_instruction)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings, client=None):
        self.settings = settings
        if client is None:
            if not settings.anthropic_api_key:
                raise ProviderError("Chưa có Anthropic API key — nhập trên trang quản lý PrimeAgent")
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key,
                                         base_url=settings.anthropic_base_url or None,
                                         timeout=float(settings.ai_timeout), max_retries=2)
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

    def _fix_json(self, text: str, schema: dict[str, Any], err: Exception) -> dict[str, Any]:
        """Tự sửa không được ⇒ gửi RIÊNG đoạn chữ hỏng (không ảnh, ~vài nghìn token) để model viết lại cho đúng."""
        try:
            with self.client.beta.messages.stream(
                model=self.settings.anthropic_model, max_tokens=8000,
                system="Sửa đoạn sau thành JSON hợp lệ, giữ nguyên nội dung, escape nháy kép trong chuỗi."
                       + schema_instruction(schema),
                messages=[{"role": "user", "content": text}],
            ) as stream:
                resp = stream.get_final_message()
            fixed = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            return parse_json_loose(fixed)
        except Exception as exc2:  # noqa: BLE001
            raise ProviderError(f"AI trả JSON không hợp lệ: {err} — sửa lại cũng lỗi ({type(exc2).__name__}). "
                                f"Trả lời gốc: {text[:1500]}") from exc2

    def analyze(self, system: str, parts: list[Part], schema: dict[str, Any]) -> ProviderResult:
        gateway = bool(getattr(self.settings, "anthropic_base_url", ""))
        extra: dict[str, Any] = {}
        if not gateway:
            # Bị bộ lọc an toàn từ chối thì máy chủ tự chạy lại trên model dự phòng phù hợp.
            # Chỉ API chính chủ — gateway/proxy thường không nhận beta này.
            extra = {"betas": [_FALLBACK_BETA], "fallbacks": "default"}
        else:
            # Gateway (vd miraiapi) nhận output_config nhưng KHÔNG ép schema ⇒ ghi schema vào prompt.
            system = system + schema_instruction(schema)
        params = dict(
            model=self.settings.anthropic_model,
            max_tokens=16000,
            **extra,
            system=system,
            output_config={"effort": self.settings.anthropic_effort,
                           "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": self._content(parts)}],
        )
        # Streaming: lượt có nhiều ảnh chạy 1–3 phút; không stream thì Cloudflare của gateway cắt ở ~100s (524)
        # dù vẫn tính token. Gom lại thành message hoàn chỉnh bằng get_final_message().
        with self.client.beta.messages.stream(**params) as stream:
            resp = stream.get_final_message()
        if resp.stop_reason == "refusal":
            raise ProviderError("AI từ chối phân tích lượt này")
        if resp.stop_reason == "max_tokens":
            raise ProviderError("AI trả lời vượt giới hạn token")
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        try:
            data = parse_json_loose(text) if gateway else json.loads(text)
        except json.JSONDecodeError as exc:
            if not gateway:
                raise ProviderError(f"AI trả JSON không hợp lệ: {exc}") from exc
            data = self._fix_json(text, schema, exc)
        if gateway:
            check_result_shape(data)
        usage = getattr(resp, "usage", None)
        return ProviderResult(
            data=data,
            model=getattr(resp, "model", self.settings.anthropic_model),
            usage={"input_tokens": getattr(usage, "input_tokens", None),
                   "output_tokens": getattr(usage, "output_tokens", None)} if usage else {},
            request_id=getattr(resp, "_request_id", None),
        )
