"""Gọi API của server PrimeHorizon (backend-etsy) — lấy gói dữ liệu, gửi kết quả."""

from __future__ import annotations

from typing import Any

import requests


class PrimeClient:
    def __init__(self, settings, session: requests.Session | None = None):
        if not settings.prime_api_base or not settings.prime_service_key:
            raise RuntimeError("Thiếu PRIME_API_BASE hoặc PRIME_SERVICE_KEY")
        self.base = settings.prime_api_base
        self.timeout = settings.http_timeout
        self.http = session or requests.Session()
        self.http.headers.update({"X-Service-Key": settings.prime_service_key, "Accept": "application/json"})

    def get_bundle(self, fulfillment_id: int) -> dict[str, Any]:
        r = self.http.get(f"{self.base}/api/agent/fulfillments/{fulfillment_id}", timeout=self.timeout)
        r.raise_for_status()
        return (r.json() or {}).get("data") or {}

    def post_result(self, fulfillment_id: int, payload: dict[str, Any]) -> None:
        r = self.http.post(f"{self.base}/api/agent/fulfillments/{fulfillment_id}/result",
                           json=payload, timeout=self.timeout)
        r.raise_for_status()
