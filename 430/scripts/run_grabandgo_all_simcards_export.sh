#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
NODE_BIN="/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
NODE_MODULES="/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"
MARKER="/Users/zhangsan/.codex/plugins/cache/openai-primary-runtime/spreadsheets/26.826.12353/skills/spreadsheets/container_tools/mark_artifact_operation_started.mjs"
OUTPUT_ROOT="/Users/zhangsan/Documents/数据分析/outputs"
RUN_STAMP="$(TZ=Asia/Hong_Kong date '+%Y%m%dT%H%M%S%z')"
RUN_DIR="${OUTPUT_ROOT}/grabandgo_all_simcards_${RUN_STAMP}"
RAW_JSON="${RUN_DIR}/grabandgo_customer_sim_cards_all.json"
PROGRESS_JSON="${RUN_DIR}/grabandgo_customer_sim_cards_progress.json"
STATE_JSON="${RUN_DIR}/grabandgo_customer_sim_cards_state.json"
PARTS_DIR="${RUN_DIR}/parts"
XLSX_PATH="${RUN_DIR}/GrabAndGo_Customer_SIM_Cards_全部数据_${RUN_STAMP}.xlsx"
PREVIEW_DIR="${RUN_DIR}/previews"
BUILD_DIR="${RUN_DIR}/artifact_build"

mkdir -p "$RUN_DIR" "$BUILD_DIR" "$PARTS_DIR"
if [[ ! -e "${BUILD_DIR}/node_modules" ]]; then
  ln -s "$NODE_MODULES" "${BUILD_DIR}/node_modules"
fi

"$NODE_BIN" "${SCRIPT_DIR}/grabandgo_export_all_simcards_cdp.mjs" \
  --output-json "$RAW_JSON" \
  --progress-json "$PROGRESS_JSON" \
  --state-json "$STATE_JSON" \
  --parts-dir "$PARTS_DIR" \
  --fetch-all \
  --timeout-ms 900000

cd "$BUILD_DIR"
"$NODE_BIN" "$MARKER" --operation-kind create --expected-output-count 1 --output-format xlsx
"$NODE_BIN" "${SCRIPT_DIR}/build_grabandgo_all_simcards_xlsx.mjs" "$RAW_JSON" "$XLSX_PATH" "$PREVIEW_DIR"
unzip -t "$XLSX_PATH" >/dev/null
python3 "${SCRIPT_DIR}/verify_grabandgo_all_simcards_xlsx.py" "$XLSX_PATH" "$RAW_JSON" >/dev/null

"$NODE_BIN" --input-type=module -e '
  import fs from "node:fs";
  import crypto from "node:crypto";
  const [rawPath, xlsxPath] = process.argv.slice(1);
  const raw = JSON.parse(fs.readFileSync(rawPath, "utf8"));
  const bytes = fs.readFileSync(xlsxPath);
  console.log(JSON.stringify({
    status: "passed",
    total: raw.total,
    extractedRowCount: raw.extractedRowCount,
    fieldNames: raw.fieldNames,
    xlsxPath,
    rawJsonPath: rawPath,
    sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    xlsxBytes: bytes.length,
  }));
' "$RAW_JSON" "$XLSX_PATH"
