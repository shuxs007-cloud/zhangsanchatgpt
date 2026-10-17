#!/usr/bin/env bash
set -euo pipefail
TASK_H_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_H_NODE="/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
cd "$TASK_H_ROOT"
# H has no inherited enable flags and never calls export, F, or timers.
exec "$TASK_H_NODE" "$TASK_H_ROOT/scripts/opensms_task_h.mjs" "$@"
