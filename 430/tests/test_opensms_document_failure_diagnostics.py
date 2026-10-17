import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_opensms_acquire_reliability as fixtures

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import opensms_process_document as DOCUMENT

RUNNER = fixtures.RUNNER


class DocumentFailureDiagnosticsTests(unittest.TestCase):
    def test_structured_check_failure_is_not_hidden_by_style_warning(self):
        failed = {"result": "失败", "failed_checks": ["系统机房及开卡日期"], "receipt_path": "/tmp/receipt.json"}
        child = subprocess.CompletedProcess([], 4, json.dumps(failed), "UserWarning: no default style")
        with patch.object(DOCUMENT.subprocess, "run", return_value=child):
            with self.assertRaises(DOCUMENT.LocalProcessingError) as captured:
                DOCUMENT.run_local(Path("builder"), Path("source"), None, Path("out"), "2026-09-06")
        self.assertIn("核验未通过：系统机房及开卡日期", str(captured.exception))
        self.assertNotIn("UserWarning", str(captured.exception))
        self.assertEqual(captured.exception.diagnostics["receipt_path"], "/tmp/receipt.json")
        self.assertIn("UserWarning", captured.exception.diagnostics["stderr_tail"])

    def test_stdout_error_precedes_unrelated_stderr(self):
        child = subprocess.CompletedProcess([], 4, json.dumps({"error": "missing header"}), "style warning")
        with patch.object(DOCUMENT.subprocess, "run", return_value=child):
            with self.assertRaisesRegex(DOCUMENT.LocalProcessingError, "missing header"):
                DOCUMENT.run_local(Path("builder"), Path("source"), None, Path("out"), "2026-09-06")

    def test_malformed_or_empty_output_retains_diagnostics(self):
        for stdout in ("not JSON", "[]", ""):
            with self.subTest(stdout=stdout):
                result = DOCUMENT.local_failure_details(subprocess.CompletedProcess([], 7, stdout, "fatal stderr"))
                self.assertEqual(result["exit_code"], 7)
                self.assertEqual(result["failed_checks"], [])
                self.assertEqual(result["stderr_tail"], "fatal stderr")

    def test_failed_document_records_full_time_and_never_publishes_stage(self):
        fixture = fixtures.AcquisitionReliabilityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runner = fixture.runner
        payload = {"result": "失败", "duration_seconds": 23.003,
                   "error": "核验未通过：系统字段", "local_failure": {"failed_checks": ["系统字段"]},
                   "dispatch_receipt_path": str(fixture.folder / "dispatch.json")}
        log = runner.run_dir / "document.log"
        log.write_text(json.dumps(payload), encoding="utf-8")
        runner.state.update({"documentLog": str(log), "documentStartedAt": "2026-09-07T07:14:25+08:00"})
        runner.document_log_handle = Mock()
        runner.document_process = SimpleNamespace(wait=Mock(return_value=5))
        runner.document_started_monotonic = 100.0
        with patch.object(RUNNER.time, "monotonic", return_value=124.0):
            with self.assertRaisesRegex(RUNNER.PatrolError, "核验未通过：系统字段"):
                runner.finish_document()
        self.assertNotIn("document", runner.state["stages"])
        self.assertEqual(runner.state["documentProcessTiming"]["durationSeconds"], 24.0)
        self.assertEqual(runner.state["documentProcessTiming"]["documentProcessSeconds"], 23.003)
        self.assertEqual(runner.state["commandTimings"][-1]["exitCode"], 5)
        self.assertEqual(runner.state["documentFailure"]["localFailure"]["failed_checks"], ["系统字段"])
        result = runner.write_failure_report(RUNNER.PatrolError("document failed"))
        self.assertIsNone(result["finalWorkbook"])
        self.assertEqual(result["performance"]["documentProcessSeconds"], 23.003)
        self.assertGreaterEqual(result["performance"]["pipelineWallSecondsBeforeFinalManifest"], 0)

    def test_compact_failure_preserves_decisions_paths_and_uncertain_counts(self):
        full = {"status": "failed", "runId": "offline", "state": "/tmp/state.json", "error": "verification failed",
                "sourceWorkbook": "/tmp/source.xlsx", "finalWorkbook": None, "report": "/tmp/report.md",
                "taskDUncertainSubmittedCount": 20, "taskDAutomaticRetryBlocked": True,
                "taskB": {"status": "pending_stage_readback", "stageReadbackPendingGroups": 2, "noWriteGroupsStageVerified": 1, "actualWriteCount": 3},
                "taskD": {"status": "passed", "swapNotificationCount": 7, "retentionUniqueCardCount": 10, "carrierActualWrite": 2,
                          "retentionSummary": {"newTransferredCount": 2, "totalInRetentionCount": 9}},
                "documentFailure": {"exitCode": 5, "localFailure": {"failed_checks": ["system_fields"],
                    "receipt_path": "/tmp/verify.json", "stderr_tail": "diagnostics" * 500}},
                "performance": {"documentProcessSeconds": 23, "commandTimings": [{"extra": "large"}]},
                "partialAcquisition": {"large": "trace" * 1000}}
        result = RUNNER.compact_launcher_result(full)
        self.assertEqual(result["completeResultLocation"], "state.summary")
        self.assertEqual(result["taskD"]["retentionSummary"], full["taskD"]["retentionSummary"])
        for key in ("swapNotificationCount", "retentionUniqueCardCount", "carrierActualWrite"):
            self.assertEqual(result["taskD"][key], full["taskD"][key])
        self.assertEqual(result["taskB"]["stageReadbackPendingGroups"], 2)
        self.assertEqual(result["taskB"]["noWriteGroupsStageVerified"], 1)
        from opensms_chat_summary import render_summary
        self.assertIn("2 组尚未完成阶段回读", render_summary(result))
        self.assertEqual(result["taskDUncertainSubmittedCount"], 20)
        self.assertTrue(result["taskDAutomaticRetryBlocked"])
        self.assertIsNone(result["finalWorkbook"])
        self.assertEqual(result["documentFailure"]["failedChecks"], ["system_fields"])
        self.assertEqual(result["documentFailure"]["verificationReceipt"], "/tmp/verify.json")
        self.assertNotIn("partialAcquisition", result)
        self.assertNotIn("commandTimings", result["performance"])
        self.assertIn("stderr_tail", full["documentFailure"]["localFailure"])

    def test_early_failure_without_archived_state_is_not_compacted(self):
        payload = {"status": "failed", "error": "preflight", "detail": "available evidence"}
        self.assertEqual(RUNNER.compact_launcher_result(payload), payload)

    def test_later_failure_keeps_completed_d_business_counts(self):
        fixture = fixtures.AcquisitionReliabilityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runner = fixture.runner
        runner.state["stages"]["taskD"] = {"status": "passed", "executed": True, "swapNotificationCount": 7,
            "retentionUniqueCardCount": 10, "carrierActualWrite": 2}
        result = runner.write_failure_report(RUNNER.PatrolError("later E failed"))
        for payload in (result, RUNNER.compact_launcher_result(result)):
            self.assertEqual(payload["taskD"]["swapNotificationCount"], 7)
            self.assertEqual(payload["taskD"]["retentionUniqueCardCount"], 10)
            self.assertEqual(payload["taskD"]["carrierActualWrite"], 2)


if __name__ == "__main__":
    unittest.main()
