from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.styles.cell_style import StyleArray
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.datetime import from_excel

try:
    from .chatr_iccid_conflicts import SHEET as ICCID_CONFLICT_SHEET, DECISION as ICCID_CONFLICT_DECISION, write_conflict_sheet
    from .device_daily_statistics import aggregate_device_daily, write_device_daily_sheet
except ImportError:  # executable entrypoint or path-based regression import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from chatr_iccid_conflicts import SHEET as ICCID_CONFLICT_SHEET, DECISION as ICCID_CONFLICT_DECISION, write_conflict_sheet
    from device_daily_statistics import aggregate_device_daily, write_device_daily_sheet

sys.path.insert(0, str(Path(__file__).resolve().parent))
from archived_system_identity import index_phones, match_archived
from card_retention_report import load_from_env as load_retention_report, add_retention_report
from machine_number import add_machine_number
from creation_usage_days import add_usage_days
from google_room_sheets import bound_room_sheets
from google_system_missing import load_from_env as load_google_difference, write_sheet as write_google_difference, PORT_HEADERS, PRESENT, attach_port_iccids
from ko_unactivated import load_from_env as load_ko_unactivated, write_sheet as write_ko_unactivated, select_system_records
from report_kooda_source import is_report_source, validate_projection, verify_projection_rows
from task_f_renewal_dates import validate_renewal
from room_mapping import CONFIG as ROOM_CONFIG, CONFIG_SHA256 as ROOM_CONFIG_SHA256, LABELS as ROOM_LABELS, room_for_device, summary_items as room_summary_items


ROOT = Path(os.environ.get("OPENSMS_REPORT_ROOT", "/Users/zhangsan/Documents/数据分析")).expanduser()
MAIN_PATH = Path(
    os.environ.get("OPENSMS_MAIN_PATH", str(ROOT / "outputs/raw/export4day.xlsx"))
).expanduser()
ASSOC_PATH = Path(
    os.environ.get(
        "OPENSMS_ASSOC_PATH",
        str(ROOT / "outputs/raw/加拿大卡文件整理 - 二號機房.xlsx"),
    )
).expanduser()
OUTPUT_DIR = Path(
    os.environ.get("OPENSMS_OUTPUT_DIR", str(ROOT / "outputs"))
).expanduser()
TASK_F_DB_PATH = Path(
    os.environ.get(
        "OPENSMS_TASK_F_DB_PATH",
        "/Users/zhangsan/Downloads/430/opensms_artifacts/state/task_f_kooda_activation.sqlite3",
    )
).expanduser()
REQUIRED_ASSOC_SHEETS = ("二號機房", "三號機房", "四號機房")
GOOGLE_ENABLED = os.environ.get("OPENSMS_GOOGLE_ASSOCIATION_ENABLED", "1") != "0"
GOOGLE_ONLY_SHEETS = (
    "未匹配卡槽", "电信商_OPR预警", "谷歌有-系统无", "所有谷歌有-系统没有",
    "谷歌文档中有激活日期-系统无法使用", "谷歌文档中有激活日期-系统没有",
    "Google有日期_主表缺失", "设备缺失端口",
)
GOOGLE_ONLY_HEADERS = ("Google電信商/價格", "OPR匹配预警")
GOOGLE_ACTIVATED_MISSING_HEADERS = [
    "Google页签",
    "Google行号",
    "卡槽",
    "归一化卡槽",
    "备注",
    "ICCID",
    "手机号",
    "上卡日期",
    "电信商",
    "激活日期",
    "截止日",
]
RATE_THRESHOLD = 0.03
MIN_DAILY_SENDS = 50
USE_DAYS_MIN_DAILY_SENDS = 80
MIN_USED_DAYS = 2
ENRICHMENT_SHEET_NAME = "A11连续低于3%"
CANCELLED_REVIEW_SHEETS = (
    "已过期_未进过期下架卡",
    "快过期_今天明天",
    "今日总回复量为0",
    "今日昨天总回复量均为0",
)
SYSTEM_KOODA_FIELD_MAPPING = {
    "ICCID": "Kooda/Telus1使用本轮系统号码管理cardStore原值（保留末尾F）；其它OPR保持原规则",
    "开卡日期": "Kooda/Telus1留空（不使用Google上卡日期或系统创建时间）；其它OPR使用Google上卡日期",
    "激活日期": "逐字段优先本轮系统导出激活日期；Kooda缺失用本轮冻结F库；Chatr及其它OPR缺失用Google激活日期",
    "截止日": "逐字段优先本轮系统导出截止日；Kooda缺失用本轮冻结F库；Chatr及其它OPR缺失用Google預計過期日；旧无日期列导出允许本轮系统库存",
}
DATE_SOURCE_NAMES = ("system_export", "system_inventory_legacy", "local_cache", "google", "missing")
if not GOOGLE_ENABLED:
    SYSTEM_KOODA_FIELD_MAPPING.update({
        "开卡日期": "无Google模式留空；不使用系统创建时间或其它日期代推",
        "激活日期": "优先本轮系统导出；Kooda缺失字段读取本轮冻结F库；Chatr允许空白",
        "截止日": "优先本轮系统导出；Kooda缺失字段读取本轮冻结F库；不由激活日期倒推",
    })


def load_report_date_cache() -> dict:
    """Load the run-frozen JSON only; never observe the actively changing F DB."""
    raw_path = os.environ.get("OPENSMS_REPORT_DATE_CACHE_PATH", "")
    expected_hash = os.environ.get("OPENSMS_REPORT_DATE_CACHE_SHA256", "").lower()
    run_id = os.environ.get("OPENSMS_RUN_ID", "")
    if not raw_path or not run_id or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
        raise RuntimeError("缺少本轮冻结日期缓存路径、SHA256或runId")
    path = Path(raw_path).resolve()
    raw = path.read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != expected_hash:
        raise RuntimeError("本轮冻结日期缓存SHA256不一致")
    payload = json.loads(raw)
    rows = payload.get("records")
    if (payload.get("schemaVersion") != 1 or payload.get("status") != "passed"
            or payload.get("runId") != run_id or not isinstance(rows, list)
            or type(payload.get("recordCount")) is not int or payload["recordCount"] != len(rows)
            or not isinstance(payload.get("sourceExists"), bool) or not payload.get("createdAt")):
        raise RuntimeError("本轮冻结日期缓存契约或runId不一致")
    if not payload["sourceExists"] and rows:
        raise RuntimeError("日期缓存声明数据库不存在但包含记录")
    index = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("queryIccid"), str):
            raise RuntimeError("冻结日期缓存ICCID必须为文本")
        key = normalize_iccid(row["queryIccid"])
        if not key or key != row["queryIccid"] or key in index:
            raise RuntimeError("冻结日期缓存ICCID无效或重复")
        activation = system_kooda_date(row.get("activationDate"), "缓存激活日期")
        cutoff = system_kooda_date(row.get("cutoffDate"), "缓存截止日")
        if row.get('renewal') is not None:
            validate_renewal(row['renewal'], key, row.get('activationDate'), row.get('cutoffDate'))
        elif not activation or not cutoff or (cutoff - activation).days != 29:
            raise RuntimeError("冻结日期缓存必须为有效成对日期且截止日为激活日加29天")
        index[key] = {"activation_dt": activation, "expiry_dt": cutoff}
    return {"path": str(path), "sha256": actual_hash, "run_id": run_id,
            "created_at": payload["createdAt"], "source_database": payload.get("sourceDatabase"),
            "source_exists": payload["sourceExists"], "record_count": len(index), "index": index}


def load_chatr_google_dates() -> dict | None:
    """Authenticate the explicit, run-frozen Chatr-only date input."""
    raw_path = os.environ.get("OPENSMS_CHATR_GOOGLE_DATES_PATH", "")
    expected_hash = os.environ.get("OPENSMS_CHATR_GOOGLE_DATES_SHA256", "").lower()
    if not raw_path and not expected_hash:
        return None
    run_id = os.environ.get("OPENSMS_RUN_ID", "")
    if not raw_path or not run_id or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
        raise RuntimeError("Chatr日期回退缺少快照路径、SHA256或runId")
    if GOOGLE_ENABLED:
        raise RuntimeError("Chatr专用日期回退不能同时开启旧版完整Google关联")
    path = Path(raw_path).resolve()
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise RuntimeError("Chatr日期快照SHA256不一致")
    payload = json.loads(raw)
    records = payload.get("records")
    if (payload.get("schemaVersion") != 1 or payload.get("status") != "passed"
            or payload.get("runId") != run_id or payload.get("source") != "google_chatr_dates"
            or not payload.get("createdAt")
            or not isinstance(records, list) or type(payload.get("recordCount")) is not int
            or payload["recordCount"] != len(records)):
        raise RuntimeError("Chatr日期快照来源、契约、行数或runId不一致")
    source_path = Path(payload.get("sourcePath") or "").resolve()
    source_hash = payload.get("sourceSha256", "")
    if not re.fullmatch(r"[a-f0-9]{64}", source_hash) or not source_path.is_file():
        raise RuntimeError("Chatr日期原始Google文件SHA256不一致")
    source_raw = source_path.read_bytes()
    if hashlib.sha256(source_raw).hexdigest() != source_hash:
        raise RuntimeError("Chatr日期原始Google文件SHA256不一致")
    try:
        source_sheets = bound_room_sheets(source_raw, payload.get("sourceSheets"))
    except ValueError as exc:
        raise RuntimeError(f"Chatr日期快照来源范围错误: {exc}") from exc
    index = {}
    for row in records:
        if not isinstance(row, dict):
            raise RuntimeError("Chatr日期记录格式错误")
        slot = row.get("normalizedSlot")
        if (not isinstance(slot, str) or not slot or normalize_slot(slot) != slot or slot in index
                or normalize_slot(row.get("rawSlot")) != slot
                or normalize_carrier_label(row.get("carrier")) != "chatr"
                or row.get("sourceSheet") not in source_sheets
                or type(row.get("sourceRow")) is not int or row["sourceRow"] < 2):
            raise RuntimeError("Chatr日期记录卡槽、运营商或原始行来源错误")
        dates = {}
        for field, name in (("activationDate", "activation_dt"), ("cutoffDate", "expiry_dt")):
            value = row.get(field)
            if value is None:
                dates[name] = None
                continue
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)?", value):
                raise RuntimeError(f"Chatr快照{field}非空但不是有效ISO日期")
            try:
                dates[name] = datetime.fromisoformat(value)
            except ValueError as exc:
                raise RuntimeError(f"Chatr快照{field}无效") from exc
        query_iccid = row.get("queryIccid")
        provenance = row.get("provenance", [row])
        if (not isinstance(provenance, list) or not provenance or not isinstance(provenance[0], dict)
                or any(row.get(name) != provenance[0].get(name)
                       for name in ("sourceSheet", "sourceRow", "rawSlot", "carrier", "iccid", "phone"))):
            raise RuntimeError("Chatr快照首条来源行证明不一致")
        if query_iccid is not None and (not isinstance(query_iccid, str) or not query_iccid
                or normalize_iccid(query_iccid) != query_iccid
                or not any(isinstance(item, dict) and isinstance(item.get("iccid"), str)
                           and normalize_iccid(item["iccid"]) == query_iccid
                           for item in provenance)):
            raise RuntimeError("Chatr快照ICCID来源冲突")
        index[slot] = {**dates, "query_iccid": query_iccid}
    return {"enabled": True, "status": "passed", "run_id": run_id,
            "snapshot_path": str(path), "snapshot_sha256": expected_hash,
            "source_path": str(source_path), "source_sha256": source_hash,
            "scope": "chatr_blank_activation_and_cutoff_only",
            "source": "current_run_system_then_fresh_google", "record_count": len(index), "index": index}


def resolve_report_date(*, exported, export_column_exists: bool, inventory, cached, google, kooda: bool,
                        chatr_google=None, strict_export=False):
    """One field at a time: export > legacy system > frozen local/Google."""
    parsed = as_datetime(exported)
    if strict_export and text_value(exported) and parsed is None:
        raise RuntimeError("Chatr系统导出日期非空但无效，禁止视为空白后回退Google")
    if parsed:
        return parsed, "system_export"
    if kooda and not export_column_exists and inventory:
        return inventory, "system_inventory_legacy"
    if not kooda and chatr_google is not None:
        return chatr_google, "google_chatr"
    fallback = cached if kooda else as_datetime(google) if GOOGLE_ENABLED else None
    if fallback:
        return fallback, "local_cache" if kooda else "google"
    return None, "missing"


def text_value(value) -> str:
    return "" if value is None else str(value).strip()


def apply_creation_date_fallback(resolved, created, original_dates):
    """Only a truly empty pair after all applicable authoritative sources."""
    if any(value is not None for value, _source in resolved.values()):
        return resolved
    if any(text_value(value) for value in original_dates.values()):
        return resolved
    creation = as_datetime(created)
    if creation is None:
        return resolved
    return {"激活日期": (creation, "creation_date_fallback"),
            "截止日": (creation + timedelta(days=29), "creation_date_fallback")}


def normalize_iccid(value) -> str:
    raw = re.sub(r"\s+", "", text_value(value))
    raw = re.sub(r"[Ff]$", "", raw)
    return raw if re.fullmatch(r"\d{18,22}", raw) else ""


