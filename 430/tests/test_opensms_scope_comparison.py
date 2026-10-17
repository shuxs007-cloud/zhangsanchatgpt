import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import opensms_scope_comparison as m


def snapshot(day='2026-09-30', time='2026-09-30T01:00:00+08:00'):
    return {'contract': m.CONTRACT, 'runId': day.replace('-', '') + 'T010000+0800',
            'serverDate': day, 'observedAt': time,
            'values': m.aggregate(['卡槽'] + [day[5:] + x for x in m.METRICS],
                                  [('101_1', 1000, 30, 100, 5), ('201_1', 500, 40, 50, 10)], day)}


class ScopeTests(unittest.TestCase):
    def test_totals_and_weighted_rate(self):
        values = snapshot()['values']
        self.assertEqual(values['整体']['总发送量'], 1500)
        self.assertEqual(values['整体']['回复率'], 10)
        self.assertEqual(values['2号机房']['回复率'], 5)
        self.assertIsNone(values['5号机房']['回复率'])
        self.assertIn('6号机房', values)

    def test_room6_and_old_mapping_contract_not_comparable(self):
        headers = ['卡槽'] + ['09-30' + x for x in m.METRICS]
        rows = [('130_1', 100, 8, 50, 4), ('174_1', 200, 10, 50, 5), ('173_1', 70, 3, 50, 2)]
        values = m.aggregate(headers, rows, '2026-09-30')
        self.assertEqual(values['6号机房']['总发送量'], 300)
        self.assertEqual(values['6号机房']['回复率'], 9)
        self.assertEqual(values['5号机房']['总发送量'], 70)
        for contract in ('source-all-rows-room-map-20260918-window-reply-v1',
                         'source-all-rows-room-map-20261014-window-reply-v1',
                         'source-all-rows-room-map-' + '0' * 64 + '-window-reply-v1'):
            with self.subTest(contract=contract):
                old = snapshot('2026-09-29', '2026-09-29T01:00:00+08:00')
                old['contract'] = contract
                self.assertIsNone(m.compare(snapshot(), [old])['previousRunId'])

    def test_current_room_configuration_binds_history_and_device_boundaries(self):
        config_hash = hashlib.sha256((ROOT / 'scripts/opensms_room_settings.json').read_bytes()).hexdigest()
        self.assertEqual(m.CONTRACT, f'source-all-rows-room-map-{config_hash}-window-reply-v1')
        headers = ['卡槽'] + ['10-16' + x for x in m.METRICS]
        rows = [('193_1', 100, 1, 50, 1), ('194_1', 200, 2, 50, 2), ('195_1', 300, 3, 50, 3)]
        values = m.aggregate(headers, rows, '2026-10-16')
        self.assertEqual(values['6号机房']['总发送量'], 500)
        self.assertEqual(values['6号机房']['回复率'], 5)
        self.assertEqual(values['未识别']['总发送量'], 100)
        self.assertEqual(values['整体']['总发送量'], 600)

    def test_window_and_business_day(self):
        for t, ok in [('00:30:00', True), ('01:30:00', True), ('00:29:59', False), ('01:30:01', False)]:
            old = snapshot('2026-09-29', '2026-09-29T' + t + '+08:00')
            self.assertEqual(m.compare(snapshot(), [old])['previousRunId'] is not None, ok)
        old = snapshot('2026-09-28', '2026-09-29T01:00:00+08:00')
        self.assertIsNone(m.compare(snapshot(), [old])['previousRunId'])

    def test_room7_totals_and_old_four_room_snapshot_are_separate(self):
        headers = ['卡槽'] + ['10-16' + x for x in m.METRICS]
        rows = [('94_1', 100, 1, 50, 1), ('100_1', 200, 5, 50, 5),
                ('101_1', 300, 3, 50, 3), ('93_1', 400, 0, 50, 0)]
        values = m.aggregate(headers, rows, '2026-10-16')
        self.assertEqual(values['7号机房']['总发送量'], 300)
        self.assertEqual(values['7号机房']['回复率'], 6)
        self.assertEqual(values['2号机房']['总发送量'], 300)
        self.assertEqual(values['未识别']['总发送量'], 400)
        config = json.loads((ROOT / 'scripts/opensms_room_settings.json').read_text())
        config['rooms'] = [room for room in config['rooms'] if room['label'] != '7号机房']
        config['totalDeviceCount'] = 130
        old_config_hash = hashlib.sha256(json.dumps(config).encode()).hexdigest()
        old = snapshot('2026-09-29', '2026-09-29T01:00:00+08:00')
        old['contract'] = f'source-all-rows-room-map-{old_config_hash}-window-reply-v1'
        self.assertIsNone(m.compare(snapshot(), [old])['previousRunId'])

    def test_nearest_conflict_and_year_boundary(self):
        old = snapshot('2025-12-31', '2025-12-31T01:00:00+08:00')
        now = snapshot('2026-01-01', '2026-01-01T01:00:00+08:00')
        self.assertEqual(m.compare(now, [old])['offsetSeconds'], 0)
        bad = copy.deepcopy(old)
        bad['values']['整体']['总发送量'] = 1
        self.assertIsNone(m.compare(now, [old, bad])['previousRunId'])

    def test_missing_invalid_zero_and_rooms(self):
        headers = ['卡槽'] + ['09-30' + x for x in m.METRICS]
        for invalid in [None, '', True, -1, 'nan', 'invalid', 0.5]:
            values = m.aggregate(headers, [('r101_1', invalid, 0, 0, 0)], '2026-09-30')
            self.assertIsNone(values['整体']['总发送量'])
            self.assertIsNone(values['未识别']['回复率'])
        values = m.aggregate(headers[:-1], [('155_1', 0, 0, 0)], '2026-09-30')
        self.assertEqual(values['5号机房']['总发送量'], 0)
        self.assertIsNone(values['5号机房']['回复率'])

    def test_persist_source_readback_and_tampering(self):
        from openpyxl import Workbook
        from opensms_export_scan_cache import build_scan_cache
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, cache, receipt = root / 'source.xlsx', root / 'cache.pkl', root / 'export.json'
            wb = Workbook()
            wb.active.append(['卡槽'] + ['09-30' + x for x in m.METRICS])
            wb.active.append(['101_1', 1000, 30, 100, 5])
            wb.save(source)
            build_scan_cache(source, cache)
            receipt.write_text(json.dumps({'serverDate': '2026-09-30', 'downloadedAt': '2026-09-30T01:00:00+08:00'}))
            e = m.build(cache, source, receipt, '20260930T010000+0800', '2026-09-30', root / 'history')
            m.verify(e)
            self.assertIn('今日总发送量', '\n'.join(m.report_lines(e)))
            e['comparison']['rows'][0]['sendDelta'] = 100
            with self.assertRaises(ValueError):
                m.verify(e)


if __name__ == '__main__':
    unittest.main()
