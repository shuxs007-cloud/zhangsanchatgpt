#!/usr/bin/env python3
import argparse
import glob
import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import openpyxl

from opensms_export_scan_cache import load_scan_cache
from services.opensms_document_api.pipeline.room_mapping import LABELS as ROOM_LABELS, room_for_device

from opensms_artifact_paths import (
    EXPORT_ARCHIVE_DIR,
    REPLY_RATE_HISTORY_DIR,
    SNAPSHOT_DIR,
    ensure_artifact_dirs,
)


TZ = ZoneInfo("Asia/Hong_Kong")
BASE = "/Users/zhangsan/Downloads/430"
HISTORY = str(REPLY_RATE_HISTORY_DIR)
SNAPSHOTS = str(SNAPSHOT_DIR)
EXPORT_ARCHIVE = str(EXPORT_ARCHIVE_DIR)
ZERO_SEND_THRESHOLD = 70


def bj_from_ts(ts):
    return datetime.fromtimestamp(ts, TZ)


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(v):
    if isinstance(v, datetime):
        return v.replace(tzinfo=TZ) if v.tzinfo is None else v.astimezone(TZ)
    if v is None or v == "":
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
    except ValueError:
        return None


def avg_age(values):
    clean = [v for v in values if v is not None]
    return None if not clean else round(sum(clean) / len(clean), 2)


