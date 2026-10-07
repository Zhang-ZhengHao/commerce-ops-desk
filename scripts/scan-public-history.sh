#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if (($# == 0)); then
  set -- "${script_dir}/.."
fi

exec python3 "${script_dir}/scan_public_history.py" "$@"
