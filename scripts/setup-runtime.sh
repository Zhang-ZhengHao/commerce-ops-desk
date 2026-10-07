#!/usr/bin/env bash
set -euo pipefail

PRODUCT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
BACKEND_DIR="$PRODUCT_DIR/backend"
FRONTEND_DIR="$PRODUCT_DIR/frontend"
REQUIREMENTS_FILE="$BACKEND_DIR/requirements.lock"
WHEELHOUSE_DIR="${COMMERCE_OPS_WHEELHOUSE_DIR:-$BACKEND_DIR/wheelhouse}"
VENV_DIR="${COMMERCE_OPS_VENV_DIR:-/var/tmp/commerce-ops-desk-venv}"
SETUP_LOCK="${COMMERCE_OPS_SETUP_LOCK:-/var/tmp/.commerce-ops-desk-setup.lock}"
SETUP_FRONTEND="${COMMERCE_OPS_SETUP_FRONTEND:-1}"
OFFLINE_ONLY="${COMMERCE_OPS_OFFLINE_ONLY:-0}"
PIP_CACHE_DIR="${PIP_CACHE_DIR:-$PRODUCT_DIR/.cache/pip}"
NPM_CACHE_DIR="${npm_config_cache:-$PRODUCT_DIR/.cache/npm}"

if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
  echo "[commerce-ops-desk setup] missing $REQUIREMENTS_FILE" >&2
  exit 66
fi

mkdir -p "$(dirname -- "$SETUP_LOCK")" "$PIP_CACHE_DIR" "$NPM_CACHE_DIR"

(
  flock -w 900 9 || {
    echo "[commerce-ops-desk setup] another setup process holds the lock" >&2
    exit 75
  }

  requirements_hash="$(sha256sum "$REQUIREMENTS_FILE" | awk '{print $1}')"
  python_bin="$VENV_DIR/bin/python"
  runtime_marker="$VENV_DIR/.requirements.sha256"
  core_imports='import alembic, fastapi, psycopg, pydantic_settings, sqlalchemy, uvicorn'

  if [[ -f "$runtime_marker" ]] \
    && [[ "$(<"$runtime_marker")" == "$requirements_hash" ]] \
    && "$python_bin" -c "$core_imports" >/dev/null 2>&1; then
    echo "[commerce-ops-desk setup] Python runtime already current"
  else
    mkdir -p "$WHEELHOUSE_DIR"
    if [[ "$OFFLINE_ONLY" != "1" ]]; then
      python3 -m pip download \
        --cache-dir "$PIP_CACHE_DIR" \
        --find-links "$WHEELHOUSE_DIR" \
        --timeout 300 \
        --retries 10 \
        --requirement "$REQUIREMENTS_FILE" \
        --dest "$WHEELHOUSE_DIR"
    fi

    python3 -m venv --clear "$VENV_DIR"
    "$python_bin" -m pip install \
      --no-index \
      --find-links "$WHEELHOUSE_DIR" \
      --requirement "$REQUIREMENTS_FILE"
    "$python_bin" -c "$core_imports"
    printf '%s\n' "$requirements_hash" >"$runtime_marker"
    echo "[commerce-ops-desk setup] Python runtime rebuilt from wheelhouse"
  fi

  if [[ "$SETUP_FRONTEND" == "1" ]]; then
    if [[ ! -f "$FRONTEND_DIR/package-lock.json" ]]; then
      echo "[commerce-ops-desk setup] missing frontend/package-lock.json" >&2
      exit 66
    fi

    frontend_hash="$(sha256sum "$FRONTEND_DIR/package-lock.json" | awk '{print $1}')"
    frontend_marker="$FRONTEND_DIR/node_modules/.package-lock.sha256"
    if [[ ! -f "$frontend_marker" ]] \
      || [[ "$(<"$frontend_marker")" != "$frontend_hash" ]] \
      || [[ ! -x "$FRONTEND_DIR/node_modules/.bin/vite" ]] \
      || [[ ! -x "$FRONTEND_DIR/node_modules/.bin/tsc" ]]; then
      (
        cd "$FRONTEND_DIR"
        npm ci --no-audit --no-fund --cache "$NPM_CACHE_DIR"
      )
      printf '%s\n' "$frontend_hash" >"$frontend_marker"
      echo "[commerce-ops-desk setup] Frontend dependencies installed"
    else
      echo "[commerce-ops-desk setup] Frontend dependencies already current"
    fi

    (
      cd "$FRONTEND_DIR"
      npm run build
    )
  fi
) 9>"$SETUP_LOCK"
