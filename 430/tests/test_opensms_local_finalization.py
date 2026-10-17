"""Local finishing must never re-enter the live business pipeline."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_opensms_acquire_reliability import RUNNER


class LocalFinalizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.run_id = "20261007T200519+0800"
        self.delivery = self.folder / self.run_id
        self.receipts = self.delivery / "receipts"
        self.receipts.mkdir(parents=True)
        self.path = self.receipts / f"opensms_patrol_runner_state_{self.run_id}.json"
        self.source = self.delivery / "source.xlsx"
        self.source.write_bytes(b"immutable source")
        self.final = self.delivery / "final.xlsx"
        self.final.write_bytes(b"verified final")
        self.state = {"runId": self.run_id, "delivery": str(self.delivery), "status": "failed",
            "error": "最终并行审计失败: one local check", "runnerPid": 99999999,
            "apiTarget": {"status": "closed"}, "authorization": {f"task{k}": False for k in "BCDEF"},
            "stages": {name: {"completedAt": "2026-10-07T21:04:22+08:00"} for name in
                       ("acquire", "candidates", "document", "standardWrites", "refresh")},
            "writeGroups": {"confirmed": {"result": "passed"}}, "fDateImport": {"status": "skipped"}}
        self.state["stages"]["acquire"].update(sourceArchive=str(self.source), sourceSha256=RUNNER.sha256(self.source))
        self.state["stages"]["refresh"].update(finalWorkbook=str(self.final), finalWorkbookSha256=RUNNER.sha256(self.final))
        self.save()
        self.addCleanup(patch.stopall)
        self.status = patch.object(RUNNER, "read_status", return_value={"processAlive": False}).start()
        patch.object(RUNNER, "RUN_LOG_ROOT", self.folder / "logs").start()
        patch.object(RUNNER, "ensure_configured_browser", side_effect=AssertionError("no browser")).start()
        patch.object(RUNNER.subprocess, "run", side_effect=AssertionError("no external subprocess")).start()
        patch.object(RUNNER.PatrolRunner, "__init__", side_effect=AssertionError("no live constructor")).start()

    def save(self):
        self.path.write_text(json.dumps(self.state), encoding="utf-8")

    def test_checkpoint_and_source_hash(self):
        self.assertEqual(RUNNER.local_finalization_checkpoint(self.path)["runId"], self.run_id)
        self.source.write_bytes(b"changed")
        with self.assertRaisesRegex(RUNNER.PatrolError, "完整性"):
            RUNNER.local_finalization_checkpoint(self.path)

    def test_incomplete_business_is_rejected(self):
        self.state["authorization"]["taskE"] = True
        self.save()
        with self.assertRaisesRegex(RUNNER.PatrolError, "任务E未完成"):
            RUNNER.local_finalization_checkpoint(self.path)

    def test_uncertain_import_is_rejected(self):
        self.state["fDateImport"] = {"status": "unknown", "submitted": True}
        self.save()
        with self.assertRaisesRegex(RUNNER.PatrolError, "F日期导入"):
            RUNNER.local_finalization_checkpoint(self.path)

    def test_running_and_wrong_failure_are_rejected(self):
        self.status.return_value = {"processAlive": True}
        with self.assertRaisesRegex(RUNNER.PatrolError, "已停止"):
            RUNNER.local_finalization_checkpoint(self.path)
        self.status.return_value = {"processAlive": False}
        self.state["error"] = "business timeout"
        self.save()
        with self.assertRaisesRegex(RUNNER.PatrolError, "最终本地审计"):
            RUNNER.local_finalization_checkpoint(self.path)

    def test_finalizer_only_archives_old_evidence_and_releases_after_manifest(self):
        audit = self.receipts / f"opensms_parallel_audit_{self.run_id}_final.json"
        audit.write_text('{"failed":1}', encoding="utf-8")
        calls = []
        with patch.object(RUNNER.PatrolRunner, "finalize", side_effect=lambda *args: calls.append("finalize") or {}) as finalize, \
             patch.object(RUNNER.PatrolRunner, "summary", side_effect=lambda *args: calls.append("summary") or {"status": "success"}), \
             patch.object(RUNNER.PatrolRunner, "command", side_effect=lambda label, *args: calls.append(label)), \
             patch.object(RUNNER.PatrolRunner, "release_expired_card_probe", side_effect=lambda: calls.append("release")):
            result = RUNNER.finish_local_report(self.path, "current-user")
        self.assertEqual(calls, ["finalize", "summary", "delivery_manifest_complete", "release"])
        self.assertEqual(finalize.call_count, 1)
        self.assertFalse(result["businessTasksReplayed"])
        saved = RUNNER.read_json(self.path)
        self.assertEqual(saved["status"], "success")
        self.assertNotIn("error", saved)
        old = saved["localFinalizationHistory"][0]["archive"]
        original = next(item for item in old if item["source"] == str(self.path.resolve()))
        self.assertEqual(RUNNER.read_json(original["archive"])["status"], "failed")
        self.assertFalse((self.receipts / ".~finalization_only.lock").exists())
        self.assertEqual(audit.read_text(), '{"failed":1}')

    def test_failed_audit_cannot_mark_success_or_release(self):
        with patch.object(RUNNER.PatrolRunner, "finalize", side_effect=RUNNER.PatrolError("audit still failed")), \
             patch.object(RUNNER.PatrolRunner, "write_failure_report", return_value={"status": "failed"}), \
             patch.object(RUNNER.PatrolRunner, "release_expired_card_probe") as release:
            with self.assertRaisesRegex(RUNNER.PatrolError, "audit still failed"):
                RUNNER.finish_local_report(self.path, "current-user")
        self.assertEqual(RUNNER.read_json(self.path)["status"], "failed")
        release.assert_not_called()

    def test_no_history_authorization(self):
        with self.assertRaisesRegex(RUNNER.PatrolError, "当轮用户"):
            RUNNER.finish_local_report(self.path, "history")

    def test_zero_row_f_import_is_completed_only_with_same_run_verified_receipt(self):
        attempt = self.receipts / "task_d_continuation_attempts" / "12345"
        attempt.mkdir(parents=True)
        self.state["taskDContinuationAttempts"] = [{"authorizationSource": "current-user",
            "attemptDirectory": str(attempt)}]
        output = self.delivery / f"zero_{self.run_id}.xlsx"
        output.write_bytes(b"verified zero-row workbook")
        receipt = attempt / f"opensms_system_missing_f_dates_{self.run_id}.json"
        difference = {"runId": self.run_id, "status": "passed", "receipt": str(receipt),
            "exportedRowCount": 0, "rowCount": 0, "independentReadbackPassed": True,
            "cloudUploadAttempted": False, "sourceSha256": RUNNER.sha256(self.source),
            "output": str(output), "sha256": RUNNER.sha256(output)}
        self.state["fDateImport"] = {"runId": self.run_id, "status": "skipped_no_new_data", "submitted": False}
        self.state["systemMissingFDates"] = difference
        receipt.write_text(json.dumps(difference))
        self.save()
        self.assertEqual(self.run_id, RUNNER.local_finalization_checkpoint(self.path)["runId"])
        for field, value in (("exportedRowCount", 1), ("independentReadbackPassed", False),
                             ("runId", "prior-run"), ("cloudUploadAttempted", True)):
            with self.subTest(field=field):
                self.state["systemMissingFDates"] = {**difference, field: value}
                receipt.write_text(json.dumps(self.state["systemMissingFDates"]))
                self.save()
                with self.assertRaisesRegex(RUNNER.PatrolError, "F日期导入"):
                    RUNNER.local_finalization_checkpoint(self.path)
        self.state["systemMissingFDates"] = difference
        receipt.write_text(json.dumps(difference))
        self.save()
        output.write_bytes(b"changed output")
        with self.assertRaisesRegex(RUNNER.PatrolError, "F日期导入"):
            RUNNER.local_finalization_checkpoint(self.path)

    def test_completed_d_continuation_allows_local_report_but_not_uncertain_import(self):
        attempt = self.receipts / 'task_d_continuation_attempts' / '12345'
        attempt.mkdir(parents=True)
        self.state['error'] = '既有差异导出未完整核验，禁止覆盖或复用'
        self.state['authorization'].update({f'task{k}': True for k in 'BCDE'})
        self.state['stages']['acquire']['serverDate'] = '2026-10-07'
        for task in 'BCDE':
            self.state['stages']['task' + task] = {'completedAt': 'complete', 'status': 'passed'}
        counts = {'confirmedSubmissionCount': 4, 'uniqueNotifiedPhoneCount': 3, 'overlapPhoneCount': 1}
        self.state['stages']['taskD'].update(manifest='/manifest', swapExpiryReceipt='/swap',
            retentionExpiryReceipt='/retention', notificationSummary={'status': 'passed', **counts})
        audit_path = attempt / 'audit.json'
        audit_path.write_text('{"passed":true}')
        self.state['stages']['taskG'] = {'completedAt': 'complete', 'status': 'passed', 'executed': True,
            'independentReadbackPassed': True, 'input': '/input', 'receipt': '/g-receipt',
            'independentAudit': str(audit_path)}
        self.state['taskDContinuationAttempts'] = [{'authorizationSource': 'current-user',
            'attemptDirectory': str(attempt), 'preflight': {'businessCompleted': True, 'serverDate': '2026-10-07'}}]
        receipt = attempt / f'opensms_f_date_import_{self.run_id}.json'
        imported = {'runId': self.run_id, 'receipt': str(receipt), 'status': 'failed_before_submission',
            'submitted': False, 'cloudUploadAttempted': False, 'error': 'F_IMPORT_PRIOR_SUBMISSION_UNRESOLVED'}
        receipt.write_text(json.dumps(imported))
        self.state['fDateImport'] = imported
        self.save()
        with patch('opensms_task_d_notification_summary.build_from_files', return_value=counts), \
                patch('opensms_task_g.audit', return_value={'passed': True}):
            self.assertEqual(RUNNER.local_finalization_checkpoint(self.path)['runId'], self.run_id)
            Path(str(receipt) + '.intent.json').write_text('{}')
            with self.assertRaisesRegex(RUNNER.PatrolError, 'F日期导入'):
                RUNNER.local_finalization_checkpoint(self.path)
            self.state['fDateImport'] = {**imported, 'status': 'submission_outcome_uncertain', 'submitted': True}
            self.save()
            with self.assertRaisesRegex(RUNNER.PatrolError, 'F日期导入'):
                RUNNER.local_finalization_checkpoint(self.path)


if __name__ == "__main__":
    unittest.main()
