"""Offline coverage: exact-run download diagnostics survive launcher failures."""

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_opensms_acquire_reliability as fixtures

RUNNER = fixtures.RUNNER


class ExportFailureDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AcquisitionReliabilityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.snapshot = self.fixture.inputs / f"opensms_export_progress_{self.runner.run_id}.json"
        self.addCleanup(patch.stopall)
        patch.object(RUNNER, "SNAPSHOTS", self.fixture.inputs).start()
        patch.object(RUNNER.subprocess, "run", return_value=subprocess.CompletedProcess(
            ["offline-export"], 2, "", "original download failure"
        )).start()
        self.diagnostic = {"httpStatus": 200, "businessCode": 500,
                           "category": "unrecognized_response", "byteLength": 63}

    def save_progress(self, **overrides):
        payload = {"runId": self.runner.run_id, "status": "download_failed",
                   "downloadFailure": self.diagnostic, **overrides}
        self.snapshot.write_text(json.dumps(payload), encoding="utf-8")

    def invoke(self):
        return self.runner.command("export", ["offline-export"])

    def test_exact_run_failure_is_archived_and_exposed(self):
        self.save_progress()
        with self.assertRaisesRegex(RUNNER.PatrolError, "HTTP 200.*业务码500.*63字节") as captured:
            self.invoke()
        archived = self.runner.receipts / self.snapshot.name
        self.assertEqual(json.loads(archived.read_text())["runId"], self.runner.run_id)
        self.assertEqual(self.runner.state["exportFailure"]["downloadFailure"], self.diagnostic)
        full = self.runner.write_failure_report(captured.exception)
        compact = RUNNER.compact_launcher_result(full)
        self.assertEqual(compact["exportFailure"], full["exportFailure"])
        self.assertIsNone(compact["finalWorkbook"])

    def test_foreign_run_is_not_used(self):
        self.save_progress(runId="another-run")
        with self.assertRaisesRegex(RUNNER.PatrolError, "export失败，退出码2"):
            self.invoke()
        self.assertNotIn("exportFailure", self.runner.state)
        self.assertFalse((self.runner.receipts / self.snapshot.name).exists())

    def test_download_timeout_reports_completion_not_null_http(self):
        self.save_progress(downloadFailure={"category": "read_timeout", "completionConfirmed": False})
        with self.assertRaisesRegex(RUNNER.PatrolError, "下载等待达到上限.*禁止重试"):
            self.invoke()

    def test_google_timeout_exact_run_is_specific_and_foreign_run_ignored(self):
        payload = {"status": "failed", "runId": self.runner.run_id,
                   "code": "OPENSMS_GOOGLE_DOWNLOAD_FAILED", "businessWrites": 0,
                   "category": "read_timeout", "attemptCount": 3}
        with patch.object(RUNNER.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["offline"], 1, json.dumps(payload), "")):
            with self.assertRaisesRegex(RUNNER.PatrolError, "Google源新鲜下载请求超时.*3次.*未复用旧文件"):
                self.runner.command("google_chatr_download", ["offline"])
        payload["runId"] = "foreign-run"
        with patch.object(RUNNER.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["offline"], 1, json.dumps(payload), "")):
            with self.assertRaisesRegex(RUNNER.PatrolError, "google_chatr_download失败，退出码1"):
                self.runner.command("google_chatr_download", ["offline"])

    def test_inventory_timeout_retains_page_and_attempts(self):
        payload = {"inventoryFailure": {"schemaVersion": 1, "runId": self.runner.run_id,
                   "businessWrites": 0, "code": "request-failed", "requestCategory": "read_timeout",
                   "pageNum": 5, "paginationAttemptCount": 3}}
        with patch.object(RUNNER.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["offline"], 1, "", json.dumps(payload))):
            with self.assertRaisesRegex(RUNNER.PatrolError, "第5页库存请求超时.*3次.*不能据此判定登录失效"):
                self.runner.command("system_kooda_inventory", ["offline"])

    def test_generation_runtime_timeout_reaches_final_json_and_report(self):
        diagnostic = {"category": "browser_runtime_timeout", "stage": "generation_status",
                      "completionConfirmed": False, "submissionReplayBlocked": True,
                      "attempts": [{"attempt": 1, "retry": False}]}
        self.save_progress(status="status_query_failed", downloadFailure=None,
                           generationFailure=diagnostic)
        with self.assertRaisesRegex(RUNNER.PatrolError, "浏览器执行通道超时.*禁止重复生成") as captured:
            self.invoke()
        full = self.runner.write_failure_report(captured.exception)
        compact = RUNNER.compact_launcher_result(full)
        self.assertEqual(compact["exportFailure"]["generationFailure"], diagnostic)
        self.assertIsNone(compact["sourceWorkbook"])
        self.assertIsNone(compact["finalWorkbook"])
        self.assertIn("浏览器执行通道超时", Path(full["report"]).read_text())

    def test_malformed_progress_does_not_mask_original_failure(self):
        for raw in ("not json", "[]", "null"):
            with self.subTest(raw=raw):
                self.snapshot.write_text(raw, encoding="utf-8")
                with self.assertRaisesRegex(RUNNER.PatrolError, "export失败，退出码2"):
                    self.invoke()

    def test_failed_receipt_copy_preserves_original_failure(self):
        self.save_progress()
        with patch.object(RUNNER.shutil, "copy2", side_effect=OSError("fixture copy failure")):
            with self.assertRaisesRegex(RUNNER.PatrolError, "export失败，退出码2"):
                self.invoke()
        self.assertEqual(self.runner.state["exportFailure"]["captureErrorType"], "OSError")
        self.assertIsNone(self.runner.state["exportFailure"]["receipt"])

    def test_malformed_download_diagnostic_does_not_mask_original_failure(self):
        self.save_progress(downloadFailure=["not an object"])
        with self.assertRaisesRegex(RUNNER.PatrolError, "export失败，退出码2"):
            self.invoke()
        self.assertIsNone(self.runner.state["exportFailure"]["downloadFailure"])

    def test_google_failure_exposes_current_round_reason_without_guessing_auth(self):
        failure = {"status": "failed", "runId": self.runner.run_id,
                   "error": "Google原表缺少可见机房页签: 五號機房（或旧版四號機房）"}
        with patch.object(RUNNER.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["offline"], 2, json.dumps(failure), "")):
            with self.assertRaisesRegex(RUNNER.PatrolError, "缺少可见机房页签.*未进入文档或写入"):
                self.runner.command("chatr_google_date_snapshot", ["offline"])
        failure["runId"] = "foreign-run"
        with patch.object(RUNNER.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["offline"], 2, json.dumps(failure), "")):
            with self.assertRaisesRegex(RUNNER.PatrolError, "chatr_google_date_snapshot失败，退出码2"):
                self.runner.command("chatr_google_date_snapshot", ["offline"])

    def test_inventory_total_drift_is_not_reported_as_403_or_cookie_expiry(self):
        failure = {"schemaVersion": 1, "runId": self.runner.run_id, "businessWrites": 0,
                   "code": "total-drift", "expectedTotal": 8105, "receivedTotal": 8106,
                   "paginationAttemptCount": 3}
        for mutation, expected in (({}, "由8105变为8106.*3次有限尝试"),
                                   ({"runId": "old-run"}, "退出码2"),
                                   ({"receivedTotal": True}, "退出码2"),
                                   ({"businessWrites": True}, "退出码2")):
            with self.subTest(mutation=mutation), patch.object(RUNNER.subprocess, "run",
                    return_value=subprocess.CompletedProcess(["offline"], 2, "",
                        json.dumps({"inventoryFailure": {**failure, **mutation}}))):
                with self.assertRaisesRegex(RUNNER.PatrolError, expected):
                    self.runner.command("system_kooda_inventory", ["offline"])


if __name__ == "__main__":
    unittest.main()
