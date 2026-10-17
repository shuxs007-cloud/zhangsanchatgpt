#!/usr/bin/env bash
set -euo pipefail

TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -gt 1 ]]; then
  printf '%s\n' '任务A仅接受 --json 或 --help；禁止附带其它任务参数。' >&2
  exit 2
fi
case "${1:---json}" in
  --json) ;;
  -h|--help)
    printf '%s\n' '定时任务A：run_opensms_task_a.sh --json' \
      '新鲜导出、普通规则、独立回读、合并报表及审计；B/C/D/E/F全部跳过。'
    exit 0
    ;;
  *) printf '%s\n' '任务A仅接受 --json 或 --help；禁止附带其它任务参数。' >&2; exit 2 ;;
esac

unset OPENSMS_FORCE_TASK_B OPENSMS_FORCE_TASK_C OPENSMS_FORCE_TASK_D OPENSMS_FORCE_TASK_E
unset OPENSMS_ENABLE_TASK_D OPENSMS_EXECUTE_TASK_D OPENSMS_ENABLE_TASK_E OPENSMS_EXECUTE_TASK_E
unset OPENSMS_FORCE_TASK_F OPENSMS_ENABLE_TASK_F OPENSMS_EXECUTE_TASK_F OPENSMS_TASK_F_POLICY_CATALOG_SYNC
exec "$TASK_ROOT/scripts/run_opensms_patrol.sh" run --task-a-only --json
