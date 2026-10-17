import argparse
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "opensms_patrol_runner.py"
SPEC = importlib.util.spec_from_file_location("opensms_patrol_runner", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def args(task_b=False, task_c=False, task_d=False, task_e=False, task_f=False, source=None):
    return argparse.Namespace(
        force_task_b=task_b,
        force_task_c=task_c,
        force_task_d=task_d,
        force_task_e=task_e,
        force_task_f=task_f,
        authorization_source=source,
    )


class PatrolRunnerTests(unittest.TestCase):
    def read_mock_status(self, status, alive, writes=None):
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "state.json"
            state_path.write_text(json.dumps({
                "runId": "20260930T233055+0800", "status": status,
                "runnerPid": 12345, "stages": {}, "writeGroups": writes or {},
            }))
            process = subprocess.CompletedProcess([], 0 if alive else 1,
                "python opensms_patrol_runner.py run" if alive else "", "")
            with patch.object(MODULE.subprocess, "run", return_value=process):
                return MODULE.read_status(state_path)

    def test_dead_running_checkpoint_is_interrupted_and_not_false_zero(self):
        result = self.read_mock_status("running", False)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["recordedStatus"], "running")
        self.assertIsNone(result["actualCarrierWrites"])
        self.assertEqual(result["checkpointCarrierWrites"], 0)
        self.assertTrue(result["writesRequireReceiptReconciliation"])

    def test_live_runner_keeps_checkpoint_count_without_reconciliation(self):
        result = self.read_mock_status("running", True, {"route": {"actualWrite": 4}})
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["actualCarrierWrites"], 4)
        self.assertFalse(result["writesRequireReceiptReconciliation"])

    def test_terminal_runner_remains_terminal_after_exit(self):
        for status in ("success", "failed"):
            result = self.read_mock_status(status, False, {"route": {"actualWrite": 4}})
            self.assertEqual(result["status"], status)
            self.assertEqual(result["actualCarrierWrites"], 4)
            self.assertFalse(result["writesRequireReceiptReconciliation"])

    def test_task_b_c_d_e_and_f_default_off(self):
        with patch.dict(os.environ, {}, clear=True):
            result = MODULE.validate_authorization(args())
        self.assertFalse(result["taskB"])
        self.assertFalse(result["taskC"])
        self.assertFalse(result["taskD"])
        self.assertFalse(result["taskE"])
        self.assertFalse(result["taskF"])

    def test_task_b_rejects_non_current_user(self):
        for source in (None, "timer", "history"):
            with self.subTest(source=source), patch.dict(os.environ, {"OPENSMS_FORCE_TASK_B": "1"}, clear=True):
                with self.assertRaises(MODULE.PatrolError):
                    MODULE.validate_authorization(args(task_b=True, source=source))

    def test_task_b_requires_environment_gate(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MODULE.PatrolError):
                MODULE.validate_authorization(args(task_b=True, source="current-user"))

    def test_task_b_accepts_double_gate(self):
        with patch.dict(os.environ, {"OPENSMS_FORCE_TASK_B": "1"}, clear=True):
            result = MODULE.validate_authorization(args(task_b=True, source="current-user"))
        self.assertTrue(result["taskB"])

    def test_task_d_rejects_timer_and_history_authorization(self):
        for source in (None, "timer", "history"):
            with self.subTest(source=source), patch.dict(os.environ, {"OPENSMS_FORCE_TASK_D": "1"}, clear=True):
                with self.assertRaises(MODULE.PatrolError):
                    MODULE.validate_authorization(args(task_d=True, source=source))

    def test_task_d_requires_environment_gate(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MODULE.PatrolError):
                MODULE.validate_authorization(args(task_d=True, source="current-user"))

    def test_task_d_accepts_current_user_double_gate(self):
        with patch.dict(os.environ, {"OPENSMS_FORCE_TASK_D": "1"}, clear=True):
            result = MODULE.validate_authorization(args(task_d=True, source="current-user"))
        self.assertTrue(result["taskD"])

    def test_task_e_rejects_timer_and_requires_environment_gate(self):
        with patch.dict(os.environ, {"OPENSMS_FORCE_TASK_E": "1"}, clear=True):
            with self.assertRaises(MODULE.PatrolError):
                MODULE.validate_authorization(args(task_e=True, source="timer"))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MODULE.PatrolError):
                MODULE.validate_authorization(args(task_e=True, source="current-user"))

    def test_task_e_accepts_current_user_double_gate(self):
        with patch.dict(os.environ, {"OPENSMS_FORCE_TASK_E": "1"}, clear=True):
            result = MODULE.validate_authorization(args(task_e=True, source="current-user"))
        self.assertTrue(result["taskE"])

    def test_task_e_recovery_requires_existing_proof_and_current_user_authorization(self):
        with tempfile.TemporaryDirectory() as folder:
            proof = Path(folder) / "progress.json"
            proof.write_text("{}", encoding="utf-8")
            invalid = args(source="current-user")
            invalid.task_e_recovery_progress = proof
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(MODULE.PatrolError):
                MODULE.validate_authorization(invalid)
            valid = args(task_e=True, source="current-user")
            valid.task_e_recovery_progress = proof
            with patch.dict(os.environ, {"OPENSMS_FORCE_TASK_E": "1"}, clear=True):
                result = MODULE.validate_authorization(valid)
            self.assertTrue(result["taskE"])

    def test_task_f_rejects_timer_and_requires_all_environment_gates(self):
        full = {
            "OPENSMS_FORCE_TASK_F": "1",
            "OPENSMS_ENABLE_TASK_F": "1",
            "OPENSMS_EXECUTE_TASK_F": "1",
        }
        with patch.dict(os.environ, full, clear=True):
            with self.assertRaises(MODULE.PatrolError):
                MODULE.validate_authorization(args(task_f=True, source="timer"))
        for missing in full:
            partial = {key: value for key, value in full.items() if key != missing}
            with self.subTest(missing=missing), patch.dict(os.environ, partial, clear=True):
                with self.assertRaises(MODULE.PatrolError):
                    MODULE.validate_authorization(args(task_f=True, source="current-user"))

    def test_task_f_accepts_current_user_triple_environment_gate(self):
        with patch.dict(os.environ, {
            "OPENSMS_FORCE_TASK_F": "1",
            "OPENSMS_ENABLE_TASK_F": "1",
            "OPENSMS_EXECUTE_TASK_F": "1",
        }, clear=True):
            result = MODULE.validate_authorization(args(task_f=True, source="current-user"))
        self.assertTrue(result["taskF"])

    def test_parse_last_json_ignores_leading_status_text(self):
        payload = MODULE.parse_last_json('progress\n{"first": 1}\nnotice\n{"final": 2}\n')
        self.assertEqual(payload, {"final": 2})

    def test_static_check_is_offline_and_passes(self):
        result = MODULE.static_check()
        self.assertTrue(result["passed"])
        self.assertFalse(result["livePatrolExecuted"])
        self.assertEqual(result["removedCarrierRouteHits"], [])
        self.assertFalse(result["browserSessionSeparation"]["exportAndDateUsePageSession"])
        self.assertTrue(result["browserSessionSeparation"]["exportAndDateUseRawBackgroundTargets"])
        self.assertFalse(result["browserSessionSeparation"]["playwrightBrowserConnectionUsed"])
        self.assertTrue(result["browserSessionSeparation"]["authenticatedPagePreferredOverStaleLoginTab"])
        self.assertEqual(result["authorizationGates"]["taskDDefault"], "off")
        self.assertEqual(result["authorizationGates"]["taskEDefault"], "off")
        self.assertEqual(result["authorizationGates"]["taskFDefault"], "on_post_document_catalog_incremental")
        self.assertTrue(result["taskEContract"]["passed"])
        self.assertEqual(result["taskEContract"]["markerBatchSize"], 20)
        self.assertTrue(result["taskEContract"]["gatewayTimeoutUncertainRetryBlocked"])
        self.assertEqual(result["taskFContract"]["progressIntervalSeconds"], 300)
        self.assertTrue(result["taskFContract"]["passed"])
        self.assertEqual(result["taskFContract"]["openSmsInventoryBrowser"], "BitBrowser 430 existing profile")
        self.assertEqual(
            result["taskFContract"]["transportPriority"],
            ["same-origin-http", "background-page-javascript"],
        )
        self.assertTrue(result["taskFContract"]["backgroundTargetRequiredOnlyForFallback"])
        self.assertTrue(result["programOnlyContractPassed"])
        self.assertTrue(result["taskAOnlyContract"]["passed"])
        self.assertTrue(result["taskAOnlyContract"]["manualDefaultTaskFEnabled"])
        self.assertEqual(result["taskAOnlyContract"]["excludedTasks"], list("BCDEF"))
        self.assertTrue(result["documentStageBarriersPassed"])
        self.assertTrue(result["reliableBackgroundAcquisitionContract"]["passed"])
        self.assertTrue(result["reliableBackgroundAcquisitionContract"]["serverDateRequiresThreeWayAgreement"])
        self.assertTrue(result["reliableBackgroundAcquisitionContract"]["cleanupBeforeDeliveryManifest"])

    def test_execution_contract_requires_no_manual_ai_steps(self):
        contract = MODULE.execution_contract()
        self.assertEqual(contract["executionMode"], "local-program-only")
        self.assertFalse(contract["manualAiStepsRequired"])
        self.assertEqual(contract["aiRole"], "read-policy-launch-once-summarize-final-json")
        self.assertEqual(contract["stageOrder"], ["document", "taskF", "standard", "taskB", "taskC", "taskD", "taskE", "expiredCardProbe", "taskG"])
        self.assertTrue(contract["safeReadOnlyConcurrency"])
        self.assertTrue(contract["liveWritesSerial"])
        self.assertEqual(contract["pipelineVersion"], "2026.10.07-v68-b14-removed-local-cpu")
        self.assertFalse(contract["taskDRetentionPopulationEnabled"])
        self.assertEqual(contract["taskDRetentionSummaryContractVersion"], 2)
        self.assertFalse(contract['fullKoodaInventoryAcquisitionEnabled'])
        self.assertEqual(contract['systemKoodaSourceScope'], 'current_10day_export_only')
        self.assertEqual(contract['expiredCardProbeScope'], 'expired_zones_all_listing_states_v2')
        self.assertTrue(contract['expiredCardProbeBackground'])
        self.assertFalse(contract['expiredCardProbeDeadlineFilterEnabled'])
        self.assertTrue(contract["cardRetentionReportEnabled"])
        self.assertFalse(contract["taskGStandaloneEnabled"])

    def test_exact_bitbrowser_430_port_is_resolved_dynamically(self):
        with tempfile.TemporaryDirectory() as directory:
            port_file = Path(directory) / "DevToolsActivePort"
            port_file.write_text("50704\n/devtools/browser/example\n", encoding="utf-8")
            with patch.object(MODULE, "BITBROWSER_430_PORT_FILE", port_file):
                self.assertEqual(MODULE.resolve_bitbrowser_430_cdp(), "http://127.0.0.1:50704")

    def test_explicit_cdp_override_is_preserved(self):
        self.assertEqual(
            MODULE.resolve_bitbrowser_430_cdp("http://127.0.0.1:60000"),
            "http://127.0.0.1:60000",
        )

    def test_compact_launcher_result_keeps_high_signal_and_paths(self):
        full = {
            "status": "success",
            **MODULE.execution_contract(),
            "runId": "20260904T120000+0800",
            "serverDate": "2026-09-04",
            "candidateCount": 12,
            "backendValidCount": 12,
            "actualWriteCount": 10,
            "independentVerificationPassed": 2,
            "balanceGroups": [],
            "taskD": {"status": "skipped_no_current_user_authorization", "unused": "large"},
            "taskE": {"status": "skipped_no_current_user_authorization", "unused": "large"},
            "taskF": {"status": "passed", "pendingCount": 0, "unused": "large"},
            "performance": {"stages": [], "acquireSubtasks": {"export": 70.0}},
            "sourceWorkbook": "/tmp/source.xlsx",
            "finalWorkbook": "/tmp/final.xlsx",
            "report": "/tmp/report.md",
            "summaryPath": "/tmp/full.json",
        }
        compact = MODULE.compact_launcher_result(full)
        self.assertEqual(compact["launcherJsonMode"], "compact-high-signal")
        self.assertNotIn("unused", compact["taskD"])
        self.assertNotIn("unused", compact["taskF"])
        self.assertEqual(compact["performance"]["acquireSubtasks"]["export"], 70.0)
        self.assertEqual(compact["summaryPath"], "/tmp/full.json")

    def test_chat_json_omits_unbounded_evidence_without_mutating_full_result(self):
        rows = [{"phone": "01234567890", "iccid": "8" * 20}] * 1000
        full = {"status": "success", "runId": "test", "summaryPath": "/tmp/full.json",
                "googleChatrDateFallback": {"iccid_conflict_count": 1000, "iccid_conflicts": rows},
                "taskD": {"filteredUnmatchedCount": 1000, "filteredUnmatchedCards": ["01234567890"] * 1000},
                "systemKoodaSource": {"rows": 1234, "exportCoverage": {"missing": rows}}}
        compact = MODULE.compact_launcher_result(full)
        self.assertEqual(compact["googleChatrDateFallback"]["iccid_conflict_count"], 1000)
        self.assertEqual(compact["googleChatrDateFallback"]["iccid_conflictsDetailCount"], 1000)
        self.assertEqual(compact["taskD"]["filteredUnmatchedCount"], 1000)
        self.assertEqual(compact["systemKoodaSource"]["rows"], 1234)
        self.assertNotIn("01234567890", json.dumps(compact))
        self.assertIs(full["googleChatrDateFallback"]["iccid_conflicts"], rows)
        self.assertEqual(compact["completeResultLocation"], "/tmp/full.json")

    def test_failed_chat_keeps_uncertain_counts_and_report(self):
        full = {"status": "failed", "runId": "test", "state": "/tmp/state.json",
                "report": "/tmp/failed.md", "finalWorkbook": None,
                "taskDUncertainSubmittedCount": 20, "taskDAutomaticRetryBlocked": True,
                "taskD": {"filteredUnmatchedCards": ["01234567890"], "filteredUnmatchedCount": 1}}
        compact = MODULE.compact_launcher_result(full)
        self.assertEqual(compact["taskDUncertainSubmittedCount"], 20)
        self.assertTrue(compact["taskDAutomaticRetryBlocked"])
        self.assertIsNone(compact["finalWorkbook"])
        self.assertEqual(compact["report"], "/tmp/failed.md")
        self.assertNotIn("01234567890", json.dumps(compact))


if __name__ == "__main__":
    unittest.main()
