#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_PYTHON="/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
case "${1:-export}" in
  import-renewals)
    shift
    exec "$TASK_PYTHON" "$TASK_ROOT/scripts/opensms_f_verified_renewal.py" "$@"
    ;;
  retry-current-session)
    shift
    exec "$TASK_PYTHON" "$TASK_ROOT/scripts/opensms_task_f_background.py" --retry-current-session "$@"
    ;;
  export)
    if [[ $# -gt 0 ]]; then shift; fi
    exec "$TASK_PYTHON" "$TASK_ROOT/scripts/opensms_task_f_reconcile_document.py" --source-kind system-export "$@"
    ;;
  export-all)
    shift
    exec "$TASK_PYTHON" "$TASK_ROOT/scripts/opensms_export_task_f_deadlines.py" "$@"
    ;;
  sync-export)
    shift
    if [[ $# -ne 2 || "$1" != "--authorization-source" || "$2" != "current-user" ]]; then
      printf '%s\n' 'sync-export只接受当前用户授权：sync-export --authorization-source current-user' >&2
      exit 2
    fi
    # Clear unrelated inherited gates. This mode can run F only, never B/C/D/E.
    unset OPENSMS_FORCE_TASK_B OPENSMS_FORCE_TASK_C OPENSMS_FORCE_TASK_D OPENSMS_FORCE_TASK_E
    unset OPENSMS_ENABLE_TASK_D OPENSMS_EXECUTE_TASK_D OPENSMS_ENABLE_TASK_E OPENSMS_EXECUTE_TASK_E
    export OPENSMS_FORCE_TASK_F=1 OPENSMS_ENABLE_TASK_F=1 OPENSMS_EXECUTE_TASK_F=1
    exec "$TASK_ROOT/scripts/run_opensms_patrol.sh" run --json --task-f-only --force-task-f \
      --authorization-source current-user --task-f-workers 8
    ;;
  status)
    shift
    if [[ $# -ne 3 || "$1" != "--state" || -z "$2" || "$3" != "--json" ]]; then
      printf '%s\n' '只读进度：status --state <本轮runner状态绝对路径> --json' >&2
      exit 2
    fi
    if [[ "$2" != /* ]]; then
      printf '%s\n' '状态文件必须是本轮runner状态的绝对路径。' >&2
      exit 2
    fi
    exec "$TASK_PYTHON" "$TASK_ROOT/scripts/opensms_task_f_progress_status.py" "$@"
    ;;
  -h|--help|help)
    printf '%s\n' '人工续费表离线更新及导出：run_opensms_task_f.sh import-renewals --source <续费表> --identity-state <成功巡检状态> --authorization-source current-user' \
      '按报表缺激活日期导出（无网络）：run_opensms_task_f.sh export --state <成功巡检状态绝对路径> --json' \
      '仅明确要求全库时：run_opensms_task_f.sh export-all --json' \
      '当前用户授权F增量同步后导出：run_opensms_task_f.sh sync-export --authorization-source current-user' \
      '只读F进度：run_opensms_task_f.sh status --state <本轮runner状态绝对路径> --json' \
      '手动巡检先交付主表，F独立后台补查；只有新激活日期入库才另生增量表。' \
      '普通手动/定时导出自动补查缺失或仅有创建日期兜底的Kooda；已有有效日期去重。显式A-only才跳过F；B/C/D/E默认关闭。'
    ;;
  *) printf '%s\n' '仅支持 import-renewals、export、export-all、sync-export、retry-current-session、status、--help' >&2; exit 2 ;;
esac
