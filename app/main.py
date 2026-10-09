"""PrimeAgent — server AI kiểm tra fulfill.

Backend PrimeHorizon gọi ``POST /jobs`` khi có lượt fulfill mới (hoặc khi bấm "phân tích lại").
PrimeAgent trả 202 ngay, phân tích nền, rồi tự gửi kết quả về backend.
"""

from __future__ import annotations

import hmac
import logging
import os

from flask import Flask, jsonify, request

from app.config import get_settings
from app.worker import Worker


def create_app(settings=None, worker: Worker | None = None) -> Flask:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = settings or get_settings()
    if not settings.agent_inbound_key:
        raise RuntimeError("Thiếu AGENT_INBOUND_KEY")
    app = Flask(__name__)
    app.config["WORKER"] = worker or Worker(settings)

    def authorized() -> bool:
        got = request.headers.get("X-Agent-Key", "")
        return bool(got) and hmac.compare_digest(got, settings.agent_inbound_key)

    @app.get("/")
    def index():
        return jsonify(service="PrimeAgent — AI kiểm tra fulfill", ok=True, provider=settings.ai_provider,
                       ai_key_set=bool(os.getenv("ANTHROPIC_API_KEY" if settings.ai_provider == "anthropic"
                                                 else "OPENAI_API_KEY")),
                       endpoints=["GET /health", "POST /jobs (X-Agent-Key)"])

    @app.get("/health")
    def health():
        return jsonify(ok=True, provider=settings.ai_provider, inflight=app.config["WORKER"].inflight())

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
