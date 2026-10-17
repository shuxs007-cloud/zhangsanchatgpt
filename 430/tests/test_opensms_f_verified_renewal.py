"""Offline tests for explicit renewal dates, metadata and safe repeat imports."""
from datetime import date
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'services/opensms_document_api/pipeline'))
from opensms_f_verified_renewal import make_plan, commit_update, read_source
from opensms_f_renewal import digest
from opensms_task_f_db import open_database, export_catalog
from opensms_report_date_cache import freeze
from task_f_renewal_dates import validate_renewal

spec = importlib.util.spec_from_file_location('verified_renewal_verifier',
    ROOT / 'services/opensms_document_api/pipeline/opensms_verify_card_slot_fast.py')
VERIFIER = importlib.util.module_from_spec(spec)
spec.loader.exec_module(VERIFIER)


class VerifiedRenewalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'f.sqlite3'
        self.source = self.root / 'source.xlsx'
        self.raw = self.root / 'raw.xlsx'
        self.state = self.root / 'state.json'
        self.iccids = ['8912230200278094459', '8912230200278150483', '8912230200278103516']
        self.phones = ['4389392694', '4389392271', '4377845479']
        self.run_id = '20261014T023000+0800'
        with open_database(self.db) as conn:
            for key in self.iccids:
                conn.execute('INSERT INTO kooda_activation(query_iccid,source_iccid,activation_date,cutoff_date,lookup_status,queried_at,receipt_json) VALUES (?,?,?,?,?,?,?)',
                    (key, key, '2026-09-14', '2026-10-13', 'found', '2026-09-14', '{}'))
        self.write_source([
            [self.phones[0], '付款待核', '$30.00', '2026/11/12', '29'],
            [self.phones[1], '登录失败', '$30.00', None, None],
            [self.phones[2], '不需要充值', '$0.00', None, '30'],
        ])
        wb = openpyxl.Workbook()
        sheet = wb.active
        sheet.title = 'sheet1'
        sheet.append(['电话','ICCID','运营商','opr','卡槽','状态','激活日期','截止日'])
        for number, key in zip(self.phones, self.iccids):
            sheet.append(['1' + number, key + 'F', 'B12-fixture', '302220 Koodo', '240_01.01',
                          '上架', '2026-09-14', '2026-10-13'])
        wb.save(self.raw)
        self.bind_state()

    def tearDown(self):
        self.temp.cleanup()

    def write_source(self, rows):
        wb = openpyxl.Workbook()
        sheet = wb.active
        sheet.title = 'Sheet'
        sheet.append(['电话','注册状态','余额','截止日期','套餐类型','email','kooPassword'])
        for row in rows:
            sheet.append(row + ['DO_NOT_EXPORT', 'DO_NOT_EXPORT'])
        wb.save(self.source)

    def bind_state(self):
        self.state.write_text(json.dumps({'status':'success','runId':self.run_id,
            'stages':{'acquire':{'sourceArchive':str(self.raw),'sourceSha256':digest(self.raw),'sourceVerified':True}},
            'summary':{'sourceWorkbook':str(self.raw)}}))

    def test_source_scope_and_plan_default(self):
        plan = make_plan(self.source, self.state, self.db)
        self.assertEqual(3, plan['matchedRows'])
        self.assertEqual({'29':1,'30':2}, plan['planCounts'])
        self.assertEqual(1, plan['defaultPlanRows'])
        self.assertNotIn('DO_NOT_EXPORT', json.dumps(plan))

    def test_direct_date_never_adds_thirty_and_missing_positive_adds_twenty_nine(self):
        plan = make_plan(self.source, self.state, self.db)
        self.assertEqual('2026-11-12', plan['rows'][0]['cutoff'])
        self.assertEqual('2026-11-11', plan['rows'][1]['cutoff'])
        self.assertIsNone(plan['rows'][2]['cutoff'])
        self.assertEqual(1, plan['provisionalRows'])
        self.assertEqual(1, plan['noDateRows'])

    def test_backup_metadata_original_dates_and_repeat_idempotency(self):
        first = self.root / 'attempt1'; first.mkdir()
        plan = make_plan(self.source, self.state, self.db)
        receipt = commit_update(plan, self.db, first, self.run_id)
        self.assertTrue(receipt['independentReadbackPassed'])
        self.assertTrue(receipt['originalActivationAndInventoryUnchanged'])
        self.assertTrue(Path(receipt['backup']).is_file())
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(('30.00',29,'2026-10-13'), conn.execute(
                'SELECT balance_cad,plan_type,cutoff_date FROM kooda_activation WHERE query_iccid=?', (self.iccids[0],)).fetchone())
        second = self.root / 'attempt2'; second.mkdir()
        repeated = make_plan(self.source, self.state, self.db)
        self.assertEqual('2026-11-11', repeated['rows'][1]['cutoff'])
        commit_update(repeated, self.db, second, self.run_id)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(2, conn.execute('SELECT count(*) FROM kooda_renewal_events').fetchone()[0])
        with self.assertRaises(FileExistsError):
            commit_update(repeated, self.db, second, self.run_id)

    def test_frozen_dates_independently_verify_and_detect_tampering(self):
        target = self.root / 'attempt'; target.mkdir()
        plan = make_plan(self.source, self.state, self.db)
        commit_update(plan, self.db, target, self.run_id)
        snapshot = self.root / 'snapshot.json'
        freeze(self.db, snapshot, self.run_id)
        checked = VERIFIER.load_frozen_date_snapshot(snapshot, digest(snapshot), self.run_id)
        self.assertEqual(date(2026,11,11), checked['index'][self.iccids[1]]['截止日'])
        data = json.loads(snapshot.read_text())
        data['records'][0]['renewal']['cutoffDate'] = '2026-12-12'
        snapshot.write_text(json.dumps(data))
        with self.assertRaises(RuntimeError):
            VERIFIER.load_frozen_date_snapshot(snapshot, digest(snapshot), self.run_id)

    def test_catalog_carries_balance_and_plan_and_renewal_precedence(self):
        target = self.root / 'attempt'; target.mkdir()
        commit_update(make_plan(self.source,self.state,self.db),self.db,target,self.run_id)
        payload = export_catalog(self.db, self.root / 'library.json')
        row = next(r for r in payload['rows'] if r['queryIccid']==self.iccids[0])
        self.assertEqual('30.00', row['balanceCad'])
        self.assertEqual(29, row['planType'])
        self.assertEqual('2026-11-12', row['effectiveCutoffDate'])
        self.assertEqual('2026-10-13', row['cutoffDate'])

    def test_duplicate_source_identity_hard_stops(self):
        self.write_source([[self.phones[0],'ok','$30','2026-11-12',30]] * 2)
        with self.assertRaisesRegex(ValueError,'duplicate source phone'):
            read_source(self.source)

    def test_invalid_row_is_excluded_without_guessing(self):
        self.write_source([[self.phones[0],'ok','$30','2026-02-30',30],
                           [self.phones[1],'ok','$1.00','2026-11-12',None]])
        plan = make_plan(self.source,self.state,self.db)
        self.assertEqual(1,len(plan['excluded']))
        self.assertEqual(1,plan['matchedRows'])

    def test_source_hash_and_changed_baseline_stop_commit(self):
        plan = make_plan(self.source,self.state,self.db)
        self.raw.write_bytes(self.raw.read_bytes()+b'changed')
        target = self.root / 'attempt'; target.mkdir()
        with self.assertRaises(ValueError):
            commit_update(plan,self.db,target,self.run_id)
        self.assertFalse((target / 'f_database_before.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
