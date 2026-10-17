#!/usr/bin/env bash
set -euo pipefail

# Read-only workbook builder. This program never runs carrier writes or Task B.
if [[ $# -ne 4 ]]; then
  printf 'Usage: %s <export4day.xlsx> <google-card-slots.xlsx or - when OPENSMS_GOOGLE_ASSOCIATION_ENABLED=0> <output-dir> <server-date>\n' "$0" >&2
  exit 2
fi

PIPELINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN_PATH="$1"
ASSOC_PATH="$2"
OUTPUT_DIR="$3"
SERVER_DATE="$4"
PYTHON_BIN="${PYTHON_BIN:-}"
NODE_BIN="${NODE_BIN:-}"
STAMP="$(date '+%Y%m%dT%H%M%S%z')"
RECEIPT_DIR="${OPENSMS_RECEIPT_DIR:-$OUTPUT_DIR/artifacts/snapshots}"
PIPELINE_STARTED_SECONDS=$SECONDS

if [[ -z "$PYTHON_BIN" ]]; then
  BUNDLED_PYTHON="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
  if [[ -x "$BUNDLED_PYTHON" ]]; then
    PYTHON_BIN="$BUNDLED_PYTHON"
  else
    PYTHON_BIN="$(command -v python3 || true)"
  fi
fi
if [[ -z "$NODE_BIN" ]]; then
  BUNDLED_NODE="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
  if [[ -x "$BUNDLED_NODE" ]]; then
    NODE_BIN="$BUNDLED_NODE"
  else
    NODE_BIN="$(command -v node || true)"
  fi
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  printf 'Python runtime not found; set PYTHON_BIN.\n' >&2
  exit 3
fi
if [[ ! -x "$NODE_BIN" ]]; then
  printf 'Node runtime not found; set NODE_BIN.\n' >&2
  exit 3
fi
if [[ ! "$SERVER_DATE" =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2}$ ]]; then
  printf 'Invalid server date: %s\n' "$SERVER_DATE" >&2
  exit 3
fi

REQUIRED_INPUTS=("$MAIN_PATH")
if [[ "${OPENSMS_GOOGLE_ASSOCIATION_ENABLED:-1}" != "0" ]]; then
  REQUIRED_INPUTS+=("$ASSOC_PATH")
fi
for required in "${REQUIRED_INPUTS[@]}"; do
  if [[ ! -f "$required" ]]; then
    printf 'Input file does not exist: %s\n' "$required" >&2
    exit 3
  fi
done

MAIN_PATH="$(cd "$(dirname "$MAIN_PATH")" && pwd)/$(basename "$MAIN_PATH")"
if [[ "${OPENSMS_GOOGLE_ASSOCIATION_ENABLED:-1}" == "0" ]]; then
  ASSOC_PATH="-"
  ASSOC_SHA256=""
else
  ASSOC_PATH="$(cd "$(dirname "$ASSOC_PATH")" && pwd)/$(basename "$ASSOC_PATH")"
  ASSOC_SHA256="$("$PYTHON_BIN" -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$ASSOC_PATH")"
fi
mkdir -p "$OUTPUT_DIR" "$RECEIPT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
RECEIPT_DIR="$(cd "$RECEIPT_DIR" && pwd)"

PROCESS_STARTED_SECONDS=$SECONDS
PAYLOAD_DIR="$(mktemp -d "$RECEIPT_DIR/builder-${STAMP}.XXXXXX")"
export PROCESS_JSON_PATH="$PAYLOAD_DIR/process.json"
export ENRICH_JSON_PATH="$PAYLOAD_DIR/enrichment.json"
export VERIFY_JSON_PATH="$PAYLOAD_DIR/verification.json"
unset PROCESS_JSON ENRICH_JSON VERIFY_JSON ENRICH_RAW
OPENSMS_REPORT_ROOT="$PIPELINE_DIR" \
  OPENSMS_MAIN_PATH="$MAIN_PATH" \
  OPENSMS_ASSOC_PATH="$ASSOC_PATH" \
  OPENSMS_OUTPUT_DIR="$OUTPUT_DIR" \
  EXPORT4DAY_SERVER_DATE="$SERVER_DATE" \
  "$PYTHON_BIN" "$PIPELINE_DIR/process_card_slots.py" > "$PROCESS_JSON_PATH"
PROCESS_SECONDS=$((SECONDS - PROCESS_STARTED_SECONDS))

FINAL_PATH="$(
  "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

print(json.loads(Path(os.environ["PROCESS_JSON_PATH"]).read_text(encoding="utf-8"))["output_path"])
PY
)"

