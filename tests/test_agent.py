from __future__ import annotations

import json
import tempfile
from types import SimpleNamespace

import pytest

from app import schema
from app.analyzer import analyze
from app.config import Settings
from app.images import ImageLoadError, LoadedImage, drive_thumbnail
from app.main import create_app
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import Part, ProviderError, ProviderResult
from app.providers.openai_provider import OpenAIProvider
from app.rules import run_rules
from app.worker import Worker


def settings(**kw):
    base = dict(prime_api_base="http://prime", prime_service_key="sk", agent_inbound_key="ak",
                ai_provider="anthropic", openai_model="gpt-x", max_images=16, data_dir=tempfile.mkdtemp())
    base.update(kw)
    return Settings(**base)


def bundle(**kw):
    b = {
        "fulfillment_id": 812, "platform": "etsy", "supplier": {"id": 2, "name": "Sellerwix", "type": "sellerwix"},
        "order": {"id": 683, "items": [{"index": 1, "title": "Shirt", "quantity": 1,
                                        "images": ["https://img/etsy1.jpg"]}]},
        "sent_items": [{"index": 1, "sku": "SW-MD-CNFG-SA-M-DTF", "quantity": 1,
                        "decoded": {"product": "Gildan 18000", "color": "Sand", "size": "M",
                                    "allowed_areas": ["Front", "Back"]},
                        "designs": [{"area": "Front", "url": "https://drive.google.com/file/d/ABCDEFGHIJKL/view"}],
                        "mockups": ["https://img/mock1.jpg"]}],
        "tickets": [{"id": 5, "status": "resolved", "content": "Đổi chữ trắng",
                     "attachments": ["https://img/t1.jpg"], "comments": []}],
    }
    b.update(kw)
    return b


IMG = LoadedImage(url="u", media_type="image/jpeg", data_b64="AAAA", width=10, height=10)


def ok_checks(**over):
    c = {k: {"status": "ok", "reason": "khớp"} for k in schema.CHECK_KEYS}
    for k, v in over.items():
        c[k] = {"status": v, "reason": v}
    return c


class FakeProvider:
    name = "fake"

    def __init__(self, data=None, exc=None):
        self.data, self.exc, self.calls = data, exc, []

    def analyze(self, system, parts, sch):
        self.calls.append(parts)
        if self.exc:
            raise self.exc
        return ProviderResult(data=self.data, model="fake-1", usage={"input_tokens": 1})


# ── rules ──

def test_rules_bat_mat_in_khong_duoc_phep_va_thieu_design():
    b = bundle()
    b["sent_items"].append({"index": 2, "sku": "X", "quantity": 1, "decoded": {"allowed_areas": ["Front"]},
                            "designs": [{"area": "Back", "url": "u2"}], "mockups": []})
    b["sent_items"].append({"index": 3, "sku": "Y", "quantity": 1, "decoded": None, "designs": [], "mockups": []})
    f = run_rules(b)
    assert {"index": 2, "check": "print_side", "status": "error"}.items() <= next(
        x for x in f if x["index"] == 2).items()
    assert any(x["index"] == 3 and x["check"] == "design" and x["status"] == "error" for x in f)
    assert any(x["index"] == 3 and x["check"] == "product_color" and x["status"] == "warn" for x in f)
    assert any(x["index"] is None and "số lượng" in x["reason"] for x in f)   # 3 gửi vs 1 đặt


def test_rules_mat_in_khop_khong_phan_biet_hoa_thuong_khoang_trang():
    b = bundle()
    b["sent_items"][0]["decoded"]["allowed_areas"] = ["Front Dtf"]
    b["sent_items"][0]["designs"][0]["area"] = "front dtf"
    assert run_rules(b) == []


def test_rules_khong_co_item():
    assert run_rules(bundle(sent_items=[]))[0]["status"] == "error"


