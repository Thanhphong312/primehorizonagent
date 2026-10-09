"""PrimeAgent — server AI kiểm tra fulfill.

Backend PrimeHorizon gọi ``POST /jobs`` khi có lượt fulfill mới (hoặc khi bấm "phân tích lại").
PrimeAgent trả 202 ngay, phân tích nền, rồi tự gửi kết quả về backend.
"""

from __future__ import annotations

import hmac
import logging

from flask import Flask, jsonify, request

from app.config import get_settings
from app.store import Store
from app.web import init_ui
from app.worker import Worker


def create_app(settings=None, worker: Worker | None = None, store: Store | None = None) -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = settings or get_settings()
    if not settings.agent_inbound_key:
        raise RuntimeError("Thiếu AGENT_INBOUND_KEY")
    app = Flask(__name__)
    store = store or Store(settings.data_dir)
    app.config["WORKER"] = worker or Worker(settings, store=store)
    init_ui(app, settings, store)

    def authorized() -> bool:
        got = request.headers.get("X-Agent-Key", "")
        return bool(got) and hmac.compare_digest(got, settings.agent_inbound_key)

    @app.get("/health")
    def health():
        eff = store.effective(settings)
        key = eff.anthropic_api_key if eff.ai_provider == "anthropic" else eff.openai_api_key
        return jsonify(ok=True, provider=eff.ai_provider, ai_key_set=bool(key),
                       inflight=app.config["WORKER"].inflight())

    @app.post("/jobs")
    def jobs():
        if not authorized():
            return jsonify(ok=False, message="Unauthorized"), 401
        body = request.get_json(silent=True) or {}
        try:
            fid = int(body.get("fulfillment_id"))
        except (TypeError, ValueError):
            return jsonify(ok=False, message="fulfillment_id phải là số"), 422
        accepted = app.config["WORKER"].submit(fid)
        return jsonify(ok=True, accepted=accepted, duplicate=not accepted), 202

    return app
