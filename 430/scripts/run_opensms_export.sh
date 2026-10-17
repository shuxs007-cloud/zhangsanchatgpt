#!/usr/bin/env bash
set -euo pipefail

TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -gt 1 ]]; then
  printf '%s\n' '导出仅接受 --json 或 --help；B/C/D/E 不在此入口执行。' >&2
  exit 2
fi
case "${1:---json}" in
  --json) ;;
  -h|--help)
    printf '%s\n' 'run_opensms_export.sh --json：新鲜任务A报表及自动后台任务F。' \
      'F 只补查本轮文档剩余 Kooda 日期缺口，结果供下次导出使用；B/C/D/E 关闭。'
    exit 0
    ;;
  *) printf '%s\n' '导出仅接受 --json 或 --help；B/C/D/E 不在此入口执行。' >&2; exit 2 ;;
esac

# Discard inherited task authorization and F diagnostic overrides. The normal
# runner owns conditional F dispatch, eight isolated workers and all validation.
for task_variable in ${!OPENSMS_FORCE_TASK_@} ${!OPENSMS_ENABLE_TASK_@} ${!OPENSMS_EXECUTE_TASK_@} ${!OPENSMS_TASK_F_@}; do
  unset "$task_variable"
done
exec "$TASK_ROOT/scripts/run_opensms_patrol.sh" run --json
