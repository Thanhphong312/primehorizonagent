# PrimeAgent — AI kiểm tra fulfill

Server riêng, chạy AI để kiểm tra mỗi lượt fulfill (Etsy + TikTok) có đúng với đơn không — để kịp huỷ
ở nhà cung cấp trước khi hàng được in. Server PrimeHorizon (backend-etsy) chỉ cung cấp dữ liệu và nhận kết quả.

## Luồng

```
Fulfill gửi nhà cung cấp thành công (backend-etsy)
  → order.agent_analysis = 1, POST {AGENT_URL}/jobs {fulfillment_id}     (X-Agent-Key)
PrimeAgent trả 202 ngay, phân tích nền (tối đa AGENT_CONCURRENCY lượt cùng lúc):
  1. GET  {PRIME_API_BASE}/api/agent/fulfillments/<id>                   (X-Service-Key)
     ← đơn khách, item đã gửi + sku đã tra (loại áo/màu/size/mặt in), file thiết kế, mockup, ticket
  2. Kiểm bằng code: thiếu file, mặt in không hợp lệ, sku không tra được, lệch số lượng, item trùng
  3. Tải + thu nhỏ ảnh (Drive → thumbnail), gửi Claude/GPT, nhận JSON cố định:
     product_color · design · ticket · print_side  =  ok | warn | error | unknown
  4. POST {PRIME_API_BASE}/api/agent/fulfillments/<id>/result
     → backend lưu fulfill_ai_reviews, cập nhật order.agent_analysis, Telegram nếu Lỗi
```

`agent_analysis` trên order: `0` chưa phân tích · `1` đang phân tích · `2` OK · `3` cảnh báo · `4` lỗi · `9` thất bại.

Hàng chờ nằm trong RAM: PrimeAgent restart thì mất job đang chờ, nhưng backend tự gửi lại các lượt còn
`agent_analysis = 1` quá 10 phút (tối đa 5 lần, sau đó đánh dấu `9`). Trên web có nút **Phân tích lại**.

## Cài đặt

### Server đang chạy (142.93.2.252 — check-trademark): venv + systemd, không Docker

- Code: `/var/www/primeagent` (clone repo này) · env: `/var/www/primeagent/.env`
- Service: `primeagent` (gunicorn `127.0.0.1:5300`, `-w 1 --threads 4`, `MemoryMax=700M`)
- Log: `/var/log/primeagent/{access,error}.log`
- Ra ngoài qua nginx HTTPS: `https://trademark.primehorizon.studio/primeagent/` (cổng 5300 không mở)

Cập nhật code:

```bash
cd /var/www/primeagent && git pull && venv/bin/pip install -q -r requirements.txt
systemctl restart primeagent && curl -s http://127.0.0.1:5300/health
```

### Chạy ở máy local (không cần Docker)

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env      # điền key
venv/bin/python -m flask --app run:app run -p 5300
```

### Hoặc bằng Docker

```bash
cp .env.example .env      # điền key
docker compose up -d --build
curl http://localhost:5300/health
```

Biến bắt buộc: `PRIME_API_BASE`, `PRIME_SERVICE_KEY`, `AGENT_INBOUND_KEY`, và key AI (`ANTHROPIC_API_KEY`
hoặc `OPENAI_API_KEY` + `OPENAI_MODEL`). Phía backend-etsy cần:

| backend-etsy | PrimeAgent |
|---|---|
| `AGENT_URL` = `https://trademark.primehorizon.studio/primeagent` | — |
| `AGENT_INBOUND_KEY` | `AGENT_INBOUND_KEY` (giống nhau) |
| `AGENT_SERVICE_KEY` | `PRIME_SERVICE_KEY` (giống nhau) |
| `AGENT_TELEGRAM_CHAT_ID` = group test | — |

Tạo key ngẫu nhiên: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
Nên chặn cổng 5300 bằng firewall, chỉ mở cho IP server PrimeHorizon.

## So sánh Claude ⇄ GPT

Đổi `AI_PROVIDER=anthropic|openai` rồi restart, bấm **Phân tích lại** trên cùng các đơn. Mỗi lượt lưu
`provider`, `model`, `usage` (token) và lịch sử 10 lần gần nhất trong `fulfill_ai_reviews` để so sánh.

## Yêu cầu server

1 vCPU, ~1 GB RAM (`mem_limit: 1g`), không cần GPU. Mạng ra ngoài: API AI, Google Drive, CDN ảnh
(r2.dev, etsystatic, TikTok CDN) và server PrimeHorizon.

## Test

```bash
pip install -r requirements.txt pytest
pytest -q
```
