"""Offline source -> candidate -> submit guard -> independent audit regression."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import date

from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from opensms_task_c_daily_guard import guard_metadata, daily_rate
import opensms_parallel_audit as audit

GENERATOR = ROOT / 'scripts/opensms_generate_task_c_candidates.py'
HEADERS = ['电话', '运营商', 'opr', '卡槽', '4日发送窗口数', '10日发送窗口数',
           '4日回复率', '换号状态', '过期状态', '截止日']


def row(phone, opr='Kooda', today=0.01, yesterday=0.01, rate4=0.01, send10=400):
    return [phone, 'B1-测试', opr, '101_1', 50, send10, rate4, '', '', '', today, yesterday]


class DailyGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input.xlsx'

    def generate(self, rows, server='2026-09-28', headers=None):
        wb = Workbook()
        wb.active.title = 'sheet1'
        wb.active.append(HEADERS + (headers if headers is not None else guard_metadata(date.fromisoformat(server))['headers']))
        for values in rows:
            wb.active.append(values)
        wb.save(self.source)
        env = os.environ.copy()
        env['OPENSMS_FORCE_TASK_C'] = '1'
        output = self.root / f'out-{len(list(self.root.glob("out-*")))}'
        result = subprocess.run([sys.executable, str(GENERATOR), str(self.source),
                                 '--server-date', server, '--target-carrier-c', '换号区C',
                                 '--target-carrier-t', '换号区T', '--tag', 'test', '--output-dir', str(output)],
                                env=env, text=True, capture_output=True)
        return result

    def successful(self, rows, **kwargs):
        result = self.generate(rows, **kwargs)
        self.assertEqual(0, result.returncode, result.stderr)
        manifest = json.loads(result.stdout)
        candidates = [json.loads(Path(manifest[key]).read_text()) for key in ('koodaCandidatePath', 'chatrCandidatePath')]
        return manifest, candidates

    def audit_config(self, manifest):
        config = {'serverDate': manifest['serverDate'], 'taskCExecuted': True,
                  'taskCRecentDailyReplyGuardRequired': True, 'taskCManifest': manifest['manifestPath'],
                  'taskCInputWorkbook': str(self.source), 'groups': [
                      {'title': '任务C T', 'candidate': manifest['koodaCandidatePath']},
                      {'title': '任务C C', 'candidate': manifest['chatrCandidatePath']}]}
        return config, audit.PathResolver(self.root / 'config.json', self.root)

    def test_both_days_or_strict_thresholds_and_all_t_aliases(self):
        rows = []
        expected = set()
        for opr, boundary, above in [('Kooda', '5%', '5.001%'), ('Koodo', .05, .05001),
                                     ('Telus1', 5, 5.001), ('Kini', '5.0%', '6%'), ('Chatr', '4%', '4.001%')]:
            for index, (today, yesterday) in enumerate([(boundary, boundary), (above, 0), (0, above), (above, above), (0, 0)]):
                phone = str(1000 + len(rows))
                rows.append(row(phone, opr, today, yesterday))
                if index in (0, 4):
                    expected.add(phone)
        manifest, candidates = self.successful(rows)
        self.assertEqual(expected, {r['电话'] for c in candidates for r in c['rows']})
        self.assertEqual({'above_threshold': 15, 'missing_or_invalid': 0}, manifest['recentDailyReplyRateExcludedCounts'])
        config, resolver = self.audit_config(manifest)
        result = audit.check_task_c(config, resolver)
        self.assertEqual(audit.PASS, result.status, result.message)

    def test_invalid_daily_values_never_become_zero(self):
        bad = [None, '', 'bad', True, 'NaN', 'Infinity', -1, '101%', '0x01', '1_0%']
        rows = [row(str(2000+i), 'Chatr', value, 0) for i, value in enumerate(bad)]
        rows += [row(str(3000+i), 'Kooda', 0, value) for i, value in enumerate(bad)]
        manifest, candidates = self.successful(rows)
        self.assertEqual(0, sum(c['count'] for c in candidates))
        self.assertEqual(20, manifest['recentDailyReplyRateExcludedCounts']['missing_or_invalid'])
        self.assertIsNone(daily_rate(float('nan')))
        self.assertIsNone(daily_rate(float('inf')))
        self.assertEqual(0, daily_rate(0))

    def test_header_missing_duplicate_and_wrong_date_fail(self):
        for headers in (['09-27回复率', '09-26回复率'], ['09-28回复率', '09-28回复率'], ['09-28回复率']):
            with self.subTest(headers=headers):
                result = self.generate([], headers=headers)
                self.assertNotEqual(0, result.returncode)
                self.assertIn('当天/前一天', result.stderr)

    def test_cross_year_leap_day_and_server_day_not_machine_day(self):
        for server, previous in [('2026-01-01', '2025-12-31'), ('2024-03-01', '2024-02-29'), ('2026-03-01', '2026-02-28')]:
            manifest, candidates = self.successful([row('4000', today='5%', yesterday='5%')], server=server)
            self.assertEqual(previous, manifest['recentDailyReplyRateGuard']['previousDate'])
            self.assertEqual(1, candidates[0]['count'])
            result = audit.check_task_c(*self.audit_config(manifest))
            self.assertEqual(audit.PASS, result.status, result.message)

    def test_existing_four_day_and_send_rules_still_apply(self):
        manifest, candidates = self.successful([row('5001', rate4=.05), row('5002', 'Chatr', rate4=.04),
                                                row('5003', send10=360), row('5004', send10=361)])
        self.assertEqual(['5004'], [r['电话'] for r in candidates[0]['rows']])
        self.assertEqual(0, candidates[1]['count'])

    def test_protections_and_exact_server_columns_remain_authoritative(self):
        records = []
        for i, carrier in enumerate(['A21-测试', 'A2-测试', '三方隔离', '快过期续费C', '快过期续费T', '过期下架卡T', '快过期隔离区C']):
            values = row(str(9000+i))
            values[1] = carrier
            records.append(values)
        eligible = row('9999', today='5%', yesterday='5%')
        # A different day >5% must not replace either exact server-calendar day.
        records.append(eligible + ['90%'])
        manifest, candidates = self.successful(records, headers=['09-28回复率', '09-27回复率', '09-29回复率'])
        self.assertEqual(['9999'], [r['电话'] for c in candidates for r in c['rows']])
        config, resolver = self.audit_config(manifest)
        self.assertEqual(audit.PASS, audit.check_task_c(config, resolver).status)

    def test_independent_audit_rechecks_exclusion_counts_and_reasons(self):
        manifest, _ = self.successful([row('9100', today='6%'), row('9101', yesterday=None)])
        config, resolver = self.audit_config(manifest)
        path = Path(manifest['manifestPath'])
        original = path.read_text()
        for tamper in ('reason', 'count', 'source_date'):
            payload = json.loads(original)
            if tamper == 'reason':
                payload['recentDailyReplyRateExcludedRows'] = {'above_threshold': [3], 'missing_or_invalid': [2]}
            if tamper == 'count': payload['recentDailyReplyRateExcludedCount'] = 0
            if tamper == 'source_date': config['serverDate'] = '2026-09-29'
            path.write_text(json.dumps(payload))
            with self.subTest(tamper=tamper):
                self.assertEqual(audit.FAIL, audit.check_task_c(config, resolver).status)

    def test_independent_audit_rejects_forgery_and_missing_new_contract(self):
        manifest, candidates = self.successful([row('6001'), row('6002', today=.06)])
        config, resolver = self.audit_config(manifest)
        path = Path(manifest['koodaCandidatePath'])
        original = path.read_text()
        for tamper in ('high', 'blank', 'wrong_row', 'forged_low', 'missing_guard', 'wrong_date'):
            payload = json.loads(original)
            if tamper == 'high': payload['rows'][0]['09-28回复率'] = .06
            if tamper == 'blank': payload['rows'][0]['09-27回复率'] = None
            if tamper == 'wrong_row': payload['rows'][0]['源行号'] = 99
            if tamper == 'forged_low': payload['rows'][0].update({'源行号': 3, '电话': '6002'})
            if tamper == 'missing_guard': payload.pop('recentDailyReplyRateGuard')
            if tamper == 'wrong_date': payload['recentDailyReplyRateGuard']['serverDate'] = '2026-09-29'
            path.write_text(json.dumps(payload))
            with self.subTest(tamper=tamper):
                result = audit.check_task_c(config, resolver)
                self.assertEqual(audit.FAIL, result.status)
                self.assertIn('近两日', result.message)
        path.write_text(original)
        mpath = Path(manifest['manifestPath'])
        body = json.loads(mpath.read_text())
        body.pop('recentDailyReplyRateGuard')
        mpath.write_text(json.dumps(body))
        self.assertEqual(audit.FAIL, audit.check_task_c(config, resolver).status)

    def test_submit_guard_matches_generator_and_rejects_stale_candidates(self):
        manifest, candidates = self.successful([row('7001', today='5%', yesterday=5), row('7002', 'Chatr', .04, '4%')])
        for key in ('koodaCandidatePath', 'chatrCandidatePath'):
            code = """
