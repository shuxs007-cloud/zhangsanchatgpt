#!/usr/bin/env python3
import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

from opensms_export_scan_cache import load_scan_cache

import patch_0500_origins_and_restore as history
from opensms_artifact_paths import SNAPSHOT_DIR, ensure_artifact_dirs


DEFAULT_E_CARRIERS = ("E-T休息区", "E-C休息区")
UNKNOWN = "归属未识别/E区历史"
DEFAULT_A1_TARGET = "A1-精普-C120"
RETIRED_ORIGIN_ALIASES = {"B2", "B21", "B13", "B14", "D1", "D3-U8"}
PROTECTED_ORIGIN_ALIASES = {"A21", "A41", "A42", "A2"}


def as_int(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def origin_alias(origin):
    match = re.match(r"^([A-Z]+\d+(?:-U\d+)?)(?:-|$)", str(origin or "").strip(), re.I)
    return match.group(1).upper() if match else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("export")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--out")
    parser.add_argument("--a1-carrier", default=DEFAULT_A1_TARGET)
    parser.add_argument(
        "--e-carrier",
        action="append",
        default=None,
        help="可重复提供休息区精确名称；默认同时处理E-T休息区和E-C休息区",
    )
    parser.add_argument("--canonical-carrier", action="append", default=[])
    parser.add_argument("--scan-cache")
    args = parser.parse_args()
    e_carriers = {name.strip() for name in (args.e_carrier or DEFAULT_E_CARRIERS) if name.strip()}
    if not e_carriers:
        raise ValueError("至少需要一个休息区精确名称")

    canonical = {}
    for item in args.canonical_carrier:
        alias, separator, full_name = item.partition("=")
        if not separator or not alias.strip() or not full_name.strip():
            raise ValueError(f"无效 canonical-carrier: {item}")
        canonical[alias.strip().upper()] = full_name.strip()

    def canonical_origin(origin):
        text = str(origin or "").strip()
        match = re.match(r"^([A-Z]+\d+(?:-U\d+)?)(?:-|$)", text, re.I)
        if not match:
            return text
        alias = match.group(1).upper()
        if alias in RETIRED_ORIGIN_ALIASES or alias in PROTECTED_ORIGIN_ALIASES:
            return UNKNOWN
        return canonical.get(alias, text)

    ensure_artifact_dirs()

    history.EXPORT_PATH = os.path.abspath(args.export)
    wb = None
    if args.scan_cache:
        scan_cache = load_scan_cache(args.scan_cache, expected_source=args.export)
        header_values = scan_cache["headers"]
        source_rows = scan_cache["rows"]
    else:
        wb = load_workbook(args.export, read_only=True, data_only=True)
        ws = wb.active
        row_iterator = ws.iter_rows(values_only=True)
        header_values = next(row_iterator)
        source_rows = row_iterator
    names = [str(value or "").strip() for value in header_values]
    headers = {name: index for index, name in enumerate(names) if name}
    latest = max(name[:5] for name in names if re.fullmatch(r"\d{2}-\d{2}发送窗口数", name))
    rows = []
    e_total = 0
    e_total_by_carrier = Counter()
    reply_positive_by_carrier = Counter()

    for values in source_rows:
        def value(name):
            index = headers.get(name)
            return values[index] if index is not None and index < len(values) else None

        current_e_carrier = str(value("运营商") or "").strip()
        if current_e_carrier not in e_carriers:
            continue
        e_total += 1
        e_total_by_carrier[current_e_carrier] += 1
        reply = as_int(value(f"{latest}回复窗口数"))
        if reply <= 0:
            continue
        reply_positive_by_carrier[current_e_carrier] += 1
        opr = str(value("OPR") or value("opr") or "").strip()
        row = {
            "电话": history.norm_phone(value("电话")),
            "phone": history.norm_phone(value("电话")),
            "运营商": current_e_carrier,
            "opr": opr,
            "卡槽": history.norm_slot(value("卡槽")),
            "状态": str(value("状态") or "").strip(),
            "发送窗口数": as_int(value(f"{latest}发送窗口数")),
            "回复窗口数": reply,
            "latestDate": latest,
            "规则": "e_current_reply_gt0_restore_origin",
        }
        rows.append(row)
    if wb is not None:
        wb.close()

    # No history enumeration/parsing when no current row needs recovery.
    origin_map = history.build_origin_map(keys={history.key_for(row) for row in rows})
    for row in rows:
        info = origin_map.get(history.key_for(row))
        raw_origin = info["origin"] if info else UNKNOWN
        origin = canonical_origin(raw_origin) if info else UNKNOWN
        row["原运营商"] = origin
        row["目标运营商"] = origin
        row["targetCarrierName"] = origin
        row["归属识别来源"] = info["source"] if info else ""
        row["归属识别来源类型"] = info["sourceType"] if info else "unidentified_e_history"
        row["恢复模式"] = "restore_identified_origin" if info else "pending_balanced_fallback"

    unidentified = sorted(
        (row for row in rows if row["原运营商"] == UNKNOWN),
        key=lambda row: (row["phone"], row["卡槽"]),
    )
    for row in unidentified:
        row["目标运营商"] = ""
        row["targetCarrierName"] = ""
        row["归属识别来源类型"] = "unidentified_origin_keep_e"
        row["恢复模式"] = "pending_origin_identification"

    recoverable = [row for row in rows if row["原运营商"] != UNKNOWN]
    counts = Counter(row["targetCarrierName"] for row in recoverable)
    unidentified_count = len(unidentified)

    out = {
        "createdAt": datetime.now().astimezone().isoformat(),
        "export": os.path.abspath(args.export),
        "latestDate": latest,
        "rule": "E-T休息区或E-C休息区最新日期回复窗口数 > 0；按转入休息区前备份恢复至精确原运营商，不排除B区或Kooda/Koodo",
        "eTotal": e_total,
        "eTotalByCarrier": dict(e_total_by_carrier),
        "replyPositiveCount": len(rows),
        "replyPositiveByCarrier": dict(reply_positive_by_carrier),
        "count": len(recoverable),
        "recoverableCount": len(recoverable),
        "identifiedOriginCount": len(recoverable),
        "unidentifiedOriginCount": unidentified_count,
        "fallbackAssignedCount": 0,
        "koodaExcludedSameRoundCount": 0,
        "bZoneOriginExcludedCount": 0,
        "bZoneOriginExcludedByAlias": {},
        "fallbackRule": "原运营商无法识别时保留在E区并报告，禁止猜测目标运营商",
        "fallbackByTarget": {},
        "byTarget": dict(counts),
        "sharedScanCacheUsed": bool(args.scan_cache),
        "candidateProcessWorkbookReopens": 0 if args.scan_cache else 1,
        "rows": rows,
    }
    out_path = Path(args.out).expanduser().resolve() if args.out else SNAPSHOT_DIR / f"opensms_restore_e_replied_{args.tag}_candidates.json"
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(out, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    group_outputs = []
    for index, target in enumerate(sorted(counts), start=1):
        target_rows = [row for row in rows if row["targetCarrierName"] == target]
        safe_target = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", target).strip("-") or "target"
        group_path = SNAPSHOT_DIR / f"opensms_e_reply_recovery_{args.tag}_{index:02d}_{safe_target}.json"
        group_payload = {
            "createdAt": out["createdAt"],
            "source": str(out_path),
            "latestDate": latest,
            "rule": out["rule"],
            "sourceCarrierNames": sorted({row["运营商"] for row in target_rows}),
            "targetCarrierName": target,
            "count": len(target_rows),
            "rows": target_rows,
            "candidates": target_rows,
        }
        with open(group_path, "w", encoding="utf-8") as handle:
            json.dump(group_payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        group_outputs.append({"target": target, "count": len(target_rows), "path": str(group_path)})

    print(json.dumps({
        "path": str(out_path),
        **{key: out[key] for key in ("latestDate", "eTotal", "eTotalByCarrier", "replyPositiveCount", "replyPositiveByCarrier", "count", "recoverableCount", "identifiedOriginCount", "unidentifiedOriginCount", "fallbackAssignedCount", "koodaExcludedSameRoundCount", "bZoneOriginExcludedCount", "bZoneOriginExcludedByAlias", "fallbackByTarget", "byTarget")},
        "groups": group_outputs,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
