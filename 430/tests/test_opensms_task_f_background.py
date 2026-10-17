"""Offline detached-F tests. All business subprocesses are forbidden/mocked."""
import argparse
import fcntl
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import opensms_patrol_runner as runner_module
import opensms_task_f_background as background
import opensms_task_f_progress_status as progress


class BackgroundFTests(unittest.TestCase):
    def setUp(self):
        # Some legacy suites load the runner under the public module name.
        # Bind that import explicitly and guard all subprocesses even if a
        # future patch target drifts; fixtures must never run real commands.
        module_guard = patch.dict(sys.modules, {"opensms_patrol_runner": runner_module})
        module_guard.start()
        self.addCleanup(module_guard.stop)
        for name in ("run", "Popen"):
            command_guard = patch.object(runner_module.subprocess, name,
                                         side_effect=AssertionError("real subprocess forbidden in offline test"))
            command_guard.start()
            self.addCleanup(command_guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        storage_root = patch.object(background, "ROOT", self.root)
        storage_root.start()
        self.addCleanup(storage_root.stop)
        constants = patch.multiple(runner_module,
            TASK_F_DB=self.root / "default-test.sqlite3",
            TASK_F_CATALOG_SOURCE=self.root / "default-catalog.json",
            TASK_F_LATEST_LIBRARY=self.root / "default-library.json",
            STATE_ROOT=self.root / "state", RUN_LOG_ROOT=self.root / "logs")
        constants.start()
        self.addCleanup(constants.stop)
        endpoint = patch("opensms_bitbrowser_430.resolve_current_430_cdp",return_value="http://127.0.0.1:49343")
        endpoint.start();self.addCleanup(endpoint.stop)
        self.run_id = "20260907T120000+0800"
        self.runner = runner_module.PatrolRunner.__new__(runner_module.PatrolRunner)
        r = self.runner
        r.args = argparse.Namespace(task_a_only=False, cdp="http://127.0.0.1:60000")
        r.run_id = self.run_id
        r.delivery = self.root / "main"
        r.delivery.mkdir()
        r.receipts = r.delivery / "receipts"
        r.receipts.mkdir()
        r.state_path = r.receipts / f"opensms_patrol_runner_state_{self.run_id}.json"
        r.state = {"runId": self.run_id, "status": "running", "createdAt": "2026-09-07T12:00:00+08:00",
                   "stages": {}, "timings": []}
        r.api_target_id = "parent-only-target"
        self.document = self.root / "document.xlsx"
        self.document.write_bytes(b"offline fixture workbook bytes")
        self.pagination_evidence = self.file("inventory.json.pagination.json", {
            "status": "passed", "runId": self.run_id,
            "attemptCount": 1, "recovered": False})
        self.inventory = self.file("inventory.json", {"status": "passed", "runId": self.run_id,
            "systemRows": [], "totalRows": 0, "paginationTotalStable": True,
            "paginationBackendIdsUnique": True, "silentBrowserMode": True,
            "browserForegroundActivationAttempted": False,
            "paginationEvidencePath": str(self.pagination_evidence),
            "paginationEvidenceSha256": background.digest(self.pagination_evidence),
            "paginationAttemptCount": 1, "paginationRecovered": False})
        self.acquire = {"systemKoodaInventory": str(self.inventory),
                        "systemKoodaSha256": background.digest(self.inventory)}
        self.proof = self.file("proof.json", {"result": "成功", "checks": {"source": True},
            "failed_checks": [], "output_path": str(self.document), "verification": {
                "system_kooda_source_review": {"matches": True, "run_id": self.run_id,
                    "snapshot_sha256": self.acquire["systemKoodaSha256"],
                    "missing_activation_rows": 1, "missing_cutoff_rows": 1,
                    "missing_either_date_rows": 1},
                "task_f_missing_activation_red_review": {"matches": True, "expected_rows": 1}}})
        self.doc = {"cardResult": str(self.document), "cardReceipt": str(self.proof)}
        self.cache = self.file("cache.json", {"schemaVersion": 1, "status": "passed", "runId": self.run_id,
                                               "recordCount": 0, "records": []})
        r.state["reportDateCache"] = {"path": str(self.cache), "sha256": background.digest(self.cache)}
        self.jobs_patch = patch.object(background, "JOBS_ROOT", self.root / "jobs")
        self.jobs_patch.start()
        self.addCleanup(self.jobs_patch.stop)

    def file(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def prepare(self):
        with patch.object(background.subprocess, "Popen", return_value=Mock(pid=9876)) as spawn:
            result = background.prepare_and_start(self.runner, self.doc, self.acquire)
        return result, spawn

    def test_detach_verified_inputs_without_parent_api_or_business_gates(self):
        result, spawn = self.prepare()
        kwargs = spawn.call_args.kwargs
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["close_fds"])
        self.assertNotIn("OPENSMS_RAW_API_TARGET_ID", kwargs["env"])
        self.assertTrue(result["mainReportIndependent"])
        self.assertTrue(result["resultsApplyNextExport"])
        self.assertEqual(result["requestedWorkers"], 8)
        self.assertFalse(result["notifyIncrementalWorkbook"])
        self.assertEqual(result["status"], "running")
        job = background.validate_job(Path(result["backgroundJob"]), result["backgroundJobSha256"])
        self.assertEqual(job["sessionPolicy"],"existing-bitbrowser-430")
        self.assertEqual(job["browserProfileId"],background.PROFILE_ID)
        self.assertIsNone(job["cdpEndpoint"])
        self.assertTrue(job["useCurrent430Session"])
        self.assertNotIn(self.runner.delivery, Path(job["backgroundState"]).parents)
        self.assertEqual(job["inputs"]["document"]["sha256"], background.digest(self.document))
        bound = job["inputs"]["inventoryPaginationEvidence"]
        self.assertEqual(bound["sourcePath"], str(self.pagination_evidence.resolve()))
        self.assertEqual(bound["sha256"], background.digest(self.pagination_evidence))
        self.assertEqual(Path(bound["path"]).read_bytes(), self.pagination_evidence.read_bytes())
        self.assertEqual(Path(job["inputs"]["inventory"]["path"]).read_bytes(), self.inventory.read_bytes())

    def test_zero_missing_dates_does_not_spawn_or_create_f_file(self):
        proof = background.read_json(self.proof)
        proof["verification"]["system_kooda_source_review"]["missing_either_date_rows"] = 0
        proof["verification"]["task_f_missing_activation_red_review"]["expected_rows"] = 0
        self.proof.write_text(json.dumps(proof))
        result, spawn = self.prepare()
        spawn.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertIsNone(result["incrementalWorkbook"])
        self.assertFalse((self.root / "jobs").exists())

    def test_automatic_job_refuses_parent_browser_port_or_profile_tamper(self):
        result,_=self.prepare();job_path=Path(result["backgroundJob"])
        original=background.read_json(job_path)
        for changes in ({"cdpEndpoint":self.runner.args.cdp},{"browserProfileId":"another-profile"},
                        {"sessionPolicy":"independent-login"},{"useCurrent430Session":False}):
            job_path.write_text(json.dumps({**original,**changes}))
            with self.assertRaisesRegex(RuntimeError,"固定会话策略"):
                background.validate_job(job_path,background.digest(job_path))

    def test_cutoff_only_missing_still_dispatches(self):
        proof = background.read_json(self.proof)
        proof["verification"]["task_f_missing_activation_red_review"]["expected_rows"] = 0
        self.proof.write_text(json.dumps(proof))
        result, spawn = self.prepare()
        spawn.assert_called_once()
        self.assertEqual(result["documentMissingDateRows"], 1)
        self.assertEqual(result["documentMissingActivationRows"], 0)

    def test_filled_creation_dates_still_dispatch_eight_worker_background_job(self):
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = "sheet1"
        sheet.append(["电话", "opr", "卡槽", "ICCID", "激活日期", "截止日"])
        sheet.append(["14385550001", "Kooda", "130_1", "8912230200000000001F", "2026-09-01", "2026-09-30"])
        book.save(self.document)
        book.close()
        inventory = background.read_json(self.inventory)
        inventory.update(totalRows=1, systemRows=[{"id": "id-1", "opr": "Kooda", "slotIdentif": "130_1.01",
                         "cardNo": "4385550001", "cardStore": "8912230200000000001F"}])
        self.inventory.write_text(json.dumps(inventory))
        self.acquire["systemKoodaSha256"] = background.digest(self.inventory)
        proof = background.read_json(self.proof)
        proof["verification"]["system_kooda_source_review"].update(missing_either_date_rows=0,
            missing_activation_rows=0, missing_cutoff_rows=0, snapshot_sha256=self.acquire["systemKoodaSha256"])
        proof["verification"]["task_f_missing_activation_red_review"]["expected_rows"] = 0
        proof["verification"]["date_resolution_review"] = {"matches": True, "creation_date_fallback_rows": [
            {"row": 2, "phone": "14385550001", "activation": "2026-09-01", "cutoff": "2026-09-30"}]}
        self.proof.write_text(json.dumps(proof))
        result, spawn = self.prepare()
        spawn.assert_called_once()
        self.assertEqual(1, result["documentCreationFallbackRows"])
        self.assertEqual(1, result["documentLookupNeededRows"])
        self.assertEqual(0, result["documentMissingDateRows"])
        self.assertEqual(8, result["requestedWorkers"])

    def test_duplicate_job_does_not_dispatch_second_worker(self):
        self.prepare()
        with patch.object(background.subprocess, "Popen") as spawn, self.assertRaises(FileExistsError):
            background.prepare_and_start(self.runner, self.doc, self.acquire)
        spawn.assert_not_called()

    def test_copied_input_tamper_rejected_before_any_business(self):
        result, _ = self.prepare()
        job = background.read_json(Path(result["backgroundJob"]))
        Path(job["inputs"]["inventory"]["path"]).write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "输入身份或哈希"):
            background.validate_job(Path(result["backgroundJob"]), result["backgroundJobSha256"])

    def test_copied_pagination_sidecar_tamper_rejected_before_any_business(self):
        result, _ = self.prepare()
        job = background.read_json(Path(result["backgroundJob"]))
        Path(job["inputs"]["inventoryPaginationEvidence"]["path"]).write_text("{}")
        with patch.object(runner_module.PatrolRunner, "run_task_f") as run:
            with self.assertRaisesRegex(RuntimeError, "输入身份或哈希"):
                background.run_job(Path(result["backgroundJob"]), result["backgroundJobSha256"])
        run.assert_not_called()

    def test_missing_copied_pagination_sidecar_rejected_before_any_business(self):
        result, _ = self.prepare()
        job = background.read_json(Path(result["backgroundJob"]))
        Path(job["inputs"]["inventoryPaginationEvidence"]["path"]).unlink()
        with patch.object(runner_module.PatrolRunner, "run_task_f") as run:
            with self.assertRaisesRegex(RuntimeError, "输入身份或哈希"):
                background.run_job(Path(result["backgroundJob"]), result["backgroundJobSha256"])
        run.assert_not_called()

    def test_startup_failure_is_recorded_only_for_owned_queued_job(self):
        result, _ = self.prepare()
        job = Path(result["backgroundJob"])
        background.record_startup_failure(job, result["backgroundJobSha256"], "bad input hash")
        state = background.read_json(Path(result["backgroundState"]))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "bad input hash")
        self.assertTrue(Path(state["startupFailureReceipt"]).is_file())

    def test_contended_job_lock_does_not_mutate_the_owner_state(self):
        result, _ = self.prepare()
        job = Path(result["backgroundJob"])
        state_path = Path(result["backgroundState"])
        before = state_path.read_bytes()
        with (job.parent / "job.lock").open("a+b") as owner:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            background.record_startup_failure(job, result["backgroundJobSha256"], "contended")
        self.assertEqual(state_path.read_bytes(), before)
        self.assertFalse((job.parent / "startup_failure.json").exists())

    def test_a_only_never_dispatches_f(self):
        self.runner.args.task_a_only = True
        with patch.object(background.subprocess, "Popen") as spawn, self.assertRaisesRegex(RuntimeError, "A-only"):
            background.prepare_and_start(self.runner, self.doc, self.acquire)
        spawn.assert_not_called()

    def test_launch_error_only_marks_f_and_does_not_raise_to_main(self):
        with patch.object(background, "prepare_and_start", side_effect=OSError("mock launch failure")):
            result = self.runner.start_background_task_f(self.doc, self.acquire)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.runner.state["status"], "running")
        self.assertTrue(result["mainReportIndependent"])

    def test_date_cache_is_reused_and_tamper_fails_closed(self):
        with patch.object(self.runner, "command") as command:
            first = self.runner.freeze_report_date_cache()
            command.assert_not_called()
            self.cache.write_text("{}")
            with self.assertRaisesRegex(runner_module.PatrolError, "变化"):
                self.runner.freeze_report_date_cache()
            command.assert_not_called()
        self.assertEqual(first["sha256"], self.runner.state["reportDateCache"]["sha256"])

    def test_status_recognizes_detached_pid(self):
        state = {"runId": self.run_id, "runnerPid": 9876}
        found = progress.owned_processes(state, self.runner.receipts, [{"pid": 9876,
            "command": f"python /scripts/opensms_task_f_background.py --job /jobs/{self.run_id}/job.json"}])
        self.assertEqual(found, [{"pid": 9876, "kind": "runner"}])

    def test_main_status_delegates_only_through_verified_job_binding(self):
        result, _ = self.prepare()
        self.runner.state["stages"]["taskF"] = result
        self.runner.state["createdAt"] = datetime.now(timezone.utc).isoformat()
        self.runner._save_state()
        child_path = Path(result["backgroundState"])
        child = background.read_json(child_path)
        child["runnerPid"] = 9876
        child["status"] = "running"
        child_path.write_text(json.dumps(child))
        processes = [{"pid": 9876, "command": f"python opensms_task_f_background.py --job {result['backgroundJob']}"}]
        observed = progress.inspect_progress(self.runner.state_path, processes=processes)
        self.assertTrue(observed["background"])
        self.assertEqual(observed["parentState"], str(self.runner.state_path.resolve()))
        Path(result["backgroundJob"]).write_text("{}")
        with self.assertRaisesRegex(progress.EvidenceError, "哈希"):
            progress.inspect_progress(self.runner.state_path, processes=processes)

    def test_committed_first_batch_still_exports_delta_if_later_batch_fails(self):
        r = self.runner
        r.state["backgroundTaskF"] = True
        database = self.root / "mock.sqlite3"
        receipt = r.receipts / f"opensms_task_f_database_import_batch_001_{self.run_id}.json"
        receipt.write_text(json.dumps({"database": str(database), "activationDateUpdatedCount": 1}))
        def export(label, argv, **kwargs):
            self.assertEqual(label, "task_f_export_confirmed_partial_dates")
            self.assertEqual(argv[argv.index("--import-receipt") + 1], receipt)
            output = Path(argv[argv.index("--output") + 1])
            output.write_bytes(b"mock verified delta")
            return runner_module.CommandResult({"status": "success", "verificationPassed": True,
                                                "generated": True, "recordCount": 1}, "", "", 0)
        r.command = Mock(side_effect=export)
        with patch.object(runner_module, "TASK_F_DB", database), patch.object(
                r, "_run_task_f_locked", side_effect=RuntimeError("second batch failed")), self.assertRaisesRegex(RuntimeError, "second batch"):
            r.run_task_f()
        self.assertTrue(r.state["stages"]["taskF"]["notifyIncrementalWorkbook"])
        self.assertEqual(r.state["stages"]["taskF"]["newActivationImportedCount"], 1)

    def test_child_failure_cannot_change_main_inputs_or_state(self):
        result, _ = self.prepare()
        main_before = {p: p.read_bytes() for p in (
            self.document, self.proof, self.inventory, self.pagination_evidence, self.cache)}
        with patch.object(runner_module.PatrolRunner, "run_task_f", side_effect=RuntimeError("mock F failure")):
            outcome = background.run_job(Path(result["backgroundJob"]), result["backgroundJobSha256"])
        self.assertEqual(outcome["status"], "failed")
        for path, content in main_before.items():
            self.assertEqual(path.read_bytes(), content)
        with patch.object(runner_module.PatrolRunner, "run_task_f") as run, self.assertRaisesRegex(RuntimeError, "重入"):
            background.run_job(Path(result["backgroundJob"]), result["backgroundJobSha256"])
        run.assert_not_called()

    def test_no_main_join_or_f_result_reads_before_finalize(self):
        import inspect
        text = inspect.getsource(runner_module.PatrolRunner.execute)
        normal = text[text.index("acquire = self.acquire()"):]
        self.assertIn("self.start_background_task_f(document, acquire)", normal)
        self.assertNotIn("self.run_task_f()", normal)
        self.assertNotIn(".join(", normal)
        self.assertNotIn("backgroundState", normal)
        self.assertLess(normal.index("self.start_background_task_f"), normal.index("self.run_standard_writes"))

    def prepare_manual_retry(self):
        original, _ = self.prepare()
        state_path = Path(original["backgroundState"])
        state = background.read_json(state_path)
        state["status"] = "failed"
        state_path.write_text(json.dumps(state))
        before = state_path.read_bytes()
        with patch.object(progress, "inspect_progress", return_value={"completed": 0, "processes": []}), patch.object(background.subprocess, "Popen", return_value=Mock(pid=9888)):
            result = background.retry_current_session(Path(original["backgroundJob"]), original["backgroundJobSha256"])
        self.assertEqual(state_path.read_bytes(), before)
        return original, result

    def test_manual_retry_is_bound_and_refuses_duplicate(self):
        original, result = self.prepare_manual_retry()
        job_path = Path(result["backgroundJob"])
        job = background.validate_job(job_path, result["backgroundJobSha256"])
        self.assertTrue(job["useCurrent430Session"])
        self.assertEqual(job["retrySourceJobSha256"], original["backgroundJobSha256"])
        with patch.object(progress, "inspect_progress", return_value={"completed": 0, "processes": []}), self.assertRaisesRegex(RuntimeError, "未终态"):
            background.retry_current_session(Path(original["backgroundJob"]), original["backgroundJobSha256"])
        job["inputs"]["inventory"]["sha256"] = "0" * 64
        job_path.write_text(json.dumps(job))
        with self.assertRaisesRegex(RuntimeError, "不得改变"):
            background.validate_job(job_path, background.digest(job_path))

    def test_manual_retry_refuses_existing_query(self):
        original, _ = self.prepare_manual_retry()
        with patch.object(progress, "inspect_progress", return_value={"completed": 1, "processes": []}), self.assertRaisesRegex(RuntimeError, "已有查询"):
            background.retry_current_session(Path(original["backgroundJob"]), original["backgroundJobSha256"])

    def test_manual_retry_runs_existing_full_f_chain(self):
        self.run_complete_mocked_job(1, manual_session=True)

    def run_complete_mocked_job(self, pending_count, remove_original=False, manual_session=False):
        if manual_session:
            _, result = self.prepare_manual_retry()
        else:
            result, _ = self.prepare()
        if remove_original:
            self.document.unlink()
            self.proof.unlink()
            self.inventory.unlink()
            self.pagination_evidence.unlink()
            self.cache.unlink()
        database = self.root / "mock-production.sqlite3"
        catalog = self.file("catalog.json", {})
        calls = []
        def write_json(path, payload):
            Path(path).write_text(json.dumps(payload), encoding="utf-8")
        def command(worker, label, argv, **kwargs):
            calls.append(label)
            def arg(name):
                return Path(argv[argv.index(name) + 1])
            payload = {"status": "passed"}
            if label == "task_f_catalog_import":
                write_json(arg("--receipt"), payload)
            elif label == "task_f_prepare_pending":
                payload = {"reportBackfillMode": True, "documentRunId": self.run_id,
                           "documentSha256": background.digest(arg("--document")),
                           "documentPath": str(arg("--document")),
                           "pendingCount": pending_count,
                           "rows": [{"queryIccid": "891223020025159114"}] * pending_count}
                write_json(arg("--output"), payload)
            elif label == "task_f_login_check":
                self.assertEqual(argv[2], "--check-login")
                self.assertEqual(kwargs["env"]["BITBROWSER_CDP"],"http://127.0.0.1:49343")
                payload = {"status": "authenticated"}
                write_json(argv[3], payload)
            elif label == "task_f_database_stats_no_gap":
                payload.update(recordSchemaVersion=2)
            elif label.startswith("task_f_database_import_batch_"):
                payload = {"status": "passed", "database": str(database), "runId": self.run_id,
                    "documentRunId": self.run_id, "resultCount": 1, "terminalImportedCount": 1,
                    "foundImportedCount": 1, "activationDateUpdatedCount": 1,
                    "rows": [{"queryIccid": "891223020025159114", "status": "found",
                              "activationDate": "2026-09-07", "cutoffDate": "2026-10-06",
                              "activationDateUpdated": True}]}
                payload["lookupReceipt"] = str(arg("--lookup"))
                payload["lookupSha256"] = background.digest(arg("--lookup"))
                write_json(arg("--receipt"), payload)
            elif label == "task_f_export_latest_iccid_library":
                payload = {"recordCount": pending_count, "foundCount": pending_count}
                write_json(arg("--output"), payload)
            elif label == "task_f_database_snapshot":
                arg("--output").write_bytes(b"mock snapshot")
            elif label == "task_f_export_incremental_dates":
                imported = background.read_json(arg("--import-receipt"))
                self.assertEqual(imported["activationDateUpdatedCount"], pending_count)
                self.assertEqual(imported["runId"], self.run_id)
                self.assertEqual(arg("--db"), database)
                payload = {"status": "success", "generated": bool(pending_count),
                           "recordCount": pending_count, "verificationPassed": True}
                if pending_count:
                    arg("--output").write_bytes(b"mock independently verified xlsx")
                write_json(arg("--receipt"), payload)
            else:
                raise AssertionError(f"unexpected external command {label}")
            return runner_module.CommandResult(payload, "", "", 0.001)
        def lookup(worker, index, pending, output, progress_path, env):
            self.assertEqual(env["OPENSMS_TASK_F_INDEPENDENT_LOGIN_ONLY"], "0")
            self.assertEqual(env["OPENSMS_TASK_F_CURRENT_SESSION_ONLY"], "1")
            payload = {"status": "passed", "runId": self.run_id, "statusCounts": {"found": 1},
                       "results": [{"queryIccid": "891223020025159114", "status": "found"}],
                       "requestedWorkers": 8, "effectiveWorkers": 1,
                       "uniqueAspNetSessionCount": 1, "perWorkerDelayMs": 1000}
            write_json(output, payload)
            return payload
        with patch.object(runner_module, "TASK_F_DB", database), patch.object(
                runner_module, "TASK_F_CATALOG_SOURCE", catalog), patch.object(
                runner_module.PatrolRunner, "command", command), patch.object(
                runner_module.PatrolRunner, "task_f_lookup_batch", lookup):
            outcome = background.run_job(Path(result["backgroundJob"]), result["backgroundJobSha256"])
        self.assertEqual(outcome["status"], "success", outcome)
        self.assertNotIn("task_f_export_deadline_template", calls)
        self.assertEqual(outcome["taskF"]["newActivationImportedCount"], pending_count)
        self.assertEqual(outcome["taskF"]["notifyIncrementalWorkbook"], bool(pending_count))
        terminal = background.read_json(Path(result["backgroundState"]))
        self.assertTrue(terminal["reportVerificationPassed"])
        self.assertTrue(Path(terminal["backgroundReport"]).is_file())
        self.assertNotIn("主报表已独立交付", Path(terminal["backgroundReport"]).read_text())
        return outcome, calls

    def test_real_complete_task_f_with_empty_pending_never_logs_in_or_exports_template(self):
        outcome, calls = self.run_complete_mocked_job(0)
        self.assertNotIn("task_f_login_check", calls)
        self.assertIsNone(outcome["taskF"]["incrementalWorkbook"])

    def test_real_complete_one_batch_records_delta_and_closes_f_after_report_readback(self):
        outcome, calls = self.run_complete_mocked_job(1)
        self.assertIn("task_f_database_import_batch_001", calls)
        self.assertTrue(Path(outcome["taskF"]["incrementalWorkbook"]).is_file())

    def test_detached_job_uses_only_private_verified_inputs_after_originals_removed(self):
        outcome, _ = self.run_complete_mocked_job(1, remove_original=True)
        self.assertEqual(outcome["status"], "success")


if __name__ == "__main__":
    unittest.main()
