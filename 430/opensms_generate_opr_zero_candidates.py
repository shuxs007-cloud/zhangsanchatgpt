#!/usr/bin/env python3
import argparse
import json
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl import load_workbook

from opensms_export_scan_cache import load_scan_cache
from scripts.opensms_temporary_carrier_hold import temporary_carrier_held, isolation_carrier_held

from opensms_artifact_paths import SNAPSHOT_DIR, ensure_artifact_dirs


SEND_THRESHOLD = 50
REST_ZONE_ENTRY_ENABLED = False
B1_B11_USE2_TO_B11_THRESHOLD = 240
B1_B11_TO_B12_THRESHOLD = 240
B_V_TEN_DAY_SEND_THRESHOLD = 240
A_ZONE_REPLY_RATE_THRESHOLD = 0.04
A1_KINI_ROUTE = "a1_kini_latest_lt4_to_b12"
A1_KINI_RULE_VERSION = "a1-kini-latest-send50-rate-lt4-to-b12-v1"
C2_REPLY_RATE_THRESHOLD = 0.05
C2_SEND_ALERT_THRESHOLD = 4000
C2_DAILY_CUTOFF_HOUR_BJ = 21
C2_DAILY_MAX_EXECUTIONS = 1
C2_DAILY_TIMEZONE = "Asia/Shanghai"
USE_DAY_SEND_THRESHOLD = 80
MIN_USED_DAYS = 2
USE_DAYS_FIELD_VALUE = "2天"
PROTECTED_SOURCE_GROUPS = ("A21", "A2")
PROTECTED_SOURCE_PATTERN = re.compile(r"^(A21|A2)(?:-|\s|$)", re.IGNORECASE)
NEVER_PROCESS_SOURCE_CARRIERS = ("快过期续费C", "快过期续费T")
REPLY_RATE_SEND_GATED_SOURCE_GROUPS = (
    "A1", "A11", "A12",
    "B1", "B11", "B12", "B1V", "B11V", "B12V",
)
CHATR_ZERO_REPLY_SEND_GATED_SOURCE_GROUPS = ("A1", "A11", "A12", "A3")
LATEST_DAY_REPLY_RATE_SEND_GATED_ROUTES = {
    A1_KINI_ROUTE,
    "a_zone_lt4_to_a12",
    "a_zone_gte4_to_a1",
    "b1_b11_b12_today_gte50_zero_to_e",
    "chatr_to_e",
}
REPLY_RATE_SEND_GATED_ROUTES = LATEST_DAY_REPLY_RATE_SEND_GATED_ROUTES
DEFAULT_CARRIERS = {
    "a1": "A1-精普-C120",
    "a11": "A11-精普-C50",
    "a12": "A12-精普-C120",
    "a3": "A3-打T精普-C120",
    "a21": "A21-股票-C-100",
    "b1": "B1-精-Te-120",
    "b11": "B11-精-Te-120",
    "b12": "B12-精-Te-120",
    "b1v": "B1V-精-Te-120",
    "b11v": "B11V-精-Te-120",
    "b12v": "B12V-精-Te-120",
    "c2": "C2-测试区1-C150",
    "e": "E-T休息区",
    "e_c": "E-C休息区",
}


def bj_now():
    return datetime.now(timezone(timedelta(hours=8)))


def parse_bj_datetime(value):
    if not value:
        return bj_now()
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    bj_timezone = timezone(timedelta(hours=8))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=bj_timezone)
    return parsed.astimezone(bj_timezone)


def stamp(value):
    return value.strftime("%Y%m%dT%H%M%S+0800")


