#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv-opensms-audit"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

if [[ -z "$PYTHON_BIN" ]]; then
  echo "未找到 Python 3，无法安装 OpenSMS 检查环境。" >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 9):
    raise SystemExit("需要 Python 3.9 或更高版本。")
PY

if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python3" -m py_compile \
  "$ROOT_DIR/scripts/opensms_parallel_audit.py"

cd "$ROOT_DIR"
"$VENV_DIR/bin/python3" -m unittest tests.test_opensms_parallel_audit

echo "OpenSMS 代码检查环境已就绪：$VENV_DIR"
echo "固定入口：$ROOT_DIR/scripts/run_opensms_parallel_audit.sh"
