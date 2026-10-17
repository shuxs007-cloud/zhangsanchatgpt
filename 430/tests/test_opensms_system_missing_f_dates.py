import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from opensms_system_missing_f_dates import select_rows, read_frozen_dates, digest
from opensms_chat_summary import render_summary

class DifferenceTests(unittest.TestCase):
    def test_frozen_dates_exclude_later_f_results_and_reject_changed_source(self):
        import json, tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dates.json'
            payload = {'runId': 'current', 'status': 'passed', 'readOnly': True,
                       'snapshotConsistent': True, 'recordCount': 1,
                       'records': [{'queryIccid': '8912230200278137092',
                                    'activationDate': '2026-09-01', 'cutoffDate': '2026-09-30'}]}
            path.write_text(json.dumps(payload))
            cache = {'path': str(path), 'sha256': digest(path), 'runId': 'current',
                     'frozenBeforeDocument': True}
            with patch('opensms_system_missing_f_dates.read_database', side_effect=AssertionError('no live DB')):
                self.assertEqual(list(read_frozen_dates(cache, 'current')), ['8912230200278137092'])
            with self.assertRaises(ValueError):
                read_frozen_dates(cache, 'other')
            path.write_text('{}')
            with self.assertRaises(ValueError):
                read_frozen_dates(cache, 'current')

    def test_frozen_dates_reject_duplicate_and_invalid_date_pairs(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dates.json'
            row = {'queryIccid': '8912230200278137092', 'activationDate': '2026-09-01',
                   'cutoffDate': '2026-09-30'}
            for rows in ([row, row], [{**row, 'cutoffDate': '2026-10-01'}]):
                payload = {'runId': 'current', 'status': 'passed', 'readOnly': True,
                           'snapshotConsistent': True, 'recordCount': len(rows), 'records': rows}
                path.write_text(json.dumps(payload))
                cache = {'path': str(path), 'sha256': digest(path), 'runId': 'current',
                         'frozenBeforeDocument': True}
                with self.assertRaises(ValueError):
                    read_frozen_dates(cache, 'current')
    def test_download_only_for_verified_nonempty_current_data(self):
        for count, verified, expected in ((0, True, False), (1, True, True),
                                          (1, False, False), (None, True, False),
                                          (True, True, False)):
            with self.subTest(count=count, verified=verified):
                result = {'status': 'success', 'systemMissingFDates': {
                    'formatVersion': 'task_f_deadline_template_v2',
                    'output': '/current-difference.xlsx', 'exportedRowCount': count,
                    'rowCount': 1, 'independentReadbackPassed': verified}}
                self.assertEqual('/current-difference.xlsx' in render_summary(result), expected)

    def test_exact_scope_and_duplicate_source_rows(self):
        key='8912230200278137092'
        record={'lookup_status':'found','activation_date':'2026-09-01','cutoff_date':'2026-09-30'}
        rows=[[key+'F',None],[key,''],[key,'2026-09-02'],['8912230200278137093',None]]
        found=select_rows(['ICCID','激活日期'],rows,{key:record})
        self.assertEqual(len(found),2)
        self.assertEqual({item[1] for item in found},{key})
    def test_invalid_local_date_not_exported(self):
        key='8912230200278137092'
        for status in ('not_found','activation_missing'):
            self.assertEqual(select_rows(['ICCID','激活日期'],[[key,None]],{key:{'lookup_status':status}}),[])
    def test_nonblank_invalid_source_date_is_not_missing(self):
        with self.assertRaises(ValueError):
            select_rows(['ICCID','激活日期'],[['8912230200278137092','bad']],{})
    def test_pipeline_and_delivery_hook(self):
        root=Path(__file__).resolve().parents[1]
        runner=(root/'scripts/opensms_patrol_runner.py').read_text()
        self.assertIn('acquire["sourceArchive"], acquire["sourceSha256"]',runner)
        self.assertIn('"systemMissingFDates": self.state.get("systemMissingFDates")',runner)
        self.assertIn('difference[\'output\']',(root/'scripts/opensms_chat_summary.py').read_text())