import fs from 'node:fs';
import assert from 'node:assert/strict';
import {enforceTaskCRecentDailyReplyGuard as guard} from './opensms_set_carrier_candidates.mjs';
const original=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
guard(original);
for(const raw of [null,'',true,NaN,Infinity,'NaN','bad','101%',0.05001,'1_0%']) {
  for(const header of original.recentDailyReplyRateGuard.headers) {
    const copy=structuredClone(original); copy.rows[0][header]=raw;
    assert.throws(()=>guard(copy),/近两日回复率保护/);
  }
}
for(const mutation of [c=>delete c.recentDailyReplyRateGuard,
  c=>c.recentDailyReplyRateGuard.thresholdExclusive=0.5,
  c=>c.recentDailyReplyRateGuard.previousDate='2026-09-26',
  c=>c.serverDate='2026-02-30', c=>c.recentDailyReplyRateGuard.headers.reverse()]) {
  const copy=structuredClone(original); mutation(copy);
  assert.throws(()=>guard(copy),/近两日回复率保护/);
}
"""
            result = subprocess.run(['node', '--input-type=module', '-e', code, manifest[key]],
                                    cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)

    def test_cli_submit_blocks_before_backend_access(self):
        manifest, candidates = self.successful([row('8001')])
        path = Path(manifest['koodaCandidatePath'])
        body = json.loads(path.read_text())
        body['rows'][0]['09-28回复率'] = '6%'
        body['candidates'] = body['rows']
        path.write_text(json.dumps(body))
        env = os.environ.copy()
        env.update(OPENSMS_FORCE_TASK_C='1', BITBROWSER_CDP='http://127.0.0.1:1',
                   OPENSMS_EXACT_CDP_ONLY='1', OPENSMS_FORCE_RAW_CDP='1', OPENSMS_RAW_API_TARGET_ID='offline-guard')
        result = subprocess.run(['node', str(ROOT/'opensms_set_carrier_candidates.mjs'), str(path), '换号区T', 'offline_daily_guard'],
                                cwd=ROOT, env=env, text=True, capture_output=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('近两日回复率保护', result.stderr)


if __name__ == '__main__':
    unittest.main()
