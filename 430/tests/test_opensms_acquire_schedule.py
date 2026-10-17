"""Local gate and mocked acquisition tests; no real subprocesses or services."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from opensms_acquire_schedule import read_submission_ready
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_opensms_acquire_reliability as fixtures


class SubmissionGateTests(unittest.TestCase):
    def test_pending_valid_and_invalid_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ready.json'
            run, token = '20261005T000302+0800', 'a' * 32
            value = dict(schemaVersion=1, status='submitted', runId=run, token=token,
                         taskId='b' * 32, serverDate='2026-10-04', exportPageClosed=True,
                         requestLaneReleased=True, publishedAt='2026-10-04T16:03:04Z')
            self.assertIsNone(read_submission_ready(path, run, token))
            path.write_text(json.dumps(value))
            self.assertEqual(read_submission_ready(path, run, token), value)
            for change in ({'runId': 'old'}, {'token': 'c' * 32}, {'taskId': 'old-task'},
                           {'schemaVersion': True}, {'status': 'pending'}, {'exportPageClosed': False},
                           {'exportPageClosed': 1}, {'requestLaneReleased': False},
                           {'serverDate': '2026-02-30'}, {'serverDate': '20261004'},
                           {'publishedAt': '2026-10-04T16:03:04'}):
                with self.subTest(change=change):
                    path.write_text(json.dumps({**value, **change}))
                    with self.assertRaises((ValueError, TypeError)):
                        read_submission_ready(path, run, token)
            for content in ('{', '[]', ' ' * 4097):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    read_submission_ready(path, run, token)
            target = path.with_name('actual.json')
            target.write_text(json.dumps(value))
            path.unlink()
            path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, '符号链接'):
                read_submission_ready(path, run, token)


class AcquisitionSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AcquisitionReliabilityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner

    def test_statistics_start_after_submit_and_before_export_completion(self):
        submitted, background_started = threading.Event(), threading.Event()
        original = self.fixture.command
        def command(label, argv, **kwargs):
            if label == 'export':
                self.assertFalse(background_started.is_set())
                self.fixture.publish_submission_ready(kwargs['env'])
                submitted.set()
                self.assertTrue(background_started.wait(3), 'generation did not overlap reads')
            elif label in ('active_carriers', 'balance', 'system_kooda_inventory', 'card_retention_report'):
                self.assertTrue(submitted.is_set(), 'system reads stole the submission lane')
                background_started.set()
            return original(label, argv, **kwargs)
        self.runner.command = Mock(side_effect=command)
        result = self.runner.acquire()['acquisitionScheduling']
        self.assertEqual(result['mode'], 'export-submit-first-then-overlap-generation')
        self.assertEqual(result['httpInFlightLimit'], 1)
        self.assertTrue(result['requestPacingUnchanged'])
        self.assertEqual(result['taskId'], self.fixture.payloads['export']['taskId'])

    def test_early_export_failure_never_starts_deferred_reads(self):
        self.fixture.failures['export'] = fixtures.RUNNER.PatrolError('offline submission failure')
        self.runner.command = Mock(side_effect=self.fixture.command)
        with self.assertRaisesRegex(fixtures.RUNNER.PatrolError, 'offline submission failure'):
            self.runner.acquire()
        labels = [name for name, _, _ in self.fixture.calls]
        self.assertEqual(labels[-1], 'export')
        self.assertFalse(any(name in ('balance', 'active_carriers', 'system_kooda_inventory', 'card_retention_report')
                             for name in labels))

    def test_missing_or_wrong_signal_never_releases_reads(self):
        for kind in ('missing', 'stale'):
            with self.subTest(kind=kind):
                self.fixture.calls.clear()
                def command(label, argv, **kwargs):
                    if label == 'export':
                        if kind == 'stale':
                            self.fixture.publish_submission_ready({**kwargs['env'],
                                'OPENSMS_EXPORT_SUBMISSION_READY_TOKEN': '0' * 32})
                        return fixtures.RUNNER.CommandResult(self.fixture.payloads['export'], '', '', 0.01)
                    return self.fixture.command(label, argv, **kwargs)
                self.runner.command = Mock(side_effect=command)
                with self.assertRaises(fixtures.RUNNER.PatrolError):
                    self.runner.acquire()
                self.assertFalse(any(name in ('balance', 'active_carriers', 'system_kooda_inventory', 'card_retention_report')
                                     for name, _, _ in self.fixture.calls))

    def test_single_worker_and_inherited_signal_isolation(self):
        self.runner.args.io_workers = 1
        self.runner.command = Mock(side_effect=self.fixture.command)
        self.assertEqual(self.runner.acquire()['readConcurrency'], 1)
        self.runner.args.task_a_only = True
        with patch.dict(fixtures.RUNNER.os.environ, {
                'OPENSMS_EXPORT_SUBMISSION_READY_PATH': '/old/file',
                'OPENSMS_EXPORT_SUBMISSION_READY_TOKEN': 'd' * 32}):
            child = self.runner.child_environment()
            self.assertNotIn('OPENSMS_EXPORT_SUBMISSION_READY_PATH', child)
            self.assertNotIn('OPENSMS_EXPORT_SUBMISSION_READY_TOKEN', child)

    def test_completed_export_identity_must_match_submission(self):
        original = self.fixture.command
        def command(label, argv, **kwargs):
            result = original(label, argv, **kwargs)
            if label == 'export':
                result.payload = {**result.payload, 'taskId': 'c' * 32}
            return result
        self.runner.command = Mock(side_effect=command)
        with self.assertRaisesRegex(fixtures.RUNNER.PatrolError, '调度回执不一致'):
            self.runner.acquire()
        self.assertFalse(self.runner.state['partialAcquisition'].get('sourceVerified'))


if __name__ == '__main__':
    unittest.main()
