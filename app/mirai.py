"""Nạp key AI từ mã redeem miraiapi (gói "Claude 10M tokens · 1D").

File/tin nhắn nhà bán gửi có dạng ``🎟 MÃ REDEEM CODE|MR-…|`` — chỉ cần tìm mã ``MR-…`` rồi gọi
``POST /api/redeem/new``. Đổi lại cùng một mã trả về đúng key cũ (``recovered: true``) nên bấm lại không mất gói.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

import requests

API = "https://api.miraiapi.com"
BASE_URL = "https://api.miraiapi.com"     # Claude SDK tự thêm /v1/messages
DEFAULT_MODEL = "claude-opus-5.5"
CODE_RE = re.compile(r"\bMR-[0-9A-Fa-f]{16,}\b")


class MiraiError(Exception):
    pass


def extract_code(text: str) -> str:
    codes = list(dict.fromkeys(CODE_RE.findall(text or "")))
    if not codes:
        raise MiraiError("Không tìm thấy mã redeem dạng MR-… trong nội dung")
    if len(codes) > 1:
        raise MiraiError(f"Có {len(codes)} mã khác nhau — mỗi lần nạp một mã")
    return codes[0]


def _post(path: str, body: dict[str, Any], http=None, timeout: int = 30) -> dict[str, Any]:
    r = (http or requests).post(f"{API}{path}", json=body, timeout=timeout)
    try:
        j = r.json()
    except ValueError:
        raise MiraiError(f"miraiapi trả HTTP {r.status_code}") from None
    if not j.get("success"):
        raise MiraiError(j.get("message") or j.get("error") or f"miraiapi trả HTTP {r.status_code}")
    return j.get("data") or {}


def redeem(code: str, http=None) -> dict[str, Any]:
    """→ ``{api_key, expires_at (epoch), quota_tokens, recovered}``."""
    data = _post("/api/redeem/new", {"redeem_code": code}, http)
    if not str(data.get("api_key") or "").startswith("sk-"):
        raise MiraiError("miraiapi không trả API key")
    return data


def usage(api_key: str, http=None) -> dict[str, Any]:
    """→ ``{quota_total, quota_remaining, requests_24h, error_rate_24h, expires_at}``."""
    return _post("/api/usage/check", {"api_key": api_key}, http)


def is_mirai(base_url: str | None) -> bool:
    return "miraiapi.com" in (base_url or "")


class Meta:
    """Thông tin gói đang dùng (mã đã che, hạn, quota) — ``data/mirai.json``."""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "mirai.json")
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
