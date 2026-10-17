"""Local-only V-group swap accounting. Candidates and unverified writes never count."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
GROUPS = ("B1V", "B11V", "B12V")
ROUTES = {
    "task_c_kooda_4day_reply_rate_lt5_to_swap": "换号区T",
    "task_c_chatr_unchanged_4day_reply_rate_lt4_to_swap": "换号区C",
}
HEADERS = ["日期（北京时间）", "B1V转出人数", "B11V转出人数", "B12V转出人数", "三组合计去重人数", "确认转换号次"]


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def v_group(value):
    match = re.match(r"^(B1V|B11V|B12V)(?:-|\s|$)", str(value or "").strip(), re.I)
    return match.group(1).upper() if match else None


def phone(value):
    value = re.sub(r"\D", "", str(value or ""))
    if len(value) == 11 and value.startswith("1"):
        value = value[1:]
    if len(value) != 10:
        raise ValueError("V组统计号码身份无效")
    return value


def immutable(path, payload):
    """Atomic publish without replacing any committed receipt; safe on resume."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            if load(path) != payload:
                raise ValueError("V组统计回执冲突，拒绝覆盖")
    finally:
        Path(tmp).unlink(missing_ok=True)


def record_verified_group(root, run_id, group):
    route = group.get("route")
    if route not in ROUTES or not group.get("actualWrite"):
        return None
    if group.get("result") != "passed" or group.get("independentReadbackPerformed") is not True:
        raise ValueError("V组统计拒绝未独立核验的提交")
    paths = {key: Path(group[key]).resolve() for key in ("write", "verify", "validated")}
    written, verified, candidate = (load(paths[key]) for key in ("write", "verify", "validated"))
    rows = (written.get("before") or {}).get("eligibleRows") or []
    n = len(rows)
    target = ROUTES[route]
    if not (n > 0 and written.get("status") == "written"
            and written.get("actualWriteCount") == n == group["actualWrite"]
            and (written.get("before") or {}).get("eligibleCount") == n
            and (written.get("target") or {}).get("name") == target
            and verified.get("passed") is True and verified.get("independentReadbackPerformed") is True
            and verified.get("total") == n == verified.get("targetCount")
            and all(verified.get(key) == 0 for key in ("missingCount", "stillNotTargetCount", "duplicateBackendCount"))
            and (verified.get("target") or {}).get("name") == target
            and (verified.get("target") or {}).get("identif") == written["target"].get("identif")
            and Path(verified.get("source", "")).resolve() == paths["validated"]
            and candidate.get("route") == route and candidate.get("task") == "C"
            and candidate.get("forceGate") is True):
        raise ValueError("V组转换号统计的写入/身份/独立回读证据不一致")
    source_names = {str(row["identif"]): row["name"] for row in written.get("sourceCarriers") or []}
    candidates = candidate.get("rows") or candidate.get("candidates") or []
    identities = {(str(row.get("后台ID")), phone(row.get("电话"))): row for row in candidates}
    if len(identities) != n or len(candidates) != n:
        raise ValueError("V组统计验证候选存在重复或缺失身份")
    stamp = datetime.fromisoformat(written["ts"].replace("Z", "+00:00"))
    checked = datetime.fromisoformat(verified["checkedAt"].replace("Z", "+00:00"))
    if stamp.tzinfo is None or checked.tzinfo is None or checked < stamp:
        raise ValueError("V组统计时间或独立回读顺序无效")
    events = []
    seen = set()
    for row in rows:
        ident = (str(row.get("id")), phone(row.get("cardNo")))
        expected = identities.get(ident)
        source_id = str(row.get("carrireIdentif"))
        source = source_names.get(source_id)
        if ident in seen or expected is None or source is None or str(expected.get("原运营商ID")) != source_id or expected.get("精确原运营商") != source:
            raise ValueError("V组统计实时来源与已核验号码身份不匹配")
        seen.add(ident)
        if v_group(source):
            events.append({"phone": ident[1], "backendId": ident[0], "sourceGroup": v_group(source), "target": target})
    payload = {"schema": 1, "runId": run_id, "route": route, "writtenAt": written["ts"],
               "date": stamp.astimezone(TZ).date().isoformat(), "events": events,
               "evidence": {key: {"path": str(path), "sha256": sha(path)} for key, path in paths.items()}}
    key = hashlib.sha256(f"{run_id}:{route}".encode()).hexdigest()
    output = Path(root) / f"event_{key}.json"
    immutable(output, payload)
    return str(output)


