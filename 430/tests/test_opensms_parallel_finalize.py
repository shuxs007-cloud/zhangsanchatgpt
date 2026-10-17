"""Distinct local writers must join before final report generation."""
from pathlib import Path
import json
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import opensms_patrol_runner as runner


class StopBeforeReport(Exception):
    pass


class ParallelFinalizeTests(unittest.TestCase):
    def make_runner(self):
        value = object.__new__(runner.PatrolRunner)
        value.run_id = 'offline-run'
        value.delivery, value.receipts = Path('/offline/delivery'), Path('/offline/receipts')
        value.state = {'writeGroups': {}}
        return value

    def test_distinct_writers_overlap_and_state_only_published_after_join(self):
        value = self.make_runner()
        value.state['reportDateCache'] = {'offline-fixture': True}
        barrier = threading.Barrier(2)
        coordinator, finished, identities = threading.get_ident(), [], []
        def job(name):
            self.assertNotEqual(threading.get_ident(), coordinator)
            self.assertNotIn('systemMissingFDates', value.state)
            self.assertNotIn('vGroupSwapDaily', value.state)
            barrier.wait(3)
            identities.append(threading.get_ident())
            finished.append(name)
            return {'status': 'passed', 'name': name}
        def save():
            self.assertEqual(threading.get_ident(), coordinator)
            self.assertCountEqual(finished, ['f-dates', 'v-daily'])
            self.assertEqual(value.state['finalizeLocalJobScheduling']['actualWorkers'], 2)
            self.assertTrue(value.state['finalizeLocalJobScheduling']['allJoinedBeforeWrite'])
        value._save_state = save
        value.build_finalize_config = lambda *args: (_ for _ in ()).throw(StopBeforeReport())
        def export_dates(*args, report_date_cache):
            self.assertIs(report_date_cache, value.state['reportDateCache'])
            return job('f-dates')
        with patch.dict(sys.modules, {
                'opensms_system_missing_f_dates': SimpleNamespace(export_difference=export_dates),
                'opensms_v_swap_daily': SimpleNamespace(export_daily=lambda *args: job('v-daily'))}):
            with self.assertRaises(StopBeforeReport):
                value.finalize({'sourceArchive': '/offline/source', 'sourceSha256': 'hash'}, {}, {}, {})
        self.assertEqual(len(set(identities)), 2)

    def test_failure_drains_other_writer_and_blocks_final_report(self):
        value = self.make_runner()
        barrier, finished = threading.Barrier(2), threading.Event()
        def fail(*args, report_date_cache):
            self.assertIsNone(report_date_cache)
            barrier.wait(3)
            raise ValueError('offline bad date proof')
        def drain(*args):
            barrier.wait(3)
            finished.set()
            return {'status': 'passed'}
        value._save_state = lambda: self.fail('cannot publish partial success')
        value.build_finalize_config = lambda *args: self.fail('cannot generate report')
        with patch.dict(sys.modules, {
                'opensms_system_missing_f_dates': SimpleNamespace(export_difference=fail),
                'opensms_v_swap_daily': SimpleNamespace(export_daily=drain)}):
            with self.assertRaisesRegex(ValueError, 'offline bad date proof'):
                value.finalize({'sourceArchive': '/offline/source', 'sourceSha256': 'hash'}, {}, {}, {})
        self.assertTrue(finished.is_set())
        self.assertNotIn('systemMissingFDates', value.state)
        self.assertNotIn('vGroupSwapDaily', value.state)

    def test_completed_continuation_uses_exact_existing_supplement_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            value = self.make_runner()
            value.delivery = Path(temp) / value.run_id
            value.receipts = value.delivery / 'receipts'
            attempt = value.receipts / 'task_d_continuation_attempts' / '12345'
            attempt.mkdir(parents=True)
            path = attempt / f'opensms_system_missing_f_dates_{value.run_id}.json'
            saved = {'runId': value.run_id, 'status': 'passed', 'receipt': str(path)}
            path.write_text(json.dumps(saved))
            value.state['systemMissingFDates'] = saved
            value.state['taskDContinuationAttempts'] = [{'authorizationSource': 'current-user',
                'attemptDirectory': str(attempt)}]
            self.assertEqual(value.current_difference_receipts(), attempt.resolve())
            value.state['taskDContinuationAttempts'][0]['authorizationSource'] = 'timer'
            with self.assertRaises(runner.PatrolError):
                value.current_difference_receipts()
            value.state['taskDContinuationAttempts'][0]['authorizationSource'] = 'current-user'
            path.write_text(json.dumps({**saved, 'changed': True}))
            with self.assertRaises(runner.PatrolError):
                value.current_difference_receipts()


if __name__ == '__main__':
    unittest.main()