ENRICHMENT_INCLUDED="$(
  "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

print("true" if json.loads(Path(os.environ["PROCESS_JSON_PATH"]).read_text(encoding="utf-8")).get("enrichment_included") is True else "false")
PY
)"

if [[ "$ENRICHMENT_INCLUDED" == "true" && ( "${OPENSMS_LEGACY_DOCUMENT_ENRICHMENT:-0}" != "1" || "${OPENSMS_GOOGLE_ASSOCIATION_ENABLED:-1}" == "0" ) ]]; then
  ENRICH_SECONDS=0
  "$PYTHON_BIN" - > "$ENRICH_JSON_PATH" <<'PY'
import json
import os
from pathlib import Path

process = json.loads(Path(os.environ["PROCESS_JSON_PATH"]).read_text(encoding="utf-8"))
print(json.dumps(process["enrichment"], ensure_ascii=False))
PY
else
  ENRICH_STARTED_SECONDS=$SECONDS
  EXPORT4DAY_SERVER_DATE="$SERVER_DATE" \
    "$NODE_BIN" "$PIPELINE_DIR/add_a11_low_reply_sheet.mjs" "$FINAL_PATH" "$MAIN_PATH" > "$PAYLOAD_DIR/enrichment.raw"
  ENRICH_SECONDS=$((SECONDS - ENRICH_STARTED_SECONDS))

  "$PYTHON_BIN" - "$PAYLOAD_DIR/enrichment.raw" > "$ENRICH_JSON_PATH" <<'PY'
import json
import sys
from pathlib import Path

raw = Path(sys.argv[1]).read_text(encoding="utf-8")
start = raw.find("{")
if start < 0:
    raise SystemExit("Card-slot enrichment did not return JSON")
print(json.dumps(json.loads(raw[start:]), ensure_ascii=False))
PY
fi

INSPECT_SIDECAR="${FINAL_PATH}.inspect.ndjson"
rm -f "$INSPECT_SIDECAR"

VERIFY_STARTED_SECONDS=$SECONDS
OPENSMS_ASSOC_PATH="$ASSOC_PATH" \
  OPENSMS_ASSOC_SHA256="$ASSOC_SHA256" \
  EXPORT4DAY_SERVER_DATE="$SERVER_DATE" \
    "$PYTHON_BIN" "$PIPELINE_DIR/opensms_verify_card_slot_fast.py" "$FINAL_PATH" "$MAIN_PATH" > "$VERIFY_JSON_PATH"
VERIFY_SECONDS=$((SECONDS - VERIFY_STARTED_SECONDS))
PRE_RECEIPT_SECONDS=$((SECONDS - PIPELINE_STARTED_SECONDS))

RECEIPT_DIR="$RECEIPT_DIR" \
RECEIPT_STAMP="$STAMP" \
PROCESS_SECONDS="$PROCESS_SECONDS" \
ENRICH_SECONDS="$ENRICH_SECONDS" \
VERIFY_SECONDS="$VERIFY_SECONDS" \
PRE_RECEIPT_SECONDS="$PRE_RECEIPT_SECONDS" \
"$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

process = json.loads(Path(os.environ["PROCESS_JSON_PATH"]).read_text(encoding="utf-8"))
enrichment = json.loads(Path(os.environ["ENRICH_JSON_PATH"]).read_text(encoding="utf-8"))
verification = json.loads(Path(os.environ["VERIFY_JSON_PATH"]).read_text(encoding="utf-8"))

