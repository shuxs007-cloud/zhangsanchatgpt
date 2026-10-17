"""Task D statistics wiring: no real subprocess, browser or business state."""
import json
from pathlib import Path
from unittest.mock import Mock
import unittest

import test_opensms_acquire_reliability as fixtures

RUNNER = fixtures.RUNNER


class RetentionRunnerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AcquisitionReliabilityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.runner.authorization["taskD"] = True
        self.calls = []
        self.fail_population = False
        self.runner.command = Mock(side_effect=self.command)
        self.runner.run_write = Mock(side_effect=AssertionError("任务D不得修改运营商"))
        self.notification_summary = {"status": "passed", "confirmedSubmissionCount": 4,
                                     "uniqueNotifiedPhoneCount": 3, "overlapPhoneCount": 1}
        self.runner.collect_task_d_notification_summary = Mock(return_value=self.notification_summary)
        self.summary = {
            "runId": self.runner.run_id, "status": "passed", "newTransferredCount": 0,
            "totalInRetentionCount": 123, "countingUnit": "unique_phone10",
            "asOf": "2026-09-06T13:00:00+08:00", "populationSha256": "a" * 64,
            "populationReceipt": "filled-by-mock", "byCarrier": [
                {"carrierName": "B14-C快下保留卡", "newTransferredCount": 0, "totalInRetentionCount": 23},
                {"carrierName": "B14-T快下保留卡", "newTransferredCount": 0, "totalInRetentionCount": 100},
            ],
        }
        candidates = []
        for suffix, target in (("chatr_to_b14_c", "B14-C快下保留卡"), ("kooda_to_b14_t", "B14-T快下保留卡")):
            candidates.append(str(self.fixture.save_json(suffix + ".json", {
                "route": "task_d_retain_" + suffix, "targetCarrierName": target, "count": 0, "rows": [],
            })))
        self.manifest = {
            "task": "D", "forceGate": True, "automationGate": True,
            "swapNotificationPath": "swap.json", "retentionNotificationPath": "retention.json",
            "retentionCarrierTransferEnabled": False, "retentionCarrierTransferStatus": "removed_by_user",
            "carrierCandidateCount": 0,
            "retentionReceiptPath": "retention-source.json", "swapExpireDate": "2026-09-06",
            "retentionExpireDate": "2026-09-13", "readOnlyPreparationConcurrent": True,
            "retentionUniqueCardCount": 398, "retentionMatchedCardCount": 254,
            "retentionActionableCardCount": 252, "filteredUnmatchedCount": 144,
            "filteredUnmatchedCards": [f"416{index:07d}" for index in range(144)],
            "neverProcessSourceExcludedCount": 2, "neverProcessSourceExcludedCards": ["6470000001", "6470000002"],
            "chatrCandidateCount": 0, "koodaCandidateCount": 0,
        }
        self.document = {"cardResult": str(self.fixture.source)}

    def write(self, spec):
        self.calls.append(spec["route"])
        return {**spec, "result": "no_candidate", "candidateCount": 0, "actualWrite": 0}

    def command(self, label, argv, **kwargs):
        self.calls.append(label)
        if label == "task_d_prepare" or label.startswith("task_d_prepare_resume_"):
            payload = self.manifest
        elif label.startswith("task_d_expiry_"):
            payload = {"status": "passed", "businessReceiptPassed": True,
                       "transactionalVerificationPassed": True,
                       "verificationMode": "synchronous_transactional_business_receipt",
                       "independentReadbackAvailable": False}
        elif label == "task_d_retention_population":
            self.assertTrue(self.runner.state["taskDCheckpoint"]["businessCompleted"])
            self.assertEqual(kwargs["env"]["OPENSMS_FORCE_TASK_D"], "1")
            if self.fail_population:
                raise RUNNER.PatrolError("offline simulated population failure")
            payload = {"runId": self.runner.run_id, "status": "passed"}
            Path(argv[-1]).write_text(json.dumps(payload))
            self.summary["populationReceipt"] = str(argv[-1])
        elif label == "task_d_retention_summary":
            params = [str(item) for item in argv]
            group_file = Path(params[params.index("--write-groups") + 1])
            bound = json.loads(group_file.read_text())
            self.assertEqual(bound["runId"], self.runner.run_id)
            self.assertEqual(bound["groups"], [])
            self.assertIs(bound["retentionCarrierTransferEnabled"], False)
            self.assertTrue(bound["businessCompletedAt"])
            payload = self.summary
            Path(argv[-1]).write_text(json.dumps(payload))
        else:
            raise AssertionError(label)
        return RUNNER.CommandResult(payload, "", "", 0)

    def test_statistics_follow_all_business_steps_and_survive_compact_json(self):
        result = self.runner.run_task_d(self.document, {"serverDate": "2026-09-06"})
        self.assertEqual(self.calls, ["task_d_prepare", "task_d_expiry_swap_zero",
            "task_d_expiry_retention_verified"])
        self.assertIsNone(result["retentionSummary"])
        self.runner.run_write.assert_not_called()
        self.assertIs(result["retentionCarrierTransferEnabled"], False)
        self.assertEqual(result["carrierCandidateCount"], 0)
        self.assertEqual(result["retentionSummaryContractVersion"], 2)
        self.assertFalse(result["retentionPopulationEnabled"])
        self.assertFalse(result["retentionPopulationReadExecuted"])
        compact = RUNNER.compact_launcher_result({"status": "success", "taskD": result})
        self.assertIs(compact["taskD"]["retentionCarrierTransferEnabled"], False)
        self.assertEqual(compact["taskD"]["retentionSummary"], result["retentionSummary"])
        self.assertEqual(compact["taskD"]["retentionMatchedCardCount"], 254)
        self.assertEqual(compact["taskD"]["retentionActionableCardCount"], 252)
        self.assertEqual(compact["taskD"]["filteredUnmatchedCount"], 144)
        self.assertNotIn("filteredUnmatchedCards", compact["taskD"])
        self.assertEqual(compact["taskD"]["filteredUnmatchedCardsDetailCount"], len(self.manifest["filteredUnmatchedCards"]))
        self.assertEqual(result["filteredUnmatchedCards"], self.manifest["filteredUnmatchedCards"])
        self.assertEqual(compact["taskD"]["carrierActualWrite"], 0)
        self.assertEqual(compact["taskD"]["notificationSummary"], self.notification_summary)
        self.runner.collect_task_d_notification_summary.assert_called_once()
        self.assertEqual(self.runner.state["stages"]["taskD"]["status"], "passed")

    def test_no_authorization_or_before_business_cannot_collect(self):
        self.runner.authorization["taskD"] = False
        with self.assertRaisesRegex(RUNNER.PatrolError, "入口已停用"):
            self.runner.collect_task_d_retention_summary([], {})
        self.runner.authorization["taskD"] = True
        with self.assertRaisesRegex(RUNNER.PatrolError, "入口已停用"):
            self.runner.collect_task_d_retention_summary([], {})
        self.runner.command.assert_not_called()

    def test_skip_d_has_unknown_statistics_and_no_read(self):
        self.runner.authorization["taskD"] = False
        result = self.runner.skip_task_d()
        self.assertIsNone(result["retentionSummary"])
        self.assertFalse(result["retentionPopulationReadExecuted"])
        self.runner.command.assert_not_called()

    def test_population_failure_preserves_completed_business_but_not_false_success(self):
        self.runner.collect_task_d_notification_summary.side_effect = RUNNER.PatrolError("notification summary failure")
        with self.assertRaisesRegex(RUNNER.PatrolError, "notification summary failure"):
            self.runner.run_task_d(self.document, {"serverDate": "2026-09-06"})
        checkpoint = self.runner.state["taskDCheckpoint"]
        self.assertTrue(checkpoint["businessCompleted"])
        self.assertEqual(checkpoint["notificationSummaryStatus"], "failed")
        self.assertNotIn("taskD", self.runner.state["stages"])
        self.runner.run_write.assert_not_called()
        result = self.runner.write_failure_report(RuntimeError("summary failed"))
        self.assertEqual(result["taskD"]["status"], "business_completed_notification_summary_failed")
        self.assertIsNone(result["taskD"]["retentionSummary"])
        self.assertEqual(result["taskD"]["filteredUnmatchedCount"], 144)
        compact = RUNNER.compact_launcher_result(result)
        self.assertEqual(compact["taskD"]["retentionActionableCardCount"], 252)
        report = Path(result["report"]).read_text(encoding="utf-8")
        self.assertIn("未配置运营商跳过 0；已删除运营商跳过 0；筛选未命中跳过 144（均不计成功）", report)

    def test_completed_d_counts_remain_when_later_e_fails(self):
        self.runner.run_task_d(self.document, {"serverDate": "2026-09-06"})
        result = self.runner.write_failure_report(RuntimeError("later E failed"))
        self.assertEqual(result["taskD"]["status"], "passed")
        self.assertIsNone(result["taskD"]["retentionSummary"])
        self.assertFalse(result["taskD"]["retentionPopulationEnabled"])
        self.assertNotIn("保留区统计", Path(result["report"]).read_text(encoding="utf-8"))

    def test_old_d_completion_cannot_replay_or_invent_statistics(self):
        self.runner.reusable_stage = Mock(return_value={"status": "passed", "executed": True})
        with self.assertRaisesRegex(RUNNER.PatrolError, "旧任务D断点"):
            self.runner.run_task_d(self.document, {"serverDate": "2026-09-06"})
        self.runner.command.assert_not_called()
        self.runner.run_write.assert_not_called()

    def test_old_transfer_manifest_is_blocked_before_any_notification(self):
        self.manifest["chatrCandidatePath"] = "old-candidate.json"
        with self.assertRaisesRegex(RUNNER.PatrolError, "转入保留区工作已删除"):
            self.runner.run_task_d(self.document, {"serverDate": "2026-09-06"})
        self.assertEqual(self.calls, ["task_d_prepare"])
        self.runner.run_write.assert_not_called()

    def test_old_transfer_checkpoint_is_blocked(self):
        self.runner.state["taskDCheckpoint"] = {"carrierRoutes": ["task_d_retain_chatr_to_b14_c"]}
        with self.assertRaisesRegex(RUNNER.PatrolError, "转入保留区工作已删除"):
            self.runner.run_task_d(self.document, {"serverDate": "2026-09-06"})
        self.assertEqual(self.calls, ["task_d_prepare"])

    def test_direct_write_entry_rejects_legacy_route_even_with_zero_or_cached_candidates(self):
        with self.assertRaisesRegex(RUNNER.PatrolError, "工作已删除"):
            RUNNER.PatrolRunner.run_write(self.runner, {"route": "task_d_retain_chatr_to_b14_c",
                                                      "target": "B14-C快下保留卡"})

    def failed_preparation(self):
        path = self.runner.receipts / f"opensms_task_d_mapping_{self.runner.run_id}_receipt.json"
        path.write_text(json.dumps({"runId": self.runner.run_id, "task": "D",
                                    "readOnly": True, "status": "failed", "errorCount": 12}))
        self.runner.args.resume_state = str(self.runner.state_path)
        self.runner.args.authorization_source = "current-user"
        return path

    def test_explicit_pre_submit_resume_preserves_old_receipt_and_uses_private_attempt(self):
        old = self.failed_preparation()
        before = old.read_bytes()
        self.runner.run_task_d(self.document, {"serverDate": "2026-09-06"})
        self.assertEqual(old.read_bytes(), before)
        command = self.runner.command.call_args_list[0]
        self.assertTrue(command.args[0].startswith("task_d_prepare_resume_"))
        attempt_dir = Path(command.args[1][-1])
        self.assertEqual(attempt_dir.parent, self.runner.receipts / "task_d_preparation_attempts")
        checkpoint = self.runner.state["taskDCheckpoint"]
        self.assertEqual(Path(checkpoint["manifest"]).parent, attempt_dir)
        self.assertEqual(checkpoint["preparationResumeHistory"][0]["failedMappingSha256"], RUNNER.sha256(old))

    def test_failed_preparation_cannot_auto_retry_or_ignore_any_submission_evidence(self):
        self.failed_preparation()
        self.runner.args.resume_state = None
        with self.assertRaisesRegex(RUNNER.PatrolError, "尚未提交通知"):
            self.runner.run_task_d(self.document, {"serverDate": "2026-09-06"})
        self.runner.args.resume_state = str(self.runner.state_path)
        progress = self.runner.receipts / f"opensms_task_d_swap_zero_{self.runner.run_id}_progress.json"
        progress.write_text('{}')
        with self.assertRaisesRegex(RUNNER.PatrolError, "尚未提交通知"):
            self.runner.run_task_d(self.document, {"serverDate": "2026-09-06"})
        self.runner.command.assert_not_called()

    def test_resumed_manifest_uses_registered_attempt_without_repreparing(self):
        directory = self.runner.receipts / "task_d_preparation_attempts" / "123"
        directory.mkdir(parents=True)
        manifest = directory / f"opensms_task_d_{self.runner.run_id}_manifest.json"
        manifest.write_text(json.dumps(self.manifest))
        self.runner.state["taskDCheckpoint"] = {"manifest": str(manifest)}
        self.runner.run_task_d(self.document, {"serverDate": "2026-09-06"})
        self.assertEqual(self.calls, ["task_d_expiry_swap_zero", "task_d_expiry_retention_verified"])

    def test_cross_run_or_missing_registered_manifest_cannot_reprepare(self):
        for filename in ("other.json", f"opensms_task_d_{self.runner.run_id}_manifest.json"):
            with self.subTest(filename=filename):
                self.runner.state["taskDCheckpoint"] = {"manifest": str(self.fixture.inputs / filename)}
                with self.assertRaises(RUNNER.PatrolError):
                    self.runner.run_task_d(self.document, {"serverDate": "2026-09-06"})
        self.runner.command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
