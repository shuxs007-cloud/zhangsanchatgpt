#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"

if [[ ! -x "$PYTHON_BIN" ]]; then
  printf 'OpenSMS patrol Python runtime not found: %s\n' "$PYTHON_BIN" >&2
  exit 3
fi

if [[ "${1:-}" == "run" ]]; then
  # Resolve only after volume/link checks; ignore stale local download overrides.
  OPENSMS_EXPORT_DIR="$("$PYTHON_BIN" "$ROOT_DIR/scripts/opensms_storage_policy.py" download-root)"
  export OPENSMS_EXPORT_DIR
fi
exec "$PYTHON_BIN" "$ROOT_DIR/scripts/opensms_patrol_runner.py" "$@"
