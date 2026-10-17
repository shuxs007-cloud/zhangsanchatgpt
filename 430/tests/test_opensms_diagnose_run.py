"""Local synthetic evidence only; no browser, process launch or business state."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/opensms_diagnose_run.py"
SPEC = importlib.util.spec_from_file_location("opensms_diagnose_run_test_module", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
RID = "20260907T071225+0800"


class RunDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / RID
        self.root.mkdir()
        self.state_path = self.root / f"opensms_patrol_runner_state_{RID}.json"
        self.log_path = self.root / "document.log"
        self.dispatch = self.root / "opensms_document_dispatch_20260907T071448+0800_receipt.json"
        self.state = {"runId": RID, "status": "failed", "createdAt": "2026-09-07T07:12:25.249386+08:00",
                      "updatedAt": "2026-09-07T07:14:49.857096+08:00", "documentLog": str(self.log_path),
                      "commandTimings": [{"command": "export", "durationSeconds": 110.985}],
                      "liveWritesSerial": True,
                      "stages": {"acquire": {"durationSeconds": 119.499, "readConcurrency": 4,
                                              "exportPerformance": {"backendGenerationMs": 95960}},
                                 "candidates": {"durationSeconds": 3.826, "analysisConcurrency": 3,
                                                "documentProcessingConcurrentWithAnalysis": True,
                                                "sharedScanCacheUsed": True}, "taskF": {"status": "skipped"}}}
        self.log = {"duration_seconds": 23.003, "dispatch_receipt_path": str(self.dispatch)}
        self.write(self.log_path, self.log)
        self.write(self.dispatch, {"runId": RID, "local_failure": {"failed_checks": ["chatr_cutoff_scope"]}})

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def run_diagnosis(self, trigger="2026-09-06T23:08:58.419Z"):
        self.write(self.state_path, self.state)
        return MODULE.diagnose(self.state_path, trigger)

    def test_exact_timeline_and_nonadditive_nested_work(self):
        result = self.run_diagnosis()
        self.assertEqual(result["timings"]["triggerToLaunchSeconds"], 206.83)
        self.assertEqual(result["timings"]["runnerWallSeconds"], 144.608)
        self.assertEqual(result["timings"]["triggerToTerminalStateSeconds"], 351.438)
        self.assertEqual(result["timings"]["backendGenerationSeconds"], 95.96)
        self.assertEqual(result["timings"]["documentProcessSeconds"], 23.003)
        self.assertTrue(result["timings"]["durationsAreNotAdditive"])
        self.assertEqual(result["failedChecks"], ["chatr_cutoff_scope"])
        self.assertIn("不是新鲜导出", result["markdown"])

    def test_current_runner_performance_preferred(self):
        self.state["summary"] = {"performance": {"documentProcessSeconds": 24.2}}
        self.assertEqual(self.run_diagnosis()["timings"]["documentProcessSeconds"], 24.2)

    def test_failed_runner_document_timing_and_structured_check(self):
        self.state["documentProcessTiming"] = {"durationSeconds": 24.91, "documentProcessSeconds": 24.2,
                                                "documentJoinSeconds": 20.0, "exitCode": 5}
        self.state["documentFailure"] = {"localFailure": {"failed_checks": ["same_run_failure"]}}
        result = self.run_diagnosis()
        self.assertEqual(result["timings"]["documentProcessSeconds"], 24.2)
        self.assertEqual(result["timings"]["documentCommandWallSeconds"], 24.91)
        self.assertIn("same_run_failure", result["failedChecks"])

    def test_no_trigger_means_unknown_not_zero(self):
        result = self.run_diagnosis(None)
        self.assertIsNone(result["timings"]["triggerToLaunchSeconds"])
        self.assertIsNone(result["timings"]["triggerToTerminalStateSeconds"])

    def test_subprocess_timings_sorted_without_inventing_ai_token_usage(self):
        self.state['commandTimings'] += [
            {'command': 'port_subset', 'durationSeconds': 53.7, 'exitCode': 0},
            {'command': 'unknown', 'durationSeconds': None},
            {'command': 'bad', 'durationSeconds': -1},
        ]
        result = self.run_diagnosis()
        self.assertEqual([row['command'] for row in result['topCommands']], ['export', 'port_subset'])
        self.assertIsNone(result['aiUsage']['billedTokens'])
        self.assertFalse(result['aiUsage']['availableInRunnerState'])
        self.assertIn('53.700', result['markdown'])

    def test_running_is_not_terminal_duration(self):
        self.state["status"] = "running"
        result = self.run_diagnosis()
        self.assertIsNone(result["timings"]["runnerWallSeconds"])

    def test_refuses_foreign_run_path_without_reading(self):
        foreign = Path(self.temporary.name).resolve() / "20260907T060126+0800"
        foreign.mkdir()
        self.log["dispatch_receipt_path"] = str(foreign / self.dispatch.name)
        self.write(Path(self.log["dispatch_receipt_path"]), {"failed_checks": ["foreign_secret"]})
        self.write(self.log_path, self.log)
        result = self.run_diagnosis()
        self.assertFalse(result["rootCauseKnown"])
        self.assertTrue(any("跨runId" in value for value in result["warnings"]))
        self.assertNotIn("foreign_secret", json.dumps(result))

    def test_refuses_symlink_receipt(self):
        target = self.root / "elsewhere.json"
        self.write(target, {"failed_checks": ["do_not_read"]})
        self.dispatch.unlink()
        self.dispatch.symlink_to(target)
        result = self.run_diagnosis()
        self.assertFalse(result["rootCauseKnown"])
        self.assertTrue(any("符号链接" in value for value in result["warnings"]))

    def test_mismatched_payload_run_id_is_not_used(self):
        self.write(self.dispatch, {"runId": "20260907T060126+0800", "failed_checks": ["bad"]})
        self.assertFalse(self.run_diagnosis()["rootCauseKnown"])

    def test_arbitrary_filename_not_read_even_in_run_directory(self):
        self.log["receipt_path"] = str(self.root / "credentials.json")
        self.log.pop("dispatch_receipt_path")
        self.write(self.log_path, self.log)
        self.write(self.root / "credentials.json", {"password": "never-read-this"})
        result = self.run_diagnosis()
        self.assertEqual(len(result["evidence"]), 1)
        self.assertNotIn("never-read-this", json.dumps(result))

    def test_warning_only_stderr_is_not_root_cause(self):
        self.dispatch.unlink()
        self.log_path.write_text('UserWarning: Workbook contains no default style\n{"duration_seconds": 23.003}')
        result = self.run_diagnosis()
        self.assertFalse(result["rootCauseKnown"])
        self.assertEqual(result["timings"]["documentProcessSeconds"], 23.003)

    def test_plain_log_unknown_not_fake_zero(self):
        self.log_path.write_text("only a warning")
        result = self.run_diagnosis()
        self.assertIsNone(result["timings"]["documentProcessSeconds"])
        self.assertFalse(result["rootCauseKnown"])

    def test_trigger_without_timezone_rejected(self):
        with self.assertRaises(MODULE.EvidenceError):
            self.run_diagnosis("2026-09-07T07:00:00")

    def test_negative_trigger_gap_unknown(self):
        result = self.run_diagnosis("2026-09-07T08:00:00+08:00")
        self.assertIsNone(result["timings"]["triggerToLaunchSeconds"])

    def test_state_filename_must_bind_run_id(self):
        self.write(self.state_path, self.state)
        target = self.root / "state.json"
        target.write_bytes(self.state_path.read_bytes())
        with self.assertRaises(MODULE.EvidenceError):
            MODULE.diagnose(target)

    def test_rejects_production_output(self):
        result = self.run_diagnosis()
        with self.assertRaises(MODULE.EvidenceError):
            MODULE.write_development_output(result, MODULE.ROOT / "opensms_artifacts/state")

    def test_default_diagnosis_does_not_mutate_inputs(self):
        self.write(self.state_path, self.state)
        before = {path: path.read_bytes() for path in (self.state_path, self.log_path, self.dispatch)}
        MODULE.diagnose(self.state_path)
        self.assertEqual(before, {path: path.read_bytes() for path in before})


if __name__ == "__main__":
    unittest.main()
