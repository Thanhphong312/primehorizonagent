"""Hàng chờ trong tiến trình: nhận job → phân tích nền với số luồng cố định.

Hàng chờ nằm trong RAM — mất khi restart là CHẤP NHẬN ĐƯỢC: backend tự gửi lại các lượt
còn ``agent_analysis = 1`` quá hạn, nên không cần thêm Redis/DB ở phía này.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.analyzer import analyze, failure_payload
from app.images import load_image
from app.prime_client import PrimeClient
from app.providers.base import ProviderError, get_provider

logger = logging.getLogger(__name__)


def _friendly_error(exc: Exception, settings) -> str:
    """Lỗi SDK ⇒ câu dễ hiểu (vẫn giữ chi tiết gốc phía sau)."""
    raw = f"{type(exc).__name__}: {exc}"
    status = getattr(exc, "status_code", None)
    base_url = settings.openai_base_url if settings.ai_provider == "openai" else settings.anthropic_base_url
    if status == 401 or type(exc).__name__ == "AuthenticationError":
        hint = ("Key miraiapi không hợp lệ hoặc đã hết quota/hết hạn — nạp mã redeem mới trên trang quản lý"
                if "miraiapi.com" in (base_url or "") else "API key sai hoặc đã bị thu hồi — sửa trên trang quản lý")
        return f"{hint}. ({raw[:300]})"
    if status == 429:
        return f"AI đang giới hạn tốc độ / hết hạn mức — thử lại sau. ({raw[:300]})"
    if status in (502, 503, 504, 524, 529):
        return f"Máy chủ AI quá tải / quá thời gian — bấm Phân tích lại sau. ({raw[:300]})"
    return raw


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

    def _check_expiry(self, settings) -> None:
        """Key miraiapi gói theo ngày: hết hạn ⇒ báo rõ thay vì lỗi 401 khó hiểu từ gateway."""
        from app import mirai

        if not self.store or settings.ai_provider != "anthropic" or not mirai.is_mirai(settings.anthropic_base_url):
            return
        exp = mirai.Meta(self.store.dir).load().get("expires_at")
        if exp and time.time() > float(exp):
            when = time.strftime("%d/%m %H:%M", time.localtime(float(exp)))
            raise ProviderError(f"Key miraiapi đã hết hạn lúc {when} — nạp mã redeem mới trên trang quản lý")

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
            self._check_expiry(settings)
            provider = self._provider or get_provider(settings)
            payload = analyze(bundle, provider, settings, loader=self._loader)
        except ProviderError as exc:
            payload = failure_payload(str(exc), name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("analyze %s", fulfillment_id)
            payload = failure_payload(_friendly_error(exc, settings), name)
        try:
            self.client.post_result(fulfillment_id, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("post_result %s: %s", fulfillment_id, exc)
        return payload
