"""Cài đặt AI sửa trên trang quản lý — lưu ``<data_dir>/settings.json`` (quyền 600).

Mỗi job đọc lại (``effective``) nên đổi key / provider / model có hiệu lực ngay, không cần restart.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import replace
from typing import Any

EDITABLE = ("ai_provider", "anthropic_api_key", "anthropic_model", "anthropic_effort", "anthropic_base_url",
            "openai_api_key", "openai_model", "openai_base_url", "image_max_px", "max_images", "gateway_image_px")
INT_FIELDS = {"image_max_px": (128, 2576), "max_images": (1, 30), "gateway_image_px": (0, 2576)}
SECRET_FIELDS = ("anthropic_api_key", "openai_api_key")
PROVIDERS = ("anthropic", "openai")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def mask(v: str | None) -> str:
    v = v or ""
    return "" if not v else (v[:6] + "…" + v[-4:] if len(v) > 14 else "••••")


class Store:
    def __init__(self, data_dir: str):
        self.dir = data_dir
        self.path = os.path.join(data_dir, "settings.json")
        self._lock = threading.Lock()

    def _ensure_dir(self) -> None:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if k in EDITABLE and isinstance(v, str)}
        except (FileNotFoundError, ValueError):
            return {}

    def effective(self, base):
        """Settings env + phần đã lưu trên trang (chuỗi rỗng = dùng env)."""
        over: dict[str, Any] = {k: (int(v) if k in INT_FIELDS else v) for k, v in self.load().items() if v}
        return replace(base, **over) if over else base

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """``None`` = giữ nguyên; ``""`` = xoá (quay về env). Giá trị lạ ⇒ ValueError."""
        clean: dict[str, str] = {}
        for k, v in (changes or {}).items():
            if k not in EDITABLE or v is None:
                continue
            v = str(v).strip()
            if k == "ai_provider" and v not in PROVIDERS:
                raise ValueError("ai_provider phải là anthropic hoặc openai")
            if k == "anthropic_effort" and v and v not in EFFORTS:
                raise ValueError(f"effort phải là một trong {', '.join(EFFORTS)}")
            if k in INT_FIELDS and v:
                lo, hi = INT_FIELDS[k]
                if not v.isdigit() or not lo <= int(v) <= hi:
                    raise ValueError(f"{k} phải là số trong khoảng {lo}–{hi}")
            if k.endswith("_base_url") and v and not v.startswith(("https://", "http://")):
                raise ValueError("Base URL phải bắt đầu bằng https://")
            clean[k] = v
        with self._lock:
            data = self.load()
            data.update(clean)
            data = {k: v for k, v in data.items() if v}
            self._ensure_dir()
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        return data

    def public(self, base) -> dict[str, Any]:
        """Cho trang quản lý — key chỉ hiện dạng che."""
        eff = self.effective(base)
        saved = self.load()
        out: dict[str, Any] = {}
        for k in EDITABLE:
            v = getattr(eff, k)
            v = str(v) if k in INT_FIELDS else v
            src = "page" if saved.get(k) else ("env" if v else "")
            out[k] = {"value": mask(v) if k in SECRET_FIELDS else v, "set": bool(v), "source": src}
        return out

    def secret_key(self) -> str:
        """Khoá ký cookie phiên đăng nhập — tự sinh lần đầu, giữ qua restart."""
        path = os.path.join(self.dir, "secret_key")
        with self._lock:
            try:
                with open(path, encoding="utf-8") as f:
                    key = f.read().strip()
                if key:
                    return key
            except FileNotFoundError:
                pass
            self._ensure_dir()
            key = secrets.token_hex(32)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(key)
            return key
