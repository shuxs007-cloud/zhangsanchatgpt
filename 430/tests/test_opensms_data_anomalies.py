"""Future-only row-error contracts and compact final handoff; offline fixtures."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import unittest

from openpyxl.utils.datetime import CALENDAR_WINDOWS_1900
from opensms_data_anomalies import anomaly_fields, anomaly_summary, source_anomaly, assert_anomaly_identity_separation
from opensms_parallel_audit import review_data_anomaly_declaration, task_c_source_anomalies
from opensms_chat_summary import render_summary


class DataAnomalyTests(unittest.TestCase):
    def test_missing_data_does_not_hide_duplicate_or_conflicting_identity(self):
        anomaly = source_anomaly(7, {}, 'deadline_missing_or_invalid', '14160000001')
        with self.assertRaisesRegex(ValueError, '重复号码身份'):
            assert_anomaly_identity_separation([anomaly], [{'电话': '4160000001'}])
        with self.assertRaisesRegex(ValueError, '重复号码身份'):
            assert_anomaly_identity_separation([anomaly, {**anomaly, 'sourceRow': 8}], [])
        assert_anomaly_identity_separation([anomaly], [{'电话': '4160000002'}])
    def test_c_independent_rebuild_skips_unknown_change_or_missing_phone_not_valid_record(self):
        rows = {index: dict(电话=phone, 运营商='A1-测试', opr='Chatr', 卡槽=f'101_{index}',
            过期状态='', 截止日='', 换号状态=change, **{'4日发送窗口数': 100, '4日回复率': 0,
            '10日发送窗口数': 400}) for index, phone, change in (
                (2, '4160000001', '状态未知'), (3, '', '未换号'), (4, '4160000002', '未换号'))}
        manifest = {'tomorrowDate': '2026-10-14'}
        expected = task_c_source_anomalies(manifest, rows, CALENDAR_WINDOWS_1900, lambda row, threshold: None)
        self.assertEqual(['change_status_unknown', 'phone_missing'], [row['reason'] for row in expected])
        self.assertEqual([2, 3], [row['sourceRow'] for row in expected])
        manifest.update(anomaly_fields(expected, Path('/tmp/offline_manifest.json')))
        self.assertEqual([], review_data_anomaly_declaration(manifest, expected))
        for field in ('dataAnomalySkippedCount', 'dataAnomalySkippedRows', 'dataAnomalyCountsByReason', 'dataAnomalyPolicy'):
            changed = deepcopy(manifest)
            changed.pop(field)
            self.assertTrue(review_data_anomaly_declaration(changed, expected))

    def test_summary_hides_identifiers_keeps_seven_sections_all_twelve_balances_and_total_rate(self):
        row = source_anomaly(7, {'电话': '4160000001', '卡槽': '210_18.01', '运营商': 'B1'},
            'deadline_missing_or_invalid', '4160000001')
        task = {'status': 'passed', **anomaly_summary(anomaly_fields([row], Path('/tmp/task_b_offline.json')))}
        text = render_summary({'status': 'success', 'runId': 'offline', 'overallReplyRatePct': 3.21,
            'taskB': task, 'taskC': {'status':'skipped'}, 'taskD': {'status':'skipped'}, 'taskE': {'status':'skipped'},
            'taskF': {'reason': 'no_missing_authoritative_dates'}})
        self.assertEqual(7, sum(line.startswith('## ') for line in text.splitlines()))
        self.assertIn('整体回复率 3.21%', text)
        self.assertIn('异常数据跳过 1 条', text)
        self.assertIn('/tmp/task_b_offline.json', text)
        self.assertNotIn('4160000001', text)
        for group in ('A1','A12','A11','B1','B11','B12','C2','A3','B3','B1V','B11V','B12V'):
            self.assertIn(f'- {group}：', text)


if __name__ == '__main__':
    unittest.main()
