FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

EXPOSE 5300
# 1 tiến trình (hàng chờ nằm trong RAM của tiến trình) + vài luồng cho request /jobs, /health.
CMD ["gunicorn", "-b", "0.0.0.0:5300", "-w", "1", "--threads", "4", "--timeout", "60", "run:app"]
