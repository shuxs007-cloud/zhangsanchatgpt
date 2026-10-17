#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 && $# -ne 4 ]]; then
  printf 'Usage: %s <export4day.xlsx> [ignored legacy association] <output-dir> <server-date>\n' "$0" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  printf 'Python runtime not found; set PYTHON_BIN.\n' >&2
  exit 3
fi

if [[ $# -eq 3 ]]; then
  OUTPUT_DIR="$2"
  SERVER_DATE="$3"
else
  OUTPUT_DIR="$3"
  SERVER_DATE="$4"
fi
export OPENSMS_GOOGLE_ASSOCIATION_ENABLED=0
exec "$PYTHON_BIN" "$SCRIPT_DIR/opensms_process_document.py" \
  --export "$1" \
  --no-google \
  --output-dir "$OUTPUT_DIR" \
  --server-date "$SERVER_DATE"
