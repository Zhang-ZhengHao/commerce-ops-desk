#!/usr/bin/env bash
set -euo pipefail

PRODUCT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="${PYTHON:-$PRODUCT_DIR/.venv/bin/python}"

if [[ ! -x "$PYTHON" ]]; then
  echo "[commerce-ops-desk smoke] Python environment is missing; run make setup first" >&2
  exit 70
fi

cd "$PRODUCT_DIR"
exec "$PYTHON" -m pytest scripts/tests/test_start_hosted.py -q