google_enabled = process.get("google_dependency", {}).get("enabled") is not False
checks = {
    **({"ko系统有-未激活独立核验": (verification.get("ko_unactivated_sheet_review") or {}).get("matches") is True}
       if os.environ.get("OPENSMS_KO_UNACTIVATED_REQUIRED") == "1" or process.get("ko_unactivated_sheet") else {}),
    "表头": verification.get("headers_match") is True,
    "最新机房配置及全部机房列独立回读": verification.get("room_configuration_review", {}).get("matches") is True,
    "卡槽别名": verification.get("slot_alias_review", {}).get("matches") is True,
    "Google字段映射": verification.get("google_field_mapping_review", {}).get("matches") is True,
    "Google三机房页签": verification.get("google_sheet_scope_review", {}).get("matches") is True,
    "Google系统缺失明细": verification.get("google_system_missing_review", {}).get("matches") is True,
    "系统无-谷歌表有": verification.get("unused_port_review", {}).get("matches") is True,
    "设备每日统计独立重算": verification.get("device_daily_statistics_review", {}).get("matches") is True,
    "电信商OPR预警": verification.get("carrier_opr_review", {}).get("matches") is True,
    "连续统计": verification.get("mismatch_count") == 0,
    "激活后天数": verification.get("activation_age_mismatch_count") == 0,
    "使用天数独立重算": verification.get("creation_usage_days_review", {}).get("matches") is True,
    "过期状态": verification.get("expiry_status_mismatch_count") == 0,
    "Kooda本轮系统四字段来源": verification.get("system_kooda_source_review", {}).get("matches") is True,
    "日期逐字段来源优先级与冻结缓存": verification.get("date_resolution_review", {}).get("matches") is True,
    "Kooda缺失激活日期整行标红": verification.get("task_f_missing_activation_red_review", {}).get("matches") is True,
    "Kooda双日期空白视为未换号新卡": verification.get("task_f_missing_dates_new_card_review", {}).get("matches") is True,
    "已取消工作表不存在": verification.get("cancelled_review_sheets", {}).get("matches") is True,
    "已激活未上架明细": verification.get("activated_unlisted_review", {}).get("details_match") is True,
    "未上架机器汇总": verification.get("activated_unlisted_review", {}).get("machine_summary_matches") is True,
    "超过5张机器": verification.get("activated_unlisted_review", {}).get("over_five_machines_match") is True,
    "已激活未上架汇总": verification.get("activated_unlisted_review", {}).get("summary_matches") is True,
    "回复率列位置": verification.get("window_metric_headers_match") is True,
    "3日4日回复率明细": verification.get("window_metric_mismatch_count") == 0,
    "持续降低计数": verification.get("window_rate_review", {}).get("counts_match") is True,
    "持续降低汇总": verification.get("window_rate_review", {}).get("summary_matches") is True,
    "公式错误": verification.get("formula_error_matches") == 0,
    "保留源单元格类型": enrichment.get("cell_type_policy", {}).get("numeric_text_conversion_enabled") is False,
}
if not google_enabled:
    for name in ("Google字段映射", "Google三机房页签", "Google系统缺失明细", "电信商OPR预警"):
        del checks[name]
    checks["Google已禁用且专属内容不存在"] = verification.get("google_dependency_review", {}).get("matches") is True
    checks["换号状态独立重算"] = verification.get("number_change_review", {}).get("matches") is True
    checks["系统机房及开卡日期"] = verification.get("system_only_fields_review", {}).get("matches") is True
if process.get("google_chatr_date_fallback") or verification.get("google_chatr_date_fallback_review"):
    checks["Chatr专用Google日期来源及逐字段回退"] = verification.get("google_chatr_date_fallback_review", {}).get("matches") is True
if os.environ.get("OPENSMS_GOOGLE_SYSTEM_MISSING_ENABLED") == "1" or process.get("google_system_missing_sheet"):
    checks["所有谷歌有系统没有独立差集核验"] = (verification.get("google_system_missing_sheet_review") or {}).get("matches") is True
failed = [name for name, passed in checks.items() if not passed]
if process.get("card_retention_report") or os.environ.get("OPENSMS_CARD_RETENTION_REPORT_REQUIRED") == "1":
    checks["需要保留时间及卡保留全量明细"] = verification.get("card_retention_report_review", {}).get("matches") is True
    failed = [name for name, passed in checks.items() if not passed]
result = {
    "result": "成功" if not failed else "失败",
    "output_path": process["output_path"],
    "checks": checks,
    "failed_checks": failed,
    "process": process,
    "enrichment": enrichment,
    "verification": verification,
    "stage_timings_seconds": {
        "build_main_workbook": int(os.environ["PROCESS_SECONDS"]),
        "enrich_workbook": int(os.environ["ENRICH_SECONDS"]),
        "verify_workbook": int(os.environ["VERIFY_SECONDS"]),
        "before_receipt": int(os.environ["PRE_RECEIPT_SECONDS"]),
        "legacy_second_full_workbook_pass_skipped": enrichment.get("implementation") == "single_pass_python_enrichment",
    },
    "task_b_executed": False,
}
receipt_dir = Path(os.environ["RECEIPT_DIR"])
receipt_path = receipt_dir / f"opensms_card_slot_report_{os.environ['RECEIPT_STAMP']}_receipt.json"
result["receipt_path"] = str(receipt_path)
rendered = json.dumps(result, ensure_ascii=False, indent=2)
receipt_path.write_text(rendered + "\n", encoding="utf-8")
print(rendered)
if failed:
    raise SystemExit(4)
PY
