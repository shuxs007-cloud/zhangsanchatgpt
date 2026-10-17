import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
import io
import sys

from scripts.opensms_v_swap_daily import (
    TZ, aggregate, export_daily, load, record_verified_group, verify_summary,
)
from scripts.opensms_report_summary import compact_phone_tables
from scripts.opensms_parallel_audit import PathResolver, check_v_group_boundary, check_v_group_daily

ROUTE = 'task_c_kooda_4day_reply_rate_lt5_to_swap'


class VDailyTests(unittest.TestCase):
    def test_generator_never_sends_v_to_rest_even_if_global_switch_reopens(self):
        import opensms_generate_opr_zero_candidates as generator
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(['电话', '运营商', 'opr', '卡槽', '状态', '10日发送窗口数',
                          '09-17发送窗口数', '09-17回复窗口数', '09-16发送窗口数', '09-16回复窗口数'])
            for i, key in enumerate(['b1v', 'b11v', 'b12v', 'b1'], 1):
                sheet.append([f'1416000000{i}', generator.DEFAULT_CARRIERS[key], 'Telus1', f'101_{i}', '上架', 300, 100, 0, 100, 1])
            source = root / 'source.xlsx'
            workbook.save(source)
            output = io.StringIO()
            with patch.object(generator, 'REST_ZONE_ENTRY_ENABLED', True), \
                    patch.object(sys, 'argv', ['generator', str(source), '--out-dir', str(root / 'candidates'),
                                              '--now-bj', '2026-09-17T22:00:00+08:00']), redirect_stdout(output):
                generator.main()
            manifest = json.loads(output.getvalue())
            zero = load(manifest['outputs']['b1_b11_b12_today_gte50_zero_to_e']['path'])
            self.assertEqual([row['phone'] for row in zero['rows']], ['14160000004'])
            self.assertFalse(manifest['vZoneRestEntryEnabled'])
            self.assertFalse(any('V-' in name for name in zero['sourceCarrierNames']))
            for route in ['b1v_b11v_use2_lt240_to_b11v', 'b1v_b11v_gte240_to_b12v']:
                self.assertEqual(load(manifest['outputs'][route]['path'])['count'], 0)

    def fixture(self, root, *, name='one', source='B1V-测试', number='14160000001', stamp='2026-09-17T16:00:01Z'):
        paths = {key: root / f'{name}-{key}.json' for key in ('write', 'verify', 'validated')}
        candidate = {'route': ROUTE, 'task': 'C', 'forceGate': True, 'targetCarrierName': '换号区T', 'count': 1,
                     'rows': [{'电话': number, '后台ID': 7, '运营商': source, '精确原运营商': source, '原运营商ID': 'from'}]}
        written = {'ts': stamp, 'status': 'written', 'actualWriteCount': 1,
                   'sourceCarriers': [{'name': source, 'identif': 'from'}],
                   'target': {'name': '换号区T', 'identif': 'target'},
                   'before': {'eligibleCount': 1, 'eligibleRows': [{'id': 7, 'cardNo': number[-10:], 'carrireIdentif': 'from'}]}}
        verified = {'source': str(paths['validated'].resolve()), 'checkedAt': stamp, 'passed': True,
                    'independentReadbackPerformed': True, 'total': 1, 'targetCount': 1,
                    'missingCount': 0, 'stillNotTargetCount': 0, 'duplicateBackendCount': 0,
                    'target': {'name': '换号区T', 'identif': 'target'}}
        for key, value in [('validated', candidate), ('write', written), ('verify', verified)]:
            paths[key].write_text(json.dumps(value), encoding='utf-8')
        return {'route': ROUTE, 'result': 'passed', 'actualWrite': 1, 'independentReadbackPerformed': True,
                **{key: str(path) for key, path in paths.items()}}

    def test_verified_only_idempotency_actual_source_and_beijing_date(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            group = self.fixture(root)
            path = record_verified_group(root / 'journal', 'run1', group)
            self.assertEqual(record_verified_group(root / 'journal', 'run1', group), path)
            saved = load(path)
            self.assertEqual(saved['date'], '2026-09-18')
            self.assertEqual(saved['events'][0]['sourceGroup'], 'B1V')
            self.assertEqual(saved['events'][0]['phone'], '4160000001')
            self.assertEqual(len(list((root / 'journal').glob('event_*.json'))), 1)
            verified = load(group['verify'])
            verified['passed'] = False
            Path(group['verify']).write_text(json.dumps(verified), encoding='utf-8')
            with self.assertRaises(ValueError):
                record_verified_group(root / 'journal', 'failed-run', group)

    def test_dedup_across_runs_and_source_groups_and_xlsx_readback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = self.fixture(root, name='one')
            second = self.fixture(root, name='two', source='B11V-测试', number='4160000001')
            standard = self.fixture(root, name='three', source='B1-测试', number='4160000002')
            for index, group in enumerate([first, second, standard]):
                record_verified_group(root / 'journal', f'run{index}', group)
            result = export_daily(root / 'journal', 'report', root, root,
                                  now=datetime(2026, 9, 18, 8, tzinfo=TZ))
            self.assertEqual(result['today'], {'date': '2026-09-18', 'B1V': 1, 'B11V': 1, 'B12V': 0, 'unique': 1, 'events': 2})
            verify_summary(result)
            self.assertEqual(check_v_group_daily({'vGroupSwapDaily': result}).status, 'passed')
            altered = copy.deepcopy(result)
            altered['rows'][0]['B1V'] = 99
            with self.assertRaises(ValueError):
                verify_summary(altered)

    def test_wrong_identity_noop_unknown_and_wrong_target_never_count(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for key, patch in [('verify', {'total': 2}), ('write', {'status': 'unknown'}),
                               ('validated', {'rows': [{'电话': '4160000001', '后台ID': 9}]}),
                               ('verify', {'target': {'name': '换号区C', 'identif': 'target'}})]:
                group = self.fixture(root)
                payload = load(group[key]); payload.update(patch)
                Path(group[key]).write_text(json.dumps(payload), encoding='utf-8')
                with self.assertRaises(ValueError):
                    record_verified_group(root / 'journal', 'run', group)
            self.assertIsNone(record_verified_group(root / 'journal', 'noop', {'route': ROUTE, 'actualWrite': 0}))
            self.assertFalse(list((root / 'journal').glob('event_*.json')))

    def test_empty_today_and_distinct_cross_days(self):
        event = {'phone': '4160000001', 'sourceGroup': 'B12V'}
        rows = aggregate([{'date': '2026-09-17', 'events': [event]},
                          {'date': '2026-09-18', 'events': [event]}], '2026-09-19')
        self.assertEqual([row['unique'] for row in rows], [0, 1, 1])

    def test_boundary_audit_uses_live_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            group = self.fixture(root)
            group['candidate'] = group['validated']
            group['target'] = 'B12-标准'
            resolver = PathResolver(root, None)
            self.assertEqual(check_v_group_boundary({'groups': [group]}, resolver).status, 'failed')
            group['target'] = '换号区T'
            self.assertEqual(check_v_group_boundary({'groups': [group], 'taskCExecuted': True}, resolver).status, 'passed')


class CompactReportTests(unittest.TestCase):
    def test_omit_identifier_tables_keep_counts_errors_and_links(self):
        lines = ['# 报告', '成功 2；失败 1；未确认 3。', '## 逐号结果',
                 '| 电话 | 状态 |', '| --- | --- |', '| 14160000001 | 成功 |', '| 14160000002 | 失败 |',
                 '', '| 系统ICCID | Google ICCID |', '| --- | --- |', '| 8900000000000000001 | 8900000000000000002 |',
                 '', '| 运营商 | 号码数量 |', '| --- | ---: |', '| B1V | 2 |',
                 '', '| 保留卡区 | 本轮新转入号码 | 执行后区内全部号码 |', '| --- | ---: | ---: |', '| B14-T | 0 | 183 |',
                 '', '[原始回执](</receipts/file.json>)']
        result = '\n'.join(compact_phone_tables(lines))
        self.assertNotIn('14160000001', result)
        self.assertNotIn('8900000000000000001', result)
        self.assertIn('成功 2；失败 1；未确认 3', result)
        self.assertIn('| B1V | 2 |', result)
        self.assertIn('| B14-T | 0 | 183 |', result)
        self.assertIn('/receipts/file.json', result)
        self.assertIn('明细记录：2 条', result)
        self.assertIn('明细记录：1 条', result)


if __name__ == '__main__':
    unittest.main()