def aggregate(records, today):
    days = {today: {name: set() for name in GROUPS}}
    occurrences = {}
    for receipt in records:
        day = receipt["date"]
        bucket = days.setdefault(day, {name: set() for name in GROUPS})
        for event in receipt["events"]:
            bucket[event["sourceGroup"]].add(event["phone"])
            occurrences[day] = occurrences.get(day, 0) + 1
    return [{"date": day, **{key: len(bucket[key]) for key in GROUPS},
             "unique": len(set().union(*bucket.values())), "events": occurrences.get(day, 0)}
            for day, bucket in sorted(days.items(), reverse=True)]


def values(row):
    return [row["date"], *(row[name] for name in GROUPS), row["unique"], row["events"]]


def verify_summary(result):
    from openpyxl import load_workbook
    if sha(result["output"]) != result["sha256"]:
        raise ValueError("V组每日统计文件哈希不一致")
    records = []
    for item in result["journal"]:
        if sha(item["path"]) != item["sha256"]:
            raise ValueError("V组每日统计台账哈希不一致")
        records.append(load(item["path"]))
    if aggregate(records, result["date"]) != result["rows"]:
        raise ValueError("V组每日统计汇总与台账不一致")
    book = load_workbook(result["output"], read_only=True, data_only=True)
    try:
        actual = list(book["V组每日转换号统计"].values)
        if actual != [tuple(HEADERS)] + [tuple(values(row)) for row in result["rows"]]:
            raise ValueError("V组每日统计Excel独立回读不一致")
    finally:
        book.close()


def export_daily(root, run_id, delivery, receipts, groups=(), now=None):
    import xlsxwriter
    root = Path(root)
    now = now or datetime.now(TZ)
    root.mkdir(parents=True, exist_ok=True)
    start = root / "coverage.json"
    if not start.exists():
        try:
            immutable(start, {"startedAt": now.isoformat(), "scope": "仅本程序实际写入且独立回读通过的任务C；非后台全量或人工转区记录"})
        except ValueError:
            if not start.exists():
                raise
    for group in groups:
        record_verified_group(root, run_id, group)
    paths = sorted(root.glob("event_*.json"))
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(paths)))) as pool:
        records = list(pool.map(load, paths))
    day = now.astimezone(TZ).date().isoformat()
    output = Path(delivery) / f"OpenSMS_V组每日转换号统计_{run_id}.xlsx"
    rows = aggregate(records, day)
    with xlsxwriter.Workbook(output) as book:
        sheet = book.add_worksheet("V组每日转换号统计")
        header = book.add_format({"bold": True, "bg_color": "#17365D", "font_color": "white", "border": 1})
        number = book.add_format({"num_format": "0", "border": 1, "align": "center"})
        sheet.write_row(0, 0, HEADERS, header)
        for index, row in enumerate(rows, 1):
            sheet.write_row(index, 0, values(row), number)
        sheet.freeze_panes(1, 1)
        sheet.autofilter(0, 0, len(rows), len(HEADERS) - 1)
        sheet.set_column(0, 0, 23)
        sheet.set_column(1, 5, 23)
        note = book.add_worksheet("统计口径")
        note.set_column(0, 0, 115)
        note.write_column(0, 0, [
            f"统计启用时间：{load(start)['startedAt']}；生成时间：{now.isoformat()}",
            "按实际写入时间换算北京时间归日；今日数据截至本轮，并非已结束整日。",
            "只计从B1V/B11V/B12V实际转入换号区C/T且独立回读通过的任务C。候选、已在目标区、失败、不确定提交不计成功。",
            "各组按每日手机号去重；三组合计再次去重，可能小于三列之和；确认转换号次保留多次事件。",
            "未回填启用前历史；不包含人工转区或其它程序的转区；完整身份和回执保存在本地审计台账。",
        ])
    result = {"status": "passed", "runId": run_id, "date": day, "generatedAt": now.isoformat(),
              "coverage": load(start), "output": str(output), "sha256": sha(output), "rows": rows,
              "today": next(row for row in rows if row["date"] == day),
              "journal": [{"path": str(path), "sha256": sha(path)} for path in paths]}
    verify_summary(result)
    result["independentReadbackPassed"] = True
    receipt = Path(receipts) / f"opensms_v_group_swap_daily_{run_id}.json"
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["receipt"] = str(receipt)
    return result


def report_lines(result):
    if not result:
        return []
    return ["", "## V组每日转换号统计", "",
            f"统计启用：{result['coverage']['startedAt']}。按北京时间、每日手机号去重，仅计实际写入并独立回读成功的任务C；今日截至本轮。不补算启用前历史，不含人工转区。",
            "", "| 日期（北京时间） | B1V | B11V | B12V | 三组合计去重 | 确认转换号次 |", "| --- | ---: | ---: | ---: | ---: | ---: |",
            *["| " + " | ".join(map(str, values(row))) + " |" for row in result["rows"][:31]],
            "", f"[V组每日转换号统计完整Excel](<{result['output']}>)。明细按来源组去重，三组合计另行去重；MD显示最近31个有记录日期及今日。"]
