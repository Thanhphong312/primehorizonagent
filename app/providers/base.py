"""Giao diện chung cho nhà cung cấp AI — đổi Claude ⇄ GPT trên trang quản lý (hoặc ``AI_PROVIDER``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.images import LoadedImage


@dataclass
class Part:
    """Một mảnh nội dung gửi AI: chữ HOẶC ảnh, theo đúng thứ tự trình bày."""

    text: str | None = None
    image: LoadedImage | None = None


@dataclass
class ProviderResult:
    data: dict[str, Any]
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None


def schema_instruction(schema: dict[str, Any]) -> str:
    """Gateway/proxy không ép được JSON schema ⇒ ghi schema vào prompt."""
    import json

    return ("\n\nĐỊNH DẠNG TRẢ LỜI (bắt buộc): chỉ trả về ĐÚNG MỘT object JSON, không markdown, không giải thích, "
            "đúng tên khoá theo JSON Schema sau:\n" + json.dumps(schema, ensure_ascii=False))


def repair_json(t: str) -> str:
    """Sửa lỗi hay gặp khi model tự viết JSON (không có structured output):
    nháy kép không escape trong chuỗi (vd chữ in "Mom"), xuống dòng thô trong chuỗi, dấu phẩy thừa."""
    import re

    out: list[str] = []
    in_str = False
    i, n = 0, len(t)
    while i < n:
        ch = t[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                out.append(t[i:i + 2])
                i += 2
                continue
            if ch == '"':
                j = i + 1
                while j < n and t[j] in " \t\r\n":
                    j += 1
                # nháy ĐÓNG chuỗi khi theo sau là , } ] : hoặc hết chuỗi; còn lại là nháy NẰM TRONG chuỗi
                if j >= n or t[j] in ",}]:":
                    in_str = False
                    out.append(ch)
                else:
                    out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                pass
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
        i += 1
    return _balance(re.sub(r",\s*([}\]])", r"\1", _drop_garbage("".join(out))))


_LITERAL = __import__("re").compile(r"^(true|false|null|-?\d+(\.\d+)?([eE][+-]?\d+)?)$")


def _drop_garbage(t: str) -> str:
    """Bỏ chữ rác NGOÀI chuỗi JSON (gặp thật qua gateway: ``…}} buffering噪?}]``); giữ true/false/null/số."""
    import re

    out: list[str] = []
    i, n = 0, len(t)
    while i < n:
        if t[i] == '"':                       # nguyên chuỗi (đã escape đúng ở bước trước)
            j = i + 1
            while j < n and t[j] != '"':
                j += 2 if t[j] == "\\" else 1
            out.append(t[i:j + 1])
            i = j + 1
            continue
        m = re.match(r'[^\s{}\[\],:"]+', t[i:])
        if m:
            tok = m.group(0)
            if _LITERAL.match(tok):
                out.append(tok)
            i += len(tok)
            continue
        out.append(t[i])
        i += 1
    return "".join(out)


def _balance(t: str) -> str:
    """Chèn ngoặc đóng bị thiếu — vd model viết ``…"}}], "summary"`` (quên đóng object item trước ``]``)."""
    pairs = {"{": "}", "[": "]"}
    out: list[str] = []
    stack: list[str] = []
    in_str = esc = False
    for ch in t:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in pairs:
            stack.append(pairs[ch])
        elif ch in "}]":
            # đóng thiếu: còn ngoặc khác đang mở phía trên ⇒ đóng hộ cho tới đúng loại
            while stack and stack[-1] != ch and ch in stack:
                out.append(stack.pop())
            if stack and stack[-1] == ch:
                stack.pop()
            else:
                continue          # ngoặc đóng thừa ⇒ bỏ
        out.append(ch)
    out.extend(reversed(stack))
    return "".join(out)


def parse_json_loose(text: str) -> dict[str, Any]:
    """JSON có thể bọc ```json … ``` hoặc kèm chữ trước/sau ⇒ lấy object ngoài cùng."""
    import json

    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    a, b = t.find("{"), t.rfind("}")
    # cut = tới "}" cuối (bỏ chữ thừa phía sau); full = tới hết chuỗi (JSON bị cụt ngoặc cuối).
    full = t[a:] if a >= 0 else t
    cut = t[a:b + 1] if a >= 0 and b > a else full
    err: json.JSONDecodeError | None = None
    data = None
    for c in (cut, full, repair_json(full), repair_json(cut)):   # nguyên văn trước, bản sửa sau
        try:
            data = json.loads(c)
            break
        except json.JSONDecodeError as exc:
            err = err or exc
    if data is None:
        raise err
    if not isinstance(data, dict):
        raise json.JSONDecodeError("không phải object", t, 0)
    return data


def check_result_shape(data: dict[str, Any]) -> None:
    """Kiểm tra tối thiểu khi không có structured output ép schema."""
    if not isinstance(data.get("items"), list) or not isinstance(data.get("summary", ""), str):
        raise ProviderError("AI trả JSON sai khuôn (thiếu items/summary)")
    for it in data["items"]:
        if not isinstance(it, dict) or not isinstance(it.get("checks"), dict):
            raise ProviderError("AI trả JSON sai khuôn (item thiếu checks)")


class ProviderError(Exception):
    """Lỗi không phục hồi được ở lượt này (từ chối, hết token, JSON hỏng…)."""


class Provider(Protocol):
    name: str

    def analyze(self, system: str, parts: list[Part], schema: dict[str, Any]) -> ProviderResult: ...


def get_provider(settings) -> Provider:
    if settings.ai_provider == "openai":
        from app.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(settings)
    from app.providers.anthropic_provider import AnthropicProvider

    return AnthropicProvider(settings)


def check_key(settings) -> dict[str, Any]:
    """Thử key bằng lệnh liệt kê model (không tốn token). Trả ``{ok, message, models?}``."""
    try:
        p = get_provider(settings)
        page = p.client.models.list()
        ids = [m.id for m in getattr(page, "data", [])][:50]
        return {"ok": True, "message": f"Key hợp lệ ({p.name})", "models": ids}
    except ProviderError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 — sai key / mạng / gateway không hỗ trợ
        return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:300]}"}
