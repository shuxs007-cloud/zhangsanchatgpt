"""Only temporary jobs and mocked children/leases. No live browser or writes."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import opensms_expired_card_probe_background as BG
from opensms_chat_summary import render_summary


class BackgroundProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.jobs = self.root / 'jobs'
        self.jobs.mkdir()
        for change in (patch.object(BG, 'JOBS_ROOT', self.jobs), patch.object(BG, 'config', return_value=None),
                       patch.object(BG, 'REQUEST_LOCK', self.root / 'request.lock'),
                       patch.object(BG.subprocess, 'Popen', return_value=SimpleNamespace(pid=999)),
                       patch.object(BG.subprocess, 'run', side_effect=AssertionError('live child forbidden'))):
            change.start()
            self.addCleanup(change.stop)

    def prepare(self):
        run_id = '20261004T050000+0800'
        self.parent = self.root / 'parent.json'
        self.parent.write_text(json.dumps({'runId': run_id, 'status': 'running'}))
        acquire = {'serverDate': '2026-10-03'}
        for key in ('phoneDateReceipt', 'dayDateReceipt'):
            proof = self.root / (key + '.json')
            proof.write_text(json.dumps({'serverDate': acquire['serverDate']}))
            acquire[key] = str(proof)
        browser = {'sessionId': 'one-session', 'pid': 42, 'cdp': 'http://127.0.0.1:12345',
                   'webSocketPath': '/devtools/browser/owned'}
        runner = SimpleNamespace(run_id=run_id, state_path=self.parent,
                                 args=SimpleNamespace(browser_session=browser),
                                 child_environment=lambda: {'PATH': '/bin', 'OPENSMS_SITE_LOCK_TOKEN': 'parent',
                                                           'OPENSMS_RAW_API_TARGET_ID': 'parent-target'})
        return BG.prepare_and_start(runner, acquire)

    def ready(self, probe):
        self.parent.write_text(json.dumps({'runId': probe['runId'], 'status': 'success'}))
        BG.release_after_delivery(probe)

    def test_dispatch_is_detached_local_only_and_has_no_inherited_page_or_authority(self):
        probe = self.prepare()
        job = BG.verify_dispatch(probe)
        self.assertFalse(Path(job['gate']).exists())
        self.assertEqual(BG.read(job['state'])['status'], 'waiting_main_delivery')
        self.assertFalse(probe['executed'])
        self.assertFalse(probe['waitForCompletion'])
        call = BG.subprocess.Popen.call_args
        self.assertTrue(call.kwargs['start_new_session'])
        self.assertNotIn('OPENSMS_SITE_LOCK_TOKEN', call.kwargs['env'])
        self.assertNotIn('OPENSMS_RAW_API_TARGET_ID', call.kwargs['env'])
        BG.subprocess.run.assert_not_called()

    def test_no_credential_or_injected_runtime_environment_is_inherited(self):
        source = dict(PATH='/bin', HOME='/private/home', NODE_OPTIONS='--import foreign',
                      PYTHONPATH='/foreign', API_KEY='private', OPENSMS_FORCE_TASK_E='1', BITBROWSER_CDP='other')
        self.assertEqual(BG.isolated_environment(source), {'PATH': '/bin', 'HOME': '/private/home'})

    def test_immutable_job_and_inputs_reject_changed_scope(self):
        probe = self.prepare()
        job = BG.verify_dispatch(probe)
        value = BG.read(job['inputPath'])
        value['deadlineFilterEnabled'] = True
        Path(job['inputPath']).write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'CONTRACT_INVALID'):
            BG.verify_dispatch(probe)

    def test_parent_failure_cancels_without_site_or_browser_work(self):
        probe = self.prepare()
        self.parent.write_text(json.dumps({'runId': probe['runId'], 'status': 'failed'}))
        with patch.object(BG, 'SiteExecutionLease', side_effect=AssertionError('lease forbidden')):
            BG.execute_job(Path(probe['backgroundJob']), probe['backgroundJobSha256'])
        state = BG.read(probe['backgroundState'])
        self.assertEqual(state['status'], 'cancelled_parent_failed')
        self.assertFalse(state['onlineStarted'])
        BG.subprocess.run.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'REPLAY_FORBIDDEN'):
            BG.execute_job(Path(probe['backgroundJob']), probe['backgroundJobSha256'])

    def test_main_success_alone_is_not_a_delivery_gate(self):
        probe = self.prepare()
        self.parent.write_text(json.dumps({'runId': probe['runId'], 'status': 'success'}))
        job = BG.verify_dispatch(probe)
        clock = iter((0, 0, 2))
        with self.assertRaisesRegex(ValueError, 'WAIT_TIMEOUT_NO_BUSINESS'):
            BG.await_delivery(job, probe['backgroundJobSha256'], wait=lambda _: None,
                              clock=lambda: next(clock), timeout=1)
        self.ready(probe)
        self.assertTrue(BG.await_delivery(job, probe['backgroundJobSha256']))

    def test_delivery_wait_does_not_repeatedly_parse_the_large_parent_checkpoint(self):
        probe = self.prepare()
        job = BG.verify_dispatch(probe)
        clock = iter((0, 0, 0.5, 2))
        with patch.object(BG, 'read', wraps=BG.read) as reader:
            with self.assertRaisesRegex(ValueError, 'WAIT_TIMEOUT_NO_BUSINESS'):
                BG.await_delivery(job, probe['backgroundJobSha256'], wait=lambda _: None,
                                  clock=lambda: next(clock), timeout=1)
        parent_reads = [call for call in reader.call_args_list if str(call.args[0]) == job['parentState']]
        self.assertEqual(len(parent_reads), 1)

    def test_worker_owns_fresh_leases_and_target_after_delivery_and_never_changes_parent(self):
        probe = self.prepare()
        self.ready(probe)
        parent_bytes = self.parent.read_bytes()
        job = BG.verify_dispatch(probe)
        events = []
        def lease(**kwargs):
            result = Mock(owner={'token': 'fresh', 'pid': 123}, environment={'OPENSMS_SITE_LOCK_TOKEN': 'fresh'})
            result.acquire.side_effect = lambda: (events.append('lease'), result)[1]
            return result
        def child(argv, **kwargs):
            events.append(Path(argv[1]).name + ':' + str(argv[2]))
            self.assertNotEqual(kwargs['env'].get('OPENSMS_RAW_API_TARGET_ID'), 'parent-target')
            if argv[2] == 'create':
                target = Path(argv[-1])
                target.write_text('{}')
                value = {'success': True, 'hidden': True, 'targetId': 'fresh-target', 'receipt': str(target)}
            elif argv[2] == 'close':
                value = {'success': True, 'closed': True}
            else:
                self.assertEqual(kwargs['env']['OPENSMS_RAW_API_TARGET_ID'], 'fresh-target')
                value = {'status': 'passed'}
            return SimpleNamespace(returncode=0, stdout=json.dumps(value))
        browser = {**job['browser'], 'status': 'ready'}
        with patch.object(BG, 'SiteExecutionLease', side_effect=lease), \
             patch.object(BG, 'resolve_configured_browser', return_value=browser), \
             patch.object(BG.subprocess, 'run', side_effect=child), \
             patch('opensms_expired_card_probe_audit.verify_receipt', return_value={'status': 'passed'}):
            BG.execute_job(Path(probe['backgroundJob']), probe['backgroundJobSha256'])
        self.assertEqual(events[:2], ['lease', 'lease'])
        self.assertEqual(BG.read(probe['backgroundState'])['status'], 'passed')
        self.assertEqual(self.parent.read_bytes(), parent_bytes)
        # The main snapshot stays queued even when the independent worker finishes.
        self.assertEqual(probe['status'], 'queued')

    def test_seven_section_summary_never_claims_queued_work_is_completed(self):
        probe = self.prepare()
        summary = render_summary({'status': 'success', 'expiredCardProbe': probe})
        self.assertIn('不筛截止日', summary)
        self.assertIn('非当前状态', summary)
        self.assertIn('未上架只尝试一次', summary)
        self.assertNotIn('回读已上架 0', summary)
        self.assertEqual(sum(line.startswith('## ') for line in summary.splitlines()), 7)

    def test_main_audit_only_verifies_dispatch_without_waiting_for_receipt(self):
        from opensms_parallel_audit import check_offline, PASS
        probe = self.prepare()
        self.assertFalse(Path(probe['receipt']).exists())
        result = check_offline({'expiredCardProbe': probe}, None)
        self.assertEqual(result.status, PASS)
        self.assertIn('仅核验后台派发', result.message)
        BG.subprocess.run.assert_not_called()

    def test_gate_failure_never_changes_already_delivered_parent_files(self):
        from opensms_patrol_runner import PatrolRunner
        probe = self.prepare()
        self.parent.write_text(json.dumps({'runId': probe['runId'], 'status': 'success'}))
        original = self.parent.read_bytes()
        runner = SimpleNamespace(state={'stages': {'expiredCardProbe': probe}},
                                 _save_state=Mock(side_effect=AssertionError('delivered state cannot change')))
        with patch.object(BG, 'release_after_delivery', side_effect=OSError('local gate error')):
            PatrolRunner.release_expired_card_probe(runner)
        self.assertEqual(self.parent.read_bytes(), original)
        runner._save_state.assert_not_called()
        self.assertEqual(BG.read(probe['backgroundState'])['status'], 'failed_delivery_gate_no_business')
        self.assertFalse(BG.await_delivery(BG.verify_dispatch(probe), probe['backgroundJobSha256']))


if __name__ == '__main__':
    unittest.main()
