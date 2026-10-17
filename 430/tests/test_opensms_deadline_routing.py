"""Offline regression for the September 22 / October 8 premature-routing bug."""
from contextlib import redirect_stdout
from datetime import date, datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook
from openpyxl.utils.datetime import to_excel, CALENDAR_WINDOWS_1900

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import opensms_generate_task_b_candidates as generator
import opensms_review_expiry_carriers as reviewer


class DeadlineRoutingTests(unittest.TestCase):
    def test_cutoff_not_age_status_or_separator_drives_date(self):
        for value in ['2026/9/22', '2026-09-22', '2026.9.22', '2026年9月22日',
                      datetime(2026, 9, 22), date(2026, 9, 22), to_excel(datetime(2026, 9, 22))]:
            with self.subTest(value=value):
                cutoff = generator.parse_date(value, CALENDAR_WINDOWS_1900)
                self.assertEqual(date(2026, 9, 22), cutoff)
                self.assertEqual(8, (cutoff - date(2026, 9, 14)).days)
                self.assertEqual(7, (cutoff - date(2026, 9, 15)).days)
                self.assertEqual('', generator.deadline_status(cutoff, date(2026, 9, 14)))
        for value in [True, False, 0.1, '=TODAY()', 'abc2026-09-22', '2026-09-220', '2026-02-30', 'x']:
            with self.subTest(value=value):
                self.assertIsNone(generator.parse_date(value, CALENDAR_WINDOWS_1900))

    def test_boundaries(self):
        reference = date(2026, 9, 14)
        from datetime import timedelta
        for delta, status in [(-1, '已过期'), (0, '已过期'), (1, '快过期'), (2, '快过期'),
                              (3, '6天'), (6, '6天'), (7, ''), (8, ''), (24, '')]:
            self.assertEqual(status, generator.deadline_status(reference + timedelta(days=delta), reference))

    def test_stale_labels_cannot_create_future_expired_or_isolation_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'synthetic.xlsx'
            book = Workbook()
            ws = book.active
            ws.title = 'sheet1'
            ws.append(['电话', '运营商', 'opr', '卡槽', '过期状态', '截止日', '激活后天数'])
            for i, cutoff in enumerate(['2026/9/22', '2026-10-08', '2026-09-17', '2026-09-16']):
                ws.append([str(1000+i), '快过期隔离区T', 'Koodo', f'141_{i+1}', '已过期', cutoff, 999])
            ws.append(['2000', 'B1', 'Koodo', '141_5', '', '2026-09-15', 0])
            ws.append(['2001', 'B1', 'Koodo', '141_6', '快过期', '2026-09-14', 0])
            book.save(source)
            book.close()
            argv = ['generator', str(source), '--server-date', '2026-09-14', '--tag', 'synthetic', '--output-dir', str(root)]
            with patch.object(sys, 'argv', argv), patch.dict(os.environ, {'OPENSMS_FORCE_TASK_B': '1'}), redirect_stdout(io.StringIO()) as out:
                generator.main()
            manifest = json.loads(out.getvalue())
            self.assertEqual(6, manifest['expiryStatusRecomputedCount'])
            expired = json.loads(Path(manifest['expiredCandidatePaths']['T']).read_text())
            tomorrow = json.loads(Path(manifest['tomorrowCandidatePaths']['T']).read_text())
            self.assertEqual(['2001'], [row['电话'] for row in expired['rows']])
            self.assertEqual(['2000'], [row['电话'] for row in tomorrow['rows']])
            self.assertEqual(0, expired['rows'][0]['距截止日天数'])
            self.assertEqual(1, tomorrow['rows'][0]['距截止日天数'])
            self.assertEqual('已过期', expired['rows'][0]['过期状态'])
            report = reviewer.review(source, '2026-09-14')
            self.assertEqual(4, report['prematureCarrierCount'])
            self.assertEqual([8, 24, 3, 2], [item['remainingDays'] for item in report['rows']])
            self.assertEqual(0, report['businessWrites'])
            self.assertIn('2026-09-22', '\n'.join(reviewer.report_lines(report)))
            # Independent source audit must use cutoff as well, not share generator logic.
            import opensms_parallel_audit as audit
            config_path = root / 'config.json'
            config_path.write_text('{}')
            errors, details = audit.review_task_b_source_coverage(
                {'serverDate': '2026-09-14'}, manifest,
                [{'candidate': path} for path in manifest['candidatePaths']],
                audit.PathResolver(config_path, root))
            self.assertEqual([], errors)
            self.assertTrue(details['source_coverage_independently_verified'])
            # A forged old-label candidate must fail the independently rebuilt partition.
            bad = {**expired['rows'][0], '电话': '1000', '源行号': 2, '截止日': '2026-09-22'}
            expired['rows'].append(bad)
            expired['candidates'] = expired['rows']
            Path(manifest['expiredCandidatePaths']['T']).write_text(json.dumps(expired, ensure_ascii=False))
            errors, _ = audit.review_task_b_source_coverage(
                {'serverDate': '2026-09-14'}, manifest,
                [{'candidate': path} for path in manifest['candidatePaths']],
                audit.PathResolver(config_path, root))
            self.assertTrue(any('未精确覆盖' in error for error in errors))
            import opensms_finalize_patrol_report as finalizer
            before = generator.sha256(source)
            lines = finalizer.final_expiry_carrier_lines({'cardResult': str(source), 'serverDate': '2026-09-14'})
            self.assertIn('2026-09-22 | 8', '\n'.join(lines))
            self.assertIn('4行', '\n'.join(lines))
            self.assertEqual(before, generator.sha256(source))
            self.assertIn('未核验', '\n'.join(finalizer.final_expiry_carrier_lines({})))

    def test_missing_and_invalid_cutoff_fail_before_publishing_any_candidate(self):
        for cutoff in [None, '', False, '2026/2/30', '=TODAY()']:
            with self.subTest(cutoff=cutoff), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                book = Workbook()
                book.active.title = 'sheet1'
                book.active.append(['电话', '运营商', 'opr', '卡槽', '过期状态', '截止日'])
                book.active.append(['1001', 'B1', 'Koodo', '141_1', '已过期', cutoff])
                source = root / 'synthetic.xlsx'
                book.save(source)
                book.close()
                argv = ['generator', str(source), '--server-date', '2026-09-14', '--tag', 'synthetic', '--output-dir', str(root)]
                with patch.object(sys, 'argv', argv), patch.dict(os.environ, {'OPENSMS_FORCE_TASK_B': '1'}):
                    with self.assertRaisesRegex(ValueError, '截止日缺失或无效'):
                        generator.main()
                self.assertEqual([], list(root.glob('*candidates.json')))

    def test_node_submission_guard_rejects_future_cutoff_without_opening_browser(self):
        completed = subprocess.run(['node', '--input-type=module', '-e', '''
import assert from 'node:assert/strict';
import {enforceTaskBDeadlineGate as gate} from './opensms_set_carrier_candidates.mjs';
const base={task:'B',forceGate:true,serverDate:'2026-09-14',tomorrowDate:'2026-09-15',
 route:'task_b_expired_to_offline_t',targetCarrierName:'过期下架卡T',
 rows:[{'电话':'1000','截止日':'2026-09-14','过期状态':'已过期'}]};
gate(base,base.targetCarrierName);
for(const cutoff of ['2026-09-15','2026-09-17','2026-09-22','2026-10-08','2026-02-30','',null,44900,'2026-09-14junk']) {
 assert.throws(()=>gate({...base,rows:[{...base.rows[0],'截止日':cutoff}]},base.targetCarrierName),/截止日/);
}
const fast={...base,route:'task_b_tomorrow_fast_to_isolation_t',targetCarrierName:'快过期隔离区T',
 rows:[{'电话':'1000','截止日':'2026-09-15','过期状态':'快过期'}]};
gate(fast,fast.targetCarrierName);
for(const cutoff of ['2026-09-14','2026-09-16','2026-09-22','2026-10-08']) {
 assert.throws(()=>gate({...fast,rows:[{...fast.rows[0],'截止日':cutoff}]},fast.targetCarrierName),/截止日/);
}
assert.throws(()=>gate({...base,serverDate:'2026-02-30'},base.targetCarrierName));
assert.throws(()=>gate({...fast,candidates:[{...fast.rows[0],'截止日':'2026-09-22'}]},fast.targetCarrierName));
assert.throws(()=>gate({...fast,task:'RESTORE',route:'e_restore'},fast.targetCarrierName));
gate({route:'a_zone_gte4_to_a1',rows:[]},'A1');
'''], cwd=SCRIPTS.parent, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == '__main__':
    unittest.main()
