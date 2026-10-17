"""Receipt-bound refresh of the current ko-system/unactivated sheet.

Pure local selection, no database/network writes. The existing Task F worker
owns deduplication, querying, import and next-export visibility.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/opensms_document_api/pipeline'))
import ko_unactivated as ko
from report_kooda_source import is_report_source, validate_projection

CONTRACT = 'verified-ko-sheet-refresh-v1'


def load_evidence(proof, inventory, inventory_hash, run_id, cache_path, cache_hash):
    """Recompute the frozen F/system intersection; do not trust sheet counts."""
    generated = (proof.get('process') or {}).get('ko_unactivated_sheet') or {}
    reviewed = (proof.get('verification') or {}).get('ko_unactivated_sheet_review') or {}
    system_review = (proof.get('verification') or {}).get('system_kooda_source_review') or {}
    report_source = is_report_source(inventory)
    if report_source:
        validate_projection(inventory, run_id)
    if (proof.get('result') != '成功' or not proof.get('checks')
            or any(value is not True for value in proof['checks'].values())
            or proof.get('failed_checks') != [] or reviewed.get('matches') is not True
            or system_review.get('matches') is not True
            or system_review.get('run_id') != run_id
            or system_review.get('snapshot_sha256') != inventory_hash
            or generated.get('snapshot_sha256') != cache_hash
            or generated.get('system_sha256') != inventory_hash
            or inventory.get('runId') != run_id or inventory.get('status') != 'passed'
            or (not report_source and (inventory.get('paginationTotalStable') is not True
            or inventory.get('paginationBackendIdsUnique') is not True))):
        raise RuntimeError('ko系统有-未激活补查缺少本轮双来源独立核验')
    frozen = ko.load_snapshot(cache_path, cache_hash, run_id, required=True)
    system = {'run_id': run_id, 'path': generated.get('system_path'),
              'sha256': inventory_hash, 'raw_rows': inventory.get('systemRows'),
              'total_rows': inventory.get('totalRows'), 'source_kind': inventory.get('sourceKind')}
    evidence = ko.select_system_records(frozen, system)
    # Detached copies keep original provenance paths, but identical bytes.
    expected = {**evidence['proof'], 'snapshot_path': generated.get('snapshot_path')}
    if generated != expected or reviewed != {**expected, 'matches': True, 'errors': {}}:
        raise RuntimeError('ko系统有-未激活补查冻结交集与独立回执不一致')
    evidence['proof'] = expected
    return evidence, system


def selected_rows(book, proof, inventory, inventory_hash, run_id, cache_path, cache_hash):
    evidence, system = load_evidence(proof, inventory, inventory_hash, run_id, cache_path, cache_hash)
    # Reopen/review is done by the caller in its already-open workbook pass.
    normalized_proof = {**evidence['proof'], 'snapshot_path': str(Path(cache_path))}
    review = ko.review(book, normalized_proof, cache_path, cache_hash, run_id,
                       required=True, system=system)
    if not review or review.get('matches') is not True:
        raise RuntimeError('ko系统有-未激活补查工作表独立回读失败')
    by_iccid = {}
    seen_ids = set()
    for raw in inventory['systemRows']:
        identity = str(raw.get('sourceRowIdentity') if is_report_source(inventory) else raw.get('id') or '').strip()
        if not identity or identity in seen_ids or not re.search(r'kooda|koodo|telus1', str(raw.get('opr') or ''), re.I):
            raise RuntimeError('ko补查库存身份缺失、重复或OPR越界')
        seen_ids.add(identity)
        text = raw.get('cardStore')
        key = re.sub(r'[Ff]$', '', text.strip()) if isinstance(text, str) else ''
        if re.fullmatch(r'[0-9]{18,22}', key):
            by_iccid.setdefault(key, []).append(raw)
    rows = []
    for number, row in enumerate(evidence['rows'], 2):
        key = row['queryIccid']
        matches = sorted(by_iccid[key], key=lambda raw: str(raw.get('sourceRowIdentity') or raw.get('id')))
        raw = matches[0]
        # ICCID is the lookup identity. Keep every system occurrence, including
        # blank or multiple slots; never use the historical phone/slot to query.
        rows.append({'documentRow': f'{ko.SHEET}!{number}', 'documentSheet': ko.SHEET,
            'sheetRow': number, 'queryIccid': key, 'sourceIccid': raw['cardStore'],
            'cardStore': raw['cardStore'], 'phone': str(raw.get('cardNo') or ''),
            'slot': str(raw.get('slotIdentif') or ''), 'opr': raw['opr'],
            'carrierName': str(raw.get('carrierName') or raw.get('carrireName') or ''),
            'backendId': raw.get('id'), 'sourceData': raw, 'systemMatches': matches,
            'missingFields': ['激活日期', '截止日'], 'dateSource': 'task_f_activation_missing',
            'lookupReason': CONTRACT})
    return rows


def pending_context(proof, inventory, inventory_hash, run_id, cache_path, cache_hash):
    load_evidence(proof, inventory, inventory_hash, run_id, cache_path, cache_hash)
    return {'contract': CONTRACT, 'cachePath': str(Path(cache_path).resolve()),
            'cacheSha256': cache_hash}
