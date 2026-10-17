"""Offline read-only progress aggregation; all evidence lives in temp receipts."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("task_f_progress_test", ROOT / "scripts/opensms_task_f_progress_status.py")
STATUS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STATUS)
RUN_ID = "20260907T020000+0800"
NOW = datetime(2026, 9, 6, 18, 10, tzinfo=timezone.utc)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="task-f-progress-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.receipts = self.root / "receipts"
        self.receipts.mkdir()
        self.state_path = self.receipts / f"opensms_patrol_runner_state_{RUN_ID}.json"
        self.state = {"runId": RUN_ID, "createdAt": "2026-09-06T18:00:00Z",
                      "updatedAt": "2026-09-06T18:09:00Z", "status": "running",
                      "runnerPid": 1234, "taskFStartedAt": "2026-09-06T18:01:00Z", "stages": {}}
        self.write(self.state_path, self.state)
        self.processes = [{"pid": 1234, "command": "/runtime/python /430/scripts/opensms_patrol_runner.py run --json"}]

    def write(self, path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def progress(self, total=600, completed=250, completed_batches=1, current_index=2, **changes):
        path = self.receipts / f"grabandgo_task_f_progress_{RUN_ID}.json"
        value = {"runId": RUN_ID, "status": "running", "updatedAt": "2026-09-06T18:08:00Z",
                 "totalInputCount": total, "completedCount": completed, "statusCounts": {"found": completed},
                 "requestedWorkers": 8, "effectiveWorkers": 8,
                 "batchCount": 3, "completedBatchCount": completed_batches,
                 "currentBatchIndex": current_index, "currentBatchInputCount": 250, **changes}
        return self.write(path, value)

    def batch(self, number=2, completed=100, **changes):
        path = self.receipts / f"grabandgo_task_f_progress_batch_{number:03d}_{RUN_ID}.json"
        self.write(path, {"status": "running", "updatedAt": "2026-09-06T18:09:00Z",
                          "totalInputCount": 250, "completedCount": completed,
                          "statusCounts": {"found": completed}, "requestedWorkers": 8,
                          "effectiveWorkers": 4, "uniqueAspNetSessionCount": 4,
                          "workerSessionMode": "isolated-http-sessions", **changes})
        return path

    def imported(self, number=1, total=250, inserted=250, kept=0, final=False, **changes):
        name = f"opensms_task_f_database_import_{RUN_ID}.json" if final else f"opensms_task_f_database_import_batch_{number:03d}_{RUN_ID}.json"
        return self.write(self.receipts / name, {"status": "passed", "createdAt": "2026-09-06T18:08:00Z",
            "resultCount": total, "terminalImportedCount": inserted, "alreadyTerminalKeptCount": kept, **changes})

    def inspect(self, processes=None, now=NOW):
        return STATUS.inspect_progress(self.state_path,
            processes=self.processes if processes is None else processes, now=now)

    def test_skipped_stage_with_duration_is_not_reported_as_preparing(self):
        self.state.update(status="success", stages={"taskF": {"status": "skipped", "executed": False,
            "reason": "no_missing_authoritative_dates", "completedAt": "2026-09-06T18:08:00Z", "durationSeconds": 1}})
        self.state.pop("taskFStartedAt")
        self.write(self.state_path, self.state)
        result = self.inspect(processes=[])
        self.assertEqual("skipped", result["phase"])
        self.assertIn("未运行", result["markdown"])
        self.assertEqual(0, result["elapsedSeconds"])

    def test_skip_with_query_evidence_is_contradictory_not_success(self):
        self.state["stages"] = {"taskF": {"status": "skipped", "executed": False}}
        self.write(self.state_path, self.state)
        self.progress()
        with self.assertRaisesRegex(STATUS.EvidenceError, "矛盾"):
            self.inspect()

    def test_adds_only_current_unaggregated_batch_and_counts_confirmed_imports(self):
        current = self.batch()
        self.progress(currentBatchProgress=str(current))
        self.imported()
        result = self.inspect()
        self.assertEqual((result["total"], result["completed"], result["persisted"], result["remaining"]), (600, 350, 250, 250))
        self.assertEqual(result["queriedNotPersisted"], 100)
        self.assertEqual(result["statusCounts"], {"found": 350})
        self.assertEqual((result["requestedWorkers"], result["effectiveWorkers"], result["independentSessionCount"]), (8, 4, 4))
        self.assertEqual(result["phase"], "in_progress")
        self.assertEqual(result["elapsedSeconds"], 540)
        self.assertTrue(result["readOnly"])
        self.assertFalse(result["networkAccess"])
        self.assertFalse(result["databaseAccess"])

    def test_current_batch_already_in_total_is_not_added_twice(self):
        current = self.batch(completed=250)
        self.progress(completed=500, completed_batches=2, current_index=2, currentBatchProgress=str(current))
        self.imported()
        self.imported(number=2)
        result = self.inspect()
        self.assertEqual(result["completed"], 500)
        self.assertEqual(result["persisted"], 500)
        self.assertEqual(result["statusCounts"], {"found": 500})

    def test_found_queries_are_not_persisted_without_import_receipt(self):
        current = self.batch(number=1, completed=100, totalInputCount=100)
        self.progress(total=100, completed=0, completed_batches=0, current_index=1,
                      batchCount=1, currentBatchInputCount=100, currentBatchProgress=str(current))
        result = self.inspect()
        self.assertEqual(result["completed"], 100)
        self.assertEqual(result["persisted"], 0)
        self.assertEqual(result["queriedNotPersisted"], 100)
        self.assertEqual(result["phase"], "importing")

    def test_import_errors_are_not_counted_as_persisted_and_kept_records_are_separate(self):
        self.progress(total=100, completed=100, completed_batches=1, current_index=1, batchCount=1)
        self.imported(total=100, inserted=90, kept=5, status="passed_with_transient_errors")
        result = self.inspect()
        self.assertEqual(result["persisted"], 95)
        self.assertEqual(result["newlyPersisted"], 90)
        self.assertEqual(result["retainedInDatabase"], 5)
        self.assertEqual(result["queriedNotPersisted"], 5)

    def test_final_aggregate_import_is_not_added_again_to_per_batch_imports(self):
        self.progress(total=500, completed=500, completed_batches=2, current_index=2, batchCount=2)
        self.imported()
        self.imported(number=2)
        self.imported(total=500, inserted=500, final=True)
        self.assertEqual(self.inspect()["persisted"], 500)

    def test_task_f_completed_does_not_claim_entire_pipeline_terminal(self):
        self.progress(total=100, completed=100, completed_batches=1, current_index=1, batchCount=1)
        self.imported(total=100, inserted=100, final=True)
        self.state["stages"]["taskF"] = {"executed": True, "status": "passed", "completedAt": "2026-09-06T18:08:00Z"}
        self.write(self.state_path, self.state)
        result = self.inspect()
        self.assertEqual(result["phase"], "completed")
        self.assertFalse(result["pipelineTerminal"])
        self.assertEqual(result["elapsedSeconds"], 420)
        self.state["status"] = "success"
        self.write(self.state_path, self.state)
        self.assertTrue(self.inspect(processes=[])["pipelineTerminal"])

    def test_failed_terminal_and_missing_process_are_distinct(self):
        self.progress(completed=0, completed_batches=0, current_index=1)
        self.assertEqual(self.inspect(processes=[])["phase"], "process_missing")
        self.state["status"] = "failed"
        self.write(self.state_path, self.state)
        self.assertEqual(self.inspect(processes=[])["phase"], "failed")

    def test_unrelated_runner_and_lookup_processes_do_not_count(self):
        self.progress(completed=0, completed_batches=0, current_index=1)
        unrelated = [{"pid": 9999, "command": "python /scripts/opensms_patrol_runner.py run"},
                     {"pid": 2222, "command": "node /scripts/grabandgo_task_f_lookup.mjs /another/receipts/old.json"}]
        self.assertEqual(self.inspect(processes=unrelated)["phase"], "process_missing")
        same_lookup = [{"pid": 2222, "command": f"node /scripts/grabandgo_task_f_lookup.mjs {self.receipts}/pending_{RUN_ID}.json"}]
        self.assertEqual(self.inspect(processes=same_lookup)["phase"], "in_progress")

    def test_cross_directory_cross_run_and_symlink_progress_paths_are_rejected(self):
        outside = self.root / f"grabandgo_task_f_progress_batch_002_{RUN_ID}.json"
        self.write(outside, {})
        link = self.receipts / f"linked_{RUN_ID}.json"
        link.symlink_to(outside)
        for path in (outside, link, self.receipts / "progress_20260907T030000+0800.json"):
            with self.subTest(path=path):
                self.progress(currentBatchProgress=str(path))
                with self.assertRaises(STATUS.EvidenceError):
                    self.inspect()

    def test_wrong_payload_run_and_evidence_older_than_run_are_rejected(self):
        for changes in ({"runId": "old-run"}, {"updatedAt": "2026-09-05T00:00:00Z"}):
            with self.subTest(changes=changes):
                self.progress(**changes)
                with self.assertRaises(STATUS.EvidenceError):
                    self.inspect()

    def test_stale_running_snapshot_is_explicitly_unknown(self):
        self.progress(completed=0, completed_batches=0, current_index=1)
        result = self.inspect(now=NOW + timedelta(minutes=20))
        self.assertTrue(result["stale"])
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["warnings"])

    def test_legacy_run_without_f_start_has_unknown_elapsed_or_terminal_duration(self):
        self.state.pop("taskFStartedAt")
        self.write(self.state_path, self.state)
        self.progress(completed=0, completed_batches=0, current_index=1)
        self.assertIsNone(self.inspect()["elapsedSeconds"])
        self.state["stages"]["taskF"] = {"executed": True, "status": "passed",
            "completedAt": "2026-09-06T18:08:00Z", "durationSeconds": 42.3, "terminalImportedCount": 0}
        self.write(self.state_path, self.state)
        self.progress(total=0, completed=0, completed_batches=0, current_index=0, batchCount=0)
        self.assertEqual(self.inspect()["elapsedSeconds"], 42)

    def test_no_progress_after_f_start_reports_preparing(self):
        result = self.inspect()
        self.assertEqual(result["phase"], "preparing")
        self.assertIn("准备中", result["markdown"])
        self.assertIsNone(result["countsAsOf"])
        self.assertIsNone(result["total"])
        self.assertIsNone(result["persisted"])

    def test_no_f_start_and_no_progress_reports_not_started(self):
        self.state.pop("taskFStartedAt")
        self.write(self.state_path, self.state)
        result = self.inspect()
        self.assertEqual(result["phase"], "not_started")
        self.assertIsNone(result["total"])
        self.assertIsNone(result["persisted"])

    def test_zero_completed_with_nonzero_status_counts_is_rejected(self):
        self.progress(completed=0, statusCounts={"found": 20})
        with self.assertRaisesRegex(STATUS.EvidenceError, "完成数与状态计数不一致"):
            self.inspect()

    def test_same_run_wrong_current_batch_path_is_rejected(self):
        current = self.batch(number=1)
        self.progress(currentBatchProgress=str(current))
        with self.assertRaisesRegex(STATUS.EvidenceError, "路径与批次索引不一致"):
            self.inspect()

    def test_zero_or_duplicate_import_batch_numbers_are_rejected(self):
        self.progress()
        zero = self.imported(number=0)
        with self.assertRaisesRegex(STATUS.EvidenceError, "批次为0或重复"):
            self.inspect()
        zero.unlink()
        normal = self.imported()
        duplicate = self.receipts / f"opensms_task_f_database_import_batch_1_{RUN_ID}.json"
        self.write(duplicate, json.loads(normal.read_text()))
        with self.assertRaisesRegex(STATUS.EvidenceError, "批次为0或重复"):
            self.inspect()

    def test_fresh_runner_timestamp_does_not_hide_stale_count_snapshot(self):
        self.progress(completed=0, completed_batches=0, current_index=1)
        later = NOW + timedelta(minutes=20)
        self.state["updatedAt"] = later.isoformat()
        self.write(self.state_path, self.state)
        result = self.inspect(now=later)
        self.assertEqual(result["countsAsOf"], "2026-09-06T18:08:00+00:00")
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["stale"])

    def test_status_labels_are_chinese_in_markdown(self):
        self.progress(total=4, completed=4, completed_batches=1, current_index=1, batchCount=1,
                      statusCounts={"found": 1, "activation_missing": 1, "not_found": 1, "error": 1})
        result = self.inspect()
        for label in ("命中 1", "未激活 1", "未找到 1", "错误 1"):
            self.assertIn(label, result["markdown"])

    def test_import_count_contradiction_fails_instead_of_inventing_completion(self):
        self.progress(completed=0, completed_batches=0, current_index=1)
        self.imported()
        with self.assertRaisesRegex(STATUS.EvidenceError, "入库数大于"):
            self.inspect()


if __name__ == "__main__":
    unittest.main()
