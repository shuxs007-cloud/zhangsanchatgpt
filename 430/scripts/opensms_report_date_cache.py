#!/usr/bin/env python3
"""Freeze read-only Task F dates once per report; never migrate or create the DB."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/opensms_document_api/pipeline'))
from task_f_renewal_dates import read_renewals, validate_renewal

from opensms_export_task_f_deadlines import normalize_iccid, publish_no_replace, valid_date


def freeze(database: Path, output: Path, run_id: str) -> dict:
    if not run_id or not run_id.strip():
        raise ValueError('日期快照必须绑定本轮 runId')
    if os.path.lexists(output):
        raise FileExistsError(f'日期快照不可覆盖: {output}')
    records, seen, excluded, unactivated = [], set(), 0, []
    exists = database.exists()
    if exists:
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=30) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            conn.row_factory = sqlite3.Row
            available = {row['name'] for row in conn.execute('PRAGMA table_info(kooda_activation)')}
            fields = ['query_iccid', 'activation_date', 'cutoff_date', 'lookup_status']
            fields.extend(field for field in ('queried_at', 'source_phone', 'source_slot', 'source_opr',
                'source_carrier_name', 'purchase_date', 'act_request_date', 'sku', 'data_source') if field in available)
            source = conn.execute('SELECT ' + ','.join(fields) + ' FROM kooda_activation ORDER BY query_iccid').fetchall()
            renewals = read_renewals(conn)
        for raw in source:
            row = dict(raw)
            iccid, activation, cutoff, status = (row[name] for name in
                ('query_iccid', 'activation_date', 'cutoff_date', 'lookup_status'))
            key = normalize_iccid(iccid)
            if key in seen:
                raise ValueError('日期库归一化 ICCID 重复')
            seen.add(key)
            if status != 'found':
                excluded += 1
                if status == 'activation_missing':
                    if any(value is not None and (not isinstance(value, str) or value.strip()) for value in (activation, cutoff)):
                        raise ValueError('未激活状态与非空日期冲突，拒绝生成未激活表')
                    unactivated.append({'queryIccid': key, 'status': status,
                        'activationDate': None, 'cutoffDate': None,
                        **{target: str(row.get(field) or '') for target, field in (
                            ('queriedAt', 'queried_at'), ('phone', 'source_phone'),
                            ('slot', 'source_slot'), ('opr', 'source_opr'),
                            ('carrier', 'source_carrier_name'), ('purchaseDate', 'purchase_date'),
                            ('actRequestDate', 'act_request_date'), ('sku', 'sku'), ('dataSource', 'data_source'))}})
                continue
            a, c = valid_date(activation), valid_date(cutoff)
            if not a or not c or c != a + timedelta(days=29):
                raise ValueError(f'日期库 found 记录日期无效: {key}')
            record = {'queryIccid': key, 'activationDate': a.isoformat(), 'cutoffDate': c.isoformat()}
            if key in renewals:
                proof = renewals[key]
                if proof['baseCutoffDate'] != c.isoformat():
                    raise ValueError('续费库与原始F库日期冲突')
                validate_renewal(proof, key, a.isoformat(), proof['cutoffDate'])
                record.update(cutoffDate=proof['cutoffDate'], renewal=proof)
            records.append(record)
    records.sort(key=lambda row: row['queryIccid'])
    unactivated.sort(key=lambda row: row['queryIccid'])
    payload = {'schemaVersion': 1, 'status': 'passed', 'runId': run_id,
               'createdAt': datetime.now().astimezone().isoformat(),
               'sourceDatabase': str(database.resolve()), 'sourceExists': exists,
               'recordCount': len(records), 'excludedWithoutValidDates': excluded,
               'readOnly': True, 'snapshotConsistent': True, 'records': records,
               'koUnactivated': {'schemaVersion': 1, 'scope': 'all_task_f_activation_missing',
                                 'recordCount': len(unactivated), 'records': unactivated}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent,
                                     prefix='.report_dates_', delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(',', ':'))
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        publish_no_replace(temporary, output)
        if hashlib.sha256(output.read_bytes()).hexdigest() != digest or json.loads(output.read_text()) != payload:
            raise ValueError('日期快照发布后独立回读不一致')
    finally:
        temporary.unlink(missing_ok=True)
    return {key: value for key, value in payload.items() if key not in ('records', 'koUnactivated')} | {
        'koUnactivatedCount': len(unactivated),
        'output': str(output.resolve()), 'sha256': digest, 'verificationPassed': True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['freeze'])
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    try:
        result = freeze(args.db, args.output, args.run_id)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
