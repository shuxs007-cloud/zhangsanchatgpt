#!/usr/bin/env python3
"""Local diagnostic replay, never a fresh report or business write."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from opensms_export_scan_cache import load_scan_cache
from opensms_inventory_coverage import check_inventory_coverage


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'services/opensms_document_api/pipeline' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser()
    for arg in ('failure', 'inventory', 'output'):
        parser.add_argument('--' + arg, type=Path, required=True)
    args = parser.parse_args(argv)
    failure = json.loads(args.failure.read_text())
    scan = load_scan_cache(failure['scanCache'], expected_source=failure['source'])
    inventory_bytes = args.inventory.read_bytes()
    inventory = json.loads(inventory_bytes)
    digest = hashlib.sha256(inventory_bytes).hexdigest()
    if inventory.get('runId') != failure.get('runId') or scan['sourceSha256'] != failure.get('sourceSha256'):
        raise RuntimeError('Replay source or inventory is not bound to the original failed run')
    full_coverage = check_inventory_coverage(scan, inventory)
    rows = [scan['rows'][entry['sourceRow'] - 2] for entry in failure['missing']]
    coverage = check_inventory_coverage({**scan, 'rows': rows}, inventory)
    os.environ.update(OPENSMS_SYSTEM_KOODA_PATH=str(args.inventory.resolve()),
        OPENSMS_SYSTEM_KOODA_SHA256=digest, OPENSMS_RUN_ID=inventory['runId'])
    generator = load_module('diagnostic_generator', 'process_card_slots.py')
    verifier = load_module('diagnostic_verifier', 'opensms_verify_card_slot_fast.py')
    source = generator.load_system_kooda_source()
    snapshot = verifier.load_system_kooda_snapshot(args.inventory, digest, inventory['runId'])
    proofs = []
    for values in rows:
        row = dict(zip(scan['headers'], values))
        generated = generator.resolve_system_kooda_row(source, slot=row['卡槽'], phone=row['电话'],
            status=row['状态'], created=row['创建时间'], iccid=row['ICCID'], has_iccid=True)
        verified = verifier.find_system_record(snapshot, row)
        if verified is None or str(verified['raw']['id']) != generated['backend_id']:
            raise RuntimeError('Independent historical identity did not agree')
        proofs.append({'phone': row['电话'], 'exportHistoricalSlot': row['卡槽'],
            'backendId': generated['backend_id'], 'matchRule': generated.get('match_rule'),
            'sourceStatus': row['状态'], 'inventoryStatus': verified['raw'].get('status'),
            'backendCurrentSlot': verified['raw'].get('slotIdentif'), 'independentIdentityVerified': True})
    if coverage['status'] != 'passed' or full_coverage['status'] != 'passed':
        raise RuntimeError('Original failure set or complete original source still fails coverage')
    # Exercise the actual F scope selector, but never prepare pending work,
    # open a database, log in, or query. Historical records must not leak into F.
    task_f = {'status': 'not_applicable', 'reason': 'source_has_no_activation_column'}
    if '激活日期' in scan['headers']:
        from opensms_task_f_db import document_backfill_rows
        selected, metadata = document_backfill_rows(inventory, Path(failure['source']),
            scan['sourceSha256'], inventory['runId'])
        archived_ids = {str(row['backendId']) for row in full_coverage['archivedMatches']}
        if any(str(row['backendId']) in archived_ids for row in selected):
            raise RuntimeError('Archived record incorrectly selected for Task F')
        excluded = [row for row in metadata['documentExcludedRows']
                    if row['reason'] == 'archived_off_shelf_no_current_slot']
        task_f = {'status': 'passed', 'archivedExcludedCount': len(excluded),
            'archivedSelectedCount': 0, 'selectedCount': len(selected),
            'databaseAccess': False, 'lookupExecuted': False, 'excluded': excluded}
    receipt = {'status': 'passed', 'mode': 'historical-failure-replay-not-fresh-delivery',
        'businessWrites': 0, 'browserAccess': False, 'networkAccess': False,
        'failureReceipt': str(args.failure.resolve()), 'sourceSha256': scan['sourceSha256'],
        'inventory': str(args.inventory.resolve()), 'inventorySha256': digest,
        'originalMissingCount': len(rows), 'resolvedCount': len(proofs), 'proofs': proofs,
        'coverage': coverage, 'fullCoverage': full_coverage, 'taskFScopeReview': task_f}
    with args.output.open('x') as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2)
    print(json.dumps({'status': 'passed', 'resolvedCount': len(proofs), 'output': str(args.output.resolve())}))


if __name__ == '__main__':
    main()
