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

from app import mirai
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


@ui_bp.get("/ui/api/prompt")
@login_required()
def get_prompt():
    """Prompt đang dùng (chỉ xem) — để người dùng biết AI được dặn chấm thế nào."""
    from app import analyzer, garment, schema
    from app.providers.base import schema_instruction

    eff = _cfg("STORE").effective(_cfg("SETTINGS"))
    base_url = eff.openai_base_url if eff.ai_provider == "openai" else eff.anthropic_base_url
    code_checks = [
        "Payload không có item nào ⇒ lỗi; item không có file thiết kế ⇒ lỗi; số lượng ≤ 0 ⇒ lỗi.",
        "File in trùng link ảnh mockup ⇒ lỗi; link file in trông như ảnh mockup/listing ⇒ cảnh báo.",
        "Mặt in không thuộc danh sách mặt in của sku (nếu tra được) ⇒ lỗi.",
        "Không tra được sku trong catalog (Sellerwix, SimplePrint, Printbelle, BullStart, Anprint) ⇒ cảnh báo.",
        "Hai item trùng hoàn toàn (cùng sku + file) ⇒ cảnh báo; tổng số lượng gửi ≠ tổng số lượng đơn ⇒ cảnh báo.",
        f"Màu thiết kế trùng màu áo (chỉ dòng áo, file in nền trong suốt): ≥ {round(garment.WARN_SHARE * 100)}% "
        f"phần có in gần màu áo (ΔE < {garment.NEAR_DE:g}) ⇒ cảnh báo \"in ra dễ bị chìm\". "
        f"Biết {len(garment.COLOR_HEX)} tên màu áo.",
    ]
    return jsonify(ok=True, data={
        "system": analyzer.SYSTEM_PROMPT + (schema_instruction(schema.AI_RESULT_SCHEMA) if base_url else ""),
        "final": analyzer._final_instruction({"sent_items": [{"index": 1}, {"index": 2}]}),
        "user_layout": ["DỮ LIỆU LƯỢT FULFILL (JSON): đơn của khách, item đã gửi (sku, decoded, file in theo mặt, "
                        "mockup), ticket — link ảnh thay bằng nhãn [Ảnh #n]",
                        "Phát hiện của bước kiểm bằng code (đã chắc chắn, không cần chấm lại)",
                        "Các ảnh: [Ảnh #n] + mô tả (mockup, file thiết kế, ảnh đơn, ảnh ticket)",
                        "Câu chốt (ví dụ 2 item) ↓"],
        "code_checks": code_checks,
        "model": eff.openai_model if eff.ai_provider == "openai" else eff.anthropic_model,
        "gateway": bool(base_url),
    })


def _meta() -> mirai.Meta:
    return mirai.Meta(_cfg("STORE").dir)


@ui_bp.post("/ui/api/redeem")
@login_required(admin=True)
def redeem():
    """Dán / tải file nhà bán gửi (hoặc chỉ mã MR-…) ⇒ đổi lấy key, lưu làm key Claude đang dùng."""
    body = request.get_json(silent=True) or {}
    try:
        code = mirai.extract_code(str(body.get("text") or "")[:20000])
        data = mirai.redeem(code)
    except mirai.MiraiError as exc:
        return _err(str(exc), 422)
    except Exception as exc:  # noqa: BLE001 — mạng
        return _err(f"Không gọi được miraiapi: {type(exc).__name__}", 502)
    store = _cfg("STORE")
    cur_model = store.load().get("anthropic_model") or ""
    store.update({"ai_provider": "anthropic", "anthropic_api_key": data["api_key"],
                  "anthropic_base_url": mirai.BASE_URL,
                  "anthropic_model": cur_model if cur_model and "." in cur_model else mirai.DEFAULT_MODEL})
    _meta().save({"code": code[:7] + "…" + code[-4:], "expires_at": data.get("expires_at"),
                  "quota_tokens": data.get("quota_tokens"), "recovered": bool(data.get("recovered")),
                  "redeemed_at": int(time.time()), "redeemed_by": session["user"].get("username")})
    return jsonify(ok=True, data={"recovered": bool(data.get("recovered")), "expires_at": data.get("expires_at"),
                                  "quota_tokens": data.get("quota_tokens"),
                                  "settings": store.public(_cfg("SETTINGS"))})


@ui_bp.get("/ui/api/quota")
@login_required()
def quota():
    """Hạn & quota còn lại của key miraiapi đang dùng (không phải miraiapi ⇒ ``data: null``)."""
    eff = _cfg("STORE").effective(_cfg("SETTINGS"))
    if eff.ai_provider != "anthropic" or not mirai.is_mirai(eff.anthropic_base_url) or not eff.anthropic_api_key:
        return jsonify(ok=True, data=None)
    meta = _meta().load()
    try:
        u = mirai.usage(eff.anthropic_api_key)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=True, data={**meta, "error": str(exc)[:200]})
    return jsonify(ok=True, data={**meta, **u, "now": int(time.time())})


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


@ui_bp.post("/ui/api/fulfillments/<int:fid>/resolve")
@login_required()
def resolve(fid: int):
    """Đã kiểm tra lượt AI báo lỗi ⇒ chuyển OK (``{"note"}``) / hoàn tác (``{"undo": true}``)."""
    body = request.get_json(silent=True) or {}
    payload = {"undo": True} if body.get("undo") else {"note": str(body.get("note") or "")[:500]}
    return _relay(*_backend("POST", f"/api/agent/fulfillments/{fid}/resolve", json=payload))


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