def atomic_write_json(path, payload):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def claim_c2_daily_execution(created, state_dir, run_stamp):
    beijing_date = created.date().isoformat()
    state_path = state_dir / f"opensms_c2_daily_action_{created:%Y%m%d}_receipt.json"
    base = {
        "ruleVersion": "c2-prevday-daily-before-21-once-v2",
        "rule": "C2前一天回复率低于5%转A11；按日统计当日发送量低于4000提醒；北京时间21:00前每日最多执行一次",
        "timezone": C2_DAILY_TIMEZONE,
        "cutoffHourExclusive": C2_DAILY_CUTOFF_HOUR_BJ,
        "maxExecutionsPerBeijingDate": C2_DAILY_MAX_EXECUTIONS,
        "beijingDate": beijing_date,
        "evaluatedAt": created.isoformat(),
        "runStamp": run_stamp,
        "stateReceiptPath": str(state_path),
    }
    after_cutoff = created.hour >= C2_DAILY_CUTOFF_HOUR_BJ
    claim = {
        **base,
        "eligible": not after_cutoff,
        "status": "skipped_after_cutoff" if after_cutoff else "claimed",
        "skipReason": "after_21_bj_cutoff" if after_cutoff else None,
    }
    if after_cutoff:
        try:
            with state_path.open("x", encoding="utf-8") as handle:
                json.dump(claim, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            return claim, state_path, True
        except FileExistsError:
            return claim, state_path, False
    try:
        with state_path.open("x", encoding="utf-8") as handle:
            json.dump(claim, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return claim, state_path, True
    except FileExistsError:
        try:
            prior = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = {}
        return {
            **base,
            "eligible": False,
            "status": "skipped_already_executed",
            "skipReason": "already_executed_today",
            "priorRunStamp": prior.get("runStamp"),
            "priorStatus": prior.get("status"),
        }, state_path, False


def find_latest_date(headers):
    for header in headers:
        match = re.match(r"^(\d{2})-(\d{2})发送窗口数$", str(header or "").strip())
        if match:
            return f"{match.group(1)}-{match.group(2)}"
    raise RuntimeError("未找到日期发送窗口数字段")


def as_text(value):
    return "" if value is None else str(value).strip()


def as_count(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def valid_window_count(value):
    """Kini's new rule must not turn a missing/invalid reply count into zero."""
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def candidate_payload(
    source, latest, created, route, target, source_names, rule, rows,
    c2_daily_execution=None, ten_day_send_threshold=None,
):
    payload = {
        "createdAt": created.isoformat(),
        "export": str(source),
        "latestDate": latest,
        "count": len(rows),
        "ruleVersion": "carrier-routing-latest-20260908-v34-rest-entry-enabled",
        "rule": rule,
        "route": route,
        "sendThresholdInclusive": SEND_THRESHOLD,
        "bZoneZeroReplySendThresholdInclusive": SEND_THRESHOLD,
        "restZoneEntryEnabled": REST_ZONE_ENTRY_ENABLED,
        "restZoneEntryPolicy": "disabled_by_current_user_20260922",
        "b1B11Use2ToB11TenDaySendThreshold": B1_B11_USE2_TO_B11_THRESHOLD,
        "b1B11ToB12TenDaySendThreshold": B1_B11_TO_B12_THRESHOLD,
        "b1vB11vTenDaySendThreshold": B_V_TEN_DAY_SEND_THRESHOLD,
        "a12ReplyRateThresholdInclusive": A_ZONE_REPLY_RATE_THRESHOLD,
        "aZoneReplyRateBoundary": A_ZONE_REPLY_RATE_THRESHOLD,
        "c2ReplyRateThresholdExclusive": C2_REPLY_RATE_THRESHOLD,
        "c2LatestDaySendAlertThresholdExclusive": C2_SEND_ALERT_THRESHOLD,
        "useDaysMinimumInclusive": MIN_USED_DAYS,
        "useDaySendThresholdExclusive": USE_DAY_SEND_THRESHOLD,
        "configuredSourceCarrierNames": sorted(source_names),
        "sourceCarrierNames": sorted(source_names),
        "targetCarrierName": target,
        "byOrigin": dict(Counter(row["原运营商"] for row in rows)),
        "rows": rows,
        "candidates": rows,
    }
    if route in REPLY_RATE_SEND_GATED_ROUTES:
        payload.update({
            "replyRateSendGateGroups": list(REPLY_RATE_SEND_GATED_SOURCE_GROUPS),
            "replyRateSendGateRoutes": sorted(REPLY_RATE_SEND_GATED_ROUTES),
        })
    if route in LATEST_DAY_REPLY_RATE_SEND_GATED_ROUTES:
        payload["replyRateLatestSendThresholdInclusive"] = SEND_THRESHOLD
        payload["replyRateWindow"] = "latest_day"
    if route == A1_KINI_ROUTE:
        payload["a1KiniRuleVersion"] = A1_KINI_RULE_VERSION
        payload["a1KiniReplyRateThresholdExclusive"] = A_ZONE_REPLY_RATE_THRESHOLD
    if route == "b1_b11_b12_today_gte50_zero_to_e":
        payload["vZonePriority"] = 1
    if route == "chatr_to_e":
        payload["chatrZeroReplySendGateGroups"] = list(CHATR_ZERO_REPLY_SEND_GATED_SOURCE_GROUPS)
    if ten_day_send_threshold is not None:
        payload["tenDaySendThreshold"] = ten_day_send_threshold
    if route in {
        "b1_b11_use2_lt240_to_b11",
        "b1v_b11v_use2_lt240_to_b11v",
    }:
        payload.update({
            "useDaysFieldEquals": USE_DAYS_FIELD_VALUE,
            "useDaysFieldComputation": "single_workbook_pass_same_as_final_result",
            "useDaysFieldRule": (
                f"归一化T系逐日发送窗口数>{USE_DAY_SEND_THRESHOLD}的日期达到"
                f"{MIN_USED_DAYS}天及以上时固定写{USE_DAYS_FIELD_VALUE}，否则留空"
            ),
        })
    if route == "b1v_b11v_use2_lt240_to_b11v":
        payload["vZonePriority"] = 2
    elif route == "b1v_b11v_gte240_to_b12v":
        payload["vZonePriority"] = 3
    if c2_daily_execution is not None:
        payload["c2DailyExecution"] = c2_daily_execution
    return payload


def append_candidate(routed, route, target, latest, values, index, send_count, reply_count, **extra):
    phone = as_text(values[index["电话"]])
    current_carrier = as_text(values[index["运营商"]])
    opr = as_text(values[index["opr"]])
    row = {
        "电话": phone,
        "phone": phone,
        "运营商": current_carrier,
        "原运营商": current_carrier,
        "归属识别来源类型": "current_carrier",
        "opr": opr,
        "卡槽": as_text(values[index["卡槽"]]),
        "状态": as_text(values[index["状态"]]),
        "发送窗口数": send_count,
        "回复窗口数": reply_count,
        "latestDate": latest,
        "route": route,
        "targetCarrierName": target,
        **extra,
    }
    routed[route].append(row)


def main():
    parser = argparse.ArgumentParser(description="Generate latest-date zero-reply routing candidates")
    parser.add_argument("xlsx")
    parser.add_argument("--out-dir")
    for key, default in DEFAULT_CARRIERS.items():
        parser.add_argument(f"--{key.replace('_', '-')}-carrier", default=default)
    parser.add_argument("--b13-carrier", help=argparse.SUPPRESS)
    parser.add_argument("--chatr-source", action="append", default=None)
    parser.add_argument("--now-bj", help=argparse.SUPPRESS)
    parser.add_argument("--c2-daily-state-dir")
    parser.add_argument("--c2-statistics-receipt")
    parser.add_argument("--c2-reconcile-from-candidate")
    parser.add_argument("--scan-cache")
    args = parser.parse_args()

    source = Path(args.xlsx).expanduser().resolve()
    ensure_artifact_dirs()
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else SNAPSHOT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    created = parse_bj_datetime(args.now_bj)
    run_stamp = stamp(created)

    carriers = {key: getattr(args, f"{key}_carrier").strip() for key in DEFAULT_CARRIERS}
    if any(not value for value in carriers.values()):
        raise ValueError("运营商精确名称不得为空")

    a_zone_carriers = {
        carriers["a1"], carriers["a11"], carriers["a12"],
        carriers["a3"],
    }
    b_zone_carriers = {
        carriers["b1"], carriers["b11"], carriers["b12"],
        carriers["b1v"], carriers["b11v"], carriers["b12v"],
    }
    b_standard_mutable_carriers = {carriers["b1"], carriers["b11"]}
    b_v_mutable_carriers = {carriers["b1v"], carriers["b11v"]}
    # --chatr-source remains accepted for old invocations; the current rule
    # always covers all four A-zone sources.
    chatr_sources = set(a_zone_carriers)
    workbook = None
    if args.scan_cache:
        scan_cache = load_scan_cache(args.scan_cache, expected_source=source)
        headers = list(scan_cache["headers"])
        source_rows = scan_cache["rows"]
    else:
        workbook = load_workbook(source, read_only=True, data_only=True)
        sheet = workbook.active
        headers = [cell.value for cell in next(sheet.iter_rows(min_row=1, max_row=1))]
        source_rows = sheet.iter_rows(min_row=2, values_only=True)
    index = {str(header).strip(): i for i, header in enumerate(headers) if header is not None}
    required = {"电话", "运营商", "opr", "卡槽", "状态", "10日发送窗口数"}
    missing = sorted(required - set(index))
    if missing:
        raise RuntimeError(f"报表缺少字段: {missing}")
    latest = find_latest_date(headers)
    send_col = index[f"{latest}发送窗口数"]
    reply_col = index[f"{latest}回复窗口数"]
    latest_month, latest_day = map(int, latest.split("-"))
    latest_date = datetime(created.year, latest_month, latest_day).date()
    if latest_date > created.date() + timedelta(days=31):
        latest_date = latest_date.replace(year=latest_date.year - 1)
    previous_date = latest_date - timedelta(days=1)
    previous = previous_date.strftime("%m-%d")
    previous_send_header = f"{previous}发送窗口数"
    previous_reply_header = f"{previous}回复窗口数"
    if previous_send_header not in index or previous_reply_header not in index:
        raise RuntimeError(f"报表缺少前一天连续自然日字段: {previous_send_header}, {previous_reply_header}")
    previous_send_col = index[previous_send_header]
    previous_reply_col = index[previous_reply_header]
    ten_day_send_col = index["10日发送窗口数"]
    daily_send_window_columns = []
    for column, header in enumerate(headers):
        match = re.match(r"^(\d{2})-(\d{2})发送窗口数$", as_text(header))
        if match:
            daily_send_window_columns.append((f"{match.group(1)}-{match.group(2)}", column))
    if len(daily_send_window_columns) < MIN_USED_DAYS:
        raise RuntimeError("报表逐日发送窗口数字段不足2天，无法按原规则判断koo是否2天是否显示2天")
    c2_state_dir = (
        Path(args.c2_daily_state_dir).expanduser().resolve()
        if args.c2_daily_state_dir
        else out_dir
    )
    c2_state_dir.mkdir(parents=True, exist_ok=True)
    c2_daily_execution, c2_daily_state_path, c2_claim_owned = claim_c2_daily_execution(
        created, c2_state_dir, run_stamp,
    )
    routed = {
        A1_KINI_ROUTE: [],
        "a_zone_kooda_to_b12": [],
        "chatr_to_e": [],
        "b1_b11_b12_today_gte50_zero_to_e": [],
        "b1_b11_use2_lt240_to_b11": [],
        "b1_b11_gte240_to_b12": [],
        "b1v_b11v_use2_lt240_to_b11v": [],
        "b1v_b11v_gte240_to_b12v": [],
        "c2_previous_day_reply_rate_lt5_to_a11": [],
        "c2_rule_correction_restore_to_c2": [],
        "a_zone_lt4_to_a12": [],
        "a_zone_gte4_to_a1": [],
    }
    excluded = Counter()
    c2_statistics_receipt = None
    c2_daily_stats_send_total = None
    c2_daily_stats_date = None
    c2_daily_stats_error = None
    if c2_daily_execution["eligible"] and args.c2_statistics_receipt:
        statistics_path = Path(args.c2_statistics_receipt).expanduser().resolve()
        c2_statistics_receipt = str(statistics_path)
        statistics = json.loads(statistics_path.read_text(encoding="utf-8"))
        matches = [row for row in statistics.get("replyRates", []) if row.get("group") == "C2"]
        if len(matches) != 1:
            c2_daily_stats_error = f"C2按日统计结果数量不是1: {len(matches)}"
        else:
            c2_stat = matches[0]
            daily_row = c2_stat.get("dailyRow") or {}
            if c2_stat.get("status") != "ok" or c2_stat.get("endpoint") != "/api/admin/statisticsList":
                c2_daily_stats_error = "C2按日统计查询状态或接口来源不符合要求"
            elif daily_row.get("sendSize") is None:
                c2_daily_stats_error = "C2按日统计最新行缺少sendSize"
            else:
                c2_daily_stats_send_total = as_count(daily_row.get("sendSize"))
                c2_daily_stats_date = as_text(daily_row.get("days") or c2_stat.get("statDate"))
    elif c2_daily_execution["eligible"]:
        c2_daily_stats_error = "未提供C2按日统计实时查询回执"
    for values in source_rows:
        current_carrier = as_text(values[index["运营商"]])
        if isolation_carrier_held(current_carrier):
            excluded["isolation_carrier_held_no_carrier_change"] += 1
            continue
        if temporary_carrier_held(current_carrier):
            excluded["temporary_b11_held_no_carrier_change"] += 1
            continue
        if current_carrier in NEVER_PROCESS_SOURCE_CARRIERS:
            excluded["never_process_quick_renew"] += 1
            excluded[f"never_process_{current_carrier}"] += 1
            continue
        protected_match = PROTECTED_SOURCE_PATTERN.match(current_carrier)
        if protected_match:
            protected_group = protected_match.group(1).upper()
            excluded["protected_source_held_no_carrier_change"] += 1
            excluded[f"protected_{protected_group.lower()}_held_no_carrier_change"] += 1
            continue
        opr = as_text(values[index["opr"]])
        opr_normalized = opr.casefold()
        # Only A1 + Kini gets this exception. Always stop here, including held
        # rows, so it cannot fall through to the ordinary A1 -> A12 split.
        if current_carrier == carriers["a1"] and re.search(r"\bkini\b", opr, re.I):
            if "chatr" in opr_normalized:
                excluded["a1_kini_ambiguous_opr_held"] += 1
                continue
            kini_send = valid_window_count(values[send_col])
            kini_reply = valid_window_count(values[reply_col])
            if kini_send is None or kini_reply is None or kini_reply > kini_send:
                excluded["a1_kini_invalid_latest_metrics_held"] += 1
                continue
            if kini_send < SEND_THRESHOLD:
                excluded["a1_kini_latest_send_lt50_held"] += 1
                continue
            # Integer cross multiplication makes the strict 4% boundary exact.
            if kini_reply * 100 >= kini_send * 4:
                excluded["a1_kini_latest_rate_gte4_held"] += 1
                continue
            append_candidate(
                routed, A1_KINI_ROUTE, carriers["b12"], latest,
                values, index, kini_send, kini_reply,
                统计日期=latest,
                当天发送窗口数=kini_send,
                当天回复窗口数=kini_reply,
                当天回复率=kini_reply / kini_send,
                回复率阈值=A_ZONE_REPLY_RATE_THRESHOLD,
                回复率判定="严格低于4%",
            )
            continue
        send_count = as_count(values[send_col])
        reply_count = as_count(values[reply_col])
        is_kooda = bool(re.search(r"kooda|koodo|telus1", opr, re.I))
        use_family = bool(re.search(r"koo|kini|telus1", opr, re.I))
        ten_day_send_count = as_count(values[ten_day_send_col])
        used_dates = [
            day
            for day, column in daily_send_window_columns
            if as_count(values[column]) > USE_DAY_SEND_THRESHOLD
        ]
        usage_days = len(used_dates) if use_family else 0
        usage_label = USE_DAYS_FIELD_VALUE if usage_days >= MIN_USED_DAYS else ""
        usage_basis = (
            f"逐日发送窗口数>{USE_DAY_SEND_THRESHOLD}的日期数："
            + ("、".join(used_dates) if use_family and used_dates else "无")
            if use_family
            else "非Kooda/Koodo/Kini/Telus1归一化T系，koo是否2天留空"
        )

        # Kooda/Koodo routing is not reply-rate based, so it has no latest-day
        # send gate. The early continue still enforces one move per run.
        if current_carrier in a_zone_carriers and is_kooda:
            append_candidate(
                routed, "a_zone_kooda_to_b12", carriers["b12"], latest,
                values, index, send_count, reply_count,
                匹配OPR=opr,
                A区精确来源=current_carrier,
                判定="OPR包含Kooda或Koodo；不设发送、回复或回复率门槛",
            )
            continue

        # B1/B11/B12 and B1V/B11V/B12V are parallel exclusive routing
        # families. V sources never leave for a rest zone, even if its global switch opens.
        if current_carrier in b_zone_carriers:
            b_zone_extra = {
                "10日发送窗口数": ten_day_send_count,
                "标准组使用2天转B11阈值": B1_B11_USE2_TO_B11_THRESHOLD,
                "标准组转B12阈值": B1_B11_TO_B12_THRESHOLD,
                "V组阈值": B_V_TEN_DAY_SEND_THRESHOLD,
                "koo是否2天": usage_label,
                "实际命中天数": usage_days,
                "实际命中日期": used_dates if use_family else [],
                "koo是否2天依据": usage_basis,
            }
            if send_count >= SEND_THRESHOLD and reply_count == 0:
                if REST_ZONE_ENTRY_ENABLED and current_carrier not in {carriers['b1v'], carriers['b11v'], carriers['b12v']}:
                    append_candidate(
                        routed, "b1_b11_b12_today_gte50_zero_to_e", carriers["e"], latest,
                        values, index, send_count, reply_count, **b_zone_extra,
                    )
                else:
                    excluded["rest_zone_entry_b_qualified_held"] += 1
                continue

        if current_carrier in b_standard_mutable_carriers:
            if usage_label == USE_DAYS_FIELD_VALUE and ten_day_send_count < B1_B11_USE2_TO_B11_THRESHOLD:
                if current_carrier == carriers["b1"]:
                    append_candidate(
                        routed, "b1_b11_use2_lt240_to_b11", carriers["b11"], latest,
                        values, index, send_count, reply_count, **b_zone_extra,
                    )
                else:
                    excluded["b1_b11_use2_lt240_already_b11"] += 1
            elif ten_day_send_count >= B1_B11_TO_B12_THRESHOLD:
                append_candidate(
                    routed, "b1_b11_gte240_to_b12", carriers["b12"], latest,
                    values, index, send_count, reply_count, **b_zone_extra,
                )
            else:
                excluded["b1_b11_lt240_use_days_not_2"] += 1
            continue

        if current_carrier in b_v_mutable_carriers:
            if usage_label == USE_DAYS_FIELD_VALUE and ten_day_send_count < B_V_TEN_DAY_SEND_THRESHOLD:
                if current_carrier == carriers["b1v"]:
                    append_candidate(
                        routed, "b1v_b11v_use2_lt240_to_b11v", carriers["b11v"], latest,
                        values, index, send_count, reply_count, **b_zone_extra,
                    )
                else:
                    excluded["b1v_b11v_use2_lt240_already_b11v"] += 1
            elif ten_day_send_count >= B_V_TEN_DAY_SEND_THRESHOLD:
                append_candidate(
                    routed, "b1v_b11v_gte240_to_b12v", carriers["b12v"], latest,
                    values, index, send_count, reply_count, **b_zone_extra,
                )
            else:
                excluded["b1v_b11v_lt240_use_days_not_2"] += 1
            continue

        if current_carrier == carriers["b12v"]:
            excluded["b12v_held_no_v_rule_match"] += 1
            excluded["b12_family_held_b13_abolished"] += 1
            continue

        if current_carrier == carriers["b12"]:
            excluded["b12_family_held_b13_abolished"] += 1
            continue

        # Chatr zero reply has priority over the A-zone 4% split. All four
        # A-zone sources share the same >=50 gate and move to E-C.
        if (
            current_carrier in chatr_sources
            and "chatr" in opr_normalized
            and send_count >= SEND_THRESHOLD
            and reply_count == 0
        ):
            if REST_ZONE_ENTRY_ENABLED:
                append_candidate(
                    routed, "chatr_to_e", carriers["e_c"], latest,
                    values, index, send_count, reply_count,
                    判定="A1/A11/A12/A3中的Chatr，最新日发送>=50且回复=0，转E-C休息区",
                )
            else:
                excluded["rest_zone_entry_chatr_qualified_held"] += 1
            continue

        if current_carrier == "B13" or current_carrier.startswith(("B13-", "B13 ")):
            excluded["existing_b13_held_pending_migration_target"] += 1
            continue

        if current_carrier == carriers["c2"]:
            if not c2_daily_execution["eligible"]:
                excluded[f"c2_daily_gate:{c2_daily_execution['skipReason']}"] += 1
                continue
            previous_send_count = as_count(values[previous_send_col])
            previous_reply_count = as_count(values[previous_reply_col])
            reply_rate = previous_reply_count / previous_send_count if previous_send_count > 0 else None
            if reply_rate is not None and reply_rate < C2_REPLY_RATE_THRESHOLD:
                append_candidate(
                    routed, "c2_previous_day_reply_rate_lt5_to_a11", carriers["a11"], latest,
                    values, index, send_count, reply_count,
                    前一天统计日期=previous,
                    前一天发送窗口数=previous_send_count,
                    前一天回复窗口数=previous_reply_count,
                    前一天回复率=reply_rate,
                    回复率阈值=C2_REPLY_RATE_THRESHOLD,
                    回复率判定="严格低于5%",
                )
                continue
            excluded["c2_previous_day_lt5_rule_not_met"] += 1
            continue

        # A1/A11/A12 4% split is calculated exactly once here from the latest
        # day.  Kooda/T-family and Chatr-zero rules above retain priority.
        if current_carrier in {carriers["a1"], carriers["a11"], carriers["a12"]}:
            if send_count < SEND_THRESHOLD:
                excluded["a_zone_reply_rate_latest_send_lt50_held_no_carrier_change"] += 1
                continue
            reply_rate = reply_count / send_count
            reply_extra = {
                "统计日期": latest,
                "当天发送窗口数": send_count,
                "当天回复窗口数": reply_count,
                "当天回复率": reply_rate,
                "回复率阈值": A_ZONE_REPLY_RATE_THRESHOLD,
            }
            if reply_rate >= A_ZONE_REPLY_RATE_THRESHOLD:
                if current_carrier in {carriers["a11"], carriers["a12"]}:
                    append_candidate(
                        routed, "a_zone_gte4_to_a1", carriers["a1"], latest,
                        values, index, send_count, reply_count,
                        回复率判定="大于等于4%", **reply_extra,
                    )
                else:
                    excluded["a_zone_reply_rate_gte4_already_a1"] += 1
            else:
                if current_carrier in {carriers["a1"], carriers["a11"]}:
                    append_candidate(
                        routed, "a_zone_lt4_to_a12", carriers["a12"], latest,
                        values, index, send_count, reply_count,
                        回复率判定="严格低于4%", **reply_extra,
                    )
                else:
                    excluded["a_zone_reply_rate_lt4_already_a12"] += 1
            continue

        # Kooda/Koodo/Telus1 outside the A-zone remains excluded from unrelated routes.
        if is_kooda:
            excluded[f"kooda_outside_a_zone_skip:{current_carrier or 'blank'}"] += 1
            continue

        if "chatr" in opr_normalized and current_carrier not in chatr_sources:
            excluded[f"chatr_not_allowed_source:{current_carrier or 'blank'}"] += 1

    if workbook is not None:
        workbook.close()

    for rows in routed.values():
        rows.sort(key=lambda row: (row["phone"], row["卡槽"]))

    targets = {
        A1_KINI_ROUTE: carriers["b12"],
        "a_zone_kooda_to_b12": carriers["b12"],
        "chatr_to_e": carriers["e_c"],
        "b1_b11_b12_today_gte50_zero_to_e": carriers["e"],
        "b1_b11_use2_lt240_to_b11": carriers["b11"],
        "b1_b11_gte240_to_b12": carriers["b12"],
        "b1v_b11v_use2_lt240_to_b11v": carriers["b11v"],
        "b1v_b11v_gte240_to_b12v": carriers["b12v"],
        "c2_previous_day_reply_rate_lt5_to_a11": carriers["a11"],
        "c2_rule_correction_restore_to_c2": carriers["c2"],
        "a_zone_lt4_to_a12": carriers["a12"],
        "a_zone_gte4_to_a1": carriers["a1"],
    }
    source_names = {
        A1_KINI_ROUTE: {carriers["a1"]},
        "a_zone_kooda_to_b12": a_zone_carriers,
        "chatr_to_e": chatr_sources,
        "b1_b11_b12_today_gte50_zero_to_e": {carriers['b1'], carriers['b11'], carriers['b12']},
        "b1_b11_use2_lt240_to_b11": {carriers["b1"], carriers["b11"]},
        "b1_b11_gte240_to_b12": {carriers["b1"], carriers["b11"]},
        "b1v_b11v_use2_lt240_to_b11v": {carriers["b1v"], carriers["b11v"]},
        "b1v_b11v_gte240_to_b12v": {carriers["b1v"], carriers["b11v"]},
        "c2_previous_day_reply_rate_lt5_to_a11": {carriers["c2"]},
        "c2_rule_correction_restore_to_c2": {carriers["a11"]},
        "a_zone_lt4_to_a12": {carriers["a1"], carriers["a11"]},
        "a_zone_gte4_to_a1": {carriers["a11"], carriers["a12"]},
    }
    rules = {
        A1_KINI_ROUTE: "仅当前运营商精确属于A1且OPR独立单词Kini；最新日发送窗口数>=50且回复窗口数/发送窗口数严格<4%转B12，其余保持A1并停止后续普通路由；Chatr规则不变",
        "a_zone_kooda_to_b12": "当前运营商唯一精确属于A1、A11、A12或A3且OPR属于Kooda/Koodo/Telus1归一化T系时优先转B12；不设发送、回复或回复率门槛；保护组不参与",
        "chatr_to_e": "A1/A11/A12/A3的Chatr最新日发送>=50且回复=0时转E-C休息区，并停止后续A区分流",
        "b1_b11_b12_today_gte50_zero_to_e": "仅B1/B11/B12最新日发送>=50且回复=0时按全局开关转E-T；V组始终禁止转入休息区",
        "b1_b11_use2_lt240_to_b11": "当前运营商为B1或B11、OPR属于Kooda/T系、koo是否2天显示2天且10日发送窗口数<240，优先转B11；不增加最新日发送门槛",
        "b1_b11_gte240_to_b12": "未命中使用2天且10日发送<240转B11规则时，当前运营商为B1或B11且10日发送窗口数>=240，转B12；不增加最新日发送门槛",
        "b1v_b11v_use2_lt240_to_b11v": "未命中V组优先级1；当前运营商为B1V或B11V、逻辑koo是否2天字段精确等于2天且10日发送窗口数<240，转B11V；不增加最新日发送门槛",
        "b1v_b11v_gte240_to_b12v": "未命中V组优先级1和2；当前运营商为B1V或B11V且10日发送窗口数>=240，转B12V；等于240命中，不要求koo是否2天字段等于2天，不增加最新日发送门槛",
        "c2_previous_day_reply_rate_lt5_to_a11": "北京时间21:00前每日首次执行时，当前运营商为C2、前一天发送窗口数>0且回复率严格低于5%转A11；等于5%不命中",
        "c2_rule_correction_restore_to_c2": "同一人工执行中纠正旧口径误转：旧候选已转A11但不符合前一天低于5%的号码恢复C2",
        "a_zone_lt4_to_a12": "当前运营商为A1或A11，最新日发送窗口数>=50且回复窗口数/发送窗口数严格低于4%时转A12；非Chatr零回复包含在内",
        "a_zone_gte4_to_a1": "当前运营商为A11或A12，最新日发送窗口数>=50且回复窗口数/发送窗口数>=4%时转A1；等于4%命中",
    }
    filenames = {
        A1_KINI_ROUTE: f"opensms_a1_kini_latest_lt4_to_b12_{run_stamp}_candidates.json",
        "a_zone_kooda_to_b12": f"opensms_a_zone_kooda_to_b12_{run_stamp}_candidates.json",
        "chatr_to_e": f"opensms_zero_opr_gte50_chatr_to_e_{run_stamp}_candidates.json",
        "b1_b11_b12_today_gte50_zero_to_e": f"opensms_b1_b11_b12_today_gte50_zero_to_e_{run_stamp}_candidates.json",
        "b1_b11_use2_lt240_to_b11": f"opensms_b1_b11_use2_10day_lt240_to_b11_{run_stamp}_candidates.json",
        "b1_b11_gte240_to_b12": f"opensms_b1_b11_10day_gte240_to_b12_{run_stamp}_candidates.json",
        "b1v_b11v_use2_lt240_to_b11v": f"opensms_b1v_b11v_use2_10day_lt240_to_b11v_{run_stamp}_candidates.json",
        "b1v_b11v_gte240_to_b12v": f"opensms_b1v_b11v_10day_gte240_to_b12v_{run_stamp}_candidates.json",
        "c2_previous_day_reply_rate_lt5_to_a11": f"opensms_c2_previous_day_reply_rate_lt5_to_a11_{run_stamp}_candidates.json",
        "c2_rule_correction_restore_to_c2": f"opensms_c2_rule_correction_restore_to_c2_{run_stamp}_candidates.json",
        "a_zone_lt4_to_a12": f"opensms_a_zone_lt4_to_a12_{run_stamp}_candidates.json",
        "a_zone_gte4_to_a1": f"opensms_a_zone_gte4_to_a1_{run_stamp}_candidates.json",
    }
    desired_c2_phones = {row["phone"] for row in routed["c2_previous_day_reply_rate_lt5_to_a11"]}
    if args.c2_reconcile_from_candidate:
        correction_source = Path(args.c2_reconcile_from_candidate).expanduser().resolve()
        previous_candidate = json.loads(correction_source.read_text(encoding="utf-8"))
        for old_row in previous_candidate.get("rows") or previous_candidate.get("candidates") or []:
            phone = as_text(old_row.get("phone") or old_row.get("电话"))
            if not phone or phone in desired_c2_phones:
                continue
            restored = dict(old_row)
            restored.update({
                "电话": phone,
                "phone": phone,
                "运营商": carriers["a11"],
                "原运营商": carriers["a11"],
                "route": "c2_rule_correction_restore_to_c2",
                "targetCarrierName": carriers["c2"],
                "判定": "旧最新日口径命中、但新前一天口径未命中，恢复C2",
                "correctionOfCandidate": str(correction_source),
            })
            routed["c2_rule_correction_restore_to_c2"].append(restored)
        routed["c2_rule_correction_restore_to_c2"].sort(key=lambda row: (row["phone"], row.get("卡槽", "")))
    c2_alert_triggered = (
        c2_daily_execution["eligible"]
        and c2_daily_stats_send_total is not None
        and c2_daily_stats_send_total < C2_SEND_ALERT_THRESHOLD
    )
    if c2_daily_execution["eligible"]:
        c2_daily_execution.update({
            "status": "evaluation_completed",
            "candidatePath": str(out_dir / filenames["c2_previous_day_reply_rate_lt5_to_a11"]),
            "candidateCount": len(routed["c2_previous_day_reply_rate_lt5_to_a11"]),
            "previousDate": previous,
            "dailyStatisticsDate": c2_daily_stats_date,
            "dailyStatisticsSendTotal": c2_daily_stats_send_total,
            "sendAlertTriggered": c2_alert_triggered,
        })
    route_thresholds = {
        "b1_b11_use2_lt240_to_b11": B1_B11_USE2_TO_B11_THRESHOLD,
        "b1_b11_gte240_to_b12": B1_B11_TO_B12_THRESHOLD,
        "b1v_b11v_use2_lt240_to_b11v": B_V_TEN_DAY_SEND_THRESHOLD,
        "b1v_b11v_gte240_to_b12v": B_V_TEN_DAY_SEND_THRESHOLD,
    }
    outputs = {}
    for route, rows in routed.items():
        if targets[route] == "B13" or targets[route].startswith(("B13-", "B13 ")):
            raise RuntimeError(f"B13已废除，禁止生成目标为B13的候选: {route}")
        path = out_dir / filenames[route]
        payload = candidate_payload(
            source, latest, created, route, targets[route], source_names[route], rules[route], rows,
            c2_daily_execution if route == "c2_previous_day_reply_rate_lt5_to_a11" else None,
            route_thresholds.get(route),
        )
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        outputs[route] = {"path": str(path), "count": len(rows), "target": targets[route]}

    manifest_path = out_dir / f"opensms_carrier_routing_{run_stamp}_manifest.json"
    manifest = {
        "createdAt": created.isoformat(),
        "export": str(source),
        "latestDate": latest,
        "sendThresholdInclusive": SEND_THRESHOLD,
        "replyRateLatestSendThresholdInclusive": SEND_THRESHOLD,
        "replyRateSendGateGroups": list(REPLY_RATE_SEND_GATED_SOURCE_GROUPS),
        "replyRateSendGateRoutes": sorted(REPLY_RATE_SEND_GATED_ROUTES),
        "a1KiniRuleVersion": A1_KINI_RULE_VERSION,
        "a1KiniReplyRateThresholdExclusive": A_ZONE_REPLY_RATE_THRESHOLD,
        "a1KiniSourceCarrierName": carriers["a1"],
        "a1KiniTargetCarrierName": carriers["b12"],
        "a1KiniHeldCounts": {
            reason: excluded[f"a1_kini_{reason}_held"]
            for reason in ("latest_send_lt50", "latest_rate_gte4", "invalid_latest_metrics", "ambiguous_opr")
        },
        "bZoneZeroReplySendThresholdInclusive": SEND_THRESHOLD,
        "restZoneEntryEnabled": REST_ZONE_ENTRY_ENABLED,
        "restZoneEntryPolicy": "disabled_by_current_user_20260922",
        "restZoneEntryDisabledRoutes": [
            "b1_b11_b12_today_gte50_zero_to_e",
            "chatr_to_e",
        ] if not REST_ZONE_ENTRY_ENABLED else [],
        "restZoneEntryBQualifiedHeldCount": excluded["rest_zone_entry_b_qualified_held"],
        "restZoneEntryChatrQualifiedHeldCount": excluded["rest_zone_entry_chatr_qualified_held"],
        "restZoneEntryCandidateCount": (
            len(routed["b1_b11_b12_today_gte50_zero_to_e"])
            + len(routed["chatr_to_e"])
        ),
        "b1B11Use2ToB11TenDaySendThreshold": B1_B11_USE2_TO_B11_THRESHOLD,
        "b1B11ToB12TenDaySendThreshold": B1_B11_TO_B12_THRESHOLD,
        "b1vB11vTenDaySendThreshold": B_V_TEN_DAY_SEND_THRESHOLD,
        "bZoneRestTarget": carriers["e"],
        "aZoneChatrRestTarget": carriers["e_c"],
        "chatrZeroReplySendGateGroups": list(CHATR_ZERO_REPLY_SEND_GATED_SOURCE_GROUPS),
        "aZoneChatrZeroReplySources": ["A1", "A11", "A12", "A3"],
        "aZoneChatrZeroToECPrecedesReplySplit": True,
        "a12ReplyRateThresholdInclusive": A_ZONE_REPLY_RATE_THRESHOLD,
        "aZoneReplyRateBoundary": A_ZONE_REPLY_RATE_THRESHOLD,
        "aZoneHighReplySourceGroups": ["A11", "A12"],
        "aZoneLowReplySourceGroups": ["A1", "A11"],
        "aZoneLowReplyIncludesZero": True,
        "aZoneLowReplyExcludesChatrZero": True,
        "c2ReplyRateThresholdExclusive": C2_REPLY_RATE_THRESHOLD,
        "c2LatestDaySendAlertThresholdExclusive": C2_SEND_ALERT_THRESHOLD,
        "c2PreviousDay": previous,
        "c2DailyStatisticsReceipt": c2_statistics_receipt,
        "c2DailyStatisticsDate": c2_daily_stats_date,
        "c2DailyStatisticsSendTotal": c2_daily_stats_send_total,
        "c2DailyStatisticsError": c2_daily_stats_error,
        "c2DailyStatisticsSendAlertTriggered": c2_alert_triggered,
        "c2DailyStatisticsSendAlert": {
            "metric": "statisticsList.latest_row.sendSize",
            "statisticsDate": c2_daily_stats_date,
            "value": c2_daily_stats_send_total,
            "thresholdExclusive": C2_SEND_ALERT_THRESHOLD,
            "evaluated": c2_daily_execution["eligible"] and c2_daily_stats_error is None,
            "triggered": c2_alert_triggered,
            "message": (
                f"C2按日统计{c2_daily_stats_date}发送量{c2_daily_stats_send_total}，低于{C2_SEND_ALERT_THRESHOLD}"
                if c2_alert_triggered
                else None
            ),
        },
        "c2DailyTimezone": C2_DAILY_TIMEZONE,
        "c2DailyCutoffHourExclusiveBj": C2_DAILY_CUTOFF_HOUR_BJ,
        "c2DailyMaxExecutions": C2_DAILY_MAX_EXECUTIONS,
        "c2DailyExecutionReceipt": str(c2_daily_state_path),
        "c2DailyExecution": c2_daily_execution,
        "abolishedCarrierGroups": ["B13"],
        "forbiddenTargetCarrierGroups": ["B13"],
        "existingB13Policy": "hold_pending_explicit_migration_target",
        "protectedSourceCarrierGroups": list(PROTECTED_SOURCE_GROUPS),
        "protectedSourcePolicy": "current_a21_a2_sources_held_no_carrier_change",
        "protectedSourceExcludedCount": excluded["protected_source_held_no_carrier_change"],
        "protectedSourceExcludedCounts": {
            group: excluded[f"protected_{group.lower()}_held_no_carrier_change"]
            for group in PROTECTED_SOURCE_GROUPS
        },
        "protectedA21Policy": "current_a21_source_held_no_carrier_change",
        "protectedA21ExcludedCount": excluded["protected_a21_held_no_carrier_change"],
        "neverProcessSourceCarrierNames": list(NEVER_PROCESS_SOURCE_CARRIERS),
        "neverProcessSourcePolicy": "exact_quick_renew_c_t_held_no_business_processing",
        "neverProcessSourceExcludedCount": excluded["never_process_quick_renew"],
        "neverProcessSourceExcludedCounts": {
            carrier: excluded[f"never_process_{carrier}"]
            for carrier in NEVER_PROCESS_SOURCE_CARRIERS
        },
        "useDaysMinimumInclusive": MIN_USED_DAYS,
        "useDaySendThresholdExclusive": USE_DAY_SEND_THRESHOLD,
        "useDaysFieldEquals": USE_DAYS_FIELD_VALUE,
        "useDaysFieldComputation": "single_workbook_pass_same_as_final_result",
        "candidateWorkbookPasses": 1,
        "sharedScanCacheUsed": bool(args.scan_cache),
        "candidateProcessWorkbookReopens": 0 if args.scan_cache else 1,
        "vZoneCandidateGenerationMergedWithMainRouting": True,
        "vZoneWriteExecutionSerial": True,
        "vZoneBoundaryEnforced": True,
        "vZoneAllowedExitTasks": ["B", "C"],
        "vZoneRestEntryEnabled": False,
        "vZonePriorityRules": [
            {
                "priority": 1,
                "sources": ["B1V", "B11V", "B12V"],
                "latestDaySendThresholdInclusive": SEND_THRESHOLD,
                "latestDayReplyEquals": 0,
                "enabled": False,
                "actionWhenDisabled": "hold_current_carrier_and_stop_lower_priority_rules",
                "configuredTargetWhenEnabled": "current_carrier",
                "route": "b1_b11_b12_today_gte50_zero_to_e",
            },
            {
                "priority": 2,
                "sources": ["B1V", "B11V"],
                "useDaysFieldEquals": USE_DAYS_FIELD_VALUE,
                "tenDaySendLessThan": B_V_TEN_DAY_SEND_THRESHOLD,
                "target": carriers["b11v"],
                "route": "b1v_b11v_use2_lt240_to_b11v",
            },
            {
                "priority": 3,
                "sources": ["B1V", "B11V"],
                "tenDaySendGreaterThanOrEqual": B_V_TEN_DAY_SEND_THRESHOLD,
                "target": carriers["b12v"],
                "route": "b1v_b11v_gte240_to_b12v",
            },
            {
                "priority": "hold",
                "sources": ["B12V"],
                "when": "priority_1_not_matched",
                "target": "current_carrier",
            },
        ],
        "vZoneRoutePriority": [
            "b1_b11_b12_today_gte50_zero_to_e",
            "b1v_b11v_use2_lt240_to_b11v",
            "b1v_b11v_gte240_to_b12v",
        ],
        "b12vHeldWhenPriority1NotMatched": excluded["b12v_held_no_v_rule_match"],
        "routePriority": [
            A1_KINI_ROUTE,
            "a_zone_kooda_to_b12",
            "b1_b11_b12_today_gte50_zero_to_e",
            "b1_b11_use2_lt240_to_b11",
            "b1_b11_gte240_to_b12",
            "b1v_b11v_use2_lt240_to_b11v",
            "b1v_b11v_gte240_to_b12v",
            "chatr_to_e",
            "c2_previous_day_reply_rate_lt5_to_a11",
            "c2_rule_correction_restore_to_c2",
            "a_zone_gte4_to_a1",
            "a_zone_lt4_to_a12",
        ],
        "outputs": outputs,
        "excluded": dict(excluded),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if c2_claim_owned:
        atomic_write_json(c2_daily_state_path, {
            **c2_daily_execution,
            "manifestPath": str(manifest_path),
        })
    print(json.dumps({"manifest": str(manifest_path), **manifest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
