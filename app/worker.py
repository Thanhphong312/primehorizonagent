"""Hàng chờ trong tiến trình: nhận job → phân tích nền với số luồng cố định.

Hàng chờ nằm trong RAM — mất khi restart là CHẤP NHẬN ĐƯỢC: backend tự gửi lại các lượt
còn ``agent_analysis = 1`` quá hạn, nên không cần thêm Redis/DB ở phía này.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from app.analyzer import analyze, failure_payload
from app.images import load_image
from app.prime_client import PrimeClient
from app.providers.base import ProviderError, get_provider

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings, *, client=None, provider=None, loader=load_image, store=None):
        self.settings = settings
        self.store = store
        self._client = client
        self._provider = provider
        self._loader = loader
        self._pool = ThreadPoolExecutor(max_workers=max(1, settings.concurrency), thread_name_prefix="agent")
        self._lock = threading.Lock()
        self._inflight: set[int] = set()

    @property
    def client(self):
        if self._client is None:
            self._client = PrimeClient(self.settings)
        return self._client

    def current_settings(self):
        """Env + cài đặt trên trang quản lý — đọc lại mỗi job nên đổi key/model có hiệu lực ngay."""
        return self.store.effective(self.settings) if self.store else self.settings

    def submit(self, fulfillment_id: int) -> bool:
        """False nếu lượt này đang được phân tích (bỏ job trùng)."""
        with self._lock:
            if fulfillment_id in self._inflight:
                return False
            self._inflight.add(fulfillment_id)
        self._pool.submit(self._run, fulfillment_id)
        return True

    def inflight(self) -> int:
        with self._lock:
            return len(self._inflight)

    def _run(self, fulfillment_id: int) -> None:
        try:
            self.process(fulfillment_id)
        finally:
            with self._lock:
                self._inflight.discard(fulfillment_id)

    def process(self, fulfillment_id: int) -> dict:
        try:
            bundle = self.client.get_bundle(fulfillment_id)
        except Exception as exc:  # noqa: BLE001
            # Không lấy được dữ liệu ⇒ KHÔNG gửi kết quả: backend sẽ tự gửi lại job sau.
            logger.warning("bundle %s: %s", fulfillment_id, exc)
            return {}
        settings = self.current_settings()
        name = getattr(self._provider, "name", None) or settings.ai_provider
        try:
            provider = self._provider or get_provider(settings)
            payload = analyze(bundle, provider, settings, loader=self._loader)
        except ProviderError as exc:
            payload = failure_payload(str(exc), name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("analyze %s", fulfillment_id)
            payload = failure_payload(f"{type(exc).__name__}: {exc}", name)
        try:
            self.client.post_result(fulfillment_id, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("post_result %s: %s", fulfillment_id, exc)
        return payload
