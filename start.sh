#!/usr/bin/env bash
# FluxoPay — Gestão de Contas a Pagar
# Uso: ./start.sh [porta]

PORTA="${1:-8000}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -d "$DIR/.venv" ]; then
    echo "Criando ambiente virtual..."
    python3 -m venv "$DIR/.venv"
    "$DIR/.venv/bin/pip" install -q fastapi uvicorn[standard] jinja2 python-multipart pypdf reportlab pillow
fi

echo "Iniciando FluxoPay em http://localhost:${PORTA}"
cd "$DIR"
"$DIR/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$PORTA" --reload
