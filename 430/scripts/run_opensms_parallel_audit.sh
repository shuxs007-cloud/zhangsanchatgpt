#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BUNDLED_PYTHON="/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
LEGACY_AUDIT_PYTHON="$ROOT_DIR/.venv-opensms-audit/bin/python3"
PYTHON_BIN="${OPENSMS_AUDIT_PYTHON:-$BUNDLED_PYTHON}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  if [[ -x "$LEGACY_AUDIT_PYTHON" ]]; then
    PYTHON_BIN="$LEGACY_AUDIT_PYTHON"
  else
    echo "OpenSMS 审计运行时不可用。" >&2
    echo "缺少捆绑运行时：$BUNDLED_PYTHON" >&2
    echo "也未找到旧审计环境：$LEGACY_AUDIT_PYTHON" >&2
    exit 1
  fi
fi

exec "$PYTHON_BIN" "$ROOT_DIR/scripts/opensms_parallel_audit.py" "$@"
