#!/usr/bin/env python3
"""Task G immutable input and independent, offline receipt audit.

Uses this run's existing scan cache and verified post-business carrier overlay;
never queries the site, modifies an Excel file or writes the production ledger.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from opensms_export_scan_cache import load_scan_cache

SETTINGS = Path(__file__).with_name("opensms_task_g_settings.json")
GROUPS = ("B1", "B11", "B12", "B1V", "B11V", "B12V")
SCOPE = "previous_day_total_send_weekend_rest_v1"


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def source_group(name):
    match = re.match(r"^(B1V|B11V|B12V|B1|B11|B12)(?:-|\s|$)", str(name or "").strip(), re.I)
    return match[1].upper() if match else None


def entry_opr_reason(value):
    if not isinstance(value, str) or not value.strip():
        return "unknown_opr"
    # Match the card type token, not a substring such as NotKini/Kinix.
    return "kini" if re.search(r"\bKini\b", value, re.I | re.ASCII) else None


def phone(value):
    if type(value) in (int, float) and math.isfinite(value) and int(value) == value:
        value = str(int(value))
    value = str(value or "").strip()
    if not re.fullmatch(r"(?:1)?\d{10}", value):
        raise ValueError("任务G号码必须为10位或1开头11位数字")
    return value[-10:]


def count(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return int(number) if math.isfinite(number) and 0 <= number <= 9007199254740991 and number.is_integer() else None


def prepare(config):
    settings = load(SETTINGS)
    threshold = settings["previousDayTotalSendThresholdExclusive"]
    if type(threshold) is not int or threshold < 0 or threshold > 9007199254740991:
        raise ValueError("任务G公共阈值必须为非负安全整数")
    if (settings.get("version") != 1 or type(settings.get("enabled")) is not bool
            or settings.get("restNaturalDays") != 1 or settings.get("sourceGroups") != list(GROUPS)
            or settings.get("excludedEntryOprTokens") != ["Kini"]
            or settings.get("standardRestCarrier") != "周末休息区T"
            or settings.get("vRestCarrier") != "周末休息区TV"):
        raise ValueError("任务G固定范围或恢复规则配置错误")
    today = date.fromisoformat(config["serverDate"])
    previous = today - timedelta(days=1)
    column = previous.strftime("%m-%d") + "总发送量"
    cached = load_scan_cache(config["scanCache"], expected_source=config["source"])
    headers = list(cached["headers"])
    for name in ("电话", "运营商", "卡槽", "ICCID", column):
        if headers.count(name) != 1:
            raise ValueError(f"任务G缺少或重复精确字段：{name}")
    indices = {name: headers.index(name) for name in headers}
    opr_columns = [n for n, name in enumerate(headers) if isinstance(name, str) and name.strip().lower() == "opr"]
    if len(opr_columns) != 1:
        raise ValueError("任务G缺少或重复精确字段：OPR")
    opr_index = opr_columns[0]
    live = load(config["postBusinessReadback"])
    if live.get("passed") is not True or live.get("failureCount") != 0:
        raise ValueError("任务G前置运营商独立回读未通过")
    overlays = {}
    for row in live.get("rows", []):
        key = phone(row["phone"])
        if row.get("passed") is not True or key in overlays:
            raise ValueError("任务G前置运营商回读冲突")
        overlays[key] = row["liveCarrierName"]
    rows, seen = [], {}
    for number, values in enumerate(cached["rows"], 2):
        raw_phone = values[indices["电话"]]
        # Rows outside the six groups can be ignored without interpreting an
        # unrelated malformed phone. Changed-to-B rows are included by overlay.
        raw_carrier = str(values[indices["运营商"]] or "").strip()
        try:
            key = phone(raw_phone)
        except ValueError:
            if source_group(raw_carrier):
                raise
            continue
        carrier = overlays.get(key, raw_carrier)
        if not source_group(carrier):
            continue
        slot = values[indices["卡槽"]]
        iccid = values[indices["ICCID"]]
        if not isinstance(slot, str) or not slot.strip() or (iccid is not None and not isinstance(iccid, str)):
            raise ValueError("任务G源卡槽/ICCID身份无效")
        total = count(values[indices[column]])
        opr_value = values[opr_index]
        opr = opr_value.strip() if isinstance(opr_value, str) and opr_value.strip() else None
        row = {"phone": key, "sourceRow": number, "sourceCarrier": carrier,
               "slot": slot.strip(), "iccid": str(iccid or "").strip(), "opr": opr,
               "previousDayTotalSend": total}
        if key in seen:
            if {k: v for k, v in row.items() if k != "sourceRow"} != seen[key]:
                raise ValueError("任务G同号源身份或发送量冲突")
            continue
        seen[key] = {k: v for k, v in row.items() if k != "sourceRow"}
        rows.append(row)
    return {"version": 1, "task": "G", "scope": SCOPE, "runId": config["runId"],
            "serverDate": today.isoformat(), "previousDate": previous.isoformat(), "metricColumn": column,
            "settings": settings, "settingsSha256": sha(SETTINGS), "source": config["source"],
            "sourceSha256": sha(config["source"]), "scanCache": config["scanCache"],
            "scanCacheSha256": sha(config["scanCache"]),
            "postBusinessWorkbook": config["postBusinessWorkbook"],
            "postBusinessWorkbookSha256": sha(config["postBusinessWorkbook"]),
            "postBusinessReadback": config["postBusinessReadback"],
            "postBusinessReadbackSha256": sha(config["postBusinessReadback"]),
            "unknownMetricRowCount": sum(row["previousDayTotalSend"] is None for row in rows),
            "sourceKiniExcludedCount": sum(entry_opr_reason(row["opr"]) == "kini" for row in rows),
            "sourceUnknownOprCount": sum(entry_opr_reason(row["opr"]) == "unknown_opr" for row in rows),
            "entryCandidateCount": sum(entry_opr_reason(row["opr"]) is None
                                       and row["previousDayTotalSend"] is not None
                                       and row["previousDayTotalSend"] > threshold for row in rows),
            "rows": rows}


def audit(input_path, receipt_path):
    spec, result = load(input_path), load(receipt_path)
    if result.get("inputSha256") != sha(input_path) or result.get("runId") != spec["runId"]:
        raise ValueError("任务G回执来源/运行编号不符")
    for field in ("source", "scanCache", "postBusinessWorkbook", "postBusinessReadback"):
        if sha(spec[field]) != spec[field + "Sha256"]:
            raise ValueError("任务G已绑定输入发生变化")
    # Independently rebuild eligibility from the raw cache and previous-stage
    # readback, not from the generator's selected rows or a claimed pass flag.
    regenerated = prepare({"runId": spec["runId"], "serverDate": spec["serverDate"],
                           "source": spec["source"], "scanCache": spec["scanCache"],
                           "postBusinessWorkbook": spec["postBusinessWorkbook"],
                           "postBusinessReadback": spec["postBusinessReadback"]})
    if regenerated != spec:
        raise ValueError("任务G独立重建候选不一致")
    if result.get("status") != "passed" or result.get("retryCount") != 0:
        raise ValueError("任务G未完整核验或发生重试")
    if (result.get("scope") != SCOPE or result.get("serverDate") != spec["serverDate"]
            or result.get("threshold") != spec["settings"]["previousDayTotalSendThresholdExclusive"]
            or any(result.get(key) != spec[key] for key in
                   ("unknownMetricRowCount", "sourceKiniExcludedCount", "sourceUnknownOprCount"))):
        raise ValueError("任务G回执规则/平台日期不一致")
    immutable_fields = ("key", "phone", "backendId", "slot", "iccid", "originalCarrier",
                        "restCarrier", "entryDate", "restoreDate", "metricDate")
    def immutable(record):
        return {key: record[key] for key in immutable_fields}
    def validate_record(record):
        parts = [record[key] for key in ("phone", "backendId", "slot", "iccid", "entryDate")]
        key = hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        g = source_group(record["originalCarrier"]["name"])
        if (key != record["key"] or not re.fullmatch(r"\d{10}", record["phone"])
                or not record["backendId"] or not record["slot"] or not isinstance(record["iccid"], str)
                or not g or not record["originalCarrier"]["identif"] or not record["restCarrier"]["identif"]
                or record["originalCarrier"]["identif"] == record["restCarrier"]["identif"]
                or record["restCarrier"]["name"] != ("周末休息区TV" if g.endswith("V") else "周末休息区T")
                or date.fromisoformat(record["restoreDate"]) != date.fromisoformat(record["entryDate"]) + timedelta(days=1)
                or date.fromisoformat(record["entryDate"]) != date.fromisoformat(record["metricDate"]) + timedelta(days=1)):
            raise ValueError("任务G台账身份/范围/日期无效")
    before_ledger, after_ledger = result["ledgerBefore"], result["ledgerAfter"]
    for ledger in (before_ledger, after_ledger):
        active_phones = set()
        if ledger["version"] != 1 or not isinstance(ledger["records"], dict):
            raise ValueError("任务G台账格式无效")
        for key, record in ledger["records"].items():
            validate_record(record)
            if key != record["key"] or record["status"] not in ("entering", "resting", "restoring", "restored", "detached"):
                raise ValueError("任务G台账索引/状态无效")
            if record["status"] in ("entering", "resting", "restoring"):
                if record["phone"] in active_phones:
                    raise ValueError("任务G同号重复休息意图")
                active_phones.add(record["phone"])
    ledger_bytes = (json.dumps(after_ledger, ensure_ascii=False, indent=2) + "\n").encode()
    if hashlib.sha256(ledger_bytes).hexdigest() != result.get("ledgerAfterSha256"):
        raise ValueError("任务G持久台账快照哈希不一致")
    expected_status = {key: record["status"] for key, record in before_ledger["records"].items()}
    for item in result.get("reconciliations", []):
        record, observed, action = item["record"], item["after"], item["action"]
        old = before_ledger["records"].get(record["key"])
        if (not old or immutable(old) != record or old["status"] != ("entering" if action == "enter" else "restoring")
                or action not in ("enter", "restore") or observed["phone"] != record["phone"]
                or observed["id"] != record["backendId"] or observed["slot"] != record["slot"]
                or observed["iccid"] != record["iccid"]
                or observed["carrierIdentif"] != record["restCarrier" if action == "enter" else "originalCarrier"]["identif"]):
            raise ValueError("任务G旧意图无独立正向回读依据")
        expected_status[record["key"]] = "resting" if action == "enter" else "restored"
    for item in result.get("skips", []):
        if item["reason"] in ("ledger_identity_or_zone_changed", "changed_before_restore"):
            record = item["record"]
            old = before_ledger["records"].get(record["key"])
            if not old or immutable(old) != record or expected_status[record["key"]] != "resting":
                raise ValueError("任务G脱离区跳过无台账依据")
            observed = item.get("observed")
            if observed and all(observed[k] == record[v] for k, v in (("id", "backendId"), ("phone", "phone"), ("slot", "slot"), ("iccid", "iccid"))) and observed["carrierIdentif"] == record["restCarrier"]["identif"]:
                raise ValueError("任务G脱离区跳过与回读矛盾")
            expected_status[record["key"]] = "detached"
    source = {row["phone"]: row for row in spec["rows"]}
    eligible = {row["phone"] for row in spec["rows"] if row["previousDayTotalSend"] is not None
                and entry_opr_reason(row["opr"]) is None
                and row["previousDayTotalSend"] > spec["settings"]["previousDayTotalSendThresholdExclusive"]}
    configs = result["carriers"]
    if len({row["identif"] for row in configs}) != len(configs):
        raise ValueError("任务G运营商配置标识重复")
    if (not result.get("dateChecks") or any(row["actual"] != spec["serverDate"] for row in result["dateChecks"])
            or len(result["dateChecks"]) < 1 + 2 * len(result["batches"])):
        raise ValueError("任务G平台日期提交前核验不完整")
    def check_carrier(value):
        if sum(row == value for row in configs) != 1:
            raise ValueError("任务G精确运营商未唯一核验")
    entry_count = restore_count = 0
    seen, entered_phones = set(), set()
    for batch in result["batches"]:
        if batch["serverDate"] != spec["serverDate"] or batch.get("accepted") is not True or not 1 <= len(batch["records"]) <= 500:
            raise ValueError("任务G日期或批次范围错误")
        before = {str(row["id"]): row for row in batch["before"]}
        after = {str(row["id"]): row for row in batch["after"]}
        if len(before) != len(batch["records"]) or len(after) != len(before):
            raise ValueError("任务G批次身份/数量不完整")
        for record in batch["records"]:
            validate_record(record)
            key = (record["key"], batch["action"])
            if key in seen:
                raise ValueError("任务G同一卡重复提交")
            seen.add(key)
            old, new = before[record["backendId"]], after[record["backendId"]]
            for item in (old, new):
                if (item["phone"] != record["phone"] or item["slot"] != record["slot"]
                        or item["iccid"] != record["iccid"]):
                    raise ValueError("任务G写前/写后身份不一致")
            original, zone = record["originalCarrier"], record["restCarrier"]
            check_carrier(original)
            check_carrier(zone)
            group = source_group(original["name"])
            expected_zone = "周末休息区TV" if group and group.endswith("V") else "周末休息区T"
            if not group or zone["name"] != expected_zone:
                raise ValueError("任务G来源或休息区越界")
            if date.fromisoformat(record["restoreDate"]) != date.fromisoformat(record["entryDate"]) + timedelta(days=1):
                raise ValueError("任务G恢复日期不是次日")
            if batch["action"] == "enter":
                row = source.get(record["phone"])
                if (not row or row["sourceCarrier"] != original["name"]
                        or entry_opr_reason(row["opr"]) is not None
                        or entry_opr_reason(old.get("opr")) is not None
                        or re.sub(r"_(\d+)$", lambda m: "_" + str(int(m[1])), row["slot"]) != record["slot"]
                        or re.sub(r"F$", "", row["iccid"].upper()) != record["iccid"]
                        or row["previousDayTotalSend"] is None
                        or row["previousDayTotalSend"] <= spec["settings"]["previousDayTotalSendThresholdExclusive"]
                        or record["entryDate"] != spec["serverDate"]
                        or record["metricDate"] != spec["previousDate"]):
                    raise ValueError("任务G转入不满足昨日总发送量或非Kini门槛")
                from_id, to_id = original["identif"], zone["identif"]
                if record["key"] in before_ledger["records"]:
                    raise ValueError("任务G重复使用已有转入周期")
                if any(old_record["phone"] == record["phone"]
                       and (expected_status[old_record["key"]] in ("entering", "resting", "restoring")
                            or old_record["entryDate"] == spec["serverDate"]
                            or old_record.get("restoredDate") == spec["serverDate"]
                            or after_ledger["records"].get(old_record["key"], {}).get("restoredDate") == spec["serverDate"])
                       for old_record in before_ledger["records"].values()):
                    raise ValueError("任务G已休息或恢复当天不得再次转入")
                expected_status[record["key"]] = "resting"
                if record["phone"] in entered_phones:
                    raise ValueError("任务G同号重复转入")
                entered_phones.add(record["phone"])
                entry_count += 1
            elif batch["action"] == "restore":
                old_record = before_ledger["records"].get(record["key"])
                if (record["restoreDate"] > spec["serverDate"] or not old_record
                        or immutable(old_record) != record or expected_status[record["key"]] != "resting"):
                    raise ValueError("任务G恢复无到期台账依据")
                from_id, to_id = zone["identif"], original["identif"]
                expected_status[record["key"]] = "restored"
                restore_count += 1
            else:
                raise ValueError("任务G动作无效")
            if old["carrierIdentif"] != from_id or new["carrierIdentif"] != to_id:
                raise ValueError("任务G运营商独立回读不一致")
            stored = result["ledgerAfter"]["records"].get(record["key"])
            terminal_status = "resting" if batch["action"] == "enter" else "restored"
            if not stored or stored["status"] != terminal_status or immutable(stored) != record:
                raise ValueError("任务G台账未持久确认")
    # Every ledger transition must have an action or explicit detached proof;
    # unchanged historical rows cannot silently disappear or change their origin.
    statuses = expected_status
    if set(statuses) != set(after_ledger["records"]):
        raise ValueError("任务G台账存在无回执新增或丢失")
    for key, status in statuses.items():
        current = after_ledger["records"][key]
        if current["status"] != status or (key in before_ledger["records"] and immutable(current) != immutable(before_ledger["records"][key])):
            raise ValueError("任务G台账存在无依据状态或原运营商变更")
        old_record = before_ledger["records"].get(key)
        if (status == "restored" and old_record and current.get("restoredDate") !=
                (old_record.get("restoredDate") if old_record["status"] == "restored" else spec["serverDate"])):
            raise ValueError("任务G恢复当天持久日期无依据")
    skipped_entry, handled_count, changed_count, detached_count = set(), 0, 0, 0
    live_kini_count = live_unknown_opr_count = 0
    opr_skips = {"kini_at_live_check": "kini", "kini_before_enter": "kini",
                 "unknown_opr_at_live_check": "unknown_opr", "unknown_opr_before_enter": "unknown_opr"}
    for item in result.get("skips", []):
        reason = item["reason"]
        if reason in ("ledger_identity_or_zone_changed", "changed_before_restore"):
            detached_count += 1
            continue
        if reason not in ("already_handled", "source_or_identity_changed", "changed_before_enter") and reason not in opr_skips:
            raise ValueError("任务G未知跳过原因")
        p = item.get("phone") or item["record"]["phone"]
        if p not in eligible or p in skipped_entry or p in entered_phones:
            raise ValueError("任务G跳过候选重复或越界")
        skipped_entry.add(p)
        if reason == "already_handled":
            handled_count += 1
            if not any(row["phone"] == p and (row["status"] in ("entering", "resting", "restoring")
                    or row["entryDate"] == spec["serverDate"] or row.get("restoredDate") == spec["serverDate"])
                    for row in after_ledger["records"].values()):
                raise ValueError("任务G已处理跳过无持久依据")
        elif reason in opr_skips:
            row, observed = source[p], item.get("observed")
            ids = [c["identif"] for c in configs if c["name"] == row["sourceCarrier"]]
            if (not observed or entry_opr_reason(observed.get("opr")) != opr_skips[reason]
                    or observed["phone"] != p or not observed["id"]
                    or len(ids) != 1 or observed["carrierIdentif"] != ids[0]
                    or observed["slot"] != re.sub(r"_(\d+)$", lambda m: "_" + str(int(m[1])), row["slot"])
                    or observed["iccid"] != re.sub(r"F$", "", row["iccid"].upper())
                    or (reason.endswith("before_enter") and observed["id"] != item["record"]["backendId"])):
                raise ValueError("任务G Kini/未知OPR跳过与实时回读矛盾")
            if opr_skips[reason] == "kini":
                live_kini_count += 1
            else:
                live_unknown_opr_count += 1
        else:
            changed_count += 1
            row, observed = source[p], item.get("observed")
            ids = [c["identif"] for c in configs if c["name"] == row["sourceCarrier"]]
            if (observed and len(ids) == 1 and observed["carrierIdentif"] == ids[0]
                    and observed["slot"] == re.sub(r"_(\d+)$", lambda m: "_" + str(int(m[1])), row["slot"])
                    and observed["iccid"] == re.sub(r"F$", "", row["iccid"].upper())
                    and (reason != "changed_before_enter" or observed["id"] == item["record"]["backendId"])):
                raise ValueError("任务G来源变化跳过与实时回读矛盾")
    if entered_phones | skipped_entry != eligible:
        raise ValueError("任务G昨日高发送量候选覆盖不完整")
    if (handled_count != result["alreadyHandledCount"] or changed_count != result["sourceChangedCount"]
            or detached_count != result["detachedCount"]
            or live_kini_count != result.get("liveKiniExcludedCount")
            or live_unknown_opr_count != result.get("liveUnknownOprCount")
            or sum(row["status"] == "resting" for row in after_ledger["records"].values()) != result["restingCount"]):
        raise ValueError("任务G跳过或休息中计数不符")
    expected_groups = [{"mode": "expected", "phones": [item["record"]["phone"]],
                        "target": item["record"]["restCarrier" if item["action"] == "enter" else "originalCarrier"]["name"],
                        "route": "task_g_reconcile"} for item in result.get("reconciliations", [])]
    expected_groups.extend({"mode": "expected", "phones": [r["phone"] for r in b["records"]],
                        "target": b["records"][0]["restCarrier" if b["action"] == "enter" else "originalCarrier"]["name"],
                        "route": "task_g_" + b["action"]} for b in result["batches"])
    if result["refreshGroups"] != expected_groups:
        raise ValueError("任务G最终工作簿刷新目标不一致")
    stored_groups = {}
    for batch in result["batches"]:
        if batch["action"] != "enter":
            continue
        for record in batch["records"]:
            key = (record["originalCarrier"]["name"], record["restCarrier"]["name"], record["entryDate"], record["restoreDate"])
            stored_groups[key] = stored_groups.get(key, 0) + 1
    actual_groups = {}
    for row in result["byOriginalCarrier"]:
        key = (row["originalCarrier"], row["restCarrier"], row["entryDate"], row["restoreDate"])
        if key in actual_groups:
            raise ValueError("任务G存入汇总重复")
        actual_groups[key] = row["count"]
    if actual_groups != stored_groups:
        raise ValueError("任务G存入汇报与逐号记录不一致")
    if (entry_count != result["enteredCount"] or restore_count != result["restoredCount"]
            or result.get("independentReadbackPassed") is not True):
        raise ValueError("任务G确认计数不符")
    return {"passed": True, "runId": spec["runId"], "inputSha256": sha(input_path),
            "receiptSha256": sha(receipt_path), "enteredCount": entry_count, "restoredCount": restore_count}


def report_lines(task):
    if not task:
        return []
    if str(task.get("status", "")).startswith("skipped"):
        reason = "公共配置已关闭" if task.get("reason") == "explicit_scope_or_disabled" else "明确仅A/独立F/纠正模式不执行"
        return [f"- 任务G：跳过（{reason}）。"]
    label = {"passed": "已完成并独立核验", "failed_no_replay": "失败且禁止重试", "pending_independent_audit": "待独立复核"}.get(task.get("status"), task.get("status") or "未确认")
    def value(key):
        return task[key] if task.get(key) is not None else "未知"
    lines = [f"- 任务G：{label}；昨日总发送量严格大于 {value('threshold')}。",
             f"- 转入周末休息区 {value('enteredCount')} 个；到期恢复 {value('restoredCount')} 个；当前台账休息中 {value('restingCount')} 个。",
             f"- 已处理/恢复当天跳过 {value('alreadyHandledCount')}；其它任务改区跳过 {value('sourceChangedCount')}；未知发送量源行 {value('unknownMetricRowCount')}。"]
    lines.append(f"- Kini不转入：源表排除 {value('sourceKiniExcludedCount')}、实时排除 {value('liveKiniExcludedCount')}；OPR未知跳过：源表 {value('sourceUnknownOprCount')}、实时 {value('liveUnknownOprCount')}。")
    for row in task.get("byOriginalCarrier") or []:
        lines.append(f"- 存入记录：{row['originalCarrier']} → {row['restCarrier']}：{row['count']} 个，平台 {row['entryDate']} 转入，{row['restoreDate']} 恢复。")
    if task.get("detachedCount"):
        lines.append(f"- {task['detachedCount']} 个台账号身份/分区已变化，未强行恢复，请排查。")
    if task.get("error"):
        lines.append("- 任务G结果未确认；已保存意图，不自动重复提交，请检查回执。")
    if task.get("receipt"):
        lines.append(f"- [任务G逐号存入/恢复记录](<{task['receipt']}>)。")
    return lines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "audit"))
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = prepare(load(args.input)) if args.mode == "prepare" else audit(args.input, args.output)
    if args.mode == "prepare":
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    print(json.dumps(result if args.mode == "audit" else {"passed": True, "input": str(args.output),
        "count": len(result["rows"]), "entryCandidateCount": result["entryCandidateCount"],
        "sourceKiniExcludedCount": result["sourceKiniExcludedCount"],
        "sourceUnknownOprCount": result["sourceUnknownOprCount"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
