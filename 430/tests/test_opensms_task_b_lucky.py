"""Synthetic, offline regression for the user-confirmed Lucky Task B routing."""
from contextlib import redirect_stdout
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import opensms_generate_task_b_candidates as generator
import opensms_parallel_audit as audit


class TaskBLuckyTests(unittest.TestCase):
    def test_alias_boundaries_and_ambiguity(self):
        for opr in ['302610 Lucky', 'Lucky', 'lucky mobile', 'LUCKY']:
            self.assertEqual('T', generator.classify_opr(opr))
        for opr in ['unlucky', 'LuckyOther', 'luckly', 'unknown', '302610']:
            self.assertEqual('unclassified', generator.classify_opr(opr))
        self.assertEqual('ambiguous', generator.classify_opr('Chatr Lucky'))

    def test_both_branches_protection_and_independent_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'synthetic.xlsx'
            book = Workbook()
            sheet = book.active
            sheet.title = 'sheet1'
            sheet.append(['电话', '运营商', 'opr', '卡槽', '过期状态', '截止日'])
            sheet.append(['1001', 'B12-test', '302610 Lucky', '101_1', '', '2026-09-26'])
            sheet.append(['1002', 'B1V-test', 'Lucky Mobile', '101_2', '', '2026-09-27'])
            sheet.append(['1003', 'B12-test', 'Lucky', '101_3', '', '2026-09-28'])
            for index, carrier in enumerate(['快过期续费C', '快过期续费T',
                                            'B14-C快下保留卡', 'B14-T快下保留卡', '三方隔离']):
                sheet.append([str(2000 + index), carrier, 'Lucky', f'102_{index+1}', '', '2026-09-26'])
            book.save(source)
            book.close()
            args = ['generator', str(source), '--server-date', '2026-09-26',
                    '--tag', 'synthetic', '--output-dir', str(root)]
            with patch.object(sys, 'argv', args), patch.dict(os.environ, {'OPENSMS_FORCE_TASK_B': '1'}), redirect_stdout(io.StringIO()) as out:
                generator.main()
            manifest = json.loads(out.getvalue())
            self.assertEqual({'C': 0, 'T': 6}, manifest['expiredCandidateCounts'])
            self.assertEqual({'C': 0, 'T': 1}, manifest['tomorrowCandidateCounts'])
            self.assertEqual('T', manifest['oprTargetRouting']['Lucky'])
            self.assertEqual(0, manifest['retainedSourceExcludedCount'])
            self.assertEqual(0, manifest['neverProcessSourceExcludedCount'])
            self.assertEqual(0, manifest['isolationCarrierHeldCount'])
            resolver = audit.PathResolver(root / 'config.json', root)
            groups = [{'candidate': path} for path in manifest['candidatePaths']]
            errors, details = audit.review_task_b_source_coverage({'serverDate': '2026-09-26'}, manifest, groups, resolver)
            self.assertEqual([], errors)
            self.assertTrue(details['source_coverage_independently_verified'])
            # Forging the target must fail the independent audit.
            path = Path(manifest['tomorrowCandidatePaths']['T'])
            payload = json.loads(path.read_text())
            payload['targetCarrierName'] = '快过期隔离区C'
            path.write_text(json.dumps(payload))
            errors, _ = audit.review_task_b_source_coverage({'serverDate': '2026-09-26'}, manifest, groups, resolver)
            self.assertTrue(any('目标不一致' in error for error in errors))

    def test_submission_guards_before_browser_and_live_opr(self):
        result = subprocess.run(['node', '--input-type=module', '-e', r'''
import assert from 'node:assert/strict';
import {enforceTaskBLuckyRouting as gate} from './opensms_set_carrier_candidates.mjs';
for (const [route,target] of [['task_b_expired_to_offline_t','过期下架卡T'],
                            ['task_b_tomorrow_fast_to_isolation_t','快过期隔离区T']]) {
  const row={'电话':'1001',opr:'302610 Lucky'};
  const c={task:'B',route,targetCarrierName:target,rows:[row],candidates:[row]};
  gate(c,target);
  gate(c,target,[{cardNo:'1001',opr:'302610 Lucky'}]);
  assert.throws(()=>gate({...c,targetCarrierName:'过期下架卡C'},'过期下架卡C'),/Lucky/);
  assert.throws(()=>gate({...c,rows:[{...row,opr:'Chatr Lucky'}]},target),/歧义/);
  for (const opr of ['Chatr','unknown','',null]) {
    assert.throws(()=>gate(c,target,[{cardNo:'1001',opr}]),/写前/);
  }
  assert.throws(()=>gate(c,target,[]),/写前/);
  assert.throws(()=>gate(c,target,[{cardNo:'1001',opr:'Lucky'},{cardNo:'1001',opr:'Lucky'}]),/写前/);
  const wrong={...c,route:route.replace(/t$/,'c'),targetCarrierName:target.replace(/T$/,'C'),rows:[{...row,opr:'Chatr'}],candidates:[]};
  assert.throws(()=>gate(wrong,wrong.targetCarrierName,[{cardNo:'1001',opr:'Lucky'}]),/Lucky/);
}
gate({task:'C',route:'task_c_example',rows:[{opr:'Lucky'}]},'换号区C');
'''], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == '__main__':
    unittest.main()
