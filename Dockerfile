# ── Stage 1: build deps ───────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Instalar dependências de compilação (numpy, Pillow)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copiar deps instaladas do builder
COPY --from=builder /install /usr/local

# Copiar código-fonte
COPY app/ ./app/
COPY run.py .

# Diretório de dados persistentes (SQLite + uploads)
# Em deploy: montar um volume em /app/data
RUN mkdir -p /app/data/uploads

# Variáveis de ambiente padrão
ENV PORT=8000 \
    HOST=0.0.0.0 \
    RELOAD=false \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Healthcheck básico
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/login')" || exit 1

# Usar uvicorn direto (mais eficiente que via run.py em produção)
CMD ["sh", "-c", "uvicorn app.main:app --host $HOST --port $PORT --workers 1"]
