"""Offline boundary tests for the user-authorized final date fallback."""
import importlib.util
from pathlib import Path
from datetime import datetime
import unittest

PATH = Path(__file__).resolve().parents[1] / 'services/opensms_document_api/pipeline/process_card_slots.py'
spec = importlib.util.spec_from_file_location('creation_fallback', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CreationFallbackTests(unittest.TestCase):
    def test_independent_review_rejects_wrong_inclusive_cutoff(self):
        verify_spec = importlib.util.spec_from_file_location('creation_independent', PATH.with_name('opensms_verify_card_slot_fast.py'))
        verify = importlib.util.module_from_spec(verify_spec)
        verify_spec.loader.exec_module(verify)
        headers = ['电话', 'opr', '卡槽', '创建时间', '激活日期', '截止日']
        raw = [['14385550123', 'Chatr', '130_1', '2026-08-03', None, None]]
        correct = [['14385550123', 'Chatr', '130_1', '2026-08-03', '2026-08-03', '2026-09-01']]
        snapshot = {'index': {}}
        result = verify.review_resolved_dates(headers, raw, headers, correct, snapshot, snapshot, snapshot, False)
        self.assertTrue(result['matches'])
        self.assertEqual(len(result['creation_date_fallback_rows']), 1)
        correct[0][-1] = '2026-09-02'
        self.assertFalse(verify.review_resolved_dates(headers, raw, headers, correct, snapshot, snapshot, snapshot, False)['matches'])

    def test_inclusive_thirty_days_and_leap_month(self):
        blank = {'激活日期': (None, 'missing'), '截止日': (None, 'missing')}
        for start, end in [('2026-08-03', datetime(2026, 9, 1)), ('2024-02-01', datetime(2024, 3, 1))]:
            result = module.apply_creation_date_fallback(blank, start, {'激活日期': None, '截止日': ''})
            self.assertEqual(result['截止日'], (end, 'creation_date_fallback'))
            self.assertEqual(result['激活日期'][0], datetime.fromisoformat(start))
        self.assertEqual(blank['激活日期'], (None, 'missing'))

    def test_partial_dates_and_all_authoritative_sources_are_preserved(self):
        for source in ('system_export', 'local_cache', 'google_chatr', 'google'):
            for field in ('激活日期', '截止日'):
                resolved = {'激活日期': (None, 'missing'), '截止日': (None, 'missing')}
                resolved[field] = (datetime(2026, 8, 3), source)
                self.assertIs(module.apply_creation_date_fallback(resolved, '2026-01-01', {}), resolved)

    def test_invalid_or_missing_creation_and_nonempty_invalid_dates_are_not_invented(self):
        blank = {'激活日期': (None, 'missing'), '截止日': (None, 'missing')}
        for created in (None, '', 'not-a-date'):
            self.assertIs(module.apply_creation_date_fallback(blank, created, {}), blank)
        self.assertIs(module.apply_creation_date_fallback(blank, '2026-01-01', {'截止日': 'invalid'}), blank)


if __name__ == '__main__':
    unittest.main()
