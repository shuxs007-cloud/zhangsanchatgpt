#!/bin/bash
set -euo pipefail
cd /Users/zhangsan/Downloads/430
TASK_I_PYTHON=/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
exec "$TASK_I_PYTHON" scripts/opensms_task_i.py "$@"
