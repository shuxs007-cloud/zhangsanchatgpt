"""Report-run Google retirement regressions, with every external process mocked."""

import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_opensms_acquire_reliability as fixtures


RUNNER = fixtures.RUNNER
DEPENDENCY = {
    "enabled": False, "status": "skipped", "reason": "user_disabled",
    "unused_port_device_scope": "system_source_only",
}


class RunnerNoGoogleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AcquisitionReliabilityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner

    def test_manual_and_task_a_contracts_force_disabled_even_with_inherited_override(self):
        for only in (False, True):
            with self.subTest(task_a_only=only):
                self.runner.args.task_a_only = only
                contract = RUNNER.execution_contract(only)
                self.assertIs(contract["googleAssociationEnabled"], False)
                self.assertEqual(contract["googleDisabledReason"], "user_disabled")
                self.assertEqual(contract["unusedPortDeviceScope"], "system_source_only")
                self.assertEqual(contract["taskFEnabledForRun"], not only)
                with patch.dict(os.environ, {"OPENSMS_GOOGLE_ASSOCIATION_ENABLED": "1"}):
                    child = self.runner.child_environment({"OPENSMS_GOOGLE_ASSOCIATION_ENABLED": "1"})
                    self.assertEqual(child["OPENSMS_GOOGLE_ASSOCIATION_ENABLED"], "0")
                    self.assertEqual(os.environ["OPENSMS_GOOGLE_ASSOCIATION_ENABLED"], "1")

    def test_all_four_independent_sources_overlap_and_google_is_not_scheduled(self):
        barrier = threading.Barrier(4, timeout=5)
        independent = {"export", "active_carriers", "balance", "card_retention_report"}
        self.runner.args.io_workers = 9

        def command(label, argv, **kwargs):
            if label == "google":
                raise AssertionError("Google must never be accessed")
            if label == "export":
                self.fixture.publish_submission_ready(kwargs["env"])
            if label in independent:
                barrier.wait()
            return self.fixture.command(label, argv, **kwargs)

        self.runner.command = Mock(side_effect=command)
        result = self.runner.acquire()
        self.assertEqual(result["readConcurrency"], 4)
        self.fixture.assert_google_skipped(result)
        self.fixture.assert_verified_date(result)
        self.assertEqual(result["systemKoodaRows"], 1)

    def test_legacy_google_checkpoint_is_rejected_before_connecting(self):
        args = RUNNER.build_parser().parse_args(["run"])
        for state in (
            {"stages": {"acquire": {"association": "/old/google.xlsx"}}},
            {"stages": {"document": {"cardResult": "/old/document.xlsx"}}},
            {"partialAcquisition": {"association": "/old/google.xlsx"}},
        ):
            with self.subTest(state=state), patch.object(RUNNER, "resolve_bitbrowser_430_cdp") as cdp:
                with self.assertRaisesRegex(RUNNER.PatrolError, "旧报表断点"):
                    RUNNER.PatrolRunner(args, state)
                cdp.assert_not_called()

    def test_old_acquisition_and_document_stages_are_never_reused(self):
        self.runner.reusable_stage = Mock(return_value={"association": "/old/google.xlsx"})
        self.runner.command = Mock(side_effect=AssertionError("must reject before network"))
        with self.assertRaisesRegex(RUNNER.PatrolError, "旧采集断点"):
            self.runner.acquire()
        self.runner.command.assert_not_called()
        self.runner.reusable_stage = Mock(return_value={"systemKoodaSha256": "same-sha"})
        with self.assertRaisesRegex(RUNNER.PatrolError, "旧文档断点"):
            self.runner.start_document({"systemKoodaSha256": "same-sha", **self.fixture.retention_acquisition()})
        with self.assertRaisesRegex(RUNNER.PatrolError, "旧文档断点"):
            self.runner.finish_document()

    def test_document_accepts_absent_association_and_preserves_frozen_system_inputs(self):
        self.runner.verify_system_kooda_acquisition = Mock()
        self.runner.freeze_report_date_cache = Mock(return_value={"path": "/frozen.json", "sha256": "frozen-sha"})
        self.runner.mark_artifact_operation = Mock()
        acquired = {
            "processingSource": str(self.fixture.source), "serverDate": "2026-09-06",
            "processingSystemKooda": "/system.json", "systemKoodaSha256": "system-sha",
            **self.fixture.retention_acquisition(),
        }
        with patch.object(RUNNER.subprocess, "Popen", return_value=SimpleNamespace()) as process:
            self.runner.start_document(acquired)
        self.addCleanup(self.runner.document_log_handle.close)
        argv = process.call_args.args[0]
        env = process.call_args.kwargs["env"]
        self.assertIn("--no-google", argv)
        self.assertNotIn("--association", argv)
        self.assertEqual(env["OPENSMS_GOOGLE_ASSOCIATION_ENABLED"], "0")
        self.assertEqual(env["OPENSMS_SYSTEM_KOODA_SHA256"], "system-sha")
        self.assertEqual(env["OPENSMS_REPORT_DATE_CACHE_SHA256"], "frozen-sha")

    def document_completion(self, dependency):
        acquired = self.fixture.retention_acquisition()
        self.runner.state['stages']['acquire'] = acquired
        retained = {'source_sha256': acquired['cardRetentionSha256'], 'run_id': self.runner.run_id}
        receipt = self.fixture.save_json("document-receipt.json", {"process": {"google_dependency": dependency,
            'card_retention_report': retained}, 'verification': {'card_retention_report_review': {**retained, 'matches': True}}})
        dispatch = self.fixture.save_json("dispatch.json", {"inputs": {"google": None}})
        payload = {
            "result": "成功", "output_path": str(self.fixture.source),
            "receipt_path": str(receipt), "dispatch_receipt_path": str(dispatch),
            "processing_mode": "local", "duration_seconds": 0,
        }
        log = self.runner.run_dir / "document.log"
        log.write_text(json.dumps(payload), encoding="utf-8")
        self.runner.state["documentLog"] = str(log)
        self.runner.document_log_handle = Mock()
        self.runner.document_process = SimpleNamespace(wait=Mock(return_value=0))

    def test_success_toast_without_google_disabled_receipt_cannot_enter_writes(self):
        for dependency in ({}, {**DEPENDENCY, "enabled": True}, {**DEPENDENCY, "reason": "network_failure"}):
            with self.subTest(dependency=dependency):
                self.document_completion(dependency)
                with self.assertRaisesRegex(RUNNER.PatrolError, "文档回执未证明Google依赖已关闭"):
                    self.runner.finish_document()
                self.assertNotIn("document", self.runner.state["stages"])

    def test_document_stage_preserves_independent_disabled_receipt(self):
        self.document_completion(DEPENDENCY)
        result = self.runner.finish_document()
        self.assertIs(result["googleAssociationEnabled"], False)
        self.assertEqual(result["googleDependency"], DEPENDENCY)
        self.assertTrue(Path(result["cardReceipt"]).is_file())
        self.assertEqual(json.loads(self.runner.state_path.read_text())["stages"]["document"]["googleDependency"], DEPENDENCY)

    def test_finalize_integrity_checks_only_source_and_final_workbooks(self):
        config = self.fixture.save_json("finalize-config.json", {})
        self.runner.build_finalize_config = Mock(return_value=config)
        self.runner.command = Mock()
        self.runner.state["portSubset"] = {"output": "/old/subset.xlsx"}

        def command(label, argv, **kwargs):
            args = [str(value) for value in argv]
            if label == "finalize_report":
                Path(args[3]).write_text("离线完整报告" * 500, encoding="utf-8")
            elif label.startswith("audit_"):
                Path(args[args.index("--output") + 1]).write_text(json.dumps({
                    "summary": {"passed": 1, "checks_total": 1, "failed": 0, "failed_writes": 0},
                }), encoding="utf-8")
            elif label != "delivery_manifest":
                raise AssertionError(label)
            return RUNNER.CommandResult({}, "{}", "", 0)

        self.runner.command.side_effect = command
        with patch.object(RUNNER, "ROOT", fixtures.ROOT), patch.object(RUNNER, "ensure_xlsx") as integrity, \
                patch.object(RUNNER, "STATE_ROOT", self.runner.delivery / "test-state"), \
                patch("opensms_system_missing_f_dates.export_difference", return_value={}):
            # This integrity-only fixture must not read the real date library.
            # Supply the current acquisition contract rather than omit its hash.
            result = self.runner.finalize({"sourceArchive": str(self.fixture.source),
                                           "sourceSha256": RUNNER.sha256(self.fixture.source)}, {}, {}, {
                "finalWorkbook": str(self.fixture.source),
                "finalWorkbookSha256": "offline-fixture-sha",
            })
        self.assertEqual(integrity.call_count, 2)
        self.assertEqual({call.args[1] for call in integrity.call_args_list}, {"交付源表", "交付最终表"})
        self.assertEqual(result["auditFailed"], 0)
        self.assertIsNone(self.runner.state["portSubset"])
        self.assertFalse(list(self.runner.delivery.glob("OpenSMS指定端口汇总_*.xlsx")))
        self.assertTrue(self.runner.state["vGroupSwapDaily"]["independentReadbackPassed"])


if __name__ == "__main__":
    unittest.main()
