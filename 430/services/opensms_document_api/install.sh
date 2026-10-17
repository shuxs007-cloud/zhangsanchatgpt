#!/usr/bin/env bash
set -euo pipefail

SERVICE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEM_PYTHON="${SYSTEM_PYTHON:-$(command -v python3 || true)}"
if [[ -z "$SYSTEM_PYTHON" ]]; then
  printf 'Python 3 is required.\n' >&2
  exit 2
fi

if [[ ! -d "$SERVICE_ROOT/.venv" ]]; then
  "$SYSTEM_PYTHON" -m venv "$SERVICE_ROOT/.venv"
fi
"$SERVICE_ROOT/.venv/bin/python3" -m pip install --upgrade pip
"$SERVICE_ROOT/.venv/bin/python3" -m pip install -r "$SERVICE_ROOT/requirements.txt"

NODE_MODULES_DIR="${OPENSMS_NODE_MODULES_DIR:-$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules}"
if [[ ! -d "$NODE_MODULES_DIR/@oai/artifact-tool" ]]; then
  printf 'Missing @oai/artifact-tool. Install Codex desktop runtime or set OPENSMS_NODE_MODULES_DIR.\n' >&2
  exit 3
fi
if [[ -e "$SERVICE_ROOT/node_modules" && ! -L "$SERVICE_ROOT/node_modules" ]]; then
  printf 'Refusing to replace existing node_modules: %s\n' "$SERVICE_ROOT/node_modules" >&2
  exit 3
fi
rm -f "$SERVICE_ROOT/node_modules"
ln -s "$NODE_MODULES_DIR" "$SERVICE_ROOT/node_modules"
chmod +x "$SERVICE_ROOT/run_server.sh" "$SERVICE_ROOT/pipeline/build_card_slot_report.sh"

printf 'Installation complete. Copy config.env.example to .env and set a strong token.\n'
