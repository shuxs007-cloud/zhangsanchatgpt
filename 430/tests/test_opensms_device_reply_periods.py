"""Offline fixtures for display-only device periods and immutable MD delivery."""
import copy
from datetime import date, timedelta
import json
from pathlib import Path
import pickle
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import opensms_device_reply_periods as m
import opensms_scope_comparison as scope
from opensms_export_scan_cache import build_scan_cache, sha256
from opensms_report_summary import compact_phone_tables


def fixture(server_date='2026-10-14'):
    today = date.fromisoformat(server_date)
    days = [(today - timedelta(days=i)).isoformat() for i in range(8)]
    headers = ['卡槽'] + [day[5:] + field for day in days for field in m.FIELDS]
    rows = [
        ['101_1', *[value for _ in days for value in (100, 1)]],
        ['101_2', *[value for _ in days for value in (10, 5)]],
        ['r101_1', *[value for _ in days for value in (20, 0)]],
        ['130_65', *[value for _ in days for value in (0, 0)]],
        [None, *[value for _ in days for value in (5, 1)]],
    ]
    return headers, rows


class DeviceReplyPeriodsTests(unittest.TestCase):
    def test_room7_today_yesterday_previous7_and_prefix_identity(self):
        headers, rows = fixture('2026-10-16')
        template = rows[0]
        rows = [[slot, *template[1:]] for slot in ('94_1', '100_1', '101_1', 'r94_1')]
        result = m.aggregate(headers, rows, '2026-10-16')
        devices = {row['device']: row for row in result['rows']}
        for device in ('94', '100'):
            self.assertEqual(devices[device]['room'], '7号机房')
            for period in ('today', 'yesterday', 'previous7'):
                self.assertEqual(devices[device]['periods'][period]['ratePct'], 1)
        self.assertEqual(devices['101']['room'], '2号机房')
        self.assertEqual(devices['r94']['room'], '未配置')

    def test_weighted_periods_and_all_devices(self):
        headers, rows = fixture()
        result = m.aggregate(headers, rows, '2026-10-14')
        self.assertEqual(result['deviceCount'], 3)
        self.assertEqual(result['sourceRows'], 5)
        self.assertEqual(result['unrecognizedDeviceRows'], 1)
        self.assertEqual([r['device'] for r in result['rows']], ['101', '130', 'r101', m.UNKNOWN_DEVICE])
        device = result['rows'][0]
        self.assertEqual(device['room'], '2号机房')
        self.assertEqual(device['recordCount'], 2)
        self.assertEqual(device['periods']['previous7']['send'], 770)
        self.assertEqual(device['periods']['previous7']['reply'], 42)
        self.assertAlmostEqual(device['periods']['today']['ratePct'], 6 / 110 * 100)
        self.assertAlmostEqual(device['periods']['previous7']['ratePct'], 42 / 770 * 100)
        self.assertIsNone(result['rows'][1]['periods']['today']['ratePct'])
        self.assertEqual(result['rows'][2]['periods']['today']['ratePct'], 0)
        self.assertEqual(result['rows'][2]['room'], '未配置')

    def test_previous7_excludes_today_and_crosses_year(self):
        headers, rows = fixture('2026-01-03')
        rows[0][1:3] = [1000, 200]
        result = m.aggregate(headers, rows[:1], '2026-01-03')
        self.assertEqual(result['periods']['previous7'][0], '2025-12-27')
        self.assertEqual(result['periods']['previous7'][-1], '2026-01-02')
        self.assertEqual(result['rows'][0]['periods']['previous7']['send'], 700)
        self.assertEqual(result['rows'][0]['periods']['previous7']['reply'], 7)
        self.assertEqual(result['rows'][0]['periods']['today']['ratePct'], 20)

    def test_any_invalid_count_makes_only_affected_period_unknown(self):
        headers, rows = fixture()
        for invalid in (None, '', ' ', True, -1, 0.5, 'nan', float('inf'), 'oops'):
            with self.subTest(invalid=invalid):
                changed = copy.deepcopy(rows)
                changed[0][5] = invalid  # D-2 sends
                result = m.aggregate(headers, changed, '2026-10-14')['rows'][0]['periods']
                self.assertIsNotNone(result['today']['ratePct'])
                self.assertIsNotNone(result['yesterday']['ratePct'])
                self.assertIsNone(result['previous7']['send'])
                self.assertIsNone(result['previous7']['ratePct'])
                self.assertEqual(result['previous7']['invalidCountCells'], 1)
                self.assertIn('无效', result['previous7']['unknownReason'])

    def test_missing_day_duplicate_column_and_short_row(self):
        headers, rows = fixture()
        result = m.aggregate(headers[:-2], [r[:-2] for r in rows], '2026-10-14')['rows'][0]['periods']
        self.assertIsNone(result['previous7']['ratePct'])
        self.assertEqual(result['previous7']['missingColumns'], ['10-07发送窗口数', '10-07回复窗口数'])
        self.assertIsNotNone(result['today']['ratePct'])
        with self.assertRaisesRegex(ValueError, '重复'):
            m.aggregate(headers + [headers[1]], rows, '2026-10-14')
        short = m.aggregate(headers, [['101_1', 100]], '2026-10-14')['rows'][0]['periods']
        self.assertIsNone(short['today']['ratePct'])
        self.assertIn('缺失', short['today']['unknownReason'])

    def test_text_counts_true_zero_and_source_rates_not_used(self):
        headers, rows = fixture()
        headers += ['10-14回复率', '10-14总回复量']
        row = ['101_1', *[v for _ in range(8) for v in ('1,000', '0')], 99, 999999]
        result = m.aggregate(headers, [row], '2026-10-14')['rows'][0]['periods']
        self.assertEqual(result['today']['ratePct'], 0)
        self.assertEqual(result['previous7']['send'], 7000)

    def test_render_and_compaction_keep_each_device_and_seven_sections(self):
        headers, rows = fixture()
        summary = m.build(headers, rows, 'fixture', '2026-10-14', 'a' * 64, '2026-10-15T04:04:00+08:00')
        text = '\n'.join(compact_phone_tables(m.report_lines(summary)))
        self.assertIn('10-07至2026-10-13', text)
        self.assertIn('不含今天', text)
        self.assertIn('| 101 | 2号机房 | 5.455% | 5.455% | 5.455% |', text)
        self.assertIn('| r101 | 未配置 | 0.000%', text)
        self.assertIn('| 130 | 6号机房 | 未知', text)
        self.assertIn('不是昨日同期快照', text)
        self.assertEqual(m.report_lines(None), [])
        finalizer = (ROOT / 'scripts/opensms_finalize_patrol_report.py').read_text()
        self.assertIn('lines.extend(device_reply_period_lines', finalizer)

    def test_independent_source_verification_and_scope_binding(self):
        from openpyxl import Workbook
        headers, rows = fixture()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, cache, receipt = root / 'source.xlsx', root / 'cache.pkl', root / 'export.json'
            book = Workbook()
            book.active.append(headers)
            for row in rows:
                book.active.append(row)
            book.save(source)
            book.close()
            build_scan_cache(source, cache)
            receipt.write_text(json.dumps({'serverDate': '2026-10-14', 'downloadedAt': '2026-10-15T04:04:00+08:00'}))
            evidence = scope.build(cache, source, receipt, '20261015T040128+0800', '2026-10-14', root / 'history')
            self.assertTrue(evidence['deviceReplyPeriodsRequired'])
            m.verify_source(evidence['deviceReplyPeriods'], source)
            import opensms_parallel_audit as audit
            analysis_path = root / 'analysis.json'
            analysis = {'rows': 5, 'latestDate': '10-14', 'overall': {'send': 135, 'reply': 7},
                        'scopeComparison': evidence}
            analysis_path.write_text(json.dumps(analysis))
            checked = audit.check_analysis_receipt(analysis_path)
            self.assertEqual(checked.status, audit.PASS)
            self.assertTrue(checked.details['deviceReplyPeriodsVerified'])
            altered = copy.deepcopy(evidence)
            altered['deviceReplyPeriods']['rows'][0]['periods']['today']['ratePct'] = 99
            with self.assertRaises(ValueError):
                scope.verify(altered)
            del altered['deviceReplyPeriods']
            with self.assertRaisesRegex(ValueError, '缺失'):
                scope.verify(altered)
            analysis['scopeComparison'] = altered
            analysis_path.write_text(json.dumps(analysis))
            self.assertEqual(audit.check_analysis_receipt(analysis_path).status, audit.FAIL)
            # A poisoned cache carrying the right source hash cannot fool the
            # audit that reopens the original workbook independently.
            with cache.open('rb') as stream:
                payload = pickle.load(stream)
            payload['rows'][0] = tuple([payload['rows'][0][0], 999, *payload['rows'][0][2:]])
            with cache.open('wb') as stream:
                pickle.dump(payload, stream)
            poisoned = m.build(payload['headers'], payload['rows'], 'fixture', '2026-10-14', sha256(source), 'time')
            with self.assertRaises(ValueError):
                m.verify_source(poisoned, source)

    def test_offline_supplement_preserves_originals_and_refuses_replay(self):
        from openpyxl import Workbook
        headers, rows = fixture()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = '20261015T040128+0800'
            source, cache = root / 'source.xlsx', root / 'cache.pkl'
            book = Workbook()
            book.active.append(headers)
            for row in rows:
                book.active.append(row)
            book.save(source)
            book.close()
            build_scan_cache(source, cache)
            original = root / f'OpenSMS_10日报表巡检_{run}.md'
            headings = ['本轮概况', '0回复与回复率', '运营商调整', '下架恢复', '余量与提醒', '文件与回执', '异常']
            original.write_text('\n'.join('## ' + title + '\n\n' + ('### 设备0回复\n\n原统计\n' if title == headings[1] else '原内容\n') for title in headings))
            receipt = root / 'export.json'
            receipt.write_text(json.dumps({'serverDate': '2026-10-14', 'downloadedAt': '2026-10-15T04:04:00+08:00'}))
            state = root / 'state.json'
            state.write_text(json.dumps({'status': 'success', 'runId': run, 'stages': {'acquire': {
                'serverDateVerified': True, 'serverDate': '2026-10-14', 'sourceArchive': str(source),
                'processingSource': str(source), 'sourceSha256': sha256(source), 'scanCache': str(cache),
                'exportReceipt': str(receipt)}, 'finalize': {'report': str(original)}}}))
            before = {path: sha256(path) for path in (source, original, state, receipt, cache)}
            output = root / 'new' / 'supplement.md'
            result = m.supplement_report(state, output)
            self.assertTrue(result['independentReportReadbackPassed'])
            self.assertEqual(before, {path: sha256(path) for path in before})
            rendered = output.read_text()
            self.assertEqual(sum(line.startswith('## ') for line in rendered.splitlines()), 7)
            self.assertEqual(rendered.count(m.TITLE), 1)
            self.assertIn('不是新的后台导出', rendered)
            with self.assertRaisesRegex(ValueError, '覆盖'):
                m.supplement_report(state, output)


if __name__ == '__main__':
    unittest.main()
