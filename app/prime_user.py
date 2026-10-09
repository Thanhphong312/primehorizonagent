"""Gọi backend PrimeHorizon BẰNG TÀI KHOẢN người đăng nhập trang quản lý (JWT).

Khác ``PrimeClient`` (service key, chỉ lấy gói dữ liệu / gửi kết quả): mọi quyền xem danh sách,
xem chi tiết, bấm phân tích đều do backend kiểm tra theo role của chính người đó.
"""

from __future__ import annotations

from typing import Any

import requests

ALLOWED_ROLES = {"admin", "fulfill"}


class PrimeAuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def _message(resp: requests.Response) -> str:
    try:
        return resp.json().get("message") or f"HTTP {resp.status_code}"
    except ValueError:
        return f"HTTP {resp.status_code}"


class PrimeUserClient:
    def __init__(self, settings, http=None):
        self.base = settings.prime_api_base
        self.timeout = settings.http_timeout
        self.http = http or requests

    def login(self, username: str, password: str) -> dict[str, Any]:
        """→ ``{access_token, refresh_token, user}``. Chỉ admin / fulfill được vào."""
        r = self.http.post(f"{self.base}/api/auth/login", json={"username": username, "password": password},
                           timeout=self.timeout)
        if r.status_code != 200:
            raise PrimeAuthError(_message(r), 401)
        data = r.json().get("data") or {}
        user = data.get("user") or {}
        if (user.get("role") or "").lower() not in ALLOWED_ROLES:
            raise PrimeAuthError("Chỉ tài khoản admin hoặc fulfill được dùng trang này", 403)
        return {"access_token": data.get("access_token"), "refresh_token": data.get("refresh_token"),
                "user": {"id": user.get("id"), "username": user.get("username"),
                         "name": user.get("full_name") or user.get("name") or user.get("username"),
                         "role": user.get("role")}}

    def _refresh(self, tokens: dict[str, Any]) -> bool:
        if not tokens.get("refresh_token"):
            return False
        r = self.http.post(f"{self.base}/api/auth/refresh",
                           headers={"Authorization": f"Bearer {tokens['refresh_token']}"}, timeout=self.timeout)
        if r.status_code != 200:
            return False
        data = r.json().get("data") or {}
        tokens["access_token"] = data.get("access_token")
        tokens["refresh_token"] = data.get("refresh_token") or tokens["refresh_token"]
        return bool(tokens["access_token"])

    def call(self, method: str, path: str, tokens: dict[str, Any], **kw) -> tuple[int, dict[str, Any]]:
        """Gọi backend; 401 ⇒ làm mới token 1 lần (``tokens`` được cập nhật tại chỗ).
        Token hết hẳn ⇒ PrimeAuthError (trang đưa về màn đăng nhập)."""
        url = f"{self.base}{path}"
        for attempt in (1, 2):
            r = self.http.request(method, url, headers={"Authorization": f"Bearer {tokens.get('access_token')}"},
                                  timeout=self.timeout, **kw)
            if r.status_code == 401 and attempt == 1 and self._refresh(tokens):
                continue
            if r.status_code == 401:
                raise PrimeAuthError("Phiên đăng nhập hết hạn — đăng nhập lại")
            try:
                body = r.json()
            except ValueError:
                body = {"success": False, "message": f"HTTP {r.status_code}"}
            return r.status_code, body
        raise PrimeAuthError("Phiên đăng nhập hết hạn — đăng nhập lại")
