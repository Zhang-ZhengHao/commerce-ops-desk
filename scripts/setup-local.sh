#!/usr/bin/env bash
set -euo pipefail

PRODUCT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
BACKEND_DIR="$PRODUCT_DIR/backend"
FRONTEND_DIR="$PRODUCT_DIR/frontend"
VENV_DIR="${COMMERCE_OPS_LOCAL_VENV_DIR:-$PRODUCT_DIR/.venv}"
SETUP_FRONTEND="${COMMERCE_OPS_SETUP_FRONTEND:-1}"
SETUP_BROWSER="${COMMERCE_OPS_SETUP_BROWSER:-1}"
PIP_CACHE_DIR="${PIP_CACHE_DIR:-$PRODUCT_DIR/.cache/pip}"
NPM_CACHE_DIR="${npm_config_cache:-$PRODUCT_DIR/.cache/npm}"
SETUP_LOCK="${COMMERCE_OPS_LOCAL_SETUP_LOCK:-$PRODUCT_DIR/.cache/setup-local.lock}"
DEV_REQUIREMENTS="$BACKEND_DIR/requirements-dev.lock"
RUNTIME_REQUIREMENTS="$BACKEND_DIR/requirements.lock"
WHEELHOUSE_DIR="${COMMERCE_OPS_WHEELHOUSE_DIR:-$BACKEND_DIR/wheelhouse}"

if [[ ! -f "$DEV_REQUIREMENTS" || ! -f "$FRONTEND_DIR/package-lock.json" ]]; then
  echo "[commerce-ops-desk setup] dependency lock files are required" >&2
  exit 66
fi

mkdir -p \
  "$(dirname -- "$SETUP_LOCK")" \
  "$PIP_CACHE_DIR" \
  "$NPM_CACHE_DIR" \
  "$WHEELHOUSE_DIR"

(
  flock -w 900 9 || {
    echo "[commerce-ops-desk setup] another local setup process holds the lock" >&2
    exit 75
  }

  python3 -m venv "$VENV_DIR"
  python_bin="$VENV_DIR/bin/python"
  requirements_hash="$(sha256sum "$BACKEND_DIR/requirements.lock" "$DEV_REQUIREMENTS" | sha256sum | awk '{print $1}')"
  requirements_marker="$VENV_DIR/.requirements-dev.sha256"
  dev_imports='import alembic, fastapi, httpx2, mypy, psycopg, pytest, ruff, sqlalchemy, uvicorn'

  if [[ ! -f "$requirements_marker" ]] \
    || [[ "$(<"$requirements_marker")" != "$requirements_hash" ]] \
    || ! "$python_bin" -c "$dev_imports" >/dev/null 2>&1; then
    if ! "$python_bin" -m pip install \
      --no-index \
      --find-links "$WHEELHOUSE_DIR" \
      --requirement "$DEV_REQUIREMENTS" >/dev/null 2>&1; then
      "$python_bin" -m pip install \
        --cache-dir "$PIP_CACHE_DIR" \
        --find-links "$WHEELHOUSE_DIR" \
        --requirement "$DEV_REQUIREMENTS"
    fi
    "$python_bin" -c "$dev_imports"
    printf '%s\n' "$requirements_hash" >"$requirements_marker"
    echo "[commerce-ops-desk setup] Python development environment installed"
  else
    echo "[commerce-ops-desk setup] Python development environment already current"
  fi

  runtime_hash="$(sha256sum "$RUNTIME_REQUIREMENTS" | awk '{print $1}')"
  wheelhouse_marker="$WHEELHOUSE_DIR/.requirements.sha256"
  wheelhouse_changed=0
  if ! "$python_bin" -m pip download \
    --no-index \
    --find-links "$WHEELHOUSE_DIR" \
    --requirement "$RUNTIME_REQUIREMENTS" \
    --dest "$WHEELHOUSE_DIR" >/dev/null 2>&1; then
    "$python_bin" -m pip download \
      --cache-dir "$PIP_CACHE_DIR" \
      --find-links "$WHEELHOUSE_DIR" \
      --requirement "$RUNTIME_REQUIREMENTS" \
      --dest "$WHEELHOUSE_DIR"
    "$python_bin" -m pip download \
      --no-index \
      --find-links "$WHEELHOUSE_DIR" \
      --requirement "$RUNTIME_REQUIREMENTS" \
      --dest "$WHEELHOUSE_DIR" >/dev/null
    wheelhouse_changed=1
  fi
  if [[ ! -f "$wheelhouse_marker" ]] \
    || [[ "$(<"$wheelhouse_marker")" != "$runtime_hash" ]]; then
    printf '%s\n' "$runtime_hash" >"$wheelhouse_marker"
    wheelhouse_changed=1
  fi
  if [[ "$wheelhouse_changed" == "1" ]]; then
    echo "[commerce-ops-desk setup] Runtime recovery wheelhouse prepared"
  else
    echo "[commerce-ops-desk setup] Runtime recovery wheelhouse already current"
  fi

  if [[ "$SETUP_FRONTEND" == "1" ]]; then
    frontend_hash="$(sha256sum "$FRONTEND_DIR/package-lock.json" | awk '{print $1}')"
    frontend_marker="$FRONTEND_DIR/node_modules/.package-lock.sha256"
    if [[ ! -f "$frontend_marker" ]] \
      || [[ "$(<"$frontend_marker")" != "$frontend_hash" ]] \
      || [[ ! -x "$FRONTEND_DIR/node_modules/.bin/vite" ]] \
      || [[ ! -x "$FRONTEND_DIR/node_modules/.bin/tsc" ]] \
      || [[ ! -x "$FRONTEND_DIR/node_modules/.bin/playwright" ]]; then
      (
        cd "$FRONTEND_DIR"
        npm ci --no-audit --no-fund --cache "$NPM_CACHE_DIR"
      )
      printf '%s\n' "$frontend_hash" >"$frontend_marker"
      echo "[commerce-ops-desk setup] Frontend dependencies installed"
    else
      echo "[commerce-ops-desk setup] Frontend dependencies already current"
    fi

    if [[ "$SETUP_BROWSER" == "1" ]]; then
      (
        cd "$FRONTEND_DIR"
        npx playwright install chromium
      )
      echo "[commerce-ops-desk setup] Playwright Chromium available"
    fi
  fi
) 9>"$SETUP_LOCK"
