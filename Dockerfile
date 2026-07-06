# ── Stage 1: Build ───────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir -r requirements.txt

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

COPY --from=builder /install /usr/local
COPY src/ ./src/

RUN mkdir -p /app/summaries /app/data /app/logs

RUN useradd -r -u 1001 -g root summarizer \
    && chown -R summarizer:root /app
USER summarizer

CMD ["python", "-m", "src.main", "--config", "/app/config.yaml"]