def phone_key(value) -> str:
    digits = "".join(character for character in text_value(value) if character.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def is_kooda_opr(value) -> bool:
    return bool(re.search(r"kooda|koodo|telus1", text_value(value), re.I))


def system_kooda_date(value, field: str) -> datetime | None:
    """Strict ISO source calendar date, with no inference or timezone conversion."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?", value, re.ASCII):
        raise RuntimeError(f"本轮系统{field}非空但不是有效ISO日期")
    try:
        parsed = datetime.fromisoformat(value).date() if len(value) > 10 else date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"本轮系统{field}非空但不是有效ISO日期") from exc
    return datetime.combine(parsed, datetime.min.time())


def load_system_kooda_source() -> dict:
    """Read one hash-bound run snapshot; never query a database or the network."""
    raw_path = os.environ.get("OPENSMS_SYSTEM_KOODA_PATH", "")
    expected_hash = os.environ.get("OPENSMS_SYSTEM_KOODA_SHA256", "")
    run_id = os.environ.get("OPENSMS_RUN_ID", "")
    if not raw_path or not run_id or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash):
        raise RuntimeError("缺少本轮系统Kooda快照路径、SHA256或runId")
    path = Path(raw_path).resolve()
    raw = path.read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != expected_hash.lower():
        raise RuntimeError("本轮系统Kooda快照SHA256不一致")
    payload = json.loads(raw)
    if payload.get("status") != "passed" or payload.get("runId") != run_id:
        raise RuntimeError("本轮系统Kooda快照状态或runId不一致")
    report_source = is_report_source(payload)
    if report_source:
        validate_projection(payload, run_id)
    for key in (("silentBrowserMode",) if report_source else ("paginationTotalStable", "paginationBackendIdsUnique", "silentBrowserMode")):
        if payload.get(key) is not True:
            raise RuntimeError(f"本轮系统Kooda快照未通过{key}")
    if payload.get("browserForegroundActivationAttempted") is not False:
        raise RuntimeError("本轮系统Kooda快照没有后台静默证据")
    rows = payload.get("systemRows")
    total = payload.get("totalRows")
    if not isinstance(rows, list) or type(total) is not int or total < 0 or len(rows) != total:
        raise RuntimeError("本轮系统Kooda快照systemRows总数不一致")
    index, backend_ids, by_source_row = {}, set(), {}
    unkeyed_rows = 0
    known_activation_rows = 0
    for row in rows:
        if not isinstance(row, dict) or not is_kooda_opr(row.get("opr")):
            raise RuntimeError("本轮系统Kooda快照包含非Kooda/Telus1记录")
        identity = text_value(row.get("sourceRowIdentity") if report_source else row.get("id"))
        backend_id = None if report_source else identity
        if not identity or identity in backend_ids:
            raise RuntimeError("本轮系统Kooda快照后台ID为空或重复")
        backend_ids.add(identity)
        iccid = row.get("cardStore")
        if iccid is not None and not isinstance(iccid, str):
            raise RuntimeError("本轮系统ICCID必须为原始文本，禁止数值化")
        activation = system_kooda_date(row.get("chatrActivateDate"), "chatrActivateDate")
        expiry = system_kooda_date(row.get("chatrPlanExpire"), "chatrPlanExpire")
        known_activation_rows += bool(activation)
        record = {"backend_id": backend_id, "iccid": iccid if iccid and iccid.strip() else None,
                  "activation_dt": activation, "expiry_dt": expiry}
        if report_source:
            by_source_row[row['sourceRow']] = {**record, 'raw': row}
        slot, phone = normalize_slot(row.get("slotIdentif")), phone_key(row.get("cardNo"))
        if not slot or len(phone) != 10:
            unkeyed_rows += 1
            continue
        key = (slot, phone)
        if key in index and not report_source:
            raise RuntimeError("本轮系统Kooda快照卡槽与号码复合键重复")
        index[key] = record
    return {
        "path": str(path), "sha256": actual_hash, "run_id": run_id,
        "total_rows": total, "indexed_rows": len(index), "unkeyed_rows": unkeyed_rows,
        "known_activation_rows": known_activation_rows, "index": index, "raw_rows": rows,
        "phone_index": index_phones(rows),
        "source": "system", "match_rule": ('hash_bound_10day_source_row_identity' if report_source
                                               else "normalized_slotIdentif_and_cardNo_last10_exact"),
        "source_kind": payload.get('sourceKind'), "source_scope": payload.get('sourceScope'),
        "projection": payload if report_source else None,
        "source_row_index": by_source_row,
    }


def resolve_system_kooda_row(source: dict, *, slot, phone, status=None, created=None, iccid=None, has_iccid=False, source_row=None) -> dict:
    if source.get('projection'):
        record = source['source_row_index'].get(source_row)
        raw = record.get('raw') if record else None
        if (raw is None or normalize_slot(raw['slotIdentif']) != normalize_slot(slot)
                or phone_key(raw['cardNo']) != phone_key(phone)
                or not (raw['cardStore'] == iccid
                        or (raw['cardStore'] in (None, '') and iccid in (None, '')))):
            raise RuntimeError('主表Kooda与本轮10日表源行身份不一致')
        return record
    key = (normalize_slot(slot), phone_key(phone))
    if not key[0] or len(key[1]) != 10 or key not in source["index"]:
        raw = match_archived(source.get('phone_index', {}), phone=phone, slot=slot,
                             status=status, created=created, iccid=iccid) if has_iccid else None
        if raw is not None:
            return {'backend_id': text_value(raw['id']), 'iccid': raw.get('cardStore') or None,
                    'activation_dt': system_kooda_date(raw.get('chatrActivateDate'), 'chatrActivateDate'),
                    'expiry_dt': system_kooda_date(raw.get('chatrPlanExpire'), 'chatrPlanExpire'),
                    'match_rule': 'archived-phone-machine-created-iccid-exact'}
        raise RuntimeError("主表Kooda卡槽与号码未在本轮系统快照中唯一精确匹配")
    return source["index"][key]


def normalize_source_layout(ws) -> dict:
    """Support old/new raw export schemas without losing source values or types."""
    original = [cell.value for cell in ws[1]]
    names = [text_value(value) for value in original]
    if len([name for name in names if name]) != len(set(name for name in names if name)):
        raise RuntimeError("本轮新鲜主表存在重复源表头，拒绝歧义映射")
    prefix = ["电话", "运营商", "opr", "卡槽"]
    if any(name not in names for name in prefix):
        raise RuntimeError("主表缺少电话/运营商/opr/卡槽必要源列")
    removed_dates = [name for name in ("激活日期", "截止日", "系统导出激活日期", "系统导出截止日") if name in names]
    source_indexes = [names.index(name) + 1 for name in prefix]
    source_indexes += [index + 1 for index, name in enumerate(names) if name not in {*prefix, "ICCID", *removed_dates}]
    if "ICCID" in names:
        source_indexes.append(names.index("ICCID") + 1)
    output_headers = [original[index - 1] for index in source_indexes]
    if source_indexes != list(range(1, len(names) + 1)):
        # One bounded per-row permutation; preserve the workbook and raw values.
        max_row = ws.max_row
        for row_num in range(1, max_row + 1):
            values = []
            for index in source_indexes:
                cell = ws.cell(row_num, index)
                values.append((cell.value, cell.data_type, copy.copy(cell._style)))
            for column, (value, data_type, style) in enumerate(values, start=1):
                cell = ws.cell(row_num, column)
                cell.value = value
                cell.data_type = data_type
                cell._style = style
        if len(source_indexes) < len(names):
            ws.delete_cols(len(source_indexes) + 1, len(names) - len(source_indexes))
    for column, name in enumerate(output_headers, start=1):
        ws.cell(1, column).value = name
    return {
        "original_headers": original, "normalized_headers": output_headers,
        "source_column_count": len(original), "existing_iccid": "ICCID" in names,
        "preserved_date_columns": {}, "removed_redundant_date_columns": removed_dates,
        "mode": "header_named_old_and_new_export_compatible",
    }


def verify_export_system_iccid(source_value, system_value) -> None:
    """An existing nonempty export ICCID must not conflict with system identity."""
    if source_value is None or (isinstance(source_value, str) and not source_value.strip()):
        return
    if not isinstance(source_value, str):
        raise RuntimeError("Kooda源ICCID不是文本，拒绝数值化身份")
    source_key = re.sub(r"[Ff]$", "", source_value.strip())
    system_key = re.sub(r"[Ff]$", "", text_value(system_value))
    if not re.fullmatch(r"\d{18,22}", source_key, re.ASCII) or source_key != system_key:
        raise RuntimeError("Kooda新鲜导出ICCID与本轮系统ICCID标准键冲突，拒绝覆盖")


def classify_number_change(*, kooda_row: bool, activation_dt, expiry_dt, open_dt, created) -> tuple[str, str]:
    # Missing authoritative dates are a report-only new-card assumption. Google open
    # dates must not turn these rows into changed numbers while dates are pending.
    if kooda_row and activation_dt is None and expiry_dt is None:
        return "未换号", "缺失日期按新卡"
    reference_dt = activation_dt or open_dt
    if not created or not reference_dt:
        return "", ""
    source = "激活日期" if activation_dt else "上卡日期"
    status = "已换号" if abs((created - reference_dt).total_seconds()) > 3 * 24 * 60 * 60 else ""
    return status, source


def metric_number(value) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    raw = text_value(value).replace(",", "")
    percent = raw.endswith("%")
    try:
        parsed = float(raw[:-1] if percent else raw)
    except ValueError:
        return None
    return parsed / 100 if percent else parsed


def resolve_header_date(month: int, day: int, reference: date) -> date:
    candidates: list[date] = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        raise RuntimeError(f"无法解析逐日统计日期: {month:02d}-{day:02d}")
    return min(candidates, key=lambda value: abs((value - reference).days))


def find_metric_runs(points: list[dict], field: str = "low") -> list[list[dict]]:
    runs: list[list[dict]] = []
    current: list[dict] = []
    for point in points:
        continues = bool(current) and (point["date"] - current[-1]["date"]).days == 1
        if point[field] and (not current or continues):
            current.append(point)
            continue
        if current:
            runs.append(current)
        current = [point] if point[field] else []
    if current:
        runs.append(current)
    return runs


def aggregate_window_sends(points: list[dict], day_count: int) -> float | None:
    if len(points) < day_count:
        return None
    selected = points[-day_count:]
    if any(point["send_windows"] is None for point in selected):
        return None
    return sum(point["send_windows"] for point in selected)


def aggregate_window_rate(points: list[dict], day_count: int) -> float | None:
    if len(points) < day_count:
        return None
    selected = points[-day_count:]
    if any(point["send_windows"] is None or point["replies"] is None for point in selected):
        return None
    denominator = sum(point["send_windows"] for point in selected)
    if denominator <= 0:
        return None
    return sum(point["replies"] for point in selected) / denominator


def decreasing_rate_classification(points: list[dict]) -> str:
    for day_count in (4, 3, 2):
        if len(points) < day_count:
            continue
        selected = points[-day_count:]
        rates = [point["window_rate"] for point in selected]
        if any(value is None for value in rates):
            continue
        if all(rates[index] < rates[index - 1] for index in range(1, len(rates))):
            return f"持续{day_count}天降低"
    return ""


def low_reply_label(days: int) -> str:
    if days >= 4:
        return "连续4天低于3%"
    if days >= 2:
        return f"连续{days}天低于3%"
    return ""


def zero_reply_label(days: int) -> str:
    if days >= 4:
        return "连续4天0回复"
    if days >= 2:
        return f"连续{days}天0回复"
    return ""


def a11_classification(days: int) -> str:
    if days >= 4:
        return "连续4天及以上"
    if days >= 2:
        return f"连续{days}天"
    return ""


def analyze_raw_metrics(raw_values: list[tuple], reference: date) -> dict:
    if len(raw_values) < 2:
        raise RuntimeError("export4day 原始工作簿没有可分析的数据")
    headers = [text_value(value) for value in raw_values[0]]
    header_index = {header: index for index, header in enumerate(headers) if header}
    for required in ("电话", "运营商", "opr", "卡槽"):
        if required not in header_index:
            raise RuntimeError(f"export4day 缺少必要列: {required}")

    daily: dict[str, dict] = {}
    field_names = {
        "发送窗口数": "send_window_index",
        "回复窗口数": "reply_index",
        "总发送量": "send_index",
        "总回复量": "total_reply_index",
    }
    for index, header in enumerate(headers):
        match = re.fullmatch(r"(\d{2})-(\d{2})(发送窗口数|回复窗口数|总发送量|总回复量)", header)
        if not match:
            continue
        month, day, label = match.groups()
        key = f"{month}-{day}"
        definition = daily.setdefault(
            key,
            {
                "label": key,
                "date": resolve_header_date(int(month), int(day), reference),
            },
        )
        definition[field_names[label]] = index

    required_indices = tuple(field_names.values())
    definitions = sorted(
        (definition for definition in daily.values() if all(name in definition for name in required_indices)),
        key=lambda definition: definition["date"],
    )
    if len(definitions) < 4:
        raise RuntimeError("export4day 没有足够的逐日发送窗口数/回复窗口数列，至少需要连续4日")
    latest_four = definitions[-4:]
    if any((latest_four[index]["date"] - latest_four[index - 1]["date"]).days != 1 for index in range(1, 4)):
        raise RuntimeError("export4day 最新4个统计日期不是连续自然日，停止计算3日/4日回复率")

    phone_index = header_index["电话"]
    carrier_index = header_index["运营商"]
    opr_index = header_index["opr"]
    slot_index = header_index["卡槽"]
    created_index = header_index.get("创建时间")
    use_days_by_phone: dict[str, str] = {}
    zero_days_by_phone: dict[str, int] = {}
    a11_records: list[dict] = []
    window_metrics: list[dict] = []
    a11_rows = 0

    for raw_row in raw_values[1:]:
        phone = text_value(raw_row[phone_index])
        carrier = text_value(raw_row[carrier_index])
        opr = text_value(raw_row[opr_index])
        points: list[dict] = []
        for definition in definitions:
            sends = metric_number(raw_row[definition["send_index"]])
            send_windows = metric_number(raw_row[definition["send_window_index"]])
            replies = metric_number(raw_row[definition["reply_index"]])
            total_replies = metric_number(raw_row[definition["total_reply_index"]])
            active = sends is not None and sends > MIN_DAILY_SENDS and replies is not None
            points.append(
                {
                    **definition,
                    "sends": sends,
                    "send_windows": send_windows,
                    "replies": replies,
                    "total_replies": total_replies,
                    "rate": replies / sends if active else None,
                    "window_rate": replies / send_windows
                    if send_windows is not None and send_windows > 0 and replies is not None
                    else None,
                    "low": bool(active and replies / sends < RATE_THRESHOLD),
                    "zero_reply": bool(send_windows is not None and send_windows > 40 and replies == 0),
                }
            )

        window_metrics.append(
            {
                "three_day_sends": aggregate_window_sends(points, 3),
                "three_day_rate": aggregate_window_rate(points, 3),
                "four_day_sends": aggregate_window_sends(points, 4),
                "four_day_rate": aggregate_window_rate(points, 4),
                "decreasing_rate": decreasing_rate_classification(points),
            }
        )
        zero_runs = [run for run in find_metric_runs(points, "zero_reply") if len(run) >= 2]
        longest_zero = max((len(run) for run in zero_runs), default=0)
        if phone and longest_zero >= 2:
            zero_days_by_phone[phone] = max(zero_days_by_phone.get(phone, 0), longest_zero)

        used_days = sum(
            point["send_windows"] is not None and point["send_windows"] > USE_DAYS_MIN_DAILY_SENDS
            for point in points
        )
        if phone and re.search(r"koo|kini|telus1", f"{carrier} {opr}", re.IGNORECASE) and used_days >= MIN_USED_DAYS:
            use_days_by_phone[phone] = "2天"

        if not re.match(r"^A11(?:-|$)", carrier, re.IGNORECASE):
            continue
        a11_rows += 1
        qualifying = [run for run in find_metric_runs(points) if len(run) >= 2]
        if not qualifying:
            continue
        qualifying.sort(key=lambda run: (len(run), run[-1]["date"]), reverse=True)
        longest = qualifying[0]
        a11_records.append(
            {
                "classification": a11_classification(len(longest)),
                "phone": phone,
                "carrier": carrier,
                "opr": opr,
                "slot": text_value(raw_row[slot_index]),
                "room": room_for_slot(raw_row[slot_index]),
                "created_at": raw_row[created_index] if created_index is not None else None,
                "longest_days": len(longest),
                "longest_start": longest[0]["date"],
                "longest_end": longest[-1]["date"],
                "qualifying_run_count": len(qualifying),
                "run_ranges": "；".join(
                    f"{run[0]['date'].isoformat()}至{run[-1]['date'].isoformat()}({len(run)}天)"
                    for run in qualifying
                ),
                "points": points,
            }
        )

    class_order = {"连续2天": 2, "连续3天": 3, "连续4天及以上": 4}
    a11_records.sort(
        key=lambda record: (
            class_order[record["classification"]],
            -record["longest_days"],
            record["phone"],
        )
    )
    low_by_phone = {record["phone"]: low_reply_label(record["longest_days"]) for record in a11_records}
    zero_by_phone = {phone: zero_reply_label(days) for phone, days in zero_days_by_phone.items()}
    decreasing_counts = {
        label: sum(metric["decreasing_rate"] == label for metric in window_metrics)
        for label in ("持续2天降低", "持续3天降低", "持续4天降低")
    }
    class_counts = {
        label: sum(record["classification"] == label for record in a11_records)
        for label in ("连续2天", "连续3天", "连续4天及以上")
    }
    zero_counts = {
        label: sum(value == label for value in zero_by_phone.values())
        for label in ("连续2天0回复", "连续3天0回复", "连续4天0回复")
    }
    return {
        "reference_date": reference,
        "headers": headers,
        "definitions": definitions,
        "latest_four": latest_four,
        "window_metrics": window_metrics,
        "use_days_by_phone": use_days_by_phone,
        "low_by_phone": low_by_phone,
        "zero_by_phone": zero_by_phone,
        "a11_records": a11_records,
        "a11_rows": a11_rows,
        "decreasing_counts": decreasing_counts,
        "class_counts": class_counts,
        "zero_counts": zero_counts,
    }


def room_for_slot(value) -> str:
    normalized = normalize_slot(value)
    if not normalized:
        return "未配置"
    match = re.match(r"^(\d+)_", normalized)
    if not match:
        return "未配置"
    return room_for_device(match.group(1))


def normalize_slot(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u3000", "").replace(" ", "").replace("-", "_")
    parts = text.split("_")
    normalized: list[str] = []
    for part in parts:
        # Google may serialize slot 204_62 as 204_62.01. The integer prefix is
        # the physical port, so both representations must resolve identically.
        numeric = re.fullmatch(r"(\d+)(?:\.\d+)?", part)
        if numeric:
            normalized.append(str(int(numeric.group(1))))
        else:
            normalized.append(part.lower())
    return "_".join(normalized)


def parse_device_port(value) -> tuple[str, int] | None:
    normalized = normalize_slot(value)
    match = re.fullmatch(r"(\d+)_(\d+)", normalized)
    if not match:
        return None
    device, port_text = match.groups()
    port = int(port_text)
    if not 1 <= port <= 64:
        return None
    return str(int(device)), port


def build_unused_port_rows(system_rows: list[dict], google_slots, google_comparison_slots=None) -> tuple[list[list], list[str]]:
    """Keep device coverage separate from the read-only Google presence annotation."""
    google_slots = list(google_slots)
    google_present = {normalize_slot(value) for value in (google_slots if google_comparison_slots is None else google_comparison_slots)}
    system_by_slot: dict[str, list[dict]] = defaultdict(list)
    devices: set[str] = set()
    for row in system_rows:
        parsed = parse_device_port(row.get("slot"))
        if not parsed:
            continue
        device, port = parsed
        devices.add(device)
        system_by_slot[f"{device}_{port}"].append(row)
    for slot in google_slots:
        parsed = parse_device_port(slot)
        if parsed:
            devices.add(parsed[0])

    output: list[list] = []
    ordered_devices = sorted(devices, key=lambda value: int(value))
    for device in ordered_devices:
        for port in range(1, 65):
            slot = f"{device}_{port}"
            hits = system_by_slot.get(slot) or []
            if not hits:
                output.append([device, room_for_slot(slot), port, slot, "", "", "系统中无", PRESENT if slot in google_present else ""])
                continue
            statuses = [text_value(row.get("status")) for row in hits]
            if statuses and all(status == "注册失败" for status in statuses):
                phones = list(OrderedDict.fromkeys(text_value(row.get("phone")) for row in hits if text_value(row.get("phone"))))
                carriers = list(OrderedDict.fromkeys(text_value(row.get("carrier")) for row in hits if text_value(row.get("carrier"))))
                output.append([
                    device,
                    room_for_slot(slot),
                    port,
                    slot,
                    "、".join(phones),
                    "、".join(carriers),
                    "注册失败",
                    "",
                ])
    return output, ordered_devices


def normalize_carrier_label(value) -> str:
    """Normalize Google carrier labels and export OPR values for comparison."""
    raw = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if not raw:
        return ""
    if "koodo" in raw or "kooda" in raw or "telus1" in raw:
        return "koodo"
    if "chatr" in raw:
        return "chatr"
    if "kini" in raw:
        return "kini"
    if "lucky" in raw:
        return "lucky mobile"
    if "rogers" in raw:
        return "rogers"
    return re.sub(r"[^a-z\u3400-\u9fff]+", "", raw)


def carrier_opr_warning(carrier_price, opr) -> str:
    google_key = normalize_carrier_label(carrier_price)
    opr_key = normalize_carrier_label(opr)
    if not google_key or not opr_key or google_key == opr_key:
        return ""
    return f"電信商不一致：Google={carrier_price}；OPR={opr}"


def make_datetime(year, month, day, hour=0, minute=0, second=0) -> datetime | None:
    try:
        return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
    except (TypeError, ValueError):
        return None


def as_datetime(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number.is_integer():
            compact = str(int(number))
            if re.fullmatch(r"(19|20)\d{6}", compact):
                parsed = make_datetime(compact[:4], compact[4:6], compact[6:8])
                if parsed:
                    return parsed
        if 20000 <= number <= 60000:
            parsed = from_excel(number)
            if isinstance(parsed, datetime):
                return parsed
            if isinstance(parsed, date):
                return datetime(parsed.year, parsed.month, parsed.day)
            return None
    text = str(value).strip()
    if not text:
        return None

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?", text):
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    text = text.strip("'\"")
    text = (
        text.replace("年", "-")
        .replace("月", "-")
        .replace("日", "")
        .replace("／", "/")
        .replace("－", "-")
        .replace("．", ".")
        .replace("T", " ")
    )
    text = re.sub(r"\s+", " ", text)

    match = re.search(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)", text)
    if match:
        parsed = make_datetime(*match.groups())
        if parsed:
            return parsed

    match = re.search(
        r"(?<!\d)((?:19|20)\d{2})[./-](\d{1,2})[./-](\d{1,2})"
        r"(?:[ ]+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?",
        text,
    )
    if match:
        y, m, d, hour, minute, second = match.groups()
        parsed = make_datetime(y, m, d, hour or 0, minute or 0, second or 0)
        if parsed:
            return parsed

    match = re.search(
        r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-]((?:19|20)?\d{2})"
        r"(?:[ ]+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?",
        text,
    )
    if match:
        first, second, year, hour, minute, second_part = match.groups()
        year = int(year)
        if year < 100:
            year += 2000 if year < 70 else 1900
        first_num = int(first)
        second_num = int(second)
        if first_num > 12:
            day, month = first_num, second_num
        elif second_num > 12:
            month, day = first_num, second_num
        else:
            month, day = first_num, second_num
        parsed = make_datetime(year, month, day, hour or 0, minute or 0, second_part or 0)
        if parsed:
            return parsed

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def as_date(value) -> date | None:
    dt = as_datetime(value)
    return dt.date() if dt else None


def expiry_reference_date(main_path: Path) -> tuple[date, str, str]:
    server_date_text = os.environ.get("EXPORT4DAY_SERVER_DATE", "").strip()
    if not server_date_text:
        raise RuntimeError("缺少后台网页右上角服务器日期 EXPORT4DAY_SERVER_DATE，禁止使用本机或文件时间代替")
    parsed = as_datetime(server_date_text)
    if not parsed:
        raise RuntimeError(f"EXPORT4DAY_SERVER_DATE 无法解析: {server_date_text}")
    return parsed.date(), parsed.strftime("%Y-%m-%d"), "后台网页右上角服务器日期"


def classify_expiry_status(expiry: date, reference: date) -> str:
    """Classify against the platform server date without overlapping states."""
    days_until_expiry = (expiry - reference).days
    if days_until_expiry <= 0:
        return "已过期"
    if days_until_expiry <= 2:
        return "快过期"
    if days_until_expiry <= 6:
        return "6天"
    return ""


def load_task_f_kooda_dates(database_path: Path = TASK_F_DB_PATH) -> dict:
    """Load immutable Kooda date indexes from Task F's local SQLite database."""
    empty = {
        "database": str(database_path),
        "database_exists": False,
        "found_count": 0,
        "by_iccid": {},
        "by_phone": {},
        "by_slot": {},
    }
    if not database_path.is_file():
        return empty

    connection = sqlite3.connect(f"file:{database_path.resolve()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
                a.query_iccid, a.source_iccid, a.activation_date, a.cutoff_date,
                a.queried_at, a.data_source, a.source_sha256,
                i.phone, i.slot, i.opr, i.carrier_name
            FROM kooda_activation AS a
            LEFT JOIN kooda_inventory AS i USING(query_iccid)
            WHERE a.lookup_status = 'found'
            ORDER BY a.query_iccid
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"任务F Kooda日期数据库结构无效: {database_path}: {exc}") from exc
    finally:
        connection.close()

    indexes = {"by_iccid": {}, "by_phone": {}, "by_slot": {}}

    def register(index_name: str, key: str, record: dict) -> None:
        if not key:
            return
        existing = indexes[index_name].get(key)
        if existing and (
            existing["query_iccid"] != record["query_iccid"]
            or existing["activation_date"] != record["activation_date"]
            or existing["cutoff_date"] != record["cutoff_date"]
        ):
            raise RuntimeError(f"任务F数据库{index_name}键冲突: {key}")
        indexes[index_name][key] = record

    for row in rows:
        activation = as_date(row["activation_date"])
        cutoff = as_date(row["cutoff_date"])
        if not activation or not cutoff:
            raise RuntimeError(f"任务F数据库found记录缺日期: {row['query_iccid']}")
        if (cutoff - activation).days != 29:
            raise RuntimeError(f"任务F数据库截止日不符合含激活日30天口径: {row['query_iccid']}")
        record = {
            "query_iccid": normalize_iccid(row["query_iccid"]),
            "source_iccid": text_value(row["source_iccid"]),
            "activation_date": activation,
            "cutoff_date": cutoff,
            "queried_at": text_value(row["queried_at"]),
            "data_source": text_value(row["data_source"]),
            "source_sha256": text_value(row["source_sha256"]),
            "phone": phone_key(row["phone"]),
            "slot": normalize_slot(row["slot"]),
            "opr": text_value(row["opr"]),
            "carrier_name": text_value(row["carrier_name"]),
        }
        if not record["query_iccid"]:
            raise RuntimeError(f"任务F数据库found记录ICCID无效: {row['query_iccid']}")
        register("by_iccid", record["query_iccid"], record)

    return {
        "database": str(database_path.resolve()),
        "database_exists": True,
        "found_count": len(rows),
        **indexes,
    }


def resolve_task_f_kooda_date(indexes: dict, *, iccid, phone, slot) -> tuple[dict | None, str]:
    """Resolve only by the normalized ICCID; phone and slot never substitute for it."""
    del phone, slot
    query_iccid = normalize_iccid(iccid)
    if not query_iccid:
        return None, ""
    record = indexes.get("by_iccid", {}).get(query_iccid)
    if not record:
        return None, ""
    return record, "ICCID去末尾单个F后精确匹配"


def first_header_index(headers: list, names: list[str]) -> int | None:
    for name in names:
        for idx, header in enumerate(headers, start=1):
            if str(header or "").strip() == name:
                return idx
    return None


def copy_cell_style(src, dst) -> None:
    if src.has_style:
        dst.font = copy.copy(src.font)
        dst.fill = copy.copy(src.fill)
        dst.border = copy.copy(src.border)
        dst.alignment = copy.copy(src.alignment)
        dst.number_format = src.number_format
        dst.protection = copy.copy(src.protection)


def fill_task_f_missing_activation_row(worksheet, row_number: int, max_column: int) -> None:
    """Give a pending Task F row the stable full-row light-red marker."""
    pending_fill = PatternFill(fill_type="solid", fgColor="FFC7CE")
    for column_number in range(1, max_column + 1):
        worksheet.cell(row_number, column_number).fill = pending_fill


def fill_row_with_registered_fill(worksheet, row_number: int, max_column: int, fill_id: int) -> None:
    """Apply one workbook-registered fill without hashing it for every cell.

    This is the fill descriptor's assignment after IndexedList.add; every cell
    retains its own other style fields, including number format and protection.
    """
    for column_number in range(1, max_column + 1):
        cell = worksheet.cell(row_number, column_number)
        if not cell._style:
            cell._style = StyleArray()
        cell._style.fillId = fill_id


def cell_has_value(value) -> bool:
    return value is not None and str(value).strip() != "" and not is_excel_error(value)


def is_excel_error(value) -> bool:
    return isinstance(value, str) and value.strip().upper() in {
        "#NULL!",
        "#DIV/0!",
        "#VALUE!",
        "#REF!",
        "#NAME?",
        "#NUM!",
        "#N/A",
        "#GETTING_DATA",
    }


def safe_source_value(value):
    return None if is_excel_error(value) else value


def safe_date_value(value, parsed: datetime | None):
    return parsed if parsed else safe_source_value(value)


def assoc_score(record: dict) -> int:
    return sum(
        [
            cell_has_value(record["activation_date"]) * 8,
            cell_has_value(record["expiry_date"]) * 4,
            cell_has_value(record["open_date"]) * 2,
            cell_has_value(record["carrier_price"]),
            cell_has_value(record["raw_slot"]),
        ]
    )


def unique_headers(headers: list) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for index, header in enumerate(headers, start=1):
        base = str(header or "").strip() or f"空列{index}"
        seen[base] = seen.get(base, 0) + 1
        result.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return result


def build_assoc_lookup():
    wb = load_workbook(ASSOC_PATH, data_only=True)
    lookup = OrderedDict()
    used_sheets: list[str] = []
    google_rows: list[dict] = []
    sheet_row_counts: OrderedDict[str, int] = OrderedDict()

    missing_sheets = [name for name in REQUIRED_ASSOC_SHEETS if name not in wb.sheetnames]
    if missing_sheets:
        raise RuntimeError(f"关联表缺少必需页签: {', '.join(missing_sheets)}")

    for sheet_name in REQUIRED_ASSOC_SHEETS:
        ws = wb[sheet_name]
        headers = [cell.value for cell in ws[1]]
        slot_col = first_header_index(headers, ["卡槽"])
        carrier_price_col = first_header_index(headers, ["電信商/價格"])
        open_col = first_header_index(headers, ["上卡日期"])
        activation_col = first_header_index(headers, ["激活日期"])
        expiry_col = first_header_index(headers, ["預計過期日"])
        note_col = first_header_index(headers, ["備註", "备注"])
        iccid_col = first_header_index(headers, ["iccid", "ICCID"])
        phone_col = first_header_index(headers, ["手機號", "手机号", "電話", "电话", "号码"])
        required = (
            slot_col,
            carrier_price_col,
            open_col,
            activation_col,
            expiry_col,
            note_col,
            iccid_col,
            phone_col,
        )
        if not any(required):
            continue
        if not all(required):
            missing = [
                name
                for name, column in (
                    ("卡槽", slot_col),
                    ("電信商/價格", carrier_price_col),
                    ("上卡日期", open_col),
                    ("激活日期", activation_col),
                    ("預計過期日", expiry_col),
                    ("備註", note_col),
                    ("iccid", iccid_col),
                    ("手機號", phone_col),
                )
                if not column
            ]
            raise RuntimeError(f"关联表页签 {ws.title} 缺少精确字段: {', '.join(missing)}")

        used_sheets.append(ws.title)
        sheet_row_counts[ws.title] = 0
        source_headers = unique_headers(headers)
        assoc_max_column = ws.max_column
        for row in range(2, ws.max_row + 1):
            raw_slot = ws.cell(row, slot_col).value
            norm_slot = normalize_slot(raw_slot)
            if not norm_slot:
                continue
            record = {
                "raw_slot": raw_slot,
                "carrier_price": ws.cell(row, carrier_price_col).value,
                "open_date": ws.cell(row, open_col).value,
                "activation_date": ws.cell(row, activation_col).value,
                "expiry_date": ws.cell(row, expiry_col).value,
                "note": ws.cell(row, note_col).value,
                "iccid": ws.cell(row, iccid_col).value,
                "phone": ws.cell(row, phone_col).value,
                "source_sheet": ws.title,
                "source_row": row,
                "source_headers": source_headers,
                "source_values": [
                    safe_source_value(ws.cell(row, col).value)
                    for col in range(1, assoc_max_column + 1)
                ],
            }
            google_rows.append(record)
            sheet_row_counts[ws.title] += 1
            existing = lookup.get(norm_slot)
            if not existing or assoc_score(record) > assoc_score(existing):
                lookup[norm_slot] = record

    if not used_sheets:
        raise RuntimeError("关联表没有可用机房页签，缺少卡槽/電信商/價格/上卡日期/激活日期/預計過期日")
    wb.close()
    return lookup, used_sheets, google_rows, sheet_row_counts


def classify_google_system_missing_rows(google_rows: list[dict], main_slots: set[str]):
    """Return all Google-only rows and its valid-activation-date subset."""
    all_missing_rows = []
    activated_missing_rows = []
    for record in google_rows:
        norm_slot = normalize_slot(record["raw_slot"])
        if not norm_slot or norm_slot in main_slots:
            continue
        open_dt = as_datetime(record["open_date"])
        activation_dt = as_datetime(record["activation_date"])
        expiry_dt = as_datetime(record["expiry_date"])
        item = (record, norm_slot, open_dt, activation_dt, expiry_dt)
        all_missing_rows.append(item)
        if activation_dt:
            activated_missing_rows.append(item)
    return all_missing_rows, activated_missing_rows


def google_activated_missing_output_row(item: tuple) -> list:
    """Build one exact, type-preserving row for the activated Google-only sheet."""
    record, norm_slot, open_dt, activation_dt, expiry_dt = item
    return [
        record["source_sheet"],
        record["source_row"],
        safe_source_value(record["raw_slot"]),
        norm_slot,
        safe_source_value(record["note"]),
        text_value(safe_source_value(record["iccid"])),
        text_value(safe_source_value(record["phone"])),
        safe_date_value(record["open_date"], open_dt),
        safe_source_value(record["carrier_price"]),
        activation_dt,
        safe_date_value(record["expiry_date"], expiry_dt),
    ]


def highlight_activated_google_missing_rows(worksheet, google_all_missing_rows: list[tuple]) -> int:
    """Highlight full output rows whose source record has a valid activation date."""
    yellow_fill = PatternFill(fill_type="solid", fgColor="FFFF00")
    highlighted = 0
    for output_row, (_record, _norm_slot, _open_dt, activation_dt, _expiry_dt) in enumerate(
        google_all_missing_rows,
        start=2,
    ):
        if not activation_dt:
            continue
        for cell in worksheet[output_row]:
            cell.fill = copy.copy(yellow_fill)
        highlighted += 1
    return highlighted


def apply_fast_enrichment(wb, ws, enrichment: dict, phone_col: int, ten_day_rate_col: int, summary_ws) -> dict:
    headers = [text_value(cell.value) for cell in ws[1]]
    header_index = {header: index for index, header in enumerate(headers, start=1) if header}
    expected_metric_columns = {
        "3日发送窗口数": ten_day_rate_col + 1,
        "3日回复率": ten_day_rate_col + 2,
        "4日发送窗口数": ten_day_rate_col + 3,
        "4日回复率": ten_day_rate_col + 4,
        "回复率持续降低": ten_day_rate_col + 5,
    }
    for header, expected_column in expected_metric_columns.items():
        if header_index.get(header) != expected_column:
            raise RuntimeError(f"快速合并文档列位置不正确: {header}")
    if len(enrichment["window_metrics"]) != ws.max_row - 1:
        raise RuntimeError("快速合并文档统计行数与主表不一致")

    use_days_col = header_index["koo是否2天"]
    low_col = header_index["连续低于x%"]
    zero_col = header_index["连续0回复"]
    metric_columns = [expected_metric_columns[name] for name in expected_metric_columns]
    for row_number, metric in enumerate(enrichment["window_metrics"], start=2):
        phone = text_value(ws.cell(row_number, phone_col).value)
        ws.cell(row_number, use_days_col).value = enrichment["use_days_by_phone"].get(phone)
        ws.cell(row_number, low_col).value = enrichment["low_by_phone"].get(phone)
        ws.cell(row_number, zero_col).value = enrichment["zero_by_phone"].get(phone)
        ws.cell(row_number, metric_columns[0]).value = metric["three_day_sends"]
        ws.cell(row_number, metric_columns[1]).value = metric["three_day_rate"]
        ws.cell(row_number, metric_columns[2]).value = metric["four_day_sends"]
        ws.cell(row_number, metric_columns[3]).value = metric["four_day_rate"]
        ws.cell(row_number, metric_columns[4]).value = metric["decreasing_rate"] or None
        ws.cell(row_number, metric_columns[0]).number_format = "#,##0"
        ws.cell(row_number, metric_columns[1]).number_format = "0.00%"
        ws.cell(row_number, metric_columns[2]).number_format = "#,##0"
        ws.cell(row_number, metric_columns[3]).number_format = "0.00%"

    for column in (use_days_col, low_col, zero_col, *metric_columns):
        ws.cell(1, column).font = Font(bold=True)
        ws.cell(1, column).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.column_dimensions[ws.cell(1, use_days_col).column_letter].width = 14
    ws.column_dimensions[ws.cell(1, low_col).column_letter].width = 20
    ws.column_dimensions[ws.cell(1, zero_col).column_letter].width = 20
    for column, width in zip(metric_columns, (16, 12, 16, 12, 18)):
        ws.column_dimensions[ws.cell(1, column).column_letter].width = width

    for cancelled in CANCELLED_REVIEW_SHEETS:
        if cancelled in wb.sheetnames:
            del wb[cancelled]
    if ENRICHMENT_SHEET_NAME in wb.sheetnames:
        raise RuntimeError(f"最终工作簿已经存在工作表 {ENRICHMENT_SHEET_NAME}，请使用本轮新生成的结果文件")
    sheet = wb.create_sheet(ENRICHMENT_SHEET_NAME)
    sheet.sheet_view.showGridLines = False
    definitions = enrichment["definitions"]
    core_headers = [
        "分类", "电话", "运营商", "opr", "卡槽", "机房", "创建时间", "最长连续天数",
        "最长连续起始日", "最长连续结束日", "命中区间数", "全部命中区间",
    ]
    daily_headers = [
        header
        for definition in definitions
        for header in (
            f"{definition['label']}总发送量",
            f"{definition['label']}回复窗口数",
            f"{definition['label']}计算回复率",
        )
    ]
    output_headers = core_headers + daily_headers
    sheet.merge_cells("A1:H1")
    sheet["A1"] = "A11 连续低回复率号码"
    sheet.merge_cells("A2:L2")
    sheet["A2"] = (
        "口径：逐日总发送量 > 50；日回复率 = 回复窗口数 ÷ 总发送量；严格低于3%；"
        "总发送量不达门槛的日期中断连续天数；分类采用每个号码的最长连续自然日天数。"
    )
    sheet.append([
        "A11总行数", "命中号码数", "连续2天", "连续3天", "连续4天及以上", "阈值", "基准日期", "逐日范围",
    ])
    sheet.append([
        enrichment["a11_rows"],
        len(enrichment["a11_records"]),
        enrichment["class_counts"]["连续2天"],
        enrichment["class_counts"]["连续3天"],
        enrichment["class_counts"]["连续4天及以上"],
        RATE_THRESHOLD,
        enrichment["reference_date"],
        f"{definitions[0]['date'].isoformat()}至{definitions[-1]['date'].isoformat()}",
    ])
    sheet.append(output_headers)
    first_data_row = 6
    for record in enrichment["a11_records"]:
        row = [
            record["classification"], record["phone"], record["carrier"], record["opr"], record["slot"],
            record["room"], record["created_at"], record["longest_days"], record["longest_start"],
            record["longest_end"], record["qualifying_run_count"], record["run_ranges"],
        ]
        for point in record["points"]:
            row.extend((point["sends"], point["replies"], point["rate"]))
        sheet.append(row)

    dark_fill = PatternFill(fill_type="solid", fgColor="1F4E78")
    header_fill = PatternFill(fill_type="solid", fgColor="4472C4")
    pale_fill = PatternFill(fill_type="solid", fgColor="D9EAF7")
    thin = Side(style="thin", color="D9E2F3")
    for cell in sheet[1]:
        cell.fill = dark_fill
        cell.font = Font(bold=True, color="FFFFFF", size=16)
    sheet["A2"].fill = pale_fill
    sheet["A2"].alignment = Alignment(wrap_text=True, vertical="center")
    for cell in sheet[5]:
        cell.fill = header_fill
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=thin)
    for row_number in range(first_data_row, sheet.max_row + 1):
        sheet.cell(row_number, 2).number_format = "@"
        sheet.cell(row_number, 5).number_format = "@"
        sheet.cell(row_number, 7).number_format = "yyyy-mm-dd hh:mm:ss"
        sheet.cell(row_number, 9).number_format = "yyyy-mm-dd"
        sheet.cell(row_number, 10).number_format = "yyyy-mm-dd"
        for definition_index in range(len(definitions)):
            start_column = len(core_headers) + definition_index * 3 + 1
            sheet.cell(row_number, start_column).number_format = "#,##0"
            sheet.cell(row_number, start_column + 1).number_format = "#,##0"
            sheet.cell(row_number, start_column + 2).number_format = "0.00%"
    fixed_widths = (15, 16, 22, 22, 14, 12, 20, 14, 16, 16, 12, 46)
    for column_number, header in enumerate(output_headers, start=1):
        width = fixed_widths[column_number - 1] if column_number <= len(fixed_widths) else 14
        sheet.column_dimensions[sheet.cell(5, column_number).column_letter].width = width
    sheet.freeze_panes = "C6"

    summary_start = summary_ws.max_row + 1
    summary_items = [
        ("koo是否2天统计口径", "Kooda/T系号码逐日发送窗口数>80的日期达到2天及以上，日期可连续或不连续"),
        ("koo是否2天命中号码数", len(enrichment["use_days_by_phone"])),
        ("A11低回复统计口径", "逐日总发送量>50，回复窗口数/总发送量严格低于3%，按最长连续自然日分类；总发送量<=50的日期中断连续天数"),
        ("A11总行数", enrichment["a11_rows"]),
        ("A11低回复命中号码数", len(enrichment["a11_records"])),
        ("A11连续2天号码数", enrichment["class_counts"]["连续2天"]),
        ("A11连续3天号码数", enrichment["class_counts"]["连续3天"]),
        ("A11连续4天及以上号码数", enrichment["class_counts"]["连续4天及以上"]),
        ("主表连续0回复统计口径", "逐日发送窗口数>40且回复窗口数=0，按最长连续自然日分类"),
        ("主表连续0回复命中号码数", len(enrichment["zero_by_phone"])),
        ("主表连续2天0回复号码数", enrichment["zero_counts"]["连续2天0回复"]),
        ("主表连续3天0回复号码数", enrichment["zero_counts"]["连续3天0回复"]),
        ("主表连续4天及以上0回复号码数", enrichment["zero_counts"]["连续4天0回复"]),
        ("已取消工作表", "、".join(CANCELLED_REVIEW_SHEETS)),
        ("最近4日统计日期", "、".join(item["date"].isoformat() for item in enrichment["latest_four"])),
        ("3日发送窗口数口径", "最新连续3日发送窗口数合计；任一日缺值则留空"),
        ("3日回复率口径", "最新连续3日回复窗口数合计/发送窗口数合计；任一日缺值或合计发送窗口数为0则留空"),
        ("4日发送窗口数口径", "最新连续4日发送窗口数合计；任一日缺值则留空"),
        ("4日回复率口径", "最新连续4日回复窗口数合计/发送窗口数合计；任一日缺值或合计发送窗口数为0则留空"),
        ("回复率持续降低口径", "每日回复率=回复窗口数/发送窗口数；从最新日向前严格逐日降低，优先显示最长4天，其次3天、2天；相等不命中"),
        ("持续2天降低号码数", enrichment["decreasing_counts"]["持续2天降低"]),
        ("持续3天降低号码数", enrichment["decreasing_counts"]["持续3天降低"]),
        ("持续4天降低号码数", enrichment["decreasing_counts"]["持续4天降低"]),
    ]
    for row_offset, (label, value) in enumerate(summary_items):
        summary_ws.cell(summary_start + row_offset, 1).value = label
        summary_ws.cell(summary_start + row_offset, 1).font = Font(bold=True)
        summary_ws.cell(summary_start + row_offset, 2).value = value

    return {
        "status": "success",
        "sheet_name": ENRICHMENT_SHEET_NAME,
        "threshold": RATE_THRESHOLD,
        "minimum_daily_sends_exclusive": MIN_DAILY_SENDS,
        "low_reply_rule": "总发送量>50且回复窗口数/总发送量严格低于3%",
        "reference_date": enrichment["reference_date"].isoformat(),
        "daily_start": enrichment["definitions"][0]["date"].isoformat(),
        "daily_end": enrichment["definitions"][-1]["date"].isoformat(),
        "a11_rows": enrichment["a11_rows"],
        "matched_phone_rows": len(enrichment["a11_records"]),
        "main_sheet_columns": [
            "koo是否2天", "激活后天数", "连续低于x%", "连续0回复", "3日发送窗口数", "3日回复率",
            "4日发送窗口数", "4日回复率", "回复率持续降低",
        ],
        "recent_window_rate_dates": [item["date"].isoformat() for item in enrichment["latest_four"]],
        "decreasing_rate_counts": enrichment["decreasing_counts"],
        "use_two_days_rows": len(enrichment["use_days_by_phone"]),
        "main_sheet_classified_rows": len(enrichment["a11_records"]),
        "class_counts": enrichment["class_counts"],
        "zero_reply_classified_rows": len(enrichment["zero_by_phone"]),
        "zero_reply_counts": enrichment["zero_counts"],
        "cancelled_review_sheets": list(CANCELLED_REVIEW_SHEETS),
        "cell_type_policy": {
            "numeric_text_conversion_enabled": False,
            "mode": "preserve_source_cell_types",
        },
        "implementation": "single_pass_python_enrichment",
    }


