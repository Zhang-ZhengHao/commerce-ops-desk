#!/usr/bin/env bash
set -euo pipefail

PRODUCT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
ENV_FILE="${COMMERCE_OPS_ENV_FILE:-$PRODUCT_DIR/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  ENV_FILE="$PRODUCT_DIR/.env.example"
  echo "[commerce-ops-desk] .env not found; using safe local defaults from .env.example"
fi

set -a
# shellcheck disable=SC1090 -- the path is selected above or supplied explicitly.
source "$ENV_FILE"
set +a

export COMMERCE_OPS_VENV_DIR="${COMMERCE_OPS_VENV_DIR:-$PRODUCT_DIR/.venv}"
exec bash "$PRODUCT_DIR/scripts/start-hosted.sh"
