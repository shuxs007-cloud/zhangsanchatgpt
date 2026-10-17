#!/usr/bin/env python3
"""Export only this job's newly committed activation dates, never a cached full library."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile

from opensms_export_task_f_deadlines import (
    DEFAULT_TEMPLATE, export_deadlines, normalize_iccid, publish_no_replace, sha256, valid_date,
)
from opensms_task_f_db import parse_activation_date


def collect_updates(database: Path, receipts: list[Path], run_id: str) -> tuple[list[list], list[dict]]:
    updates, proofs, seen_paths = {}, [], set()
    for path in receipts:
        path = path.resolve()
        if path in seen_paths:
            raise ValueError('导入回执重复')
        seen_paths.add(path)
        payload = json.loads(path.read_text(encoding='utf-8'))
        if payload.get('status') not in {'passed', 'passed_with_transient_errors'}:
            raise ValueError('只接受成功的数据库导入回执')
        if Path(payload.get('database') or '').resolve() != database.resolve():
            raise ValueError('导入回执数据库不匹配')
        if not run_id or payload.get('runId') != run_id:
            raise ValueError('导入回执不属于本次 runId，禁止旧数据冒充新增')
        lookup_path = Path(payload.get('lookupReceipt') or '')
        if not lookup_path.is_absolute() or not lookup_path.is_file() or sha256(lookup_path) != payload.get('lookupSha256'):
            raise ValueError('导入回执对应查询文件缺失或哈希不匹配')
        lookup = json.loads(lookup_path.read_text(encoding='utf-8'))
        if lookup.get('runId') != run_id:
            raise ValueError('查询回执不属于本次 runId')
        queried = {}
        for item in lookup.get('results') or []:
            if item.get('status') != 'found':
                continue
            key = normalize_iccid(item.get('queryIccid'))
            activation = parse_activation_date(item.get('activationDate'))
            if not activation:
                raise ValueError('查询回执 found 缺少有效激活日期')
            if key in queried and queried[key] != activation:
                raise ValueError('本次查询结果存在 ICCID 日期冲突')
            queried[key] = activation
        marked = [row for row in payload.get('rows', []) if row.get('activationDateUpdated') is True]
        if payload.get('activationDateUpdatedCount') != len(marked):
            raise ValueError('导入回执缺少或不匹配新增激活日期计数')
        for row in marked:
            key = normalize_iccid(row.get('queryIccid'))
            a, c = valid_date(row.get('activationDate')), valid_date(row.get('cutoffDate'))
            if row.get('status') != 'found' or not a or not c or c != a + timedelta(days=29):
                raise ValueError('增量回执包含无效激活日期')
            if queried.get(key) != a:
                raise ValueError('增量日期没有本轮对应 ICCID 查询结果证明')
            expected = [key, c.isoformat(), a.isoformat()]
            if key in updates and updates[key] != expected:
                raise ValueError('同一批增量 ICCID 日期冲突')
            updates[key] = expected
        proofs.append({'path': str(path), 'sha256': sha256(path), 'activationDateUpdatedCount': len(marked)})
    return sorted(updates.values(), key=lambda row: row[0]), proofs


def export_delta(database: Path, receipts: list[Path], output: Path, receipt: Path, run_id: str,
                 template: Path = DEFAULT_TEMPLATE) -> dict:
    if not run_id.strip():
        raise ValueError('增量导出必须绑定 runId')
    if os.path.lexists(output) or os.path.lexists(receipt):
        raise FileExistsError('增量文件或回执已存在，不覆盖旧交付')
    rows, proofs = collect_updates(database, receipts, run_id)
    if rows:
        return export_deadlines(database, template, output, receipt, selected_rows=rows,
                                extra_receipt={'generated': True, 'runId': run_id,
                                               'scope': 'newly_committed_activation_dates_only',
                                               'importReceipts': proofs})
    result = {'status': 'success', 'generated': False, 'output': None, 'recordCount': 0,
              'runId': run_id, 'reason': 'no_new_activation_dates', 'importReceipts': proofs,
              'createdAt': datetime.now().astimezone().isoformat(), 'networkAccess': False,
              'databaseReadOnly': True, 'verificationPassed': True}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=receipt.parent,
                                     prefix='.delta_receipt_', delete=False) as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        publish_no_replace(temporary, receipt)
        if json.loads(receipt.read_text(encoding='utf-8')) != result:
            raise ValueError('零增量回执发布后回读不一致')
    finally:
        temporary.unlink(missing_ok=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--import-receipt', type=Path, action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--template', type=Path, default=DEFAULT_TEMPLATE)
    args = parser.parse_args()
    try:
        result = export_delta(args.db, args.import_receipt, args.output, args.receipt, args.run_id, args.template)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'generated': False, 'error': str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
