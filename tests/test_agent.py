from __future__ import annotations

import json
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
                ai_provider="anthropic", openai_model="gpt-x", max_images=16)
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
    def __init__(self, resp):
        self.kw = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.resp = resp

    def _create(self, **kw):
        self.kw = kw
        return self.resp


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
