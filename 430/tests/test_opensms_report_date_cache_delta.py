"""Offline immutable report snapshot and truly incremental Task F Excel tests."""
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from opensms_report_date_cache import freeze
from opensms_task_f_delta import collect_updates, export_delta
from opensms_export_task_f_deadlines import valid_date, verify_output


class CacheDeltaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='opensms-dates-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'dates.sqlite3'
        self.iccid = '8912230200251591144'
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE kooda_activation (query_iccid TEXT PRIMARY KEY, '
                         'activation_date TEXT,cutoff_date TEXT,lookup_status TEXT)')

    def seed(self, iccid=None, activation='2026-09-01', cutoff='2026-09-30', status='found'):
        with sqlite3.connect(self.db) as conn:
            conn.execute('INSERT INTO kooda_activation VALUES (?,?,?,?)',
                         (iccid or self.iccid, activation, cutoff, status))

    def receipt(self, rows):
        path = self.root / 'import.json'
        lookup = self.root / 'lookup.json'
        lookup.write_text(json.dumps({'runId': 'run', 'results': rows}))
        path.write_text(json.dumps({'status': 'passed', 'database': str(self.db), 'rows': rows,
                                   'runId': 'run', 'lookupReceipt': str(lookup),
                                   'lookupSha256': hashlib.sha256(lookup.read_bytes()).hexdigest(),
                                   'activationDateUpdatedCount': sum(x.get('activationDateUpdated') is True for x in rows)}))
        return path

    def row(self, updated=True):
        return {'queryIccid': self.iccid + 'F', 'status': 'found', 'activationDate': '2026-09-01',
                'cutoffDate': '2026-09-30', 'activationDateUpdated': updated}

    def test_freeze_is_immutable_and_next_snapshot_sees_updates(self):
        self.seed()
        self.seed('8912230200251591152', None, None, 'activation_missing')
        path = self.root / 'frozen.json'
        before_db = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = freeze(self.db, path, 'run-1')
        frozen_bytes = path.read_bytes()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before_db)
        self.assertEqual(result['recordCount'], 1)
        self.assertEqual(result['excludedWithoutValidDates'], 1)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE kooda_activation SET activation_date='2026-09-02',"
                         "cutoff_date='2026-10-01',lookup_status='found' WHERE activation_date IS NULL")
        self.assertEqual(path.read_bytes(), frozen_bytes)
        self.assertEqual(freeze(self.db, self.root / 'next.json', 'run-2')['recordCount'], 2)
        with self.assertRaises(FileExistsError):
            freeze(self.db, path, 'run-1')

    def test_missing_db_empty_snapshot_does_not_create_db(self):
        missing = self.root / 'missing' / 'date.sqlite3'
        result = freeze(missing, self.root / 'empty.json', 'run')
        self.assertFalse(missing.exists())
        self.assertFalse(result['sourceExists'])
        self.assertEqual(result['recordCount'], 0)

    def test_invalid_pair_fails_before_publish(self):
        self.seed(cutoff='2026-10-01')
        with self.assertRaises(ValueError):
            freeze(self.db, self.root / 'invalid.json', 'run')
        self.assertFalse((self.root / 'invalid.json').exists())

    def test_normalized_duplicate_fails(self):
        self.seed()
        self.seed(self.iccid + 'F')
        with self.assertRaisesRegex(ValueError, '重复'):
            freeze(self.db, self.root / 'invalid.json', 'run')

    def test_new_rows_only_excel_and_no_old_rows(self):
        self.seed()
        self.seed('8912230200251591152')
        source = self.receipt([self.row(), {'queryIccid': '8912230200251591152', 'activationDateUpdated': False}])
        output = self.root / 'delta.xlsx'
        result = export_delta(self.db, [source], output, self.root / 'delta.json', 'run')
        self.assertTrue(result['generated'])
        self.assertEqual(result['recordCount'], 1)
        verify_output(output, [[self.iccid, '2026-09-30', '2026-09-01']])

    def test_no_updates_no_excel_and_no_db_required(self):
        source = self.receipt([self.row(False)])
        result = export_delta(self.db, [source], self.root / 'no.xlsx', self.root / 'no.json', 'run')
        self.assertFalse(result['generated'])
        self.assertIsNone(result['output'])
        self.assertFalse((self.root / 'no.xlsx').exists())

    def test_changed_receipt_date_or_identity_cannot_export(self):
        self.seed()
        row = self.row()
        row['activationDate'] = '2026-09-02'
        row['cutoffDate'] = '2026-10-01'
        source = self.receipt([row])
        with self.assertRaisesRegex(ValueError, '不一致'):
            export_delta(self.db, [source], self.root / 'bad.xlsx', self.root / 'bad.json', 'run')
        self.assertFalse((self.root / 'bad.xlsx').exists())

    def test_legacy_receipt_cannot_masquerade_as_increment(self):
        source = self.receipt([self.row()])
        content = json.loads(source.read_text())
        del content['activationDateUpdatedCount']
        source.write_text(json.dumps(content))
        with self.assertRaisesRegex(ValueError, '计数'):
            collect_updates(self.db, [source], 'run')

    def test_duplicate_receipt_rejected(self):
        source = self.receipt([self.row()])
        with self.assertRaisesRegex(ValueError, '重复'):
            collect_updates(self.db, [source, source], 'run')

    def test_prior_run_or_changed_lookup_cannot_be_new_delta(self):
        source = self.receipt([self.row()])
        with self.assertRaisesRegex(ValueError, 'runId'):
            collect_updates(self.db, [source], 'new-run')
        (self.root / 'lookup.json').write_text(json.dumps({'runId': 'other-run'}))
        with self.assertRaisesRegex(ValueError, '哈希'):
            collect_updates(self.db, [source], 'run')

    def test_mixed_old_row_absent_from_current_lookup_is_rejected(self):
        source = self.receipt([self.row()])
        payload = json.loads(source.read_text())
        payload['rows'][0]['queryIccid'] = '8912230200251591152'
        source.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, '查询结果证明'):
            collect_updates(self.db, [source], 'run')


if __name__ == '__main__':
    unittest.main()