def test_rules_item_trung():
    b = bundle()
    b["sent_items"].append(dict(b["sent_items"][0], index=2))
    b["order"]["items"][0]["quantity"] = 2
    assert [x["status"] for x in run_rules(b)] == ["warn"]


# ── schema ──

def test_overall_code():
    assert schema.overall_code([{"checks": ok_checks()}], []) == schema.OK
    assert schema.overall_code([{"checks": ok_checks(design="unknown")}], []) == schema.WARN
    assert schema.overall_code([{"checks": ok_checks(ticket="warn")}], []) == schema.WARN
    assert schema.overall_code([{"checks": ok_checks()}], [{"status": "error"}]) == schema.ERROR


def test_schema_moi_object_deu_khoa_additional_properties():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(schema.AI_RESULT_SCHEMA)


# ── images ──

def test_drive_thumbnail():
    assert drive_thumbnail("https://drive.google.com/file/d/1AbC_dEf-123/view?usp=sharing", 1568) == \
        "https://drive.google.com/thumbnail?id=1AbC_dEf-123&sz=w1568"
    assert drive_thumbnail("https://drive.google.com/open?id=1AbCdEfGh12", 800).endswith("id=1AbCdEfGh12&sz=w800")
    assert drive_thumbnail("https://i.etsystatic.com/a.jpg", 800) == "https://i.etsystatic.com/a.jpg"


# ── analyzer ──

def test_analyze_ghep_ket_qua_va_thay_url_bang_nhan():
    p = FakeProvider({"items": [{"index": 1, "checks": ok_checks(print_side="error")}], "summary": "Sai mặt in"})
    out = analyze(bundle(), p, settings(), loader=lambda *a, **k: IMG)
    assert out["status_code"] == schema.ERROR and out["summary"] == "Sai mặt in"
    assert out["items"][0]["sku"] == "SW-MD-CNFG-SA-M-DTF"
    assert out["images_used"] == 4      # mockup, design, ảnh đơn, ảnh ticket
    texts = "".join(x.text or "" for x in p.calls[0])
    assert "drive.google.com" not in texts and "[Ảnh #1]" in texts
    assert sum(1 for x in p.calls[0] if x.image) == 4


def test_analyze_anh_loi_va_vuot_gioi_han_thanh_canh_bao():
    def loader(url, **k):
        if "mock1" in url:
            raise ImageLoadError("404")
        return IMG
    p = FakeProvider({"items": [{"index": 1, "checks": ok_checks()}], "summary": ""})
    out = analyze(bundle(), p, settings(max_images=2), loader=loader)
    reasons = " ".join(f["reason"] for f in out["rule_findings"])
    assert "không tải được" in reasons and "Bỏ qua 2 ảnh" in reasons
    assert out["status_code"] == schema.WARN


def test_analyze_ai_thieu_item():
    p = FakeProvider({"items": [], "summary": ""})
    out = analyze(bundle(), p, settings(), loader=lambda *a, **k: IMG)
    assert any("không trả kết quả" in f["reason"] for f in out["rule_findings"])


# ── providers ──

def _anthropic_resp(stop="end_turn", text=None):
    data = text if text is not None else json.dumps({"items": [], "summary": "ok"})
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=data)],
                           model="claude-opus-5-5", usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                           _request_id="req_1")


class FakeAnthropic:
    """Giả ``client.beta.messages.stream(...)`` (context manager + get_final_message)."""

    def __init__(self, resp):
        self.kw = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))
        self.resp = resp

    def _stream(self, **kw):
        self.kw = kw
        resp = self.resp

        class _S:
            def __enter__(self):
                return SimpleNamespace(get_final_message=lambda: resp)

            def __exit__(self, *a):
                return False
        return _S()


