#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PORT:-}" ]]; then
  echo "[commerce-ops-desk] PORT is required" >&2
  exit 64
fi

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || ((PORT < 1 || PORT > 65535)); then
  echo "[commerce-ops-desk] PORT must be an integer from 1 to 65535" >&2
  exit 64
fi

PRODUCT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV_DIR="${COMMERCE_OPS_VENV_DIR:-/var/tmp/commerce-ops-desk-venv}"
PYTHON_BIN="$VENV_DIR/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[commerce-ops-desk] Python runtime is missing; run the workspace setup first" >&2
  exit 70
fi

if [[ ! -f "$PRODUCT_DIR/frontend/dist/index.html" ]]; then
  echo "[commerce-ops-desk] Frontend build is missing; run npm run build first" >&2
  exit 70
fi

mkdir -p "$PRODUCT_DIR/data"
cd "$PRODUCT_DIR"

export COMMERCE_OPS_ENVIRONMENT="${COMMERCE_OPS_ENVIRONMENT:-demo}"

"$PYTHON_BIN" -m alembic \
  -c "$PRODUCT_DIR/backend/alembic.ini" \
  upgrade head

exec "$PYTHON_BIN" -m uvicorn app.main:create_app \
  --factory \
  --app-dir "$PRODUCT_DIR/backend" \
  --host 0.0.0.0 \
  --port "$PORT"
