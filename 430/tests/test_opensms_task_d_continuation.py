import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import opensms_patrol_runner as runner


class ContinuationTests(unittest.TestCase):
    def test_gate_rejects_no_current_user_or_mixed_mode_before_reading_state(self):
        base = dict(resume_state=Path('/not-read.json'), authorization_source='current-user')
        for update in ({'resume_state': None}, {'authorization_source': 'timer'},
                       {'continue_task_g': True}, {'force_task_f': True}, {'task_a_only': True}):
            args = SimpleNamespace(**{**base, **update})
            with patch.object(runner, 'read_json', side_effect=AssertionError('must not read checkpoint')):
                with self.assertRaises(runner.PatrolError):
                    runner.task_d_continuation_checkpoint(args)

    def view(self):
        view = object.__new__(runner.PatrolRunner)
        view.run_id = '20261010T221537+0800'
        view.receipts = Path('/attempt')
        view.state_path = Path('/original/state.json')
        view.delivery = Path('/original')
        view.args = SimpleNamespace(cdp='http://127.0.0.1:1')
        view.state = {'taskDContinuationAttempts': [{'preflight': {}}], 'stages': {
            'acquire': {'serverDate': '2026-10-10'}, 'document': {}, 'candidates': {}, 'refreshAfterTaskC': {}}}
        view.create_api_target = Mock()
        view.command = Mock(return_value=SimpleNamespace(payload={'success': True,
            'day': {'serverDate': '2026-10-10', 'path': '/day'},
            'phoneConfig': {'serverDate': '2026-10-10', 'path': '/phoneConfig'}}))
        for name in ('acquire', 'start_document', 'run_standard_writes', 'run_task_b', 'run_task_c', 'start_background_task_f'):
            setattr(view, name, Mock(side_effect=AssertionError('completed work must never replay')))
        for name in ('_save_state', 'run_task_d', 'run_task_e', 'run_task_g', 'import_current_f_dates',
                     'run_expired_card_probe', 'close_api_target', 'release_expired_card_probe'):
            setattr(view, name, Mock())
        view.copy_artifact = Mock(side_effect=lambda p, n: Path('/attempt') / n)
        view.refreshed_document = Mock(return_value={'current': True})
        view.refresh_workbook = Mock(return_value={'fresh': True})
        view.add_task_e_unwritten_sheet = Mock(return_value={'current': True})
        view.finalize = Mock(return_value={})
        view.summary = Mock(return_value={})
        return view

    def test_continuation_does_not_export_or_replay_a_b_c_f(self):
        view = self.view()
        with patch.object(runner, 'read_json', return_value={'enabled': True}):
            result = view.continue_task_d()
        self.assertTrue(result['taskDContinuation'])
        self.assertFalse(result['exportReplayed'])
        self.assertFalse(result['completedBusinessStagesReplayed'])
        view.run_task_d.assert_called_once()
        view.run_task_e.assert_called_once()
        view.run_task_g.assert_called_once()
        self.assertEqual(view.state['status'], 'success')
        for name in ('acquire', 'start_document', 'run_standard_writes', 'run_task_b', 'run_task_c', 'start_background_task_f'):
            getattr(view, name).assert_not_called()

    def test_preparation_continuation_executes_both_new_branches_without_a_prior_submission_progress(self):
        view = self.view()
        view.authorization = {'taskD': True}
        view.args.continue_task_d = True
        view.state['taskDContinuationAttempts'][-1]['preflight'].update(
            preparationOnly=True, approvedAnomalyRows=[{'cardNo': '4160000001', 'backendId': '12'}])
        view.state['taskDCheckpoint'] = {}
        view.reusable_stage = Mock(return_value=None)
        view.task_d_preparation_location = Mock(return_value=(Path('/attempt/manifest.json'), Path('/attempt'), 'prepare'))
        view.command = Mock(side_effect=[SimpleNamespace(payload={'status':'passed','businessReceiptPassed':True,
            'transactionalVerificationPassed':True,'verificationMode':'synchronous_transactional_business_receipt',
            'independentReadbackAvailable':False,'path':f'/attempt/{phase}.json'}) for phase in ('swap','retention')])
        view.collect_task_d_notification_summary = Mock(return_value={'status':'passed'})
        view.complete_stage = Mock(side_effect=lambda name, value, started: value)
        manifest = {'task':'D','forceGate':True,'automationGate':True,'retentionCarrierTransferEnabled':False,
            'retentionCarrierTransferStatus':'removed_by_user','retentionReceiptPath':'/attempt/source.json',
            'swapNotificationPath':'/attempt/swap-notification.json','retentionNotificationPath':'/attempt/retention-notification.json',
            'swapExpireDate':'2026-10-10','retentionExpireDate':'2026-10-17',
            'dataAnomalyPolicy':'skip_row_data_anomalies_v1','dataAnomalySkippedRows':[{'cardNo':'4160000001','backendId':'12'}]}
        with patch.object(Path,'is_file',lambda path: str(path)=='/attempt/manifest.json'), \
                patch.object(runner,'read_json',return_value=manifest),patch.object(runner,'sha256',return_value='unchanged'):
            result = runner.PatrolRunner.run_task_d(view, {'cardResult':'/source.xlsx'}, {'serverDate':'2026-10-10'})
        self.assertEqual(2,view.command.call_count)
        self.assertEqual(['task_d_expiry_swap_zero','task_d_expiry_retention_verified'],
            [call.args[0] for call in view.command.call_args_list])
        for call in view.command.call_args_list:
            self.assertNotIn('--continue-progress',call.args[1])
        self.assertEqual(result['status'],'passed')

    def test_anomaly_skip_flag_requires_current_user_continuation_not_a_new_run(self):
        for args in (['run','--skip-data-anomalies-this-run'],
            ['run','--continue-task-d','--resume-state','/exact/state.json','--skip-data-anomalies-this-run','--authorization-source','timer']):
            with self.assertRaises(runner.PatrolError):
                runner.validate_task_selection(runner.build_parser().parse_args(args))

    def test_platform_date_drift_stops_before_any_business(self):
        view = self.view()
        view.command.return_value.payload['day']['serverDate'] = '2026-10-11'
        with self.assertRaisesRegex(runner.PatrolError, 'PLATFORM_DATE_CHANGED'):
            view.continue_task_d()
        view.run_task_d.assert_not_called()
        view.run_task_e.assert_not_called()
        view.run_task_g.assert_not_called()

    def test_parser_and_a_only_conflict(self):
        args = runner.build_parser().parse_args(['run', '--continue-task-d', '--resume-state', '/exact/state.json', '--task-a-only'])
        with self.assertRaises(runner.PatrolError):
            runner.validate_task_selection(args)

    def test_completed_d_summary_repair_never_enters_submission(self):
        view = self.view()
        view.authorization = {'taskD': True}
        view.args.continue_task_d = True
        view.state['taskDContinuationAttempts'][-1]['preflight']['businessCompleted'] = True
        view.state['taskDCheckpoint'] = {'swapExpiryReceipt': '/swap.json',
            'retentionExpiryReceipt': '/retention.json', 'businessCompletedAt': 'actual-submit-time'}
        view.reusable_stage = Mock(return_value=None)
        view.task_d_preparation_location = Mock(return_value=(Path('/manifest.json'), Path('/original'), 'prepare'))
        view.command = Mock(side_effect=AssertionError('completed D must not submit or prepare again'))
        view.collect_task_d_notification_summary = Mock(return_value={'status': 'passed'})
        view.complete_stage = Mock(side_effect=lambda name, value, started: value)
        manifest = {'task': 'D', 'forceGate': True, 'automationGate': True,
            'retentionCarrierTransferEnabled': False, 'retentionCarrierTransferStatus': 'removed_by_user',
            'retentionReceiptPath': '/source.json', 'swapNotificationPath': '/swap-notification.json',
            'retentionNotificationPath': '/retention-notification.json',
            'swapExpireDate': '2026-10-10', 'retentionExpireDate': '2026-10-17'}
        def read(path):
            return manifest if str(path) == '/manifest.json' else {'status': 'passed'}
        with patch.object(Path, 'is_file', return_value=True), patch.object(runner, 'read_json', side_effect=read), \
                patch.object(runner, 'sha256', return_value='unchanged'):
            result = runner.PatrolRunner.run_task_d(view, {'cardResult': '/source.xlsx'}, {'serverDate': '2026-10-10'})
        view.command.assert_not_called()
        self.assertEqual(result['swapExpiryReceipt'], '/swap.json')
        self.assertEqual(result['retentionExpiryReceipt'], '/retention.json')
        self.assertEqual(view.state['taskDCheckpoint']['businessCompletedAt'], 'actual-submit-time')


if __name__ == '__main__':
    unittest.main()
