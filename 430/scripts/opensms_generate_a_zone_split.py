#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
from collections import Counter
from decimal import Decimal
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opensms_artifact_paths import SNAPSHOT_DIR, ensure_artifact_dirs


TZ = ZoneInfo("Asia/Hong_Kong")
KOODA_PATTERN = re.compile(r"kooda|koodo|telus1", re.IGNORECASE)
CHATR_PATTERN = re.compile(r"chatr", re.IGNORECASE)


def number(value):
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return 0.0


def phone_key(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())[-10:]


def text(value):
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    result = str(value).strip()
    return result[:-2] if result.endswith(".0") and result[:-2].isdigit() else result


def load_excluded_phones(paths):
    excluded = set()
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        for row in payload.get("rows") or payload.get("candidates") or []:
            key = phone_key(row.get("电话") or row.get("phone"))
            if key:
                excluded.add(key)
    return excluded


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def candidate_payload(
    *, export_path, latest, route, sources, target, threshold, min_send_window,
    excluded_paths, rows, rate_condition, rate_operator, counts,
):
    return {
        "createdAt": datetime.now(TZ).isoformat(),
        "source": str(export_path),
        "route": route,
        "latestDate": latest,
        "scope": "latest_date",
        "rule": (
            f"当前运营商精确属于{'/'.join(sorted(sources))}，{latest}发送窗口数>="
            f"{min_send_window:g}且{rate_condition}，转{target}"
        ),
        "sourceCarrierNames": sorted(sources),
        "targetCarrierName": target,
        "thresholdPct": threshold,
        "rateOperator": rate_operator,
        "minSendWindowInclusive": min_send_window,
        "replyRateLatestSendThresholdInclusive": min_send_window,
        "replyRateSendGateGroups": ["A1", "A11", "A12"],
        "replyRateSendGateRoutes": [
            "a_zone_zero_to_e",
            "a_zone_lt4_to_a12",
            "a_zone_gte4_to_a1",
        ],
        "rateSource": "latest_date_reply_windows/latest_date_send_windows",
        "excludeCandidatePaths": [str(Path(path).resolve()) for path in excluded_paths],
        "counts": counts,
        "count": len(rows),
        "bySource": dict(Counter(row["原运营商"] for row in rows)),
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(
        description="按最新日4%边界生成A1/A11/A12双向分流候选。"
    )
    parser.add_argument("export")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-carrier", action="append", required=True)
    parser.add_argument(
        "--high-source-carrier",
        action="append",
        default=None,
        help="兼容参数；必须精确提供A11和A12两个高回复率来源",
    )
    parser.add_argument("--target-zero", required=True, help="兼容参数；Chatr零回复由主生成器转E-C，本脚本不重复生成")
    parser.add_argument("--target-low", required=True, help="回复率严格低于阈值的目标A12完整名称，包含非Chatr的0回复")
    parser.add_argument("--target-high", required=True, help="回复率大于等于阈值的目标A1完整名称")
    parser.add_argument("--threshold", type=float, default=4.0)
    parser.add_argument("--min-send-window", type=float, default=50.0)
    parser.add_argument("--exclude-candidates", action="append", default=[])
    parser.add_argument("--output-dir", default=str(SNAPSHOT_DIR))
    args = parser.parse_args()

    if 0 < args.threshold < 1:
        raise ValueError(
            "--threshold 使用百分数单位；4%应传4，禁止传0.04以免误生成批量候选"
        )
    if args.min_send_window < 50:
        raise ValueError("A1/A11/A12回复判断前最新日发送窗口数必须>=50")

    ensure_artifact_dirs()
    export_path = Path(args.export).resolve()
    output_dir = Path(args.output_dir).resolve()
    sources = set(args.source_carrier)
    if len(sources) != 3:
        raise ValueError("必须唯一提供A1、A11、A12三个精确来源完整名称")
    low_sources = sources - {args.target_low}
    expected_high_sources = sources - {args.target_high}
    high_sources = set(args.high_source_carrier or expected_high_sources)
    if len(low_sources) != 2 or args.target_high not in low_sources:
        raise ValueError("低回复率来源必须精确对应A1和A11")
    if high_sources != expected_high_sources:
        raise ValueError("高回复率来源必须精确对应A11和A12，禁止只处理A12")
    targets = {args.target_zero, args.target_low, args.target_high}
    if len(targets) != 3:
        raise ValueError("E零回复目标、A12低回复目标和A1高回复目标必须互异")
    if args.target_low not in sources or args.target_high not in sources:
        raise ValueError("A12低回复目标和A1高回复目标必须属于精确来源集合")
    if args.target_zero in sources:
        raise ValueError("E零回复目标不得属于A1、A11、A12精确来源集合")

    excluded_phones = load_excluded_phones(args.exclude_candidates)
    workbook = openpyxl.load_workbook(export_path, read_only=True, data_only=True)
    sheet = workbook.active
    iterator = sheet.iter_rows(values_only=True)
    headers = list(next(iterator))
    indexes = {
        str(value or "").strip(): index
        for index, value in enumerate(headers)
        if value not in (None, "")
    }
    dates = [
        match.group(1)
        for header in headers
        if isinstance(header, str)
        for match in [re.fullmatch(r"(\d{2}-\d{2})发送窗口数", header.strip())]
        if match
    ]
    if not dates:
        raise RuntimeError("报表没有每日发送窗口数列")

    latest = dates[0]
    send_index = indexes[f"{latest}发送窗口数"]
    reply_index = indexes[f"{latest}回复窗口数"]
    zero_rows = []
    low_rows = []
    high_rows = []
    counts = {
        "sourceRows": 0,
        "excludedSameRound": 0,
        "excludedKoodaKoodoTelus1": 0,
        "excludedChatrZeroToEC": 0,
        "unusableReplyRate": 0,
        "exactBoundaryToHigh": 0,
        "alreadyZeroTarget": 0,
        "alreadyLowTarget": 0,
        "alreadyHighTarget": 0,
        "highSourceExcluded": 0,
    }

    for values in iterator:
        carrier = text(values[indexes["运营商"]])
        if carrier not in sources:
            continue
        counts["sourceRows"] += 1
        phone = text(values[indexes["电话"]])
        if phone_key(phone) in excluded_phones:
            counts["excludedSameRound"] += 1
            continue
        opr = text(values[indexes["opr"]]) if "opr" in indexes else ""
        if KOODA_PATTERN.search(opr):
            counts["excludedKoodaKoodoTelus1"] += 1
            continue
        send = number(values[send_index])
        reply = number(values[reply_index])
        if send < args.min_send_window or send <= 0:
            counts["unusableReplyRate"] += 1
            continue
        if reply == 0 and CHATR_PATTERN.search(opr):
            counts["excludedChatrZeroToEC"] += 1
            continue
        rate = reply / send * 100
        left = Decimal(str(reply)) * Decimal("100")
        right = Decimal(str(send)) * Decimal(str(args.threshold))
        if left < right:
            if carrier not in low_sources:
                counts["alreadyLowTarget"] += 1
                continue
            bucket = low_rows
            target = args.target_low
            rate_rule = f"回复率<{args.threshold:g}%"
            already_key = "alreadyLowTarget"
        else:
            if carrier == args.target_high:
                counts["alreadyHighTarget"] += 1
                continue
            if carrier not in high_sources:
                counts["highSourceExcluded"] += 1
                continue
            bucket = high_rows
            target = args.target_high
            rate_rule = f"回复率>={args.threshold:g}%"
            already_key = "alreadyHighTarget"
            if left == right:
                counts["exactBoundaryToHigh"] += 1
        if carrier == target:
            counts[already_key] += 1
            continue
        row = {
            "电话": phone,
            "phone": phone,
            "运营商": carrier,
            "原运营商": carrier,
            "opr": opr,
            "卡槽": text(values[indexes["卡槽"]]) if "卡槽" in indexes else "",
            "状态": text(values[indexes["状态"]]) if "状态" in indexes else "",
            "创建时间": text(values[indexes["创建时间"]]) if "创建时间" in indexes else "",
            "发送窗口数": send,
            "回复窗口数": reply,
            "当天回复率": round(rate, 4),
            "统计日期": latest,
            "目标运营商": target,
            "targetCarrierName": target,
            "规则": f"{latest}{rate_rule}转{target}",
        }
        bucket.append(row)
    workbook.close()

    common = {
        "export_path": export_path,
        "latest": latest,
        "sources": sources,
        "threshold": args.threshold,
        "min_send_window": args.min_send_window,
        "excluded_paths": args.exclude_candidates,
        "counts": counts,
    }
    zero_path = output_dir / f"opensms_a_zone_zero_to_e_{args.tag}_candidates.json"
    low_path = output_dir / f"opensms_a_zone_lt4_to_a12_{args.tag}_candidates.json"
    high_path = output_dir / f"opensms_a_zone_gte4_to_a1_{args.tag}_candidates.json"
    manifest_path = output_dir / f"opensms_a_zone_split_{args.tag}_manifest.json"
    write_json(zero_path, candidate_payload(
        **common,
        route="a_zone_zero_to_e",
        target=args.target_zero,
        rows=zero_rows,
        rate_condition="回复率=0%",
        rate_operator="eq_zero",
    ))
    low_common = {**common, "sources": low_sources}
    write_json(low_path, candidate_payload(
        **low_common,
        route="a_zone_lt4_to_a12",
        target=args.target_low,
        rows=low_rows,
        rate_condition=f"回复率<{args.threshold:g}%（包含非Chatr的0%）",
        rate_operator="lt",
    ))
    high_common = {**common, "sources": high_sources}
    write_json(high_path, candidate_payload(
        **high_common,
        route="a_zone_gte4_to_a1",
        target=args.target_high,
        rows=high_rows,
        rate_condition=f"回复率>={args.threshold:g}%",
        rate_operator="gte",
    ))
    manifest = {
        "createdAt": datetime.now(TZ).isoformat(),
        "source": str(export_path),
        "latestDate": latest,
        "thresholdPct": args.threshold,
        "highSourceCarrierNames": sorted(high_sources),
        "boundary": (
            f"A1/A11 rate < {args.threshold:g}% -> {args.target_low}; "
            f"A11/A12 rate >= {args.threshold:g}% -> {args.target_high}; "
            "non-Chatr zero reply is included in the low bucket; Chatr zero reply is handled by the E-C route"
        ),
        "minimumSendRule": f"latest date send windows >= {args.min_send_window:g}",
        "replyRateLatestSendThresholdInclusive": args.min_send_window,
        "replyRateSendGateGroups": ["A1", "A11", "A12"],
        "replyRateSendGateRoutes": [
            "a_zone_zero_to_e",
            "a_zone_lt4_to_a12",
            "a_zone_gte4_to_a1",
        ],
        "zeroToETargetAbolished": True,
        "lowIncludesZero": True,
        "lowExcludesChatrZero": True,
        "chatrZeroHandledByMainRouting": True,
        "lowSourceCarrierNames": sorted(low_sources),
        "highSourceCarrierNames": sorted(high_sources),
        "counts": counts,
        "zeroCandidateCount": len(zero_rows),
        "zeroCandidatePath": str(zero_path),
        "lowCandidateCount": len(low_rows),
        "highCandidateCount": len(high_rows),
        "lowCandidatePath": str(low_path),
        "highCandidatePath": str(high_path),
    }
    write_json(manifest_path, manifest)
    print(json.dumps({
        "manifest": str(manifest_path),
        "latestDate": latest,
        "zeroCandidatePath": str(zero_path),
        "zeroCandidateCount": len(zero_rows),
        "lowCandidatePath": str(low_path),
        "lowCandidateCount": len(low_rows),
        "highCandidatePath": str(high_path),
        "highCandidateCount": len(high_rows),
        "minimumSendRule": manifest["minimumSendRule"],
        "replyRateLatestSendThresholdInclusive": args.min_send_window,
        "replyRateSendGateGroups": ["A1", "A11", "A12"],
        "zeroToETargetAbolished": True,
        "lowIncludesZero": True,
        "lowExcludesChatrZero": True,
        "chatrZeroHandledByMainRouting": True,
        "lowSourceCarrierNames": sorted(low_sources),
        "highSourceCarrierNames": sorted(high_sources),
        "counts": counts,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