def process():
    process_started = time.perf_counter()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    expiry_ref_date, expiry_ref_display, expiry_ref_source = expiry_reference_date(MAIN_PATH)

    # All inputs are immutable. Load the large export, Google association and
    # this run's hash-bound system Kooda snapshot concurrently. Writes remain single-
    # threaded against one workbook.
    input_workers = 4 if GOOGLE_ENABLED or os.environ.get("OPENSMS_CHATR_GOOGLE_DATES_PATH") else 3
    with ThreadPoolExecutor(max_workers=input_workers, thread_name_prefix="opensms-doc-input") as executor:
        assoc_future = executor.submit(build_assoc_lookup) if GOOGLE_ENABLED else None
        system_future = executor.submit(load_system_kooda_source)
        cache_future = executor.submit(load_report_date_cache)
        chatr_future = executor.submit(load_chatr_google_dates)
        retention_future = executor.submit(load_retention_report)
        difference_future = executor.submit(load_google_difference)
        ko_unactivated_future = executor.submit(load_ko_unactivated)
        main_future = executor.submit(load_workbook, MAIN_PATH)
        wb = main_future.result()
        ws = wb.active
        raw_values = list(ws.iter_rows(values_only=True))
        device_future = executor.submit(aggregate_device_daily, raw_values[0], tuple(raw_values[1:]))
        raw_header_index = {text_value(name): index for index, name in enumerate(raw_values[0])}
        raw_iccid_index = next((i for i, name in enumerate(raw_values[0]) if text_value(name).lower() == "iccid"), None)
        raw_system_iccids = [{"slot": values[raw_header_index["卡槽"]],
                              "iccid": values[raw_iccid_index] if raw_iccid_index is not None else None}
                             for values in raw_values[1:]]
        exported_dates = [
            {name: values[raw_header_index[name]] if name in raw_header_index else None
             for name in ("激活日期", "截止日")}
            for values in raw_values[1:]
        ]
        enrichment = analyze_raw_metrics(raw_values, expiry_ref_date)
        assoc, assoc_sheets, google_rows, assoc_sheet_row_counts = assoc_future.result() if assoc_future else ({}, [], [], {})
        system_kooda = system_future.result()
        if system_kooda.get('projection'):
            verify_projection_rows(system_kooda['projection'], raw_values[0], raw_values[1:],
                                   os.environ['OPENSMS_RUN_ID'], MAIN_PATH)
        report_cache = cache_future.result()
        chatr_dates = chatr_future.result()
        device_daily_result = device_future.result()
        retention_evidence = retention_future.result()
        difference_evidence = difference_future.result()
        ko_unactivated_evidence = select_system_records(ko_unactivated_future.result(), system_kooda)
    if chatr_dates:
        SYSTEM_KOODA_FIELD_MAPPING.update({
            "激活日期": "系统导出优先；Kooda缺失读取冻结F库；仅Chatr缺失读取本轮Google激活日期",
            "截止日": "系统导出优先；Kooda缺失读取冻结F库；仅Chatr缺失读取本轮Google預計過期日"})
    if system_kooda.get('projection'):
        SYSTEM_KOODA_FIELD_MAPPING['ICCID'] = 'Kooda/Telus1使用本轮10日表ICCID原值（保留末尾F）；不查询全平台库存'
    del raw_values
    input_seconds = time.perf_counter() - process_started
    transform_started = time.perf_counter()

    source_layout = normalize_source_layout(ws)
    main_headers = [cell.value for cell in ws[1]]
    main_slot_col = first_header_index(main_headers, ["卡槽"])
    if not main_slot_col:
        raise RuntimeError("主表缺少必要列: 卡槽")
    main_slots = {
        normalize_slot(ws.cell(row, main_slot_col).value)
        for row in range(2, ws.max_row + 1)
        if normalize_slot(ws.cell(row, main_slot_col).value)
    }

    # The former implementation shifted the whole worksheet five times.  The
    # same final layout is two insertion blocks when applied right-to-left:
    # eight columns before original E, then four columns before original C.
    # This is materially faster on the multi-thousand-row 10-day export and
    # preserves the exact positions checked below by the independent verifier.
    ws.insert_cols(5, 8)
    ws.insert_cols(3, 4)
    ws.cell(1, 3).value = "过期状态"
    ws.cell(1, 4).value = "换号状态"
    ws.cell(1, 5).value = "koo是否2天"
    ws.cell(1, 6).value = "激活后天数"
    ws.cell(1, 9).value = "连续低于x%"
    ws.cell(1, 10).value = "连续0回复"
    ws.cell(1, 11).value = "开卡日期"
    ws.cell(1, 12).value = "激活日期"
    ws.cell(1, 13).value = "截止日"
    ws.cell(1, 14).value = "机房"
    ws.cell(1, 15).value = "Google電信商/價格"
    ws.cell(1, 16).value = "OPR匹配预警"

    pre_metric_headers = [cell.value for cell in ws[1]]
    ten_day_rate_col = first_header_index(pre_metric_headers, ["10日回复率", "近10日回复率"])
    if not ten_day_rate_col:
        raise RuntimeError("主表缺少必要列: 10日回复率")
    ws.insert_cols(ten_day_rate_col + 1, 5)
    ws.cell(1, ten_day_rate_col + 1).value = "3日发送窗口数"
    ws.cell(1, ten_day_rate_col + 2).value = "3日回复率"
    ws.cell(1, ten_day_rate_col + 3).value = "4日发送窗口数"
    ws.cell(1, ten_day_rate_col + 4).value = "4日回复率"
    ws.cell(1, ten_day_rate_col + 5).value = "回复率持续降低"
    for col in range(ten_day_rate_col + 1, ten_day_rate_col + 6):
        copy_cell_style(ws.cell(1, ten_day_rate_col), ws.cell(1, col))
        if not ws.cell(1, col).has_style:
            ws.cell(1, col).font = Font(bold=True)

    iccid_col = first_header_index([cell.value for cell in ws[1]], ["ICCID"]) or ws.max_column + 1
    ws.cell(1, iccid_col).value = "ICCID"
    copy_cell_style(ws.cell(1, 2), ws.cell(1, iccid_col))
    ws.cell(1, iccid_col).font = Font(bold=True)
    ws.column_dimensions[ws.cell(1, iccid_col).column_letter].width = 26

    for col in (3, 4, 5, 6, 9, 10, 11, 12, 13, 14, 15, 16):
        copy_cell_style(ws.cell(1, 2), ws.cell(1, col))
        ws.cell(1, col).font = copy.copy(ws.cell(1, 2).font) if ws.cell(1, 2).has_style else Font(bold=True)
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["F"].width = 14
    ws.column_dimensions["O"].width = 24
    ws.column_dimensions["P"].width = 60
    main_max_column = ws.max_column

    headers = [cell.value for cell in ws[1]]
    slot_col = first_header_index(headers, ["卡槽"])
    created_col = first_header_index(headers, ["创建时间"])
    phone_col = first_header_index(headers, ["电话", "号码", "手机号"])
    carrier_col = first_header_index(headers, ["运营商"])
    opr_col = first_header_index(headers, ["opr"])
    status_col = first_header_index(headers, ["状态"])
    if not slot_col or not created_col or not phone_col or not opr_col or not status_col:
        raise RuntimeError(
            "主表缺少必要列: "
            f"slot={slot_col}, created={created_col}, phone={phone_col}, opr={opr_col}, status={status_col}"
        )

    yellow = PatternFill(fill_type="solid", fgColor="FFFF00")
    yellow_fill_id = None
    main_rows = ws.max_row - 1
    matched_rows = 0
    google_matched_rows = 0
    changed_rows = 0
    changed_by_activation_rows = 0
    changed_by_open_rows = 0
    change_checked_by_activation_rows = 0
    change_checked_by_open_rows = 0
    expired_rows = 0
    fast_expire_rows = 0
    six_day_rows = 0
    carrier_opr_mismatch_rows = 0
    carrier_opr_mismatches: list[list] = []
    activation_age_known_rows = 0
    activation_age_unknown_rows = 0
    activation_age_future_rows = 0
    kooda_rows = 0
    kooda_task_f_date_rows = 0
    kooda_task_f_missing_rows = 0
    kooda_task_f_missing_red_rows = 0
    kooda_missing_dates_new_card_rows = 0
    kooda_task_f_match_methods: dict[str, int] = defaultdict(int)
    export_system_iccid_compared_rows = 0
    export_system_date_conflicts = {"激活日期": 0, "截止日": 0}
    export_system_date_conflict_details = []
    date_field_counts = {name: {source: 0 for source in (*DATE_SOURCE_NAMES, *(("google_chatr",) if chatr_dates else ()))}
                         for name in ("激活日期", "截止日")}
    chatr_matched_rows = chatr_filled_rows = 0
    chatr_iccid_conflicts = []
    chatr_rows = chatr_missing_activation_rows = chatr_missing_cutoff_rows = 0
    kooda_system_blank_activation_rows = 0
    local_cache_filled_rows = 0
    missing_date_scopes = []
    creation_date_fallback_rows = []
    matched_system_keys = set()
    matched_archived_backend_ids = set()
    unmatched = OrderedDict()
    room_counts = OrderedDict((room, 0) for room in (*ROOM_LABELS, "未配置"))
    activated_unlisted_rows: list[list] = []
    activated_unlisted_by_machine: dict[str, list[list]] = defaultdict(list)
    system_port_rows: list[dict] = []

    for row in range(2, ws.max_row + 1):
        raw_slot = ws.cell(row, slot_col).value
        norm_slot = normalize_slot(raw_slot)
        room = room_for_slot(raw_slot)
        ws.cell(row, 14).value = room
        room_counts[room] += 1
        if norm_slot:
            system_port_rows.append({
                "slot": norm_slot,
                "phone": ws.cell(row, phone_col).value,
                "carrier": ws.cell(row, carrier_col).value if carrier_col else None,
                "status": ws.cell(row, status_col).value,
            })
        phone_value = ws.cell(row, phone_col).value
        opr_value = safe_source_value(ws.cell(row, opr_col).value)
        kooda_row = is_kooda_opr(opr_value)
        chatr_row = normalize_carrier_label(opr_value) == "chatr"
        chatr_rows += int(chatr_row)
        chatr_card = chatr_dates["index"].get(norm_slot) if chatr_dates and chatr_row else None
        iccid_conflict = None
        if chatr_card:
            chatr_matched_rows += 1
            source_iccid = ws.cell(row, iccid_col).value if source_layout["existing_iccid"] else None
            source_iccid_key = normalize_iccid(source_iccid) if isinstance(source_iccid, str) else ""
            if source_iccid_key and chatr_card["query_iccid"] and source_iccid_key != chatr_card["query_iccid"]:
                iccid_conflict = {
                    "row": row, "phone": text_value(phone_value), "slot": text_value(raw_slot),
                    "system_iccid": source_iccid, "google_iccid": chatr_card["query_iccid"],
                    "system_activation": (as_datetime(exported_dates[row - 2]["激活日期"]).isoformat()
                                          if as_datetime(exported_dates[row - 2]["激活日期"]) else ""),
                    "system_cutoff": (as_datetime(exported_dates[row - 2]["截止日"]).isoformat()
                                      if as_datetime(exported_dates[row - 2]["截止日"]) else ""),
                    "google_activation": chatr_card["activation_dt"].isoformat() if chatr_card["activation_dt"] else "",
                    "google_cutoff": chatr_card["expiry_dt"].isoformat() if chatr_card["expiry_dt"] else "",
                    "decision": ICCID_CONFLICT_DECISION,
                }
                # Never borrow dates from another card or rewrite system identity.
                # Remaining blank dates retain the existing final fallback rules.
                chatr_card = None
        if kooda_row:
            kooda_rows += 1
        card = assoc.get(norm_slot)
        if GOOGLE_ENABLED and not card:
            if norm_slot:
                unmatched.setdefault(norm_slot, raw_slot)

        if card:
            google_matched_rows += 1
            if not kooda_row:
                matched_rows += 1
        open_value = card["open_date"] if card and not kooda_row else None
        carrier_price = safe_source_value(card["carrier_price"]) if card else None
        open_dt = as_datetime(open_value)
        if kooda_row:
            system_key = (norm_slot, phone_key(phone_value))
            if system_key in matched_system_keys and not system_kooda.get('projection'):
                raise RuntimeError("主表Kooda卡槽与号码复合键重复")
            system_row = resolve_system_kooda_row(system_kooda, slot=raw_slot, phone=phone_value,
                status=ws.cell(row, status_col).value, created=ws.cell(row, created_col).value,
                iccid=ws.cell(row, iccid_col).value, has_iccid=source_layout['existing_iccid'], source_row=row)
            if system_row.get('match_rule') == 'archived-phone-machine-created-iccid-exact':
                if system_row['backend_id'] in matched_archived_backend_ids:
                    raise RuntimeError('多个历史源行对应同一后台ID，禁止推测历史卡槽')
                matched_archived_backend_ids.add(system_row['backend_id'])
            matched_system_keys.add(system_key)
            matched_rows += 1
            if source_layout["existing_iccid"]:
                verify_export_system_iccid(ws.cell(row, iccid_col).value, system_row["iccid"])
                export_system_iccid_compared_rows += 1
            for name, expected in (
                ("激活日期", system_row["activation_dt"]),
                ("截止日", system_row["expiry_dt"]),
            ):
                if name in raw_header_index:
                    original_date = exported_dates[row - 2][name]
                    actual = as_datetime(original_date)
                    if (actual.date() if actual else None) != (expected.date() if expected else None) or (original_date not in (None, "") and actual is None):
                        export_system_date_conflicts[name] += 1
                        export_system_date_conflict_details.append({
                            "row": row, "phone": text_value(phone_value), "slot": text_value(raw_slot), "field": name,
                            "export_date": actual.date().isoformat() if actual else text_value(original_date),
                            "inventory_date": expected.date().isoformat() if expected else None,
                            "decision": "valid_export_first_else_frozen_cache",
                        })
            ws.cell(row, iccid_col).value = system_row["iccid"]
            if system_row["iccid"] is not None:
                ws.cell(row, iccid_col).data_type = "s"
            ws.cell(row, iccid_col).number_format = "@"
            cache_row = report_cache["index"].get(normalize_iccid(system_row["iccid"]), {})
            resolved = {}
            for name, key in (("激活日期", "activation_dt"), ("截止日", "expiry_dt")):
                resolved[name] = resolve_report_date(
                    exported=exported_dates[row - 2][name], export_column_exists=name in raw_header_index,
                    inventory=system_row[key], cached=cache_row.get(key), google=None, kooda=True,
                )
            system_activation = (as_datetime(exported_dates[row - 2]["激活日期"])
                                 if "激活日期" in raw_header_index else system_row["activation_dt"])
            kooda_system_blank_activation_rows += int(system_activation is None)
            local_cache_filled_rows += int(any(source == "local_cache" for value, source in resolved.values()))
            activation_dt = activation_value = resolved["激活日期"][0]
            expiry_dt = expiry_value = resolved["截止日"][0]
            kooda_task_f_match_methods[system_row.get('match_rule', system_kooda["match_rule"])] += 1
            if activation_dt:
                kooda_task_f_date_rows += 1
            else:
                kooda_task_f_missing_rows += 1
        else:
            resolved = {
                name: resolve_report_date(
                    exported=exported_dates[row - 2][name], export_column_exists=name in raw_header_index,
                    inventory=None, cached=None, google=card[key] if card else None, kooda=False,
                    chatr_google=chatr_card.get("activation_dt" if name == "激活日期" else "expiry_dt") if chatr_card else None,
                    strict_export=bool(chatr_dates and chatr_row),
                )
                for name, key in (("激活日期", "activation_date"), ("截止日", "expiry_date"))
            }
            activation_dt = activation_value = resolved["激活日期"][0]
            expiry_dt = expiry_value = resolved["截止日"][0]
            if not GOOGLE_ENABLED or (not card and (activation_dt or expiry_dt)):
                matched_rows += 1
        resolved = apply_creation_date_fallback(
            resolved, ws.cell(row, created_col).value, exported_dates[row - 2])
        if kooda_row and activation_dt is None and resolved["激活日期"][0] is not None:
            kooda_task_f_missing_rows -= 1
            kooda_task_f_date_rows += 1
        activation_dt = activation_value = resolved["激活日期"][0]
        expiry_dt = expiry_value = resolved["截止日"][0]
        for name, (_value, source) in resolved.items():
            date_field_counts[name][source] = date_field_counts[name].get(source, 0) + 1
        if iccid_conflict is not None:
            iccid_conflict.update(final_activation=activation_dt.isoformat() if activation_dt else "",
                                  final_cutoff=expiry_dt.isoformat() if expiry_dt else "",
                                  activation_source=resolved["激活日期"][1], cutoff_source=resolved["截止日"][1])
            chatr_iccid_conflicts.append(iccid_conflict)
        if resolved["激活日期"][1] == "creation_date_fallback":
            creation_date_fallback_rows.append({"row": row, "phone": text_value(phone_value),
                "activation": activation_dt.isoformat(), "cutoff": expiry_dt.isoformat()})
        chatr_filled_rows += int(any(source == "google_chatr" for _value, source in resolved.values()))
        if chatr_row:
            chatr_missing_activation_rows += int(activation_dt is None)
            chatr_missing_cutoff_rows += int(expiry_dt is None)
        if kooda_row and (activation_dt is None or expiry_dt is None):
            missing_date_scopes.append({
                "row": row, "phone": text_value(phone_value), "slot": text_value(raw_slot),
                "iccid": system_row["iccid"], "queryIccid": normalize_iccid(system_row["iccid"]),
                "activation_missing": activation_dt is None, "cutoff_missing": expiry_dt is None,
            })
        ws.cell(row, 11).value = safe_date_value(open_value, open_dt)
        ws.cell(row, 12).value = safe_date_value(activation_value, activation_dt)
        ws.cell(row, 13).value = safe_date_value(expiry_value, expiry_dt)
        ws.cell(row, 15).value = carrier_price
        warning = carrier_opr_warning(carrier_price, opr_value) if card else ""
        ws.cell(row, 16).value = warning or None
        if warning:
            carrier_opr_mismatch_rows += 1
            ws.cell(row, 16).fill = PatternFill(fill_type="solid", fgColor="F4CCCC")
            carrier_opr_mismatches.append([
                ws.cell(row, phone_col).value,
                ws.cell(row, carrier_col).value if carrier_col else None,
                opr_value,
                raw_slot,
                norm_slot,
                carrier_price,
                warning,
                card["source_sheet"],
                card["source_row"],
            ])
        if open_dt:
            ws.cell(row, 11).number_format = "yyyy-mm-dd"
        if activation_dt:
            ws.cell(row, 12).number_format = "yyyy-mm-dd"
            activation_age_days = (expiry_ref_date - activation_dt.date()).days
            if activation_age_days >= 0:
                ws.cell(row, 6).value = activation_age_days
                ws.cell(row, 6).number_format = "0"
                activation_age_known_rows += 1
            else:
                activation_age_future_rows += 1
        else:
            activation_age_unknown_rows += 1
        if expiry_dt:
            ws.cell(row, 13).number_format = "yyyy-mm-dd"

        activation_reference_dt = activation_dt or open_dt
        activation_reference_source = "激活日期" if activation_dt else "上卡日期"
        status = str(ws.cell(row, status_col).value or "").strip()
        if activation_reference_dt and status != "上架":
            machine = norm_slot.split("_", 1)[0] if "_" in norm_slot else "未识别"
            activated_record = [
                machine,
                room,
                raw_slot,
                norm_slot,
                ws.cell(row, phone_col).value,
                ws.cell(row, carrier_col).value if carrier_col else None,
                status or "空白",
                activation_reference_source,
                activation_reference_dt,
                open_dt or safe_source_value(open_value),
                expiry_dt or safe_source_value(expiry_value),
                resolved["激活日期"][1] if activation_dt else (card["source_sheet"] if card else ""),
                card["source_row"] if card and not kooda_row and resolved["激活日期"][1] == "google" else None,
            ]
            activated_unlisted_rows.append(activated_record)
            activated_unlisted_by_machine[machine].append(activated_record)

        created = as_datetime(ws.cell(row, created_col).value)
        expiry = expiry_dt.date() if expiry_dt else None
        if expiry:
            expiry_status = classify_expiry_status(expiry, expiry_ref_date)
            ws.cell(row, 3).value = expiry_status or None
            if expiry_status == "已过期":
                expired_rows += 1
            elif expiry_status == "快过期":
                fast_expire_rows += 1
            elif expiry_status == "6天":
                six_day_rows += 1

        change_status, change_reference_source = classify_number_change(
            kooda_row=kooda_row, activation_dt=activation_dt, expiry_dt=expiry_dt,
            open_dt=open_dt, created=created,
        )
        ws.cell(row, 4).value = change_status or None
        if change_status == "未换号":
            kooda_missing_dates_new_card_rows += 1
        if change_reference_source == "激活日期":
            change_checked_by_activation_rows += 1
        elif change_reference_source == "上卡日期":
            change_checked_by_open_rows += 1

        if change_status == "已换号":
            changed_rows += 1
            if change_reference_source == "激活日期":
                changed_by_activation_rows += 1
            else:
                changed_by_open_rows += 1
            if yellow_fill_id is None:
                yellow_fill_id = wb._fills.add(yellow)
            fill_row_with_registered_fill(ws, row, main_max_column, yellow_fill_id)

        # The frozen cache can remove a prior missing-date red mark. New F results
        # produced after this document cannot enter this immutable input snapshot.
        if kooda_row and not activation_dt:
            fill_task_f_missing_activation_row(ws, row, main_max_column)
            kooda_task_f_missing_red_rows += 1

    # Compare every Google card-slot row against this run's fresh system export.
    # The first output contains every Google-only row. The second is the subset
    # with a valid activation date. Neither list is inferred from device capacity.
    google_all_missing_rows, google_activated_missing_rows = classify_google_system_missing_rows(
        google_rows,
        main_slots,
    )
    google_activated_missing_field_counts = {
        "ICCID": sum(cell_has_value(record[0]["iccid"]) for record in google_activated_missing_rows),
        "手机号": sum(cell_has_value(record[0]["phone"]) for record in google_activated_missing_rows),
        "上卡日期": sum(cell_has_value(record[0]["open_date"]) for record in google_activated_missing_rows),
        "电信商": sum(cell_has_value(record[0]["carrier_price"]) for record in google_activated_missing_rows),
    }
    unused_port_rows, unused_port_devices = build_unused_port_rows(
        system_port_rows,
        (record["raw_slot"] for record in google_rows),
        (record["slot"] for record in difference_evidence["rows"]) if difference_evidence else None,
    )
    unused_port_rows = attach_port_iccids(unused_port_rows,
        difference_evidence["rows"] if difference_evidence else google_rows, raw_system_iccids)
    system_missing_port_count = sum(record[6] == "系统中无" for record in unused_port_rows)
    registration_failed_port_count = sum(record[6] == "注册失败" for record in unused_port_rows)
    google_present_port_count = sum(record[7] == PRESENT for record in unused_port_rows)

    def machine_sort_key(item: tuple[str, list[list]]):
        machine = item[0]
        return (0, int(machine)) if machine.isdigit() else (1, machine)

    activated_unlisted_machine_rows = []
    over_five_unlisted_machines = []
    for machine, records in sorted(activated_unlisted_by_machine.items(), key=machine_sort_key):
        slots = list(OrderedDict.fromkeys(str(record[2] or "") for record in records))
        phones = list(OrderedDict.fromkeys(str(record[4] or "") for record in records))
        room = records[0][1] if records else "未配置"
        summary_record = [
            machine,
            room,
            len(records),
            "是" if len(records) > 5 else "否",
            "、".join(slots),
            "、".join(phones),
        ]
        activated_unlisted_machine_rows.append(summary_record)
        if len(records) > 5:
            over_five_unlisted_machines.append(summary_record)

    # Add compact audit sheets so the final workbook carries the requested summary and unmatched slots.
    if "处理汇总" in wb.sheetnames:
        del wb["处理汇总"]
    summary_ws = wb.create_sheet("处理汇总")
    summary_items = room_summary_items() + [
        ("过期判断基准", f"{expiry_ref_source} vs 截止日"),
        ("过期判断日期", expiry_ref_display),
        ("已过期规则", "后台服务器日期 >= 截止日"),
        ("快过期规则", "截止日 - 后台服务器日期为1到2天（含2天）"),
        ("6天规则", "截止日 - 后台服务器日期为3到6天"),
        ("3日发送窗口数口径", "最近连续3日发送窗口数合计"),
        ("3日回复率口径", "最近连续3日回复窗口数合计/发送窗口数合计"),
        ("4日发送窗口数口径", "最近连续4日发送窗口数合计"),
        ("4日回复率口径", "最近连续4日回复窗口数合计/发送窗口数合计"),
        ("回复率持续降低口径", "最近每日回复窗口数/发送窗口数严格逐日降低，优先显示最长4天，其次3天、2天"),
        ("卡槽归一化规则", "204_62 = 204_62.01；115_01 = 115_1 = 115_1.01"),
        ("ICCID来源", SYSTEM_KOODA_FIELD_MAPPING["ICCID"]),
        ("开卡日期来源", SYSTEM_KOODA_FIELD_MAPPING["开卡日期"]),
        ("激活日期来源", SYSTEM_KOODA_FIELD_MAPPING["激活日期"]),
        ("激活后天数口径", "后台服务器日期 - 对应权威激活日期；同日为0天"),
        ("激活后天数可计算行数", activation_age_known_rows),
        ("激活日期缺失行数", activation_age_unknown_rows),
        ("激活日期晚于服务器日期行数", activation_age_future_rows),
        ("截止日来源", SYSTEM_KOODA_FIELD_MAPPING["截止日"]),
        ("本轮系统Kooda快照", system_kooda["path"]),
        ("本轮系统Kooda快照SHA256", system_kooda["sha256"]),
        ("本轮系统Kooda原始记录数", system_kooda["total_rows"]),
        ("本轮系统Kooda缺完整匹配键库存行数", system_kooda["unkeyed_rows"]),
        ("Kooda/Telus1主表行数", kooda_rows),
        ("Kooda/Telus1使用系统激活日期行数", kooda_task_f_date_rows),
        ("Kooda/Telus1系统激活日期缺失行数", kooda_task_f_missing_rows),
        ("Kooda/Telus1系统激活日期缺失整行标红数", kooda_task_f_missing_red_rows),
        ("Kooda/Telus1双日期缺失按未换号新卡行数", kooda_missing_dates_new_card_rows),
        ("系统日期更新生效时点", "下次新鲜系统快照取得日期后重新计算并取消缺失激活日期红色；不读取任务F或Google的Kooda日期"),
        ("换号判断日期优先级", "Kooda双日期缺失优先按未换号新卡；Kooda仅使用系统激活日期；其它OPR激活日期优先于Google上卡日期"),
        ("换号规则", "Kooda激活日期和截止日均空时显示未换号；其它情况主表创建时间与优先判断日期相差超过3天显示已换号"),
        ("電信商/價格与OPR不一致行数", carrier_opr_mismatch_rows),
        ("主表行数", main_rows),
        ("匹配行数", matched_rows),
        ("匹配行数口径", "Kooda/Telus1按本轮系统卡槽与号码精确匹配；其它OPR按Google卡槽关联"),
        ("Google卡槽关联匹配行数", google_matched_rows),
        ("未匹配卡槽数", len(unmatched)),
        ("已换号行数", changed_rows),
        ("已换号-按激活日期", changed_by_activation_rows),
        ("已换号-按上卡日期", changed_by_open_rows),
        ("换号检查-使用激活日期", change_checked_by_activation_rows),
        ("换号检查-使用上卡日期", change_checked_by_open_rows),
        ("已过期行数", expired_rows),
        ("快过期行数", fast_expire_rows),
        ("6天行数", six_day_rows),
        ("主表来源", str(MAIN_PATH)),
        ("关联表来源", str(ASSOC_PATH)),
        ("关联表页签", "、".join(assoc_sheets)),
        ("关联表页签行数", "、".join(f"{name}:{assoc_sheet_row_counts[name]}" for name in assoc_sheets)),
        ("关联表卡槽数", len(assoc)),
        ("谷歌有-系统无行数", len(google_all_missing_rows)),
        ("谷歌文档中有激活日期-系统无法使用行数", len(google_activated_missing_rows)),
        ("其中ICCID非空行数", google_activated_missing_field_counts["ICCID"]),
        ("其中手机号非空行数", google_activated_missing_field_counts["手机号"]),
        ("其中上卡日期非空行数", google_activated_missing_field_counts["上卡日期"]),
        ("其中电信商非空行数", google_activated_missing_field_counts["电信商"]),
        ("系统无-谷歌表有设备数", len(unused_port_devices)),
        ("系统无-谷歌表有总数", len(unused_port_rows)),
        ("系统中无端口数", system_missing_port_count),
        ("注册失败端口数", registration_failed_port_count),
        ("系统无-谷歌表有标记数", google_present_port_count),
        ("已激活未上架总数", len(activated_unlisted_rows)),
        ("超过5张未上架机器数", len(over_five_unlisted_machines)),
        (
            "超过5张未上架机器",
            "；".join(
                f"{record[0]}号机({record[2]}张)：卡槽{record[4]}；号码{record[5]}"
                for record in over_five_unlisted_machines
            ) or "无",
        ),
        *((room + "行数", room_counts[room]) for room in ROOM_LABELS),
        ("未配置机房行数", room_counts["未配置"]),
    ]
    if not GOOGLE_ENABLED:
        google_summary_labels = {
            "電信商/價格与OPR不一致行数", "Google卡槽关联匹配行数", "未匹配卡槽数",
            "关联表来源", "关联表页签", "关联表页签行数", "关联表卡槽数",
            "谷歌有-系统无行数", "谷歌文档中有激活日期-系统无法使用行数",
            "其中ICCID非空行数", "其中手机号非空行数", "其中上卡日期非空行数", "其中电信商非空行数",
        }
        replacements = {
            "匹配行数口径": "全部来自本轮系统主表；Kooda另按系统库存卡槽与号码精确核验",
            "系统日期更新生效时点": "本轮系统优先、缺失Kooda字段读取冻结本地库；新F结果下轮生效",
            "换号判断日期优先级": "Kooda双日期缺失按未换号新卡；其余只使用已有激活日期，缺失保持空白",
        }
        summary_items = [(key, replacements.get(key, value)) for key, value in summary_items if key not in google_summary_labels]
        summary_items.extend([
            ("Google关联依赖", "完整关联已禁用；仅Chatr系统日期空白字段读取本轮Google日期" if chatr_dates else "已禁用（用户要求）；不下载、不读取、不做关联核对"),
            ("系统无-谷歌表有设备覆盖范围", "仅本轮系统源主表中的有效数字设备；每台1至64端口；无法覆盖仅Google存在的设备"),
        ])
    for r, (k, v) in enumerate(summary_items, start=1):
        summary_ws.cell(r, 1).value = k
        summary_ws.cell(r, 2).value = v
    summary_ws.column_dimensions["A"].width = 38
    summary_ws.column_dimensions["B"].width = 90

    google_activated_missing_highlighted_rows = 0
    if GOOGLE_ENABLED:
        if "未匹配卡槽" in wb.sheetnames:
            del wb["未匹配卡槽"]
        unmatched_ws = wb.create_sheet("未匹配卡槽")
        unmatched_ws.append(["归一化卡槽", "主表原始卡槽"])
        for norm_slot, raw_slot in unmatched.items():
            unmatched_ws.append([norm_slot, raw_slot])
        unmatched_ws.column_dimensions["A"].width = 20
        unmatched_ws.column_dimensions["B"].width = 24

        if "电信商_OPR预警" in wb.sheetnames:
            del wb["电信商_OPR预警"]
        carrier_warning_ws = wb.create_sheet("电信商_OPR预警")
        carrier_warning_ws.append([
            "电话",
            "运营商",
            "opr",
            "卡槽",
            "归一化卡槽",
            "Google電信商/價格",
            "预警",
            "Google页签",
            "Google行号",
        ])
        for record in carrier_opr_mismatches:
            carrier_warning_ws.append(record)
        carrier_warning_ws.freeze_panes = "A2"
        carrier_warning_ws.auto_filter.ref = carrier_warning_ws.dimensions
        for cell in carrier_warning_ws[1]:
            cell.font = Font(bold=True)
        for row in range(2, carrier_warning_ws.max_row + 1):
            carrier_warning_ws.cell(row, 7).fill = PatternFill(fill_type="solid", fgColor="F4CCCC")
        for column_letter, width in (
            ("A", 18), ("B", 18), ("C", 30), ("D", 18), ("E", 18),
            ("F", 24), ("G", 60), ("H", 18), ("I", 12),
        ):
            carrier_warning_ws.column_dimensions[column_letter].width = width

        for obsolete_sheet in ("设备缺失端口", "所有谷歌有-系统没有", "谷歌有-系统无"):
            if obsolete_sheet in wb.sheetnames:
                del wb[obsolete_sheet]
        google_all_missing_ws = wb.create_sheet("谷歌有-系统无")
        google_all_missing_ws.append([
            "Google页签",
            "Google行号",
            "卡槽",
            "归一化卡槽",
            "备注",
            "ICCID",
            "上卡日期",
            "电信商",
        ])
        for record, norm_slot, open_dt, _activation_dt, _expiry_dt in google_all_missing_rows:
            google_all_missing_ws.append([
                record["source_sheet"],
                record["source_row"],
                safe_source_value(record["raw_slot"]),
                norm_slot,
                safe_source_value(record["note"]),
                text_value(safe_source_value(record["iccid"])),
                safe_date_value(record["open_date"], open_dt),
                safe_source_value(record["carrier_price"]),
            ])
        google_activated_missing_highlighted_rows = highlight_activated_google_missing_rows(
            google_all_missing_ws,
            google_all_missing_rows,
        )
        for cell in google_all_missing_ws[1]:
            cell.font = Font(bold=True)
        google_all_missing_ws.freeze_panes = "A2"
        google_all_missing_ws.auto_filter.ref = google_all_missing_ws.dimensions
        for row in range(2, google_all_missing_ws.max_row + 1):
            google_all_missing_ws.cell(row, 5).alignment = Alignment(wrap_text=True, vertical="top")
            google_all_missing_ws.cell(row, 6).number_format = "@"
            if isinstance(google_all_missing_ws.cell(row, 7).value, (datetime, date)):
                google_all_missing_ws.cell(row, 7).number_format = "yyyy-mm-dd"
        for column_letter, width in (
            ("A", 16), ("B", 14), ("C", 18), ("D", 18),
            ("E", 52), ("F", 24), ("G", 16), ("H", 22),
        ):
            google_all_missing_ws.column_dimensions[column_letter].width = width

    for obsolete_sheet in ("未使用端口", "系统无-谷歌表有"):
        if obsolete_sheet in wb.sheetnames:
            del wb[obsolete_sheet]
    unused_port_ws = wb.create_sheet("系统无-谷歌表有")
    unused_port_ws.append(PORT_HEADERS)
    for record in unused_port_rows:
        unused_port_ws.append(record)
        for cell in unused_port_ws[unused_port_ws.max_row][8:10]:
            cell.data_type = "s"
            cell.number_format = "@"
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    for cell in unused_port_ws[1]:
        cell.font = Font(bold=True)
    unused_port_ws.freeze_panes = "A2"
    unused_port_ws.auto_filter.ref = unused_port_ws.dimensions
    missing_fill = PatternFill(fill_type="solid", fgColor="FFF2CC")
    failed_fill = PatternFill(fill_type="solid", fgColor="F4CCCC")
    conflict_fill = PatternFill(fill_type="solid", fgColor="FFFF00")
    for row in range(2, unused_port_ws.max_row + 1):
        fill = missing_fill if unused_port_ws.cell(row, 7).value == "系统中无" else failed_fill
        if unused_port_ws.cell(row, 9).value != unused_port_ws.cell(row, 10).value:
            fill = conflict_fill
        for col in range(1, len(PORT_HEADERS) + 1):
            unused_port_ws.cell(row, col).fill = fill
    for column_letter, width in (
        ("A", 10), ("B", 14), ("C", 10), ("D", 18),
        ("E", 24), ("F", 24), ("G", 14), ("H", 20), ("I", 28), ("J", 28),
    ):
        unused_port_ws.column_dimensions[column_letter].width = width

    if GOOGLE_ENABLED:
        for obsolete_sheet in (
            "Google有日期_主表缺失",
            "谷歌文档中有激活日期-系统没有",
            "谷歌文档中有激活日期-系统无法使用",
        ):
            if obsolete_sheet in wb.sheetnames:
                del wb[obsolete_sheet]
        missing_google_ws = wb.create_sheet("谷歌文档中有激活日期-系统无法使用")
        missing_google_ws.append(GOOGLE_ACTIVATED_MISSING_HEADERS)
        for item in google_activated_missing_rows:
            missing_google_ws.append(google_activated_missing_output_row(item))

        for cell in missing_google_ws[1]:
            cell.font = Font(bold=True)
        missing_google_ws.freeze_panes = "A2"
        missing_google_ws.auto_filter.ref = missing_google_ws.dimensions
        for row in range(2, missing_google_ws.max_row + 1):
            missing_google_ws.cell(row, 5).alignment = Alignment(wrap_text=True, vertical="top")
            missing_google_ws.cell(row, 6).number_format = "@"
            missing_google_ws.cell(row, 7).number_format = "@"
            for col in (8, 10, 11):
                if isinstance(missing_google_ws.cell(row, col).value, (datetime, date)):
                    missing_google_ws.cell(row, col).number_format = "yyyy-mm-dd"
        for col in range(1, missing_google_ws.max_column + 1):
            header = str(missing_google_ws.cell(1, col).value or "")
            column_letter = missing_google_ws.cell(1, col).column_letter
            missing_google_ws.column_dimensions[column_letter].width = min(max(len(header) + 4, 14), 28)

    if "已激活未上架" in wb.sheetnames:
        del wb["已激活未上架"]
    activated_unlisted_ws = wb.create_sheet("已激活未上架")
    activated_unlisted_ws.append([
        "机器号",
        "机房",
        "卡槽",
        "归一化卡槽",
        "电话",
        "运营商",
        "状态",
        "激活依据",
        "激活时间/日期",
        "开卡日期",
        "截止日",
        "Google页签",
        "Google行号",
    ])
    for record in activated_unlisted_rows:
        activated_unlisted_ws.append(record)
    activated_unlisted_ws.freeze_panes = "A2"
    activated_unlisted_ws.auto_filter.ref = activated_unlisted_ws.dimensions
    for cell in activated_unlisted_ws[1]:
        cell.font = Font(bold=True)
    for row in range(2, activated_unlisted_ws.max_row + 1):
        for col in (9, 10, 11):
            if isinstance(activated_unlisted_ws.cell(row, col).value, (datetime, date)):
                activated_unlisted_ws.cell(row, col).number_format = "yyyy-mm-dd"
    for column_letter, width in (
        ("A", 10), ("B", 14), ("C", 16), ("D", 18), ("E", 18),
        ("F", 20), ("G", 14), ("H", 20), ("I", 18), ("J", 14),
        ("K", 14), ("L", 20), ("M", 12),
    ):
        activated_unlisted_ws.column_dimensions[column_letter].width = width

    if "未上架机器汇总" in wb.sheetnames:
        del wb["未上架机器汇总"]
    unlisted_machine_ws = wb.create_sheet("未上架机器汇总")
    unlisted_machine_ws.append([
        "机器号",
        "机房",
        "已激活未上架数",
        "是否超过5张",
        "卡槽",
        "号码",
    ])
    for record in activated_unlisted_machine_rows:
        unlisted_machine_ws.append(record)
    unlisted_machine_ws.freeze_panes = "A2"
    unlisted_machine_ws.auto_filter.ref = unlisted_machine_ws.dimensions
    for cell in unlisted_machine_ws[1]:
        cell.font = Font(bold=True)
    for column_letter, width in (
        ("A", 10), ("B", 14), ("C", 18), ("D", 16), ("E", 60), ("F", 60),
    ):
        unlisted_machine_ws.column_dimensions[column_letter].width = width

    enrichment_receipt = apply_fast_enrichment(
        wb,
        ws,
        enrichment,
        phone_col=phone_col,
        ten_day_rate_col=ten_day_rate_col,
        summary_ws=summary_ws,
    )
    if not GOOGLE_ENABLED:
        # Delete only after all positional calculations and formatting complete.
        # Never publish empty Google comparisons as successful checks.
        for title in GOOGLE_ONLY_SHEETS:
            if title in wb.sheetnames:
                del wb[title]
        removed_columns = sorted(headers.index(name) + 1 for name in GOOGLE_ONLY_HEADERS)
        original_dimensions = [(column_index_from_string(key), copy.copy(value)) for key, value in ws.column_dimensions.items()]
        for column in reversed(removed_columns):
            ws.delete_cols(column)
        # openpyxl moves cells but leaves explicit widths at their old letters.
        ws.column_dimensions.clear()
        for old_column, dimension in original_dimensions:
            if old_column in removed_columns:
                continue
            new_column = old_column - sum(column < old_column for column in removed_columns)
            dimension.index = get_column_letter(new_column)
            dimension.min = new_column
            dimension.max = max(new_column, (dimension.max or old_column) - sum(column <= (dimension.max or old_column) for column in removed_columns))
            ws.column_dimensions[dimension.index] = dimension
        if ws.auto_filter.ref:
            ws.auto_filter.ref = ws.dimensions
        activated_unlisted_ws.cell(1, 12).value = "日期来源"
        activated_unlisted_ws.delete_cols(13)
        activated_unlisted_ws.auto_filter.ref = activated_unlisted_ws.dimensions
    difference_receipt = write_google_difference(wb, difference_evidence, set(main_slots), as_datetime)
    if GOOGLE_ENABLED and "谷歌有-系统无" in wb.sheetnames:
        wb.move_sheet("谷歌有-系统无", wb.sheetnames.index("系统无-谷歌表有") - wb.sheetnames.index("谷歌有-系统无"))
    device_daily_receipt = write_device_daily_sheet(wb, ws, device_daily_result)
    if chatr_dates:
        write_conflict_sheet(wb, chatr_iccid_conflicts)
        summary_ws.append(("系统/Google ICCID冲突数", len(chatr_iccid_conflicts)))
        summary_ws.append(("ICCID冲突处理", ICCID_CONFLICT_DECISION))
    summary_ws.append(("设备每日统计", f"{device_daily_receipt['device_count']}台设备 / {device_daily_receipt['date_count']}个日期；详见设备每日统计工作表"))
    if system_kooda.get('projection'):
        summary_ws.append(('Kooda来源范围', '仅本轮新鲜10日表源行；非全平台库存'))
        summary_ws.append(('F联网补查规则', '本轮10日原始表和本地F库均无有效激活日期的ICCID才补查；仅缺截止日不补查'))
    summary_ws.append(("设备每日回复率口径", device_daily_receipt["rate_rule"]))
    summary_ws.append(("设备每日计数缺失口径", device_daily_receipt["missing_rule"]))
    summary_ws.append(("设备每日统计并发", f"日期线程{device_daily_receipt['actual_workers']}；工作表统一单线程写入"))
    summary_ws.append(("未识别设备记录数", device_daily_receipt["unrecognized_device_rows"]))
    retention_receipt = add_retention_report(wb, ws, retention_evidence)
    usage_receipt = add_usage_days(ws, expiry_ref_date)
    summary_ws.append(("使用天数口径", usage_receipt['rule']))
    summary_ws.append(("使用天数可计算行数", usage_receipt['known_rows']))
    summary_ws.append(("使用天数创建时间缺失或非法行数", usage_receipt['unknown_rows']))
    summary_ws.append(("使用天数创建时间晚于服务器日期行数", usage_receipt['future_rows']))
    machine_receipt = add_machine_number(ws)
    ko_unactivated_receipt = write_ko_unactivated(wb, ko_unactivated_evidence)
    transform_seconds = time.perf_counter() - transform_started

    requested_output = os.environ.get("OPENSMS_OUTPUT_PATH", "").strip()
    if requested_output:
        out_path = Path(requested_output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            raise RuntimeError(f"拒绝覆盖历史结果表: {out_path}")
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = OUTPUT_DIR / f"export4day_卡槽关联结果_{stamp}.xlsx"
        sequence = 1
        while out_path.exists():
            out_path = OUTPUT_DIR / f"export4day_卡槽关联结果_{stamp}_{sequence:02d}.xlsx"
            sequence += 1
    save_started = time.perf_counter()
    wb.save(out_path)
    save_seconds = time.perf_counter() - save_started
    total_seconds = time.perf_counter() - process_started
    enrichment_receipt["output_path"] = str(out_path)

    summary = {
        "output_path": str(out_path),
        "device_daily_statistics": device_daily_receipt,
        "card_retention_report": retention_receipt,
        "google_system_missing_sheet": difference_receipt,
        "ko_unactivated_sheet": ko_unactivated_receipt,
        "machine_number": machine_receipt,
        "creation_usage_days": usage_receipt,
        "expiry_status_reference": f"{expiry_ref_source} vs 截止日",
        "expiry_reference_date": expiry_ref_display,
        "slot_alias_204_62_matches": normalize_slot("204_62") == normalize_slot("204_62.01") == "204_62",
        "google_source_has_204_62_alias": any(
            normalize_slot(record["raw_slot"]) == "204_62" for record in google_rows
        ),
        "google_field_mapping": {
            **SYSTEM_KOODA_FIELD_MAPPING,
            "carrier_compare": "電信商/價格 vs opr",
        },
        "expiry_status_rules": {
            "expired": "后台服务器日期 >= 截止日",
            "fast_expiry": "截止日 - 后台服务器日期为1到2天（含2天）",
            "six_days": "截止日 - 后台服务器日期为3到6天",
        },
        "main_rows": main_rows,
        "source_layout": source_layout,
        "source_system_comparison": {
            "iccid_compared_rows": export_system_iccid_compared_rows,
            "iccid_conflict_rows": 0,
            "activation_date_conflict_rows": export_system_date_conflicts["激活日期"],
            "cutoff_date_conflict_rows": export_system_date_conflicts["截止日"],
            "date_conflict_details": export_system_date_conflict_details,
            "date_priority": "system_export_then_legacy_inventory_then_frozen_cache_or_google" if GOOGLE_ENABLED else "system_export_then_legacy_inventory_then_frozen_cache",
        },
        "date_resolution": {
            "creation_date_fallback_rows": creation_date_fallback_rows,
            "source": "system_export_then_frozen_local_or_chatr_google" if chatr_dates else "system_export_then_frozen_local_or_google" if GOOGLE_ENABLED else "system_export_then_frozen_local", "field_counts": date_field_counts,
            "cache": {key: value for key, value in report_cache.items() if key != "index"},
            "source_export": {"path": str(MAIN_PATH.resolve()), "sha256": hashlib.sha256(MAIN_PATH.read_bytes()).hexdigest()},
            "association": {"path": str(ASSOC_PATH.resolve()), "sha256": hashlib.sha256(ASSOC_PATH.read_bytes()).hexdigest()} if GOOGLE_ENABLED and ASSOC_PATH.is_file() else None,
            "kooda_system_blank_activation_rows": kooda_system_blank_activation_rows,
            "kooda_local_cache_filled_rows": local_cache_filled_rows,
            "missing_activation_rows": kooda_task_f_missing_rows,
            "missing_cutoff_rows": sum(item["cutoff_missing"] for item in missing_date_scopes),
            "missing_either_date_rows": len(missing_date_scopes), "missing_date_scopes": missing_date_scopes,
            "input_parallel_workers": input_workers, "new_query_results_apply_next_export": True,
            "system_inventory_fallback_only_when_export_column_absent": True,
            "redundant_system_date_columns_removed": True,
        },
        "matched_rows": matched_rows,
        "matched_rows_rule": "kooda_system_composite_identity_and_other_opr_google_slot" if GOOGLE_ENABLED else "system_source_rows_with_kooda_composite_identity_verification",
        "google_matched_rows": google_matched_rows,
        "unmatched_slot_count": len(unmatched),
        "unmatched_slots": list(unmatched.keys()),
        "changed_rows": changed_rows,
        "carrier_opr_mismatch_rows": carrier_opr_mismatch_rows,
        "activation_age_rule": "后台服务器日期 - 对应权威激活日期；同日为0天",
        "system_kooda_source": {
            **{key: value for key, value in system_kooda.items()
               if key != 'index' and (not system_kooda.get('projection')
                   or key not in {'source_row_index', 'projection', 'phone_index', 'raw_rows'})},
            "matched_rows": kooda_rows if system_kooda.get('projection') else len(matched_system_keys), "missing_rows": 0,
            "archived_matched_rows": len(matched_archived_backend_ids),
            "missing_activation_rows": kooda_task_f_missing_rows,
            "missing_red_rows": kooda_task_f_missing_red_rows,
            "field_mapping": SYSTEM_KOODA_FIELD_MAPPING,
            "google_date_fallback_enabled": False, "task_f_date_fallback_enabled": True,
            "cutoff_rule": "system_export_direct_else_frozen_cache",
        },
        "task_f_kooda_date_source": {
            "source": "system_export_then_frozen_local", "database": report_cache["source_database"],
            "database_exists": report_cache["source_exists"], "database_found_count": report_cache["record_count"],
            "kooda_rows": kooda_rows,
            "matched_rows": kooda_task_f_date_rows,
            "missing_rows": kooda_task_f_missing_rows,
            "missing_red_rows": kooda_task_f_missing_red_rows,
            "missing_dates_new_card_rows": kooda_missing_dates_new_card_rows,
            "missing_dates_change_status": "未换号",
            "missing_fill_rgb": "FFC7CE",
            "post_document_supplemental_lookup": True,
            "supplemental_results_apply_next_export": True,
            "match_methods": dict(kooda_task_f_match_methods),
            "google_date_fallback_enabled": False,
            "task_f_date_fallback_enabled": True,
            "cutoff_rule": "system_export_direct_else_frozen_cache",
        },
        "activation_age_known_rows": activation_age_known_rows,
        "activation_age_unknown_rows": activation_age_unknown_rows,
        "activation_age_future_rows": activation_age_future_rows,
        "changed_by_activation_rows": changed_by_activation_rows,
        "changed_by_open_rows": changed_by_open_rows,
        "change_checked_by_activation_rows": change_checked_by_activation_rows,
        "change_checked_by_open_rows": change_checked_by_open_rows,
        "expired_rows": expired_rows,
        "fast_expire_rows": fast_expire_rows,
        "six_day_rows": six_day_rows,
        "assoc_sheets": assoc_sheets,
        "required_assoc_sheets": list(REQUIRED_ASSOC_SHEETS),
        "assoc_sheet_row_counts": dict(assoc_sheet_row_counts),
        "assoc_slot_count": len(assoc),
        "google_all_missing_from_main_rows": len(google_all_missing_rows),
        "google_activated_missing_from_main_rows": len(google_activated_missing_rows),
        "google_activated_missing_field_counts": google_activated_missing_field_counts,
        "google_activated_missing_highlighted_yellow_rows": google_activated_missing_highlighted_rows,
        "unused_port_sheet_name": "系统无-谷歌表有",
        "device_port_capacity": 64,
        "unused_port_device_scope": "fresh_system_and_google_numeric_device_union" if GOOGLE_ENABLED else "system_source_only",
        "unused_port_device_count": len(unused_port_devices),
        "unused_port_devices": unused_port_devices,
        "unused_port_count": len(unused_port_rows),
        "system_missing_port_count": system_missing_port_count,
        "registration_failed_port_count": registration_failed_port_count,
        "system_missing_google_present_count": google_present_port_count,
        "activated_unlisted_rows": len(activated_unlisted_rows),
        "activated_unlisted_machine_count": len(activated_unlisted_machine_rows),
        "over_five_unlisted_machine_count": len(over_five_unlisted_machines),
        "over_five_unlisted_machines": [
            {
                "machine": record[0],
                "room": record[1],
                "count": record[2],
                "slots": record[4].split("、") if record[4] else [],
                "phones": record[5].split("、") if record[5] else [],
            }
            for record in over_five_unlisted_machines
        ],
        "room_counts": dict(room_counts),
        "room_configuration": {"config": ROOM_CONFIG, "sha256": ROOM_CONFIG_SHA256},
        "enrichment_included": True,
        "enrichment": enrichment_receipt,
        "stage_timings_seconds": {
            "parallel_input_load_and_raw_analysis": round(input_seconds, 3),
            "transform_and_build_sheets": round(transform_seconds, 3),
            "save_workbook_once": round(save_seconds, 3),
            "total_single_pass_build": round(total_seconds, 3),
        },
    }
    summary["google_dependency"] = ({"enabled": True, "status": "enabled"} if GOOGLE_ENABLED else {
        "enabled": False, "status": "skipped", "reason": "user_disabled",
        "unused_port_device_scope": "system_source_only",
    })
    if chatr_dates:
        proof = {key: value for key, value in chatr_dates.items() if key != "index"}
        proof.update(field_counts={name: values["google_chatr"] for name, values in date_field_counts.items()},
                     matched_rows=chatr_matched_rows, filled_rows=chatr_filled_rows,
                     chatr_rows=chatr_rows, unmatched_rows=chatr_rows - chatr_matched_rows,
                     missing_activation_rows=chatr_missing_activation_rows,
                     missing_cutoff_rows=chatr_missing_cutoff_rows,
                     iccid_conflict_count=len(chatr_iccid_conflicts), iccid_conflicts=chatr_iccid_conflicts,
                     iccid_conflict_sheet=ICCID_CONFLICT_SHEET, iccid_conflict_policy="system_wins_skip_google_dates")
        summary["google_chatr_date_fallback"] = proof
        summary["date_resolution"]["chatr_google_dates"] = proof
        summary["source_system_comparison"]["date_priority"] = "system_export_then_kooda_frozen_cache_or_chatr_fresh_google"
        summary["system_kooda_source"]["field_mapping"] = {**SYSTEM_KOODA_FIELD_MAPPING,
            "激活日期": "系统导出优先；Kooda缺失读取冻结F库；仅Chatr缺失读取本轮Google激活日期",
            "截止日": "系统导出优先；Kooda缺失读取冻结F库；仅Chatr缺失读取本轮Google預計過期日"}
    if not GOOGLE_ENABLED:
        for key in ("google_source_has_204_62_alias", "google_field_mapping", "google_matched_rows",
                    "unmatched_slot_count", "unmatched_slots", "carrier_opr_mismatch_rows", "assoc_sheets",
                    "required_assoc_sheets", "assoc_sheet_row_counts", "assoc_slot_count",
                    "google_all_missing_from_main_rows", "google_activated_missing_from_main_rows",
                    "google_activated_missing_field_counts", "google_activated_missing_highlighted_yellow_rows"):
            summary.pop(key, None)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    process()
