import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import opensms_f_date_import as imp
from opensms_patrol_runner import PatrolRunner

class ImportTests(unittest.TestCase):
    def test_status_not_acceptance_as_completion(self):
        self.assertIn('尚未确认',imp.import_summary({'status':'accepted_pending_readback'}))
        self.assertIn('未提交',imp.import_summary({'status':'skipped_no_new_data'}))
        self.assertIn('回读通过',imp.import_summary({'status':'passed','independentReadbackPassed':True,'verifiedDatePairs':2}))

    def test_unverified_and_zero_input_rejected(self):
        for receipt in ({},{'runId':'current','status':'passed','independentReadbackPassed':True,
                            'formatVersion':'task_f_deadline_template_v2','exportedRowCount':0}):
            with self.assertRaises(ValueError): imp.prepare_import(receipt,'current')

    def test_zero_no_command(self):
        obj=SimpleNamespace(task_a_only=False,receipts=Path('/tmp'),delivery=Path('/tmp'),
                            run_id='offline',state={},_save_state=lambda:None,
                            command=lambda *a,**kw:self.fail('zero rows must never access browser'))
        with patch('opensms_system_missing_f_dates.export_difference',return_value={'exportedRowCount':0}):
            PatrolRunner.import_current_f_dates(obj,{'sourceArchive':'unused','sourceSha256':'unused'})
        self.assertEqual(obj.state['fDateImport']['status'],'skipped_no_new_data')

    def test_task_a_only_no_export(self):
        obj=SimpleNamespace(task_a_only=True,state={},_save_state=lambda:None)
        with patch('opensms_system_missing_f_dates.export_difference',side_effect=AssertionError):
            PatrolRunner.import_current_f_dates(obj,{})
        self.assertEqual(obj.state['fDateImport']['submitted'],False)

    def test_prepare_recomputes_and_checks_template(self):
        r=dict(runId='current',status='passed',independentReadbackPassed=True,
               formatVersion='task_f_deadline_template_v2',exportedRowCount=1,rowCount=1)
        for name,hash_name in (('source','sourceSha256'),('output','sha256'),
                              ('databaseSnapshot','databaseSnapshotSha256'),
                              ('sourceDetail','sourceDetailSha256'),('template','templateSha256')):
            r[name]='/tmp/'+name;r[hash_name]='hash'
        key='12345678901234567890'
        item={'lookup_status':'found','activation_date':'2026-10-01','cutoff_date':'2026-10-30'}
        headers=['电话','卡槽','ICCID','激活日期']
        from unittest.mock import mock_open
        with patch.object(imp,'digest',return_value='hash'), \
             patch.object(imp,'read_source',return_value=(headers,[['16470000000','101_1',key,None]])), \
             patch.object(Path,'read_text',return_value=__import__('json').dumps({key:item})), \
             patch.object(imp,'verify_output') as verify:
            data=imp.prepare_import(r,'current')
        self.assertEqual(data['records'][0]['phone'],'6470000000')
        verify.assert_called_once_with(Path('/tmp/output'),[[key,'2026-10-30','2026-10-01']])

    def test_hash_change_rejected_before_template_or_browser(self):
        r=dict(runId='current',status='passed',independentReadbackPassed=True,
               formatVersion='task_f_deadline_template_v2',exportedRowCount=1,
               source='/tmp/source',sourceSha256='correct')
        with patch.object(imp,'digest',return_value='changed'),self.assertRaisesRegex(ValueError,'FILE_CHANGED'):
            imp.prepare_import(r,'current')

    def test_reports_import_failure_in_successful_a(self):
        from opensms_chat_summary import render_summary
        text=render_summary({'status':'success','fDateImport':{'status':'submission_outcome_uncertain'}})
        self.assertIn('未自动重试',text)

    def test_current_import_runs_before_owned_target_close(self):
        source=(Path(__file__).resolve().parents[1]/'scripts/opensms_patrol_runner.py').read_text()
        self.assertIn('self.import_current_f_dates(acquire)\n            self.run_expired_card_probe(acquire)',source)
        self.assertIn('self.run_task_g(acquire, before_g)\n            refresh = self.refresh_workbook(document)\n            self.close_api_target()',source)
        body = source[source.index('    def execute(self)'):source.index('\ndef static_check')]
        self.assertLess(body.index('self.run_expired_card_probe(acquire)'), body.index('self.run_task_g(acquire, before_g)'))
        self.assertIn('child_env.pop("OPENSMS_F_DATE_IMPORT_RUN_ID", None)',source)

    def test_exact_saved_error_is_not_clobbered_by_command_failure(self):
        import tempfile,json
        with tempfile.TemporaryDirectory() as directory:
            run='20261004T030338+0800';receipt=Path(directory)/f'opensms_f_date_import_{run}.json'
            def command(*args,**kwargs):
                receipt.write_text(json.dumps({'runId':run,'status':'failed_before_submission','submitted':False,'error':'F_IMPORT_EXISTING_DATE_CONFLICT'}))
                raise RuntimeError('command exited 1')
            obj=SimpleNamespace(task_a_only=False,receipts=Path(directory),delivery=Path(directory),
                run_id=run,state={},_save_state=lambda:None,command=command)
            with patch('opensms_system_missing_f_dates.export_difference',return_value={'exportedRowCount':1}),patch.object(imp,'prepare_import',return_value={}):
                PatrolRunner.import_current_f_dates(obj,{'sourceArchive':'unused','sourceSha256':'unused'})
            self.assertEqual(obj.state['fDateImport']['error'],'F_IMPORT_EXISTING_DATE_CONFLICT')

    def test_prereceipt_command_error_is_preserved_without_copying_stderr(self):
        import tempfile, subprocess
        with tempfile.TemporaryDirectory() as directory:
            obj=object.__new__(PatrolRunner)
            obj.state={};obj.run_dir=Path(directory)
            obj.child_environment=lambda env: {}
            for code in ('F_IMPORT_PRIOR_SUBMISSION_UNRESOLVED', 'F_IMPORT_REPLAY_FORBIDDEN',
                         'F_IMPORT_RUNNER_GATE_MISSING', 'F_IMPORT_INPUT_UNCONFIRMED'):
                completed=subprocess.CompletedProcess(['offline'],1,'',f'untrusted details\n{code}\n')
                with patch('opensms_patrol_runner.subprocess.run',return_value=completed):
                    with self.assertRaisesRegex(RuntimeError, f'^{code}$'):
                        obj.command('task_a_f_date_import',['offline'])

    def test_blocked_receipt_keeps_prior_run_and_safe_summary(self):
        import tempfile,json
        with tempfile.TemporaryDirectory() as directory:
            run='20261009T020735+0800';prior='20261008T225421+0800'
            receipt=Path(directory)/f'opensms_f_date_import_{run}.json'
            saved={'runId':run,'status':'failed_before_submission','submitted':False,
                   'error':'F_IMPORT_PRIOR_SUBMISSION_UNRESOLVED','blockingRunId':prior}
            def command(*args,**kwargs):
                receipt.write_text(json.dumps(saved))
                raise RuntimeError(saved['error'])
            obj=SimpleNamespace(task_a_only=False,receipts=Path(directory),delivery=Path(directory),
                run_id=run,state={},_save_state=lambda:None,command=command)
            with patch('opensms_system_missing_f_dates.export_difference',return_value={'exportedRowCount':48}),patch.object(imp,'prepare_import',return_value={}):
                PatrolRunner.import_current_f_dates(obj,{'sourceArchive':'unused','sourceSha256':'unused'})
            self.assertEqual(obj.state['fDateImport'],saved)
            from opensms_chat_summary import render_summary
            summary=render_summary({'status':'success','fDateImport':saved})
            self.assertIn(prior,summary)
            self.assertIn('本轮未提交',summary)
            self.assertIn('未自动重试',summary)
            self.assertNotIn('导入前核验失败',summary)

    def test_summary_only_exposes_valid_symbolic_code_and_run(self):
        self.assertIn('F_IMPORT_INPUT_UNCONFIRMED',imp.import_summary(
            {'status':'failed_before_submission','error':'F_IMPORT_INPUT_UNCONFIRMED'}))
        self.assertIn('日期与本轮模板冲突',imp.import_summary(
            {'status':'failed_before_submission','error':'F_IMPORT_EXISTING_DATE_CONFLICT'}))
        for error,run in [('private credential text','secret'),(None,None)]:
            summary=imp.import_summary({'status':'failed_before_submission','error':error})
            self.assertNotIn('private',summary)
            summary=imp.import_summary({'status':'failed_before_submission',
                'error':'F_IMPORT_PRIOR_SUBMISSION_UNRESOLVED','blockingRunId':run})
            self.assertNotIn('secret',summary)

if __name__=='__main__': unittest.main()
