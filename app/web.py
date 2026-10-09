"""Trang quản lý PrimeAgent: đăng nhập bằng tài khoản PrimeHorizon, cài đặt AI, lọc lượt fulfill, bấm phân tích.

Bảo mật:
  • Đăng nhập = ``POST /api/auth/login`` của backend; chỉ role admin / fulfill. Agent KHÔNG giữ mật khẩu.
  • Cookie phiên ký bằng ``data/secret_key`` (tự sinh), HttpOnly + Secure + SameSite=Strict.
  • Mọi request ghi (POST/PUT) phải có header ``X-Requested-With: primeagent`` (chống CSRF).
  • Sửa cài đặt AI (key) chỉ admin.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import socket
import threading
import time
from collections import OrderedDict
from datetime import timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, Response, current_app, jsonify, request, send_file, session

from app.images import ImageLoadError, load_image
from app.prime_user import PrimeAuthError, PrimeUserClient
from app.providers.base import check_key

ui_bp = Blueprint("ui", __name__)
STATIC = Path(__file__).parent / "static"

_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()
MAX_FAILS, FAIL_WINDOW = 5, 300


def _too_many_fails(ip: str) -> bool:
    now = time.time()
    with _fails_lock:
        recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = recent
        return len(recent) >= MAX_FAILS


def _record_fail(ip: str) -> None:
    with _fails_lock:
        _fails.setdefault(ip, []).append(time.time())


def _cfg(name):
    return current_app.config[name]


def _client() -> PrimeUserClient:
    return PrimeUserClient(_cfg("SETTINGS"))


def _err(message: str, status: int):
    return jsonify(ok=False, message=message), status


def login_required(admin: bool = False):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if request.method not in ("GET", "HEAD") and request.headers.get("X-Requested-With") != "primeagent":
                return _err("Thiếu header X-Requested-With", 400)
            user = session.get("user")
            if not user or not session.get("t"):
                return _err("Chưa đăng nhập", 401)
            if admin and (user.get("role") or "").lower() != "admin":
                return _err("Chỉ admin được sửa cài đặt AI", 403)
            try:
                return fn(*a, **kw)
            except PrimeAuthError as exc:
                session.clear()
                return _err(str(exc), 401)
        return wrapper
    return deco


def _backend(method: str, path: str, **kw):
    """Gọi backend bằng token người dùng; lưu lại token nếu vừa được làm mới."""
    tokens = dict(session["t"])
    status, body = _client().call(method, path, tokens, **kw)
    if tokens != session["t"]:
        session["t"] = tokens
    return status, body


def _relay(status: int, body: dict):
    if body.get("success"):
        return jsonify(ok=True, data=body.get("data"), message=body.get("message")), status
    return _err(body.get("message") or f"Backend trả HTTP {status}", status if status >= 400 else 502)


# ── trang ──

@ui_bp.get("/")
def index():
    return send_file(STATIC / "admin.html", max_age=0)


# ── đăng nhập ──

@ui_bp.post("/ui/api/login")
def login():
    if request.headers.get("X-Requested-With") != "primeagent":
        return _err("Thiếu header X-Requested-With", 400)
    ip = request.headers.get("X-Real-IP") or request.remote_addr or "?"
    if _too_many_fails(ip):
        return _err("Sai quá nhiều lần — thử lại sau 5 phút", 429)
    body = request.get_json(silent=True) or {}
    username, password = str(body.get("username") or "").strip(), str(body.get("password") or "")
    if not username or not password:
        return _err("Nhập tài khoản và mật khẩu", 422)
    try:
        data = _client().login(username, password)
    except PrimeAuthError as exc:
        _record_fail(ip)
        return _err(str(exc), exc.status)
    except Exception as exc:  # noqa: BLE001 — backend không gọi được
        return _err(f"Không kết nối được backend: {type(exc).__name__}", 502)
    session.clear()
    session.permanent = True
    session["user"] = data["user"]
    session["t"] = {"access_token": data["access_token"], "refresh_token": data["refresh_token"]}
    return jsonify(ok=True, user=data["user"])


@ui_bp.post("/ui/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@ui_bp.get("/ui/api/me")
@login_required()
def me():
    return jsonify(ok=True, user=session["user"], inflight=_cfg("WORKER").inflight())


# ── cài đặt AI ──

@ui_bp.get("/ui/api/settings")
@login_required()
def get_settings():
    return jsonify(ok=True, data=_cfg("STORE").public(_cfg("SETTINGS")))


@ui_bp.put("/ui/api/settings")
@login_required(admin=True)
def put_settings():
    try:
        _cfg("STORE").update(request.get_json(silent=True) or {})
    except ValueError as exc:
        return _err(str(exc), 422)
    return jsonify(ok=True, data=_cfg("STORE").public(_cfg("SETTINGS")))


@ui_bp.post("/ui/api/settings/test")
@login_required()
def test_settings():
    return jsonify(ok=True, data=check_key(_cfg("STORE").effective(_cfg("SETTINGS"))))


@ui_bp.get("/ui/api/modes")
@login_required()
def get_modes():
    return _relay(*_backend("GET", "/api/agent/settings"))


@ui_bp.put("/ui/api/modes")
@login_required()
def put_modes():
    return _relay(*_backend("PUT", "/api/agent/settings", json=request.get_json(silent=True) or {}))


# ── lượt fulfill ──

_LIST_ARGS = ("platform", "supplier_id", "date_from", "date_to", "analysis", "q", "page", "per_page")


@ui_bp.get("/ui/api/fulfillments")
@login_required()
def list_fulfillments():
    params = {k: request.args[k] for k in _LIST_ARGS if request.args.get(k)}
    return _relay(*_backend("GET", "/api/agent/fulfillments", params=params))


@ui_bp.get("/ui/api/fulfillments/<int:fid>")
@login_required()
def fulfillment_detail(fid: int):
    return _relay(*_backend("GET", f"/api/agent/fulfillments/{fid}/detail"))


@ui_bp.post("/ui/api/analyze")
@login_required()
def analyze():
    """Đi đúng luồng thật: backend đặt chờ ⇒ gọi ``/jobs`` của Agent ⇒ Agent gửi kết quả về đơn."""
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    try:
        ids = list(dict.fromkeys(int(i) for i in ids))[:50]
    except (TypeError, ValueError):
        return _err("ids phải là danh sách số", 422)
    if not ids:
        return _err("Chưa chọn lượt fulfill nào", 422)
    results = []
    for fid in ids:
        status, body = _backend("POST", f"/api/agent/fulfillments/{fid}/recheck")
        results.append({"fulfillment_id": fid, "ok": bool(body.get("success")), "message": body.get("message")})
    return jsonify(ok=True, data=results)


# ── ảnh thu nhỏ (file thiết kế gốc 13–21 MB, Drive link…) ──

_thumbs: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
_thumbs_lock = threading.Lock()
THUMB_PX, THUMB_CACHE = 480, 300


def _public_host(url: str) -> bool:
    """Chặn gọi vào mạng nội bộ (localhost, 10.x, 169.254…)."""
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


@ui_bp.get("/ui/api/thumb")
@login_required()
def thumb():
    url = request.args.get("url") or ""
    if not url.startswith(("https://", "http://")) or not _public_host(url):
        return _err("URL ảnh không hợp lệ", 422)
    with _thumbs_lock:
        hit = _thumbs.get(url)
        if hit:
            _thumbs.move_to_end(url)
    if not hit:
        s = _cfg("SETTINGS")
        try:
            img = load_image(url, max_px=THUMB_PX, max_bytes=s.max_download_mb * 1024 * 1024, timeout=s.http_timeout)
        except ImageLoadError as exc:
            return _err(f"Không tải được ảnh: {exc}", 502)
        hit = (img.media_type, base64.b64decode(img.data_b64))
        with _thumbs_lock:
            _thumbs[url] = hit
            while len(_thumbs) > THUMB_CACHE:
                _thumbs.popitem(last=False)
    return Response(hit[1], mimetype=hit[0], headers={"Cache-Control": "private, max-age=86400"})


def init_ui(app, settings, store) -> None:
    app.secret_key = store.secret_key()
    app.config.update(SETTINGS=settings, STORE=store, SESSION_COOKIE_HTTPONLY=True,
                      # Chạy local qua http: AGENT_DEV=1 (cookie không Secure)
                      SESSION_COOKIE_SECURE=os.getenv("AGENT_DEV") != "1",
                      SESSION_COOKIE_SAMESITE="Strict", PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    app.register_blueprint(ui_bp)
