#!/usr/bin/env python3
"""Fast, fail-closed carrier refresh for an existing OpenSMS XLSX workbook.

Only carrier cells in the first worksheet are replaced.  Backend-updated
phones absent from the current 10-day source are written to one supplemental
sheet in the same workbook.  Every other OOXML member is copied byte-for-byte,
and an independent second read verifies target values, style ids, sheet
structure, formula errors and non-target XML content.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
ERROR_VALUES = {"#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A", "#NUM!", "#NULL!", "#SPILL!", "#CALC!"}
CACHE_SCHEMA = "opensms-carrier-refresh-verified-v1"


class RefreshError(RuntimeError):
    pass


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def short_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) > 10 else digits


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_verified_cache(args: argparse.Namespace, workbook_hash: str) -> dict[str, Any] | None:
    cache_path = getattr(args, "verified_cache_input", None)
    expected_hash = getattr(args, "verified_cache_sha256", None)
    run_id = getattr(args, "run_id", None)
    if bool(cache_path) != bool(expected_hash):
        raise RefreshError("核验缓存输入与SHA256必须同时提供")
    if (cache_path or getattr(args, "verified_cache_output", None)) and not run_id:
        raise RefreshError("核验缓存必须绑定本轮唯一run-id")
    if not cache_path:
        return None
    raw = Path(cache_path).read_bytes()
    if sha256_bytes(raw) != expected_hash:
        raise RefreshError("核验缓存SHA256不一致，拒绝复用")
    cache = json.loads(raw)
    if (
        cache.get("schema") != CACHE_SCHEMA or cache.get("runId") != run_id
        or cache.get("workbookSha256") != workbook_hash
        or cache.get("passed") is not True or cache.get("formulaErrorCount") != 0
        or cache.get("zipIntegrityPassed") is not True
    ):
        raise RefreshError("核验缓存的本轮身份、工作簿SHA256或完整性证明不一致")
    return cache


def column_letter(index: int) -> str:
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        raise RefreshError(f"无效单元格引用: {reference}")
    value = 0
    for char in letters.group(0):
        value = value * 26 + ord(char) - 64
    return value - 1


def workbook_sheets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        item.attrib["Id"]: item.attrib["Target"]
        for item in relations.findall(f"{{{PKG_REL_NS}}}Relationship")
        if item.attrib.get("Id") and item.attrib.get("Target")
    }
    result: list[tuple[str, str]] = []
    for sheet in workbook.findall(f".//{{{MAIN_NS}}}sheet"):
        name = sheet.attrib.get("name", "")
        relation_id = sheet.attrib.get(f"{{{REL_NS}}}id", "")
        target = targets.get(relation_id, "").lstrip("/")
        if not target:
            raise RefreshError(f"工作表缺少OOXML关系: {name}")
        if not target.startswith("xl/"):
            target = "xl/" + target.removeprefix("./")
        result.append((name, target))
    if not result:
        raise RefreshError("工作簿没有工作表")
    return result


def shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    result = []
    for item in root.findall(f"{{{MAIN_NS}}}si"):
        result.append("".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t")))
    return result


def cell_value(cell: ET.Element, strings: list[str]) -> str:
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{MAIN_NS}}}t"))
    value = cell.find(f"{{{MAIN_NS}}}v")
    raw = "" if value is None or value.text is None else value.text
    if kind == "s" and raw:
        try:
            return strings[int(raw)]
        except (ValueError, IndexError) as exc:
            raise RefreshError(f"共享字符串索引无效: {raw}") from exc
    return raw


def parse_sheet_rows(
    xml: bytes, strings: list[str], *, error_sink: list[dict[str, str]] | None = None,
    sheet_path: str = "",
) -> tuple[dict[int, dict[int, tuple[str, str, str]]], str]:
    root = ET.fromstring(xml)
    dimension = root.find(f"{{{MAIN_NS}}}dimension")
    rows: dict[int, dict[int, tuple[str, str, str]]] = {}
    for row in root.findall(f".//{{{MAIN_NS}}}row"):
        row_number = int(row.attrib.get("r") or 0)
        cells: dict[int, tuple[str, str, str]] = {}
        for cell in row.findall(f"{{{MAIN_NS}}}c"):
            reference = cell.attrib.get("r", "")
            value = cell_value(cell, strings)
            if error_sink is not None and (cell.attrib.get("t") == "e" or text(value) in ERROR_VALUES):
                error_sink.append({"sheetPath": sheet_path, "cell": reference, "value": text(value)})
            if not reference:
                continue
            cells[column_index(reference)] = (
                reference,
                value,
                cell.attrib.get("s", "0"),
            )
        rows[row_number] = cells
    return rows, dimension.attrib.get("ref", "") if dimension is not None else ""


def cell_pattern(reference: str) -> re.Pattern[bytes]:
    escaped = re.escape(reference.encode("ascii"))
    return re.compile(
        rb"<(?P<prefix>[A-Za-z0-9_]+:)?c\b(?=[^>]*\br=\"" + escaped
        + rb"\")(?P<attrs>[^>]*)>(?P<body>.*?)</(?:[A-Za-z0-9_]+:)?c>",
        re.DOTALL,
    )


def self_closing_cell_pattern(reference: str) -> re.Pattern[bytes]:
    escaped = re.escape(reference.encode("ascii"))
    return re.compile(
        rb"<(?P<prefix>[A-Za-z0-9_]+:)?c\b(?=[^>]*\br=\"" + escaped + rb"\")(?P<attrs>[^>]*)/>"
    )


ANY_CELL_PATTERN = re.compile(
    rb"<(?P<prefix>[A-Za-z0-9_]+:)?c\b(?P<attrs>[^>]*\br=\"(?P<reference>[A-Z]+\d+)\"[^>]*)"
    rb"(?:(?P<empty>/>)|>(?P<body>.*?)</(?:[A-Za-z0-9_]+:)?c>)",
    re.DOTALL,
)


def style_from_attrs(attrs: bytes) -> str:
    match = re.search(rb"\bs=\"([^\"]+)\"", attrs)
    return match.group(1).decode("utf-8") if match else "0"


def replace_cell(xml: bytes, reference: str, value: str) -> tuple[bytes, str]:
    payload = html.escape(value, quote=False).encode("utf-8")
    inline_body = lambda prefix: (
        b"<" + prefix + b"is><" + prefix + b"t>" + payload
        + b"</" + prefix + b"t></" + prefix + b"is>"
    )
    pattern = cell_pattern(reference)
    match = pattern.search(xml)
    if match:
        attrs = re.sub(rb"\s+t=\"[^\"]*\"", b"", match.group("attrs"))
        prefix = match.group("prefix") or b""
        replacement = b"<" + prefix + b"c" + attrs + b' t="inlineStr">' + inline_body(prefix) + b"</" + prefix + b"c>"
        return xml[: match.start()] + replacement + xml[match.end() :], style_from_attrs(attrs)
    empty_pattern = self_closing_cell_pattern(reference)
    match = empty_pattern.search(xml)
    if match:
        attrs = re.sub(rb"\s+t=\"[^\"]*\"", b"", match.group("attrs"))
        prefix = match.group("prefix") or b""
        replacement = b"<" + prefix + b"c" + attrs + b' t="inlineStr">' + inline_body(prefix) + b"</" + prefix + b"c>"
        return xml[: match.start()] + replacement + xml[match.end() :], style_from_attrs(attrs)
    row_match = re.search(r"(\d+)$", reference)
    if not row_match:
        raise RefreshError(f"目标单元格不存在且行号无效: {reference}")
    row_number = row_match.group(1).encode("ascii")
    row_pattern = re.compile(
        rb"<(?P<prefix>[A-Za-z0-9_]+:)?row\b(?=[^>]*\br=\"" + row_number
        + rb"\")(?P<attrs>[^>]*)>(?P<body>.*?)</(?:[A-Za-z0-9_]+:)?row>",
        re.DOTALL,
    )
    row = row_pattern.search(xml)
    if not row:
        raise RefreshError(f"目标单元格和所在行均不存在: {reference}")
    prefix = row.group("prefix") or b""
    new_cell = b"<" + prefix + b'c r="' + reference.encode("ascii") + b'" t="inlineStr">' + inline_body(prefix) + b"</" + prefix + b"c>"
    target_column = column_index(reference)
    insert_at = len(row.group("body"))
    for existing in re.finditer(
        rb"<(?:[A-Za-z0-9_]+:)?c\b[^>]*\br=\"([A-Z]+\d+)\"[^>]*(?:/>|>.*?</(?:[A-Za-z0-9_]+:)?c>)",
        row.group("body"), re.DOTALL,
    ):
        if column_index(existing.group(1).decode("ascii")) > target_column:
            insert_at = existing.start()
            break
    new_body = row.group("body")[:insert_at] + new_cell + row.group("body")[insert_at:]
    replacement = b"<" + prefix + b"row" + row.group("attrs") + b">" + new_body + b"</" + prefix + b"row>"
    return xml[: row.start()] + replacement + xml[row.end() :], "0"


def normalize_allowed_cells(xml: bytes, references: list[str]) -> bytes:
    wanted = set(references)
    found: set[str] = set()

    def replace(match: re.Match[bytes]) -> bytes:
        reference = match.group("reference").decode("ascii")
        if reference not in wanted:
            return match.group(0)
        found.add(reference)
        return f'<c r="{reference}"/>'.encode("ascii")

    normalized = ANY_CELL_PATTERN.sub(replace, xml)
    missing = wanted - found
    if missing:
        raise RefreshError(f"独立复核无法定位目标单元格: {sorted(missing)[:10]}")
    return normalized


def replace_cells(xml: bytes, replacements: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    found: set[str] = set()
    styles: dict[str, str] = {}

    def replace(match: re.Match[bytes]) -> bytes:
        reference = match.group("reference").decode("ascii")
        value = replacements.get(reference)
        if value is None:
            return match.group(0)
        prefix = match.group("prefix") or b""
        attrs = re.sub(rb"\s+t=\"[^\"]*\"", b"", match.group("attrs"))
        payload = html.escape(value, quote=False).encode("utf-8")
        body = (
            b"<" + prefix + b"is><" + prefix + b"t>" + payload
            + b"</" + prefix + b"t></" + prefix + b"is>"
        )
        found.add(reference)
        styles[reference] = style_from_attrs(attrs)
        return b"<" + prefix + b"c" + attrs + b' t="inlineStr">' + body + b"</" + prefix + b"c>"

    updated = ANY_CELL_PATTERN.sub(replace, xml)
    for reference in sorted(set(replacements) - found, key=lambda item: (int(re.search(r"\d+$", item).group()), column_index(item))):
        updated, style = replace_cell(updated, reference, replacements[reference])
        styles[reference] = style
    return updated, styles


def formula_errors(archive: zipfile.ZipFile, sheet_paths: list[str], strings: list[str]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for sheet_path in sheet_paths:
        root = ET.fromstring(archive.read(sheet_path))
        for cell in root.findall(f".//{{{MAIN_NS}}}c"):
            value = text(cell_value(cell, strings))
            if cell.attrib.get("t") == "e" or value in ERROR_VALUES:
                errors.append({"sheetPath": sheet_path, "cell": cell.attrib.get("r", ""), "value": value})
    return errors


def inline_cell(reference: str, value: Any, style: str = "0") -> str:
    escaped = html.escape(text(value), quote=False)
    style_attr = f' s="{html.escape(style, quote=True)}"' if style != "0" else ""
    return f'<c r="{reference}"{style_attr} t="inlineStr"><is><t>{escaped}</t></is></c>'


def build_supplemental_sheet(rows: list[dict[str, Any]], header_style: str, data_style: str) -> bytes:
    headers = ["电话", "最新运营商", "预期运营商", "处理规则", "后台号码ID", "说明"]
    sheet_rows = []
    header_cells = "".join(
        inline_cell(f"{column_letter(index)}1", value, header_style)
        for index, value in enumerate(headers)
    )
    sheet_rows.append(f'<row r="1" ht="22" customHeight="1">{header_cells}</row>')
    for row_number, row in enumerate(rows, 2):
        values = [
            row["phone"], row.get("liveCarrierName", ""), row.get("expectedTarget", ""),
            row.get("route", ""), row.get("backendId", ""),
            "本轮后台已更新并回读，但号码不在本轮10日报表源表中",
        ]
        cells = "".join(
            inline_cell(f"{column_letter(index)}{row_number}", value, data_style)
            for index, value in enumerate(values)
        )
        sheet_rows.append(f'<row r="{row_number}">{cells}</row>')
    last_row = len(rows) + 1
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{MAIN_NS}">'
        f'<dimension ref="A1:F{last_row}"/>'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="A2" '
        'sqref="A2"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/>'
        '<cols><col min="1" max="1" width="16" customWidth="1"/>'
        '<col min="2" max="3" width="24" customWidth="1"/>'
        '<col min="4" max="4" width="42" customWidth="1"/>'
        '<col min="5" max="5" width="22" customWidth="1"/>'
        '<col min="6" max="6" width="56" customWidth="1"/></cols>'
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
        '</worksheet>'
    )
    return xml.encode("utf-8")


def add_sheet_metadata(
    archive: zipfile.ZipFile,
    source_members: list[str],
    sheet_name: str,
) -> tuple[dict[str, bytes], str]:
    try:
        workbook_xml = archive.read("xl/workbook.xml")
        rels_xml = archive.read("xl/_rels/workbook.xml.rels")
        content_types_xml = archive.read("[Content_Types].xml")
    except KeyError as exc:
        raise RefreshError(f"工作簿缺少新增工作表所需OOXML成员: {exc}") from exc
    if html.escape(sheet_name, quote=True).encode("utf-8") in workbook_xml:
        raise RefreshError(f"输入工作簿已存在补充工作表: {sheet_name}")
    sheet_id = max([int(value) for value in re.findall(rb'\bsheetId="(\d+)"', workbook_xml)] or [0]) + 1
    relation_id = max([int(value) for value in re.findall(rb'\bId="rId(\d+)"', rels_xml)] or [0]) + 1
    sheet_number = max([
        int(match.group(1))
        for member in source_members
        if (match := re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", member))
    ] or [0]) + 1
    sheet_member = f"xl/worksheets/sheet{sheet_number}.xml"
    sheet_tag = (
        f'<sheet xmlns:r="{REL_NS}" name="{html.escape(sheet_name, quote=True)}" '
        f'sheetId="{sheet_id}" state="visible" r:id="rId{relation_id}"/>'
    ).encode("utf-8")
    relationship = (
        f'<Relationship Type="{REL_NS}/worksheet" Target="/{sheet_member}" Id="rId{relation_id}"/>'
    ).encode("utf-8")
    override = (
        f'<Override PartName="/{sheet_member}" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    ).encode("utf-8")
    if b"</sheets>" not in workbook_xml or b"</Relationships>" not in rels_xml or b"</Types>" not in content_types_xml:
        raise RefreshError("无法定位新增工作表所需OOXML节点")
    return {
        "xl/workbook.xml": workbook_xml.replace(b"</sheets>", sheet_tag + b"</sheets>", 1),
        "xl/_rels/workbook.xml.rels": rels_xml.replace(b"</Relationships>", relationship + b"</Relationships>", 1),
        "[Content_Types].xml": content_types_xml.replace(b"</Types>", override + b"</Types>", 1),
    }, sheet_member


def copy_with_replacements(
    input_path: Path,
    output_path: Path,
    replacements: dict[str, bytes],
    additions: dict[str, bytes],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix="opensms-carrier-refresh-", dir=str(output_path.parent)))
    temporary = temporary_dir / output_path.name
    try:
        if not replacements and not additions:
            # Preserve the ZIP bytes, including compression and member metadata.
            shutil.copyfile(input_path, temporary)
            temporary.replace(output_path)
            return
        with zipfile.ZipFile(input_path, "r") as source, zipfile.ZipFile(temporary, "w") as target:
            for info in source.infolist():
                data = replacements.get(info.filename, source.read(info.filename))
                target.writestr(info, data)
            for member, data in additions.items():
                target.writestr(member, data)
        with zipfile.ZipFile(temporary, "r") as check:
            bad = check.testzip()
            if bad:
                raise RefreshError(f"输出XLSX压缩结构损坏: {bad}")
        temporary.replace(output_path)
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    input_path = args.input.resolve()
    live_path = args.live_receipt.resolve()
    output_path = args.output.resolve()
    if input_path == output_path:
        raise RefreshError("输入和输出工作簿必须使用不同路径")
    input_hash = sha256_file(input_path)
    cache = load_verified_cache(args, input_hash)
    live_bytes = live_path.read_bytes()
    live = json.loads(live_bytes)
    if not live.get("passed"):
        raise RefreshError("最终后台运营商回读未通过，拒绝刷新文档")
    rows_live = live.get("rows") or []
    live_by_phone = {short_phone(row.get("phone")): text(row.get("liveCarrierName")) for row in rows_live}
    invalid_live_rows = [
        row for row in rows_live
        if not short_phone(row.get("phone"))
        or (not text(row.get("liveCarrierName")) and not bool(row.get("liveCarrierUnassigned")))
    ]
    declared_candidate_count = int(live.get("candidateCount") or 0)
    declared_backend_valid_count = int(live.get("backendValidCount") or 0)
    declared_failure_count = int(live.get("failureCount") or 0)
    legitimate_empty_readback = (
        declared_candidate_count == 0
        and declared_backend_valid_count == 0
        and declared_failure_count == 0
        and not rows_live
    )
    if invalid_live_rows or (not live_by_phone and not legitimate_empty_readback):
        raise RefreshError("最终后台运营商回读为空或存在未声明的空号码/空名称")
    if (
        declared_candidate_count != len(rows_live)
        or declared_backend_valid_count != len(rows_live)
        or declared_failure_count != 0 or len(live_by_phone) != len(rows_live)
        or any(int(row.get("backendMatchCount") or 0) != 1 or row.get("passed") is not True for row in rows_live)
    ):
        raise RefreshError("后台回读唯一身份或全量覆盖计数不一致")
    backend_ids = [text(row.get("backendId")) for row in rows_live if text(row.get("backendId"))]
    if len(backend_ids) != len(set(backend_ids)) or (getattr(args, "run_id", None) and len(backend_ids) != len(rows_live)):
        raise RefreshError("后台号码ID缺失或重复")
    if getattr(args, "run_id", None):
        if not live.get("checkedAt") or live.get("silentBrowserMode") is not True or live.get("browserForegroundActivationAttempted") is not False:
            raise RefreshError("本轮后台回读缺少时间或静默操作证明")
        checked_at = datetime.fromisoformat(str(live["checkedAt"]).replace("Z", "+00:00"))
        if checked_at.tzinfo is None or (cache and checked_at < datetime.fromisoformat(cache["lastLiveCheckedAt"].replace("Z", "+00:00"))):
            raise RefreshError("后台回读时间早于本轮已核验阶段或缺少时区")

    with zipfile.ZipFile(input_path, "r") as source:
        source_members = source.namelist()
        if len(source_members) != len(set(source_members)):
            raise RefreshError("输入工作簿包含重复ZIP成员")
        sheets = workbook_sheets(source)
        main_name, main_path = sheets[0]
        if cache:
            strings = []
            source_xml = b""
            rows = {int(number): {int(index): tuple(cell) for index, cell in cells.items()} for number, cells in cache["mainRows"].items()}
            source_dimension = cache["mainDimension"]
        else:
            strings = shared_strings(source)
            source_xml = source.read(main_path)
            rows, source_dimension = parse_sheet_rows(source_xml, strings)
        header_row = rows.get(1, {})
        header_map = {text(value): index for index, (_, value, _) in header_row.items()}
        phone_index = header_map.get("电话")
        carrier_index = header_map.get("运营商")
        if phone_index is None or carrier_index is None:
            raise RefreshError("首个工作表缺少电话或运营商列")

        updates: list[dict[str, Any]] = []
        found: set[str] = set()
        target_styles: dict[str, str] = {}
        replacement_values: dict[str, str] = {}
        for row_number in sorted(rows):
            if row_number == 1:
                continue
            cells = rows[row_number]
            phone = short_phone(cells.get(phone_index, ("", "", "0"))[1])
            if phone not in live_by_phone:
                continue
            live_carrier = live_by_phone[phone]
            found.add(phone)
            prior = text(cells.get(carrier_index, ("", "", "0"))[1])
            reference = cells.get(carrier_index, (f"{column_letter(carrier_index)}{row_number}", "", "0"))[0]
            source_style = cells.get(carrier_index, ("", "", "0"))[2]
            target_styles[reference] = source_style
            if prior != live_carrier:
                replacement_values[reference] = live_carrier
            updates.append({
                "sheet": main_name,
                "row": row_number,
                "cell": reference,
                "phone": phone,
                "beforeCarrier": prior,
                "afterCarrier": live_carrier,
                "changed": prior != live_carrier,
            })
        unmatched = sorted(set(live_by_phone) - found)
        updated_xml, patched_styles = replace_cells(source_xml, replacement_values) if replacement_values and not cache else (source_xml, {})
        for reference, patched_style in patched_styles.items():
            if patched_style != target_styles[reference]:
                raise RefreshError(f"目标单元格样式读取不一致: {reference}")
        replacements = {main_path: updated_xml} if replacement_values else {}
        additions: dict[str, bytes] = {}
        supplemental_sheet_name = "后台更新未在源表"
        supplemental_member = ""
        supplemental_added = False
        metadata_members: set[str] = set()
        existing_supplemental = [sheet_path for name, sheet_path in sheets if name == supplemental_sheet_name]
        if len(existing_supplemental) > 1:
            raise RefreshError(f"输入工作簿存在重复补充工作表: {supplemental_sheet_name}")
        if existing_supplemental:
            supplemental_member = existing_supplemental[0]
            metadata_members.add(supplemental_member)
        elif unmatched:
            metadata_replacements, supplemental_member = add_sheet_metadata(
                source, source_members, supplemental_sheet_name
            )
            replacements.update(metadata_replacements)
            metadata_members = set(metadata_replacements)
            supplemental_added = True
        if unmatched or existing_supplemental:
            row_by_phone = {short_phone(row.get("phone")): {**row, "phone": short_phone(row.get("phone"))} for row in rows_live}
            supplemental_rows = [row_by_phone[phone] for phone in unmatched]
            header_style = header_row.get(phone_index, ("", "", "0"))[2]
            first_data_row = next((rows[number] for number in sorted(rows) if number != 1), {})
            data_style = first_data_row.get(phone_index, ("", "", "0"))[2]
            supplemental_xml = build_supplemental_sheet(supplemental_rows, header_style, data_style)
            if supplemental_added:
                additions[supplemental_member] = supplemental_xml
            elif source.read(supplemental_member) != supplemental_xml:
                replacements[supplemental_member] = supplemental_xml
        if cache and (replacements or additions):
            # Cache reuse proves only unchanged bytes. A real delta must use the
            # complete source scan and independent second parse below.
            full_args = argparse.Namespace(**vars(args))
            full_args.verified_cache_input = None
            full_args.verified_cache_sha256 = None
            full_args.cache_rejected_for_delta = True
            return run(full_args)
        source_member_hashes = {} if cache else {
            name: sha256_bytes(source.read(name))
            for name in source_members
            if name != main_path and name not in metadata_members
        }

    copy_with_replacements(input_path, output_path, replacements, additions)
    byte_preserving_copy = not replacements and not additions
    output_hash = sha256_file(output_path)
    if sha256_file(input_path) != input_hash or sha256_bytes(live_path.read_bytes()) != sha256_bytes(live_bytes):
        raise RefreshError("刷新期间输入工作簿或后台回读发生变化")
    if byte_preserving_copy and output_hash != input_hash:
        raise RefreshError("独立输出SHA256复核失败，字节复制内容不一致")

    references = [item["cell"] for item in updates]
    with zipfile.ZipFile(output_path, "r") as final:
        final_members = final.namelist()
        final_sheets = workbook_sheets(final)
        errors: list[dict[str, str]] = []
        if cache:
            # SHA equality transfers the complete prior independent verification
            # to this separately read output; backend rows were compared anew.
            final_strings = []
            final_xml = b""
            final_rows, final_dimension = rows, source_dimension
        else:
            final_strings = shared_strings(final)
            final_xml = final.read(main_path)
            final_rows, final_dimension = parse_sheet_rows(final_xml, final_strings, error_sink=errors, sheet_path=main_path)
            bad_member = final.testzip()
            if bad_member:
                raise RefreshError(f"输出XLSX压缩结构损坏: {bad_member}")
        changed_members = [
            name for name, expected_hash in source_member_hashes.items()
            if sha256_bytes(final.read(name)) != expected_hash
        ]
        expected_final_members = source_members + ([supplemental_member] if supplemental_added else [])
        if expected_final_members != final_members:
            changed_members.append("__member_list__")
        non_target_xml_equal = source_xml == final_xml or normalize_allowed_cells(source_xml, references) == normalize_allowed_cells(final_xml, references)
        target_mismatches = []
        style_mismatches = []
        for update in updates:
            row_number = update["row"]
            reference = update["cell"]
            cell = final_rows.get(row_number, {}).get(carrier_index)
            actual = text(cell[1] if cell else "")
            style = cell[2] if cell else ""
            if actual != update["afterCarrier"]:
                target_mismatches.append({"sheet": main_name, "cell": reference, "expected": update["afterCarrier"], "actual": actual})
            if style != target_styles[reference]:
                style_mismatches.append({"key": f"{main_name}!{reference}", "before": target_styles[reference], "after": style})
        supplemental_mismatches = []
        if supplemental_member:
            final_supplemental_xml = final.read(supplemental_member)
            supplemental_rows_read, supplemental_dimension = parse_sheet_rows(final_supplemental_xml, final_strings)
            if final_supplemental_xml != supplemental_xml:
                supplemental_mismatches.append({"reason": "all-six-fields-and-styles-xml-mismatch"})
            if supplemental_dimension != f"A1:F{len(unmatched) + 1}":
                supplemental_mismatches.append({
                    "reason": "dimension", "expected": f"A1:F{len(unmatched) + 1}", "actual": supplemental_dimension,
                })
            supplemental_by_phone = {
                text(cells.get(0, ("", "", "0"))[1]): text(cells.get(1, ("", "", "0"))[1])
                for number, cells in supplemental_rows_read.items() if number != 1
            }
            for phone in unmatched:
                if supplemental_by_phone.get(phone) != live_by_phone[phone]:
                    supplemental_mismatches.append({
                        "phone": phone, "expected": live_by_phone[phone], "actual": supplemental_by_phone.get(phone, ""),
                    })
        if not cache:
            errors.extend(formula_errors(final, [path for _, path in final_sheets if path != main_path], final_strings))

    dimension_mismatches = [] if source_dimension == final_dimension else [{"sheet": main_name, "before": source_dimension, "after": final_dimension}]
    sheet_names = [name for name, _ in sheets]
    final_sheet_names = [name for name, _ in final_sheets]
    expected_final_sheet_names = sheet_names + ([supplemental_sheet_name] if supplemental_added else [])
    non_carrier_mismatch_count = len(changed_members) + (0 if non_target_xml_equal else 1)
    passed = (
        expected_final_sheet_names == final_sheet_names
        and not dimension_mismatches
        and not changed_members
        and non_target_xml_equal
        and not target_mismatches
        and not style_mismatches
        and not supplemental_mismatches
        and not errors
    )
    if sha256_file(output_path) != output_hash:
        raise RefreshError("独立复核期间输出工作簿发生变化")
    cache_output = getattr(args, "verified_cache_output", None)
    cache_output_hash = ""
    if cache_output and passed:
        cache_output = Path(cache_output).resolve()
        cache_payload = {
            "schema": CACHE_SCHEMA,
            "runId": args.run_id,
            "createdAt": iso_now(),
            "workbookSha256": output_hash,
            "workbookPath": str(output_path),
            "lastLiveCheckedAt": live["checkedAt"],
            "liveReceiptSha256": sha256_bytes(live_bytes),
            "passed": True,
            "formulaErrorCount": 0,
            "zipIntegrityPassed": True,
            "mainDimension": final_dimension,
            "mainRows": {
                number: {index: cell for index, cell in cells.items() if index in {phone_index, carrier_index}}
                for number, cells in final_rows.items()
            },
            "verificationMode": "independent-output-sha256-bound-prior-verification" if cache else "independent-ooxml-member-hash-plus-second-parse",
            "priorCacheSha256": getattr(args, "verified_cache_sha256", None),
        }
        atomic_json(cache_output, cache_payload)
        cache_output_hash = sha256_file(cache_output)
    common = {
        "previewSkipped": True,
        "previewSkipReason": "OPENSMS巡检策略明确禁止生成或打开预览",
        "runId": getattr(args, "run_id", None),
        "workbookRefreshMode": "byte-preserving-copy" if byte_preserving_copy else "targeted-ooxml-repack",
        "zeroDeltaConfirmed": byte_preserving_copy,
        "verifiedCacheReused": bool(cache),
        "cacheRejectedForDelta": bool(getattr(args, "cache_rejected_for_delta", False)),
        "inputWorkbookSha256": input_hash,
        "outputWorkbookSha256": output_hash,
        "liveReceiptSha256": sha256_bytes(live_bytes),
        "independentOutputSha256Passed": True,
        "zipIntegrityPassed": True,
        "formulaErrorScanMode": "same-run-sha256-bound-complete-scan" if cache else "complete-independent-scan",
        "verifiedCachePath": str(cache_output) if cache_output else "",
        "verifiedCacheSha256": cache_output_hash,
    }
    changed_count = sum(1 for item in updates if item["changed"])
    artifact_receipt = {
        "createdAt": iso_now(),
        "operation": "post-write-carrier-refresh-targeted-ooxml",
        "inputWorkbook": str(input_path),
        "liveCarrierReceipt": str(live_path),
        "outputWorkbook": str(output_path),
        "liveCandidateCount": len(live_by_phone),
        "matchedPhoneCount": len(found),
        "changedCellCount": changed_count,
        "occurrenceCount": len(updates),
        "unmatchedPhones": unmatched,
        "supplementalSheetName": supplemental_sheet_name if unmatched else "",
        "supplementalRowCount": len(unmatched),
        "updates": updates,
        "preExportFormulaErrorScan": "deferred to independent OOXML verification",
        "nonTargetMembersCopiedByteForByte": True,
        **common,
        "passed": passed,
    }
    verify_receipt = {
        "checkedAt": iso_now(),
        "mode": "independent-output-sha256-bound-prior-verification" if cache else "independent-ooxml-member-hash-plus-second-parse",
        "sourceWorkbook": str(input_path),
        "finalWorkbook": str(output_path),
        "sheetNames": sheet_names,
        "expectedFinalSheetNames": expected_final_sheet_names,
        "finalSheetNames": final_sheet_names,
        "liveCandidateCount": len(live_by_phone),
        "matchedPhoneCount": len(found),
        "carrierOccurrenceCount": len(updates),
        "dimensionMismatchCount": len(dimension_mismatches),
        "dimensionMismatches": dimension_mismatches,
        "nonCarrierValueMismatchCount": non_carrier_mismatch_count,
        "nonCarrierValueMismatchSample": changed_members[:20] + ([] if non_target_xml_equal else [main_path]),
        "targetMismatchCount": len(target_mismatches),
        "targetMismatches": target_mismatches,
        "styleMismatchCount": len(style_mismatches),
        "styleMismatches": style_mismatches,
        "supplementalRowCount": len(unmatched),
        "supplementalMismatchCount": len(supplemental_mismatches),
        "supplementalMismatches": supplemental_mismatches,
        "formulaErrorCount": len(errors),
        "formulaErrorScan": errors[:300],
        "verificationWallSeconds": round(time.monotonic() - started, 3),
        **common,
        "passed": passed,
    }
    refresh_receipt = {
        "createdAt": iso_now(),
        "operation": "post-write-carrier-refresh",
        "inputPath": str(input_path),
        "outputPath": str(output_path),
        "candidateCount": live.get("candidateCount"),
        "backendResolvedCount": live.get("backendValidCount"),
        "backendMissingCount": sum(1 for row in rows_live if int(row.get("backendMatchCount") or 0) == 0),
        "backendDuplicateCount": sum(1 for row in rows_live if int(row.get("backendMatchCount") or 0) > 1),
        "backendUnassignedCarrierCount": sum(1 for row in rows_live if bool(row.get("liveCarrierUnassigned"))),
        "matchedWorkbookRowCount": len(found),
        "sourceMissingCount": len(unmatched),
        "supplementalSheetName": supplemental_sheet_name if unmatched else "",
        "supplementalRowCount": len(unmatched),
        "mergedWorkbookCoveredCount": len(found) + len(unmatched),
        "missingInWorkbookCount": len(supplemental_mismatches),
        "duplicateWorkbookCount": max(0, len(updates) - len(found)),
        "updatedCellCount": changed_count,
        "alreadyCurrentCount": len(updates) - changed_count,
        "stillStaleCount": len(target_mismatches),
        "formulaErrorCount": len(errors),
        "nonCarrierValueMismatchCount": non_carrier_mismatch_count,
        "styleMismatchCount": len(style_mismatches),
        "durationSeconds": round(time.monotonic() - started, 3),
        **common,
        "passed": passed,
        "rows": [
            {"phone": item["phone"], "sheet": item["sheet"], "cell": item["cell"], "beforeCarrier": item["beforeCarrier"], "currentCarrierName": item["afterCarrier"]}
            for item in updates
        ],
        "artifactReceipt": str(args.artifact_receipt.resolve()),
        "independentVerificationReceipt": str(args.verify_receipt.resolve()),
    }
    readback_receipt = {
        "checkedAt": live.get("checkedAt"),
        "mode": live.get("mode"),
        "apiTransport": live.get("apiTransport"),
        "silentBrowserMode": True,
        "browserForegroundActivationAttempted": False,
        "candidateCount": live.get("candidateCount"),
        "targetCount": live.get("targetMatchCount"),
        "missingCount": sum(1 for row in rows_live if int(row.get("backendMatchCount") or 0) == 0),
        "duplicateBackendCount": sum(1 for row in rows_live if int(row.get("backendMatchCount") or 0) > 1),
        "mismatchCount": live.get("failureCount"),
        "passed": live.get("passed"),
        "rows": [
            {
                "phone": row.get("phone"), "backendId": row.get("backendId"),
                "currentCarrierName": row.get("liveCarrierName"), "currentCarrierIdentif": row.get("liveCarrierIdentif"),
                "expectedCarrierName": row.get("expectedTarget"), "passed": row.get("passed"),
            }
            for row in rows_live
        ],
        "sourceReceipt": str(live_path),
    }
    for path, payload in (
        (args.artifact_receipt, artifact_receipt),
        (args.verify_receipt, verify_receipt),
        (args.refresh_receipt, refresh_receipt),
        (args.readback_receipt, readback_receipt),
    ):
        atomic_json(path.resolve(), payload)
    result = {
        "outputPath": str(output_path),
        "passed": passed,
        "changed": changed_count,
        "styleMismatchCount": len(style_mismatches),
        "formulaErrorCount": len(errors),
        "nonCarrierValueMismatchCount": non_carrier_mismatch_count,
        "durationSeconds": round(time.monotonic() - started, 3),
        **common,
    }
    if not passed:
        raise RefreshError(json.dumps(result, ensure_ascii=False))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenSMS最终工作簿运营商快速定点刷新")
    parser.add_argument("input", type=Path)
    parser.add_argument("live_receipt", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("artifact_receipt", type=Path)
    parser.add_argument("verify_receipt", type=Path)
    parser.add_argument("refresh_receipt", type=Path)
    parser.add_argument("readback_receipt", type=Path)
    parser.add_argument("--run-id", help="本轮新鲜导出的唯一运行标识；缓存不得跨轮复用")
    parser.add_argument("--verified-cache-input", type=Path, help="同轮上一刷新阶段已独立核验的元数据")
    parser.add_argument("--verified-cache-sha256", help="调用方从上一刷新结果保存的元数据SHA256")
    parser.add_argument("--verified-cache-output", type=Path, help="只在本次刷新独立核验通过后输出元数据")
    return parser


def main() -> int:
    try:
        result = run(build_parser().parse_args())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
