#!/usr/bin/env bash
set -euo pipefail

SERVICE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SERVICE_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$SERVICE_ROOT/.env"
  set +a
fi

if [[ -x "$SERVICE_ROOT/.venv/bin/python3" ]]; then
  PYTHON_BIN="$SERVICE_ROOT/.venv/bin/python3"
elif [[ -x "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" ]]; then
  PYTHON_BIN="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
else
  PYTHON_BIN="$(command -v python3 || true)"
fi

if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  printf 'Python 3 not found. Run install.sh first.\n' >&2
  exit 2
fi

export PYTHON_BIN
DATA_ROOT="${OPENSMS_API_DATA_ROOT:-$SERVICE_ROOT/data}"
if [[ "$DATA_ROOT" != /* ]]; then
  DATA_ROOT="$SERVICE_ROOT/$DATA_ROOT"
fi
export OPENSMS_API_DATA_ROOT="$DATA_ROOT"
exec "$PYTHON_BIN" "$SERVICE_ROOT/server.py" "$@"