def test_anthropic_request_shape():
    c = FakeAnthropic(_anthropic_resp())
    r = AnthropicProvider(settings(), client=c).analyze("sys", [Part(text="hi"), Part(image=IMG)], {"type": "object"})
    assert r.data["summary"] == "ok" and r.request_id == "req_1"
    kw = c.kw
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in kw
    content = kw["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "hi"}
    assert content[1]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_anthropic_tu_choi_hoac_het_token(stop):
    with pytest.raises(ProviderError):
        AnthropicProvider(settings(), client=FakeAnthropic(_anthropic_resp(stop))).analyze("s", [], {})


def test_anthropic_json_hong():
    with pytest.raises(ProviderError):
        AnthropicProvider(settings(), client=FakeAnthropic(_anthropic_resp(text="{oops"))).analyze("s", [], {})


def test_openai_request_shape():
    seen = {}

    def create(**kw):
        seen.update(kw)
        return SimpleNamespace(output_text=json.dumps({"items": [], "summary": "x"}), model="gpt-x", usage=None, id="r1")

    c = SimpleNamespace(responses=SimpleNamespace(create=create))
    r = OpenAIProvider(settings(ai_provider="openai"), client=c).analyze("sys", [Part(image=IMG)], {"type": "object"})
    assert r.data["summary"] == "x"
    assert seen["text"]["format"]["strict"] is True
    assert seen["input"][0]["content"][0]["image_url"].startswith("data:image/jpeg;base64,")


def test_openai_bat_buoc_model():
    with pytest.raises(ProviderError):
        OpenAIProvider(settings(openai_model=""), client=object())


# ── worker + http ──

class FakeClient:
    def __init__(self, b=None, exc=None):
        self.b, self.exc, self.posted = b, exc, []

    def get_bundle(self, fid):
        if self.exc:
            raise self.exc
        return self.b

    def post_result(self, fid, payload):
        self.posted.append((fid, payload))


def test_worker_loi_ai_thi_gui_ma_9():
    c = FakeClient(bundle())
    w = Worker(settings(), client=c, provider=FakeProvider(exc=ProviderError("từ chối")), loader=lambda *a, **k: IMG)
    w.process(812)
    assert c.posted[0][1]["status_code"] == schema.FAILED and "từ chối" in c.posted[0][1]["error"]


def test_worker_khong_lay_duoc_bundle_thi_khong_gui_gi():
    c = FakeClient(exc=RuntimeError("down"))
    Worker(settings(), client=c, provider=FakeProvider()).process(1)
    assert c.posted == []


def test_http_jobs_auth_va_trung():
    class W:
        def __init__(self):
            self.n = 0

        def submit(self, fid):
            self.n += 1
            return self.n == 1

        def inflight(self):
            return 0

    app = create_app(settings(), worker=W())
    c = app.test_client()
    assert c.post("/jobs", json={"fulfillment_id": 1}).status_code == 401
    assert c.post("/jobs", json={"fulfillment_id": 1}, headers={"X-Agent-Key": "sai"}).status_code == 401
    h = {"X-Agent-Key": "ak"}
    assert c.post("/jobs", json={"fulfillment_id": "x"}, headers=h).status_code == 422
    r1 = c.post("/jobs", json={"fulfillment_id": 1}, headers=h)
    r2 = c.post("/jobs", json={"fulfillment_id": 1}, headers=h)
    assert r1.status_code == 202 and r1.get_json()["accepted"] is True
    assert r2.get_json()["duplicate"] is True
    assert c.get("/health").get_json()["ok"] is True


def test_rules_file_in_la_mockup_hoac_trung_link_mockup():
    b = bundle()
    it = b["sent_items"][0]
    it["designs"] = [{"area": "Front", "url": "https://r2/etsy/media/mockups/2026/09/22/x_il_fullxfull.1.jpg"}]
    it["mockups"] = ["https://r2/horizon/media/staff-files/2026/09/22/design.png"]
    [f] = run_rules(b)
    assert f["check"] == "design" and f["status"] == "warn" and "dán nhầm" in f["reason"]
    it["mockups"] = [it["designs"][0]["url"]]
    assert [x["status"] for x in run_rules(b)] == ["error"]


def test_trang_goc_la_trang_quan_ly_va_health_bao_key():
    app = create_app(settings(), worker=SimpleNamespace(inflight=lambda: 0))
    c = app.test_client()
    r = c.get("/")
    assert r.status_code == 200 and b"PrimeAgent" in r.data
    assert c.get("/health").get_json()["ai_key_set"] is False


# ── store (cài đặt trên trang) ──

def test_store_luu_che_key_va_ghi_de_env(tmp_path):
    from app.store import Store, mask
    st = Store(str(tmp_path))
    base = settings(anthropic_api_key="env-key-aaaaaaaaaaaa", anthropic_model="claude-opus-5-5")
    assert st.effective(base).anthropic_api_key == "env-key-aaaaaaaaaaaa"
    st.update({"anthropic_api_key": "sk-ant-page-1234567890", "anthropic_model": "claude-sonnet-5-5", "openai_model": None})
    eff = st.effective(base)
    assert eff.anthropic_api_key == "sk-ant-page-1234567890" and eff.anthropic_model == "claude-sonnet-5-5"
    pub = st.public(base)
    assert pub["anthropic_api_key"]["value"] == mask("sk-ant-page-1234567890") and "1234567890" not in pub["anthropic_api_key"]["value"]
    assert pub["anthropic_api_key"]["source"] == "page"
    st.update({"anthropic_model": ""})          # xoá ⇒ về env
    assert st.effective(base).anthropic_model == "claude-opus-5-5"
    assert (tmp_path / "settings.json").stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        st.update({"ai_provider": "gemini"})
    assert st.secret_key() == st.secret_key()


def test_worker_dung_cai_dat_tren_trang(tmp_path):
    from app.store import Store
    st = Store(str(tmp_path))
    st.update({"ai_provider": "openai", "openai_api_key": "k"})
    c = FakeClient(bundle())
    w = Worker(settings(), client=c, store=st, loader=lambda *a, **k: IMG)
    assert w.current_settings().ai_provider == "openai"
    st.update({"ai_provider": "anthropic", "anthropic_api_key": ""})
    w.process(812)    # không có key anthropic ⇒ báo thất bại rõ ràng, không crash
    assert c.posted[0][1]["status_code"] == schema.FAILED and "API key" in c.posted[0][1]["error"]


def test_anthropic_base_url_bo_beta_fallback():
    c = FakeAnthropic(_anthropic_resp())
    AnthropicProvider(settings(anthropic_base_url="https://gw.example"), client=c).analyze("s", [], {})
    assert "betas" not in c.kw and "fallbacks" not in c.kw


# ── trang quản lý ──

class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


class FakeBackend:
    """Giả backend PrimeHorizon: login, refresh, các route /api/agent/*."""

    def __init__(self, role="admin"):
        self.role, self.calls, self.expire_once = role, [], False

    def post(self, url, json=None, headers=None, timeout=None):
        if url.endswith("/api/auth/login"):
            if json["password"] != "pw":
                return FakeResp(401, {"success": False, "message": "Sai mật khẩu"})
            return FakeResp(200, {"success": True, "data": {"access_token": "a1", "refresh_token": "r1",
                                                            "user": {"id": 1, "username": "u", "role": self.role}}})
        if url.endswith("/api/auth/refresh"):
            return FakeResp(200, {"success": True, "data": {"access_token": "a2", "refresh_token": "r2"}})
        raise AssertionError(url)

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append((method, url.replace("http://prime", ""), headers["Authorization"], kw))
        if self.expire_once and headers["Authorization"] == "Bearer a1":
            return FakeResp(401, {"success": False})
        if "/recheck" in url:
            return FakeResp(200, {"success": True, "message": "Đã đưa vào hàng chờ"})
        return FakeResp(200, {"success": True, "data": {"items": [], "total": 0}})


@pytest.fixture
def ui(monkeypatch):
    be = FakeBackend()
    import app.prime_user as pu
    monkeypatch.setattr(pu, "requests", be)
    app = create_app(settings(), worker=SimpleNamespace(inflight=lambda: 0))
    return app.test_client(), be


H = {"X-Requested-With": "primeagent"}


def _login(c, pw="pw"):
    return c.post("/ui/api/login", json={"username": "u", "password": pw}, headers=H)


def test_ui_dang_nhap_va_chong_csrf(ui):
    c, be = ui
    assert c.get("/ui/api/me").status_code == 401
    assert c.post("/ui/api/login", json={"username": "u", "password": "pw"}).status_code == 400   # thiếu header
    assert _login(c, "sai").status_code == 401
    assert _login(c).get_json()["user"]["role"] == "admin"
    assert c.get("/ui/api/me").status_code == 200
    assert c.put("/ui/api/settings", json={"ai_provider": "openai"}).status_code == 400            # thiếu header


def test_ui_role_khac_khong_vao_duoc(ui):
    c, be = ui
    be.role = "user"
    assert _login(c).status_code == 403


def test_ui_fulfill_khong_sua_duoc_key(ui):
    c, be = ui
    be.role = "fulfill"
    _login(c)
    assert c.get("/ui/api/settings").status_code == 200
    assert c.put("/ui/api/settings", json={"anthropic_api_key": "x"}, headers=H).status_code == 403


def test_ui_luu_key_va_chuyen_tiep_backend(ui):
    c, be = ui
    _login(c)
    r = c.put("/ui/api/settings", json={"anthropic_api_key": "sk-ant-abcdefghijklmnop"}, headers=H).get_json()
    assert r["data"]["anthropic_api_key"]["set"] is True and "ijklmnop" not in r["data"]["anthropic_api_key"]["value"]
    assert c.get("/health").get_json()["ai_key_set"] is True
    c.get("/ui/api/fulfillments?platform=etsy&analysis=0&bogus=1")
    method, path, auth, kw = be.calls[-1]
    assert path == "/api/agent/fulfillments" and auth == "Bearer a1"
    assert kw["params"] == {"platform": "etsy", "analysis": "0"}
    res = c.post("/ui/api/analyze", json={"ids": [5, 5, 6]}, headers=H).get_json()["data"]
    assert [x["fulfillment_id"] for x in res] == [5, 6] and all(x["ok"] for x in res)
    assert be.calls[-1][1] == "/api/agent/fulfillments/6/recheck"


def test_ui_token_het_han_tu_lam_moi(ui):
    c, be = ui
    _login(c)
    be.expire_once = True
    assert c.get("/ui/api/fulfillments").status_code == 200
    assert [x[2] for x in be.calls] == ["Bearer a1", "Bearer a2"]
    c.get("/ui/api/fulfillments")
    assert be.calls[-1][2] == "Bearer a2"       # token mới được lưu vào phiên


def test_ui_thumb_chan_mang_noi_bo(ui):
    c, be = ui
    _login(c)
    assert c.get("/ui/api/thumb?url=http://127.0.0.1:5300/health").status_code == 422
    assert c.get("/ui/api/thumb?url=file:///etc/passwd").status_code == 422


def test_gateway_ghi_schema_vao_prompt_va_doc_json_chiu_loi():
    txt = 'Kết quả:\n```json\n{"items": [{"index": 1, "checks": {}}], "summary": "ok"}\n```'
    c = FakeAnthropic(_anthropic_resp(text=txt))
    r = AnthropicProvider(settings(anthropic_base_url="https://gw"), client=c).analyze("sys", [], {"type": "object"})
    assert r.data["summary"] == "ok" and "JSON Schema" in c.kw["system"]
    with pytest.raises(ProviderError):     # sai khuôn
        AnthropicProvider(settings(anthropic_base_url="https://gw"),
                          client=FakeAnthropic(_anthropic_resp(text='{"r": 1}'))).analyze("s", [], {})


# ── nạp mã redeem miraiapi ──

FILE_TXT = """🎟 MÃ REDEEM CODE|MR-126B54C9B6BBFA1749A4F610125CE41EDF82C7A3D7CAD14C|
📦 Gói|🤖 Claude 10M tokens · 1D|
Body JSON|{"redeem_code"|"MR-126B54C9B6BBFA1749A4F610125CE41EDF82C7A3D7CAD14C"}
• API Key|<API_KEY_CỦA_BẠN>|"""


def test_tim_ma_redeem_trong_file():
    from app import mirai
    assert mirai.extract_code(FILE_TXT) == "MR-126B54C9B6BBFA1749A4F610125CE41EDF82C7A3D7CAD14C"
    with pytest.raises(mirai.MiraiError):
        mirai.extract_code("không có mã")
    with pytest.raises(mirai.MiraiError):
        mirai.extract_code("MR-AAAAAAAAAAAAAAAAAAAA MR-BBBBBBBBBBBBBBBBBBBB")


def test_ui_nap_ma_luu_key_va_quota(ui, monkeypatch):
    from app import mirai
    c, be = ui
    _login(c)
    calls = []

    def fake_post(path, body, http=None, timeout=30):
        calls.append((path, body))
        if path == "/api/redeem/new":
            return {"api_key": "sk-NEWKEY1234567890abcd", "expires_at": 1791614904, "quota_tokens": 10_000_000,
                    "recovered": False}
        return {"quota_total": 10_000_000, "quota_remaining": 9_000_000, "expires_at": 1791614904}

    monkeypatch.setattr(mirai, "_post", fake_post)
    assert c.post("/ui/api/redeem", json={"text": "rác"}, headers=H).status_code == 422
    d = c.post("/ui/api/redeem", json={"text": FILE_TXT}, headers=H).get_json()["data"]
    assert calls[-1] == ("/api/redeem/new", {"redeem_code": "MR-126B54C9B6BBFA1749A4F610125CE41EDF82C7A3D7CAD14C"})
    s = d["settings"]
    assert s["anthropic_base_url"]["value"] == "https://api.miraiapi.com" and s["anthropic_model"]["value"] == "claude-opus-5.5"
    assert s["anthropic_api_key"]["set"] and "567890" not in s["anthropic_api_key"]["value"]
    q = c.get("/ui/api/quota").get_json()["data"]
    assert q["quota_remaining"] == 9_000_000 and q["code"].startswith("MR-126B") and calls[-1][1] == {"api_key": "sk-NEWKEY1234567890abcd"}


def test_ui_fulfill_khong_nap_ma_duoc(ui):
    c, be = ui
    be.role = "fulfill"
    _login(c)
    assert c.post("/ui/api/redeem", json={"text": FILE_TXT}, headers=H).status_code == 403


def test_worker_key_mirai_het_han(tmp_path):
    from app import mirai
    from app.store import Store
    st = Store(str(tmp_path))
    st.update({"anthropic_api_key": "sk-x", "anthropic_base_url": mirai.BASE_URL})
    mirai.Meta(str(tmp_path)).save({"expires_at": 1000})
    c = FakeClient(bundle())
    Worker(settings(), client=c, store=st, loader=lambda *a, **k: IMG).process(812)
    assert c.posted[0][1]["status_code"] == schema.FAILED and "hết hạn" in c.posted[0][1]["error"]


def test_gateway_anh_nho_jpeg(monkeypatch):
    seen = []
    p = FakeProvider({"items": [{"index": 1, "checks": ok_checks()}], "summary": ""})
    analyze(bundle(), p, settings(anthropic_base_url="https://gw", gateway_image_px=512),
            loader=lambda url, **k: seen.append(k) or IMG)
    assert seen and all(k["max_px"] == 512 and k["jpeg_only"] for k in seen)
    seen.clear()
    analyze(bundle(), p, settings(), loader=lambda url, **k: seen.append(k) or IMG)
    assert all(k["max_px"] == 1568 and "jpeg_only" not in k for k in seen)


def test_jpeg_only_nen_trong_suot(monkeypatch):
    import io
    from PIL import Image
    from app import images
    buf = io.BytesIO()
    Image.new("RGBA", (2000, 1000), (255, 255, 255, 0)).save(buf, format="PNG")

    class R:
        def raise_for_status(self): pass
        def iter_content(self, n): yield buf.getvalue()
    monkeypatch.setattr(images.requests, "get", lambda *a, **k: R())
    img = images.load_image("https://x/a.png", max_px=512, max_bytes=10 << 20, timeout=5, jpeg_only=True)
    assert img.media_type == "image/jpeg" and max(img.width, img.height) == 512


def test_store_so_nguyen(tmp_path):
    from app.store import Store
    st = Store(str(tmp_path))
    st.update({"gateway_image_px": "384", "max_images": "8"})
    eff = st.effective(settings())
    assert eff.gateway_image_px == 384 and eff.max_images == 8
    with pytest.raises(ValueError):
        st.update({"max_images": "999"})


def test_loi_401_mirai_bao_nap_ma_moi(tmp_path):
    from app.store import Store

    class AuthErr(Exception):
        status_code = 401
    st = Store(str(tmp_path))
    st.update({"anthropic_api_key": "sk-x", "anthropic_base_url": "https://api.miraiapi.com"})
    c = FakeClient(bundle())
    Worker(settings(), client=c, store=st, provider=FakeProvider(exc=AuthErr("Invalid token")),
           loader=lambda *a, **k: IMG).process(812)
    err = c.posted[0][1]["error"]
    assert "nạp mã redeem mới" in err and "Invalid token" in err


def test_sua_json_nhay_kep_trong_chuoi():
    from app.providers.base import parse_json_loose
    bad = '{"items":[{"index":1,"checks":{"design":{"status":"error","reason":"File in ghi "Mom" nhưng đơn là "Mommy"","x":1}}}],"summary":"Sai tên\nkhách",}'
    d = parse_json_loose(bad)
    assert d["items"][0]["checks"]["design"]["reason"] == 'File in ghi "Mom" nhưng đơn là "Mommy"'
    assert d["summary"] == "Sai tên\nkhách"


def test_json_hong_qua_gateway_goi_sua_rieng():
    good = '{"items": [{"index": 1, "checks": {}}], "summary": "đã sửa"}'

    class C(FakeAnthropic):
        def __init__(self):
            super().__init__(_anthropic_resp(text='{"items": [ {{{ hỏng'))
            self.n = 0

        def _stream(self, **kw):
            self.n += 1
            if self.n == 2:
                self.resp = _anthropic_resp(text=good)
                assert all(isinstance(m["content"], str) for m in kw["messages"])   # lần sửa: chỉ chữ, không ảnh
            return super()._stream(**kw)
    c = C()
    r = AnthropicProvider(settings(anthropic_base_url="https://gw"), client=c).analyze("s", [], {"type": "object"})
    assert r.data["summary"] == "đã sửa" and c.n == 2


def test_sua_json_thieu_ngoac_dong():
    from app.providers.base import parse_json_loose
    # lỗi gặp thật ở #940: quên "}" đóng object item trước "]"
    bad = ('{"items": [{"index": 1, "checks": {"print_side": {"status": "ok", "reason": "khớp nhau."}}], '
           '"summary": "ok"}')
    d = parse_json_loose(bad)
    assert d["items"][0]["checks"]["print_side"]["status"] == "ok" and d["summary"] == "ok"
    assert parse_json_loose('{"items": [{"index": 1, "checks": {}}], "summary": "thiếu cuối"')["summary"] == "thiếu cuối"