def num(v):
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        if isinstance(v, float) and math.isnan(v):
            return 0.0
        return float(v)
    s = str(v).replace(",", "").replace("%", "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def pct(reply, send):
    return None if send <= 0 else round(reply / send * 100, 3)


def slot_key(row):
    phone = str(row.get("电话") or "").strip()
    slot = str(row.get("卡槽") or "").strip()
    return f"{phone}|{slot}"


def device_key(row):
    slot = str(row.get("卡槽") or "").strip()
    if not slot:
        return "设备未识别"
    device = slot.split("_", 1)[0].strip()
    if not device:
        return "设备未识别"
    return str(int(device)) if device.isdigit() else device


def room_key(row):
    device = device_key(row)
    return ROOM_LABELS.get(room_for_device(device), "未识别")


def is_unknown_orig(carrier):
    c = str(carrier or "")
    return (not c) or ("E-" in c) or ("休息" in c) or ("换号" in c) or ("未识别" in c)


def read_export(path, scan_cache=None):
    wb = None
    if scan_cache:
        payload = load_scan_cache(scan_cache, expected_source=path)
        headers = list(payload["headers"])
        row_iter = iter(payload["rows"])
    else:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        row_iter = ws.iter_rows(values_only=True)
        headers = list(next(row_iter))
    hidx = {h: i for i, h in enumerate(headers) if h}
    dates = []
    for h in headers:
        if isinstance(h, str):
            m = re.match(r"^(\d{2}-\d{2})发送窗口数$", h)
            if m:
                dates.append(m.group(1))
    if not dates:
        raise RuntimeError(f"no daily send columns in {path}")
    latest = max(dates, key=lambda s: tuple(int(part) for part in s.split("-")))
    send_col = hidx[f"{latest}发送窗口数"]
    reply_col = hidx[f"{latest}回复窗口数"]
    rows = []
    zero = {}
    for values in row_iter:
        row = {}
        for name in ("电话", "运营商", "opr", "卡槽", "状态", "创建时间"):
            row[name] = values[hidx[name]] if name in hidx and hidx[name] < len(values) else None
        row["send"] = num(values[send_col] if send_col < len(values) else None)
        row["reply"] = num(values[reply_col] if reply_col < len(values) else None)
        key = slot_key(row)
        row["key"] = key
        rows.append(row)
        if row["send"] > ZERO_SEND_THRESHOLD and row["reply"] == 0:
            zero[key] = row
    if wb is not None:
        wb.close()
    return {
        "path": path,
        "latestDate": latest,
        "rows": rows,
        "zero": zero,
        "sharedScanCacheUsed": bool(scan_cache),
        "candidateProcessWorkbookReopens": 0 if scan_cache else 1,
    }


def export_sort_key(path):
    return os.path.getmtime(path)


def export_label(path):
    base = os.path.basename(path)
    if base == "export4day.xlsx":
        return "export4day"
    m = re.search(r"export4day \((\d+)\)\.xlsx$", base)
    return m.group(1) if m else re.sub(r"\W+", "_", base)


def aggregate_rates(data):
    as_of = data.get("asOf") or datetime.now(TZ)
    total_send = 0.0
    total_reply = 0.0
    carriers = defaultdict(lambda: {"send": 0.0, "reply": 0.0, "zero": 0, "denom_gt50": 0})
    devices = defaultdict(lambda: {"send": 0.0, "reply": 0.0, "zero": 0, "denom_gt50": 0, "slots": set(), "ages": []})
    rooms = defaultdict(lambda: {"send": 0.0, "reply": 0.0, "zero": 0, "denom_gt50": 0, "slots": set(), "ages": []})
    for row in data["rows"]:
        carrier = str(row.get("运营商") or "运营商未识别").strip() or "运营商未识别"
        device = device_key(row)
        room = room_key(row)
        send = row["send"]
        reply = row["reply"]
        created = parse_dt(row.get("创建时间"))
        age_days = None
        if created:
            age_days = max(0.0, min(30.0, (as_of - created).total_seconds() / 86400))
        total_send += send
        total_reply += reply
        carriers[carrier]["send"] += send
        carriers[carrier]["reply"] += reply
        devices[device]["send"] += send
        devices[device]["reply"] += reply
        rooms[room]["send"] += send
        rooms[room]["reply"] += reply
        if age_days is not None:
            devices[device]["ages"].append(age_days)
            rooms[room]["ages"].append(age_days)
        if row.get("key"):
            devices[device]["slots"].add(row["key"])
            rooms[room]["slots"].add(row["key"])
        if send > ZERO_SEND_THRESHOLD:
            carriers[carrier]["denom_gt50"] += 1
            devices[device]["denom_gt50"] += 1
            rooms[room]["denom_gt50"] += 1
            if reply == 0:
                carriers[carrier]["zero"] += 1
                devices[device]["zero"] += 1
                rooms[room]["zero"] += 1
    carrier_rows = []
    for carrier, v in carriers.items():
        carrier_rows.append({
            "carrier": carrier,
            "reply": int(v["reply"]),
            "send": int(v["send"]),
            "rate_pct": pct(v["reply"], v["send"]),
            "zero": v["zero"],
            "denom_gt50": v["denom_gt50"],
            "zero_rate_pct": pct(v["zero"], v["denom_gt50"]),
        })
    carrier_rows.sort(key=lambda x: (x["send"], x["reply"]), reverse=True)
    device_rows = []
    for device, v in devices.items():
        device_rows.append({
            "device": device,
            "reply": int(v["reply"]),
            "send": int(v["send"]),
            "rate_pct": pct(v["reply"], v["send"]),
            "zero": v["zero"],
            "denom_gt50": v["denom_gt50"],
            "zero_rate_pct": pct(v["zero"], v["denom_gt50"]),
            "slot_count": len(v["slots"]),
            "avg_age_days": avg_age(v["ages"]),
        })
    device_rows.sort(key=lambda x: (x["rate_pct"] is None, x["rate_pct"] if x["rate_pct"] is not None else 999, -x["send"], x["device"]))
    room_order = {**{label: i for i, label in enumerate(ROOM_LABELS.values())}, "未识别": 9}
    room_rows = []
    for room, v in rooms.items():
        room_rows.append({
            "room": room,
            "reply": int(v["reply"]),
            "send": int(v["send"]),
            "rate_pct": pct(v["reply"], v["send"]),
            "zero": v["zero"],
            "denom_gt50": v["denom_gt50"],
            "zero_rate_pct": pct(v["zero"], v["denom_gt50"]),
            "slot_count": len(v["slots"]),
            "avg_age_days": avg_age(v["ages"]),
        })
    room_rows.sort(key=lambda x: (room_order.get(x["room"], 8), x["room"]))
    return {
        "reply": int(total_reply),
        "send": int(total_send),
        "rate_pct": pct(total_reply, total_send),
        "carrier": carrier_rows,
        "device": device_rows,
        "room": room_rows,
    }


def find_prev_export(current_path):
    files = sorted(
        glob.glob(os.path.join(BASE, "export4day*.xlsx"))
        + glob.glob(os.path.join(EXPORT_ARCHIVE, "export4day*.xlsx")),
        key=export_sort_key,
    )
    before = [p for p in files if os.path.abspath(p) != os.path.abspath(current_path) and os.path.getmtime(p) < os.path.getmtime(current_path)]
    return before[-1] if before else None


def find_same_time_export(current_dt):
    files = (
        glob.glob(os.path.join(BASE, "export4day*.xlsx"))
        + glob.glob(os.path.join(EXPORT_ARCHIVE, "export4day*.xlsx"))
    )
    target = current_dt - timedelta(days=1)
    candidates = []
    fallback = []
    for p in files:
        dt = bj_from_ts(os.path.getmtime(p))
        diff = abs((dt - target).total_seconds()) / 60
        if diff <= 60:
            item = (diff, dt, p)
            fallback.append(item)
            # Ignore obvious partial/test exports when a full export exists.
            # The real 10-day all-number exports are ~1MB here; tiny files
            # produce invalid same-time reply-rate baselines.
            if os.path.getsize(p) >= 500_000:
                candidates.append(item)
    return min(candidates or fallback, default=None, key=lambda x: x[0])


def find_same_time_snapshot(current_dt):
    target = current_dt - timedelta(days=1)
    candidates = []
    for snapshot_path in glob.glob(os.path.join(HISTORY, "opensms_reply_rate_snapshot_export4day_*.json")):
        try:
            snapshot = json.load(open(snapshot_path, encoding="utf-8"))
        except Exception:
            continue
        snapshot_dt = parse_dt(snapshot.get("download_time_bj") or snapshot.get("analysis_time_bj"))
        overall = snapshot.get("overall") or {}
        if snapshot_dt is None or not overall.get("send") or overall.get("rate_pct") is None:
            continue
        diff = abs((snapshot_dt - target).total_seconds()) / 60
        if diff <= 60:
            candidates.append((diff, snapshot_dt, snapshot_path, snapshot))
    return min(candidates, default=None, key=lambda x: x[0])


def today_exports(current_path, current_dt):
    files = (
        glob.glob(os.path.join(BASE, "export4day*.xlsx"))
        + glob.glob(os.path.join(EXPORT_ARCHIVE, "export4day*.xlsx"))
    )
    out = []
    cur_mtime = os.path.getmtime(current_path)
    for p in files:
        mt = os.path.getmtime(p)
        dt = bj_from_ts(mt)
        if dt.date() == current_dt.date() and mt <= cur_mtime:
            out.append(p)
    return sorted(set(out), key=export_sort_key)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("current")
    ap.add_argument("--export-method", default="BitBrowser 430；adminsms.smsoug.com/export4day；后台生成全部号码近10日统计 -> 确认生成 -> 自动下载/下载文件")
    ap.add_argument("--fast", action="store_true", help="derive today cumulative from previous saved summary instead of reopening all today's Excel files")
    ap.add_argument("--scan-cache")
    ap.add_argument("--skip-legacy-same-time", action="store_true")
    args = ap.parse_args()

    ensure_artifact_dirs()

    current = os.path.abspath(args.current)
    current_dt = bj_from_ts(os.path.getmtime(current))
    now_dt = datetime.now(TZ)
    cur = read_export(current, args.scan_cache)
    cur["asOf"] = current_dt
    rates = aggregate_rates(cur)
    label = export_label(current)
    stamp = current_dt.strftime("%Y%m%dT%H%M%S%z")
    summary_path = os.path.join(SNAPSHOTS, f"opensms_current_analysis_export4day_{label}_{stamp}.json")
    snapshot_path = os.path.join(HISTORY, f"opensms_reply_rate_snapshot_export4day_{label}_{stamp}.json")
    zero_snapshot_path = os.path.join(SNAPSHOTS, f"opensms_zero_all_{stamp}_candidates.json")

    prev_summary = None
    prev_summaries = sorted(
        glob.glob(os.path.join(SNAPSHOTS, "opensms_current_analysis_export4day_*.json"))
        + glob.glob(os.path.join(BASE, "opensms_current_analysis_export4day_*.json")),
        key=lambda p: os.path.getmtime(p),
    )
    if prev_summaries:
        for p in reversed(prev_summaries):
            if os.path.abspath(p) == os.path.abspath(summary_path):
                continue
            try:
                d = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            prev_summary = d
            break

    prev_path = None
    prev_zero = {}
    candidate_files = sorted(
        glob.glob(os.path.join(SNAPSHOTS, "opensms_zero_all_*_candidates.json"))
        + glob.glob(os.path.join(BASE, "opensms_zero_all_*_candidates.json")),
        key=lambda p: os.path.getmtime(p),
    )
    for candidate_path in reversed(candidate_files):
        if os.path.getmtime(candidate_path) >= os.path.getmtime(current):
            continue
        try:
            candidate_data = json.load(open(candidate_path, encoding="utf-8"))
        except Exception:
            continue
        rows = candidate_data.get("rows") or candidate_data.get("candidates") or []
        prev_zero = {slot_key(row): row for row in rows}
        prev_path = candidate_path
        break
    if prev_path is None:
        fallback_path = find_prev_export(current)
        if fallback_path and os.path.exists(fallback_path):
            prev_zero = read_export(fallback_path)["zero"]
            prev_path = fallback_path

    current_zero = cur["zero"]
    current_zero_keys = set(current_zero)
    prev_zero_keys = set(prev_zero)
    origin_by_key = {}
    wanted_origin_keys = current_zero_keys | prev_zero_keys
    for candidate_path in reversed(candidate_files):
        if len(origin_by_key) == len(wanted_origin_keys):
            break
        try:
            candidate_data = json.load(open(candidate_path, encoding="utf-8"))
        except Exception:
            continue
        for row in candidate_data.get("rows") or candidate_data.get("candidates") or []:
            key = slot_key(row)
            if key not in wanted_origin_keys or key in origin_by_key:
                continue
            carrier = str(row.get("原运营商") or row.get("运营商") or "").strip()
            if carrier and not is_unknown_orig(carrier):
                origin_by_key[key] = carrier

    new_keys = current_zero_keys - prev_zero_keys
    recovered_since_prev_keys = prev_zero_keys - current_zero_keys
    today_keys = set(current_zero_keys)
    recovered_today = set()
    same_report_date = prev_summary and prev_summary.get("latestDate") == cur.get("latestDate")
    if args.fast and same_report_date and prev_summary.get("origOperatorZero"):
        today_cumulative_count = int(prev_summary.get("todayCumulative", 0)) + len(new_keys)
        today_recovered_count = max(0, today_cumulative_count - len(current_zero_keys))
    else:
        today_union = {}
        origin = {}
        for p in today_exports(current, current_dt):
            try:
                data = read_export(p)
            except Exception:
                continue
            if data.get("latestDate") != cur.get("latestDate"):
                continue
            for k, row in data["zero"].items():
                today_union[k] = row
                c = str(row.get("运营商") or "").strip()
                if k not in origin or (is_unknown_orig(origin[k]) and not is_unknown_orig(c)):
                    origin[k] = c
        today_keys = set(today_union)
        recovered_today = today_keys - current_zero_keys
        today_cumulative_count = len(today_keys)
        today_recovered_count = len(recovered_today)

    same_snapshot = None if args.skip_legacy_same_time else find_same_time_snapshot(current_dt)
    same = find_same_time_export(current_dt) if not same_snapshot and not args.skip_legacy_same_time else None
    same_rates = None
    if same_snapshot:
        diff, same_dt, same_path, same_data = same_snapshot
        same_rates = same_data["overall"]
        prev_by_carrier = {x["carrier"]: x for x in same_rates.get("carrier", [])}
        for row in rates["carrier"]:
            p = prev_by_carrier.get(row["carrier"])
            if p and p.get("send", 0) > 0 and row["rate_pct"] is not None:
                row["prev_reply"] = p["reply"]
                row["prev_send"] = p["send"]
                row["prev_rate_pct"] = p["rate_pct"]
                row["delta_pp"] = round(row["rate_pct"] - p["rate_pct"], 3)
            else:
                row["prev_reply"] = p["reply"] if p else 0
                row["prev_send"] = p["send"] if p else 0
                row["prev_rate_pct"] = None
                row["delta_pp"] = None
        rates["sameTimeComparison"] = {
            "status": "matched",
            "source": "local_history_same_time",
            "file": same_path,
            "mtime_bj": fmt_dt(same_dt),
            "diff_minutes": round(diff, 1),
            "latestDate": same_data.get("latestDate"),
            "reply": same_rates["reply"],
            "send": same_rates["send"],
            "rate_pct": same_rates["rate_pct"],
            "delta_pp": round(rates["rate_pct"] - same_rates["rate_pct"], 3) if rates["rate_pct"] is not None and same_rates["rate_pct"] is not None else None,
        }
    elif same:
        diff, same_dt, same_path = same
        same_data = read_export(same_path)
        same_data["asOf"] = same_dt
        same_rates = aggregate_rates(same_data)
        prev_by_carrier = {x["carrier"]: x for x in same_rates["carrier"]}
        for row in rates["carrier"]:
            p = prev_by_carrier.get(row["carrier"])
            if p and p["send"] > 0 and row["rate_pct"] is not None:
                row["prev_reply"] = p["reply"]
                row["prev_send"] = p["send"]
                row["prev_rate_pct"] = p["rate_pct"]
                row["delta_pp"] = round(row["rate_pct"] - p["rate_pct"], 3)
            else:
                row["prev_reply"] = p["reply"] if p else 0
                row["prev_send"] = p["send"] if p else 0
                row["prev_rate_pct"] = None
                row["delta_pp"] = None
        rates["sameTimeComparison"] = {
            "status": "matched",
            "file": same_path,
            "mtime_bj": fmt_dt(same_dt),
            "diff_minutes": round(diff, 1),
            "latestDate": same_data["latestDate"],
            "reply": same_rates["reply"],
            "send": same_rates["send"],
            "rate_pct": same_rates["rate_pct"],
            "delta_pp": round(rates["rate_pct"] - same_rates["rate_pct"], 3) if rates["rate_pct"] is not None and same_rates["rate_pct"] is not None else None,
        }
    else:
        rates["sameTimeComparison"] = {"status": "no_match", "message": "昨天同时间段无匹配快照（±60分钟）"}

    orig_stats = defaultdict(lambda: {"today": 0, "recovered": 0, "current": 0})

    def original_carrier(key, row):
        for value in (row.get("原运营商"), row.get("运营商"), origin_by_key.get(key)):
            name = str(value or "").strip()
            if name and not is_unknown_orig(name):
                return name
        return "归属未识别/E区历史"

    if args.fast and same_report_date and prev_summary.get("origOperatorZero"):
        for item in prev_summary["origOperatorZero"]:
            name = item.get("carrier") or item.get("operator") or "归属未识别/E区历史"
            orig_stats[name]["today"] += int(item.get("today", item.get("today_produced", 0)))
            orig_stats[name]["recovered"] += int(item.get("recovered", 0))
            orig_stats[name]["current"] += int(item.get("current", item.get("unrecovered", 0)))
        for k in new_keys:
            name = original_carrier(k, current_zero[k])
            orig_stats[name]["today"] += 1
            orig_stats[name]["current"] += 1
        for k in recovered_since_prev_keys:
            row = prev_zero.get(k, {})
            name = original_carrier(k, row)
            orig_stats[name]["current"] = max(0, orig_stats[name]["current"] - 1)
            orig_stats[name]["recovered"] += 1
    else:
        for k in today_keys:
            name = origin.get(k) or (today_union[k].get("运营商") if k in today_union else "")
            if is_unknown_orig(name):
                name = "归属未识别/E区历史"
            orig_stats[name]["today"] += 1
            if k in current_zero_keys:
                orig_stats[name]["current"] += 1
            else:
                orig_stats[name]["recovered"] += 1
    orig_rows = [{"carrier": k, **v} for k, v in sorted(orig_stats.items())]

    port_stats = defaultdict(lambda: {"active": set(), "total": set()})
    room_carrier_stats = defaultdict(lambda: {"zero": 0, "denom_gt50": 0})
    for r in cur["rows"]:
        key = r.get("key")
        if not key:
            continue
        room = room_key(r)
        port_stats[room]["total"].add(key)
        if str(r.get("状态") or "").strip() == "上架":
            port_stats[room]["active"].add(key)
        if r["send"] > ZERO_SEND_THRESHOLD:
            carrier = str(r.get("运营商") or "运营商未识别").strip() or "运营商未识别"
            bucket = room_carrier_stats[(room, carrier)]
            bucket["denom_gt50"] += 1
            if r["reply"] == 0:
                bucket["zero"] += 1

    room_order = {**{label: i for i, label in enumerate(ROOM_LABELS.values())}, "未识别": 9}
    port_rows = []
    for room, v in port_stats.items():
        active = len(v["active"])
        total = len(v["total"])
        port_rows.append({"room": room, "active": active, "total": total, "down": total - active})
    port_rows.sort(key=lambda x: (room_order.get(x["room"], 8), x["room"]))

    room_zero_totals = defaultdict(lambda: {"zero": 0, "denom_gt50": 0})
    room_carriers = []
    for (room, carrier), v in room_carrier_stats.items():
        room_zero_totals[room]["zero"] += v["zero"]
        room_zero_totals[room]["denom_gt50"] += v["denom_gt50"]
        room_carriers.append({
            "room": room,
            "carrier": carrier,
            "zero": v["zero"],
            "denom_gt50": v["denom_gt50"],
            "zero_rate_pct": pct(v["zero"], v["denom_gt50"]),
        })
    room_carriers.sort(key=lambda x: (room_order.get(x["room"], 8), x["room"], -x["denom_gt50"], x["carrier"]))
    room_zero_rows = []
    for room, v in room_zero_totals.items():
        room_zero_rows.append({
            "room": room,
            "zero": v["zero"],
            "denom_gt50": v["denom_gt50"],
            "zero_rate_pct": pct(v["zero"], v["denom_gt50"]),
        })
    room_zero_rows.sort(key=lambda x: (room_order.get(x["room"], 8), x["room"]))

    def carrier_for_group(group):
        matches = [
            item for item in rates["carrier"]
            if item["carrier"] == group or item["carrier"].startswith(f"{group}-")
        ]
        return matches[0] if len(matches) == 1 else None

    c2 = carrier_for_group("C2")
    triggers = []
    for target_group in ("A3", "A1"):
        t = carrier_for_group(target_group)
        if c2 and t and c2["rate_pct"] is not None and t["rate_pct"] is not None:
            threshold = round(t["rate_pct"] * 1.2, 3)
            triggers.append({
                "target": t["carrier"],
                "target_rate_pct": t["rate_pct"],
                "threshold_pct": threshold,
                "triggered": c2["rate_pct"] < threshold,
            })

    snap = {
        "createdAt": now_dt.isoformat(),
        "file": current,
        "open_link": current,
        "download_time_bj": fmt_dt(current_dt),
        "used_file_mtime_bj": fmt_dt(current_dt),
        "analysis_time_bj": fmt_dt(now_dt),
        "export_method": args.export_method,
        "latestDate": cur["latestDate"],
        "rows": len(cur["rows"]),
        "currentZero": len(current_zero_keys),
        "newSincePrev": len(new_keys),
        "recoveredSincePrev": len(recovered_since_prev_keys),
        "todayCumulative": today_cumulative_count,
        "todayRecovered": today_recovered_count,
        "overall": rates,
        "deviceReplyRate": rates["device"],
        "origOperatorZero": orig_rows,
        "roomReplyRate": rates["room"],
        "ports": port_rows,
        "roomCarrierZeroRate": {
            "overall": room_zero_rows,
            "carrier": room_carriers,
        },
        "c2Rule": {"c2_rate_pct": c2["rate_pct"] if c2 else None, "triggers": triggers},
        "prev_file": prev_path,
        "healthZeroSnapshot": zero_snapshot_path,
        "sharedScanCacheUsed": cur["sharedScanCacheUsed"],
        "candidateProcessWorkbookReopens": cur["candidateProcessWorkbookReopens"],
    }

    os.makedirs(HISTORY, exist_ok=True)
    zero_rows = []
    for row in current_zero.values():
        zero_rows.append({
            "电话": row.get("电话"),
            "phone": str(row.get("电话") or "").strip(),
            "运营商": row.get("运营商"),
            "opr": row.get("opr"),
            "卡槽": row.get("卡槽"),
            "状态": row.get("状态"),
            "发送窗口数": row.get("send"),
            "回复窗口数": row.get("reply"),
            "latestDate": cur["latestDate"],
        })
    with open(zero_snapshot_path, "w", encoding="utf-8") as f:
        json.dump({
            "createdAt": now_dt.isoformat(),
            "export": current,
            "latestDate": cur["latestDate"],
            "rule": f"latest_date_send_window_gt{ZERO_SEND_THRESHOLD}_reply_window_eq0",
            "count": len(zero_rows),
            "rows": zero_rows,
        }, f, ensure_ascii=False, indent=2)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    print(json.dumps({"summary": summary_path, "snapshot": snapshot_path, **snap}, ensure_ascii=False))


if __name__ == "__main__":
    main()
