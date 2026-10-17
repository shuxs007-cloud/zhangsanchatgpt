"""No-network regressions for immutable main delivery and detached Task F."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def module(name):
    # Never replace a production module already imported by another test. The
    # scoped registration is needed only while dataclass definitions execute.
    alias = "background_reporting_test_" + name
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    with patch.object(sys, "path", [str(SCRIPTS), *sys.path]), patch.dict(sys.modules, {alias: result}):
        spec.loader.exec_module(result)
    return result


AUDIT = module("opensms_parallel_audit")
REPORT = module("opensms_finalize_patrol_report")
CHAT = module("opensms_chat_summary")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return str(path)


class BackgroundContractTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.delivery = self.root / "main"
        self.delivery.mkdir()
        self.document = self.delivery / "final.xlsx"
        self.document.write_bytes(b"separately verified exact document bytes")
        self.receipt = self.delivery / "card.json"
        write_json(self.receipt, {})
        self.job_path = self.root / "job.json"
        self.job = {
            "parentRunId": "current", "mainReportIndependent": True,
            "resultsApplyNextExport": True, "resultsWriteBackToDocument": False,
            "cloudUploadAttempted": False, "requestedWorkers": 8,
            "documentPath": str(self.document), "documentSha256": AUDIT.sha256_file(self.document),
            "documentRunId": "current", "reportDateCachePath": str(self.delivery / "date_cache.json"),
            "reportDateCacheSha256": "a" * 64,
        }
        self.config = {
            "runId": "current", "taskFBackground": True,
            "taskFMainReportIndependent": True, "taskFResultsApplyNextExport": True,
            "taskFResultsWriteBackToDocument": False, "taskFCloudUploadAttempted": False,
            "taskFRequestedWorkers": 8, "taskFAutomaticCatalogSync": True,
            "taskFDocumentPath": str(self.document), "taskFDocumentSha256": AUDIT.sha256_file(self.document),
            "taskFDocumentReceipt": str(self.receipt), "taskFDocumentGeneratedBeforeLookup": True,
            "reportDateCachePath": self.job["reportDateCachePath"], "reportDateCacheSha256": "a" * 64,
            "taskFBackgroundJob": str(self.job_path), "taskFStageStatus": "running",
            "taskFBackgroundState": str(self.root / "child_state.json"),
            "taskFBackgroundReport": str(self.root / "child_report.md"),
        }
        self.resolver = AUDIT.PathResolver(self.delivery / "config.json", self.delivery)
        self.refresh()

    def refresh(self):
        write_json(self.job_path, self.job)
        self.config["taskFBackgroundJobSha256"] = AUDIT.sha256_file(self.job_path)

    def check(self):
        with patch.object(AUDIT, "check_card_receipt", return_value=AUDIT.CheckResult("card", "document", AUDIT.PASS, "ok")):
            return AUDIT.check_task_f(self.config, self.resolver)

    def test_pending_is_not_passed_failed_or_completed_and_never_reads_mutable_state(self):
        original = AUDIT.read_json
        with patch.object(AUDIT, "read_json", wraps=original) as read:
            result = self.check()
        self.assertEqual(AUDIT.PENDING, result.status)
        self.assertFalse(result.details["completion_verified"])
        self.assertEqual([self.job_path.resolve()], [call.args[0] for call in read.call_args_list])

    def test_dispatch_receipt_must_have_exact_hash_and_parent_run(self):
        self.config["taskFBackgroundJobSha256"] = "b" * 64
        self.assertEqual(AUDIT.FAIL, self.check().status)
        self.job["parentRunId"] = "previous"
        self.refresh()
        self.assertEqual(AUDIT.FAIL, self.check().status)

    def test_document_change_is_rejected(self):
        self.document.write_bytes(b"modified after dispatch")
        self.assertEqual(AUDIT.FAIL, self.check().status)

    def test_mutable_child_output_inside_main_manifest_directory_is_rejected(self):
        self.config["taskFBackgroundState"] = str(self.delivery / "child.json")
        self.assertEqual(AUDIT.FAIL, self.check().status)

    def test_unconfirmed_incremental_link_or_import_count_is_rejected(self):
        self.config["taskFIncrementalWorkbook"] = "/old/previous.xlsx"
        self.assertEqual(AUDIT.FAIL, self.check().status)
        self.config.pop("taskFIncrementalWorkbook")
        self.config["taskFNewActivationImportedCount"] = 33
        self.assertEqual(AUDIT.FAIL, self.check().status)

    def test_writeback_or_cloud_upload_is_rejected(self):
        for key in ("taskFResultsWriteBackToDocument", "taskFCloudUploadAttempted"):
            with self.subTest(key=key), patch.dict(self.config, {key: True}):
                self.assertEqual(AUDIT.FAIL, self.check().status)

    def test_proved_launch_failure_warns_without_invalidating_main_document(self):
        failure = {"schemaVersion": 1, "runId": "current", "status": "failed",
            "reason": "background_launch_failed", "error": "local spawn refused",
            "executed": False, "workerSpawnConfirmed": False, "mainReportIndependent": True,
            "cloudUploadAttempted": False, "resultsWriteBackToDocument": False,
            "documentPath": str(self.document), "documentSha256": AUDIT.sha256_file(self.document),
            "reportDateCacheSha256": "a" * 64}
        path = self.delivery / "launch_failure.json"
        write_json(path, failure)
        write_json(self.receipt, {"verification": {"system_kooda_source_review": {"run_id": "current"}}})
        self.config.update({"taskFStageStatus": "failed", "taskFExecuted": False,
            "taskFBackgroundLaunchReceipt": str(path), "taskFBackgroundLaunchReceiptSha256": AUDIT.sha256_file(path),
            "taskFBackgroundLaunchError": "local spawn refused"})
        result = self.check()
        self.assertEqual(AUDIT.WARN, result.status)
        self.assertFalse(result.details["completion_verified"])
        self.config["taskFExecuted"] = True
        self.assertEqual(AUDIT.FAIL, self.check().status)

    def test_no_gap_skip_requires_independent_zero_and_no_execution(self):
        write_json(self.receipt, {"verification": {"system_kooda_source_review": {
            "run_id": "current", "missing_either_date_rows": 0}}})
        self.config.update({"taskFSkippedReason": "no_missing_activation_after_fallback", "taskFStageStatus": "skipped",
            "taskFExecuted": False, "taskFBackground": False, "taskFOnlineLookupTriggered": False,
            "taskFDocumentMissingDateRows": 0, "taskFNewActivationImportedCount": 0})
        self.assertEqual(AUDIT.SKIP, self.check().status)
        self.config["taskFDocumentMissingDateRows"] = 1
        self.assertEqual(AUDIT.FAIL, self.check().status)

    def test_new_no_authoritative_gap_skip_has_same_no_execution_contract(self):
        self.config.update({"taskFSkippedReason": "no_missing_authoritative_dates", "taskFStageStatus": "skipped",
            "taskFExecuted": False, "taskFBackground": False, "taskFOnlineLookupTriggered": False,
            "taskFDocumentMissingDateRows": 0, "taskFNewActivationImportedCount": 0})
        write_json(self.receipt, {"verification": {"system_kooda_source_review": {
            "run_id": "current", "missing_either_date_rows": 0}}})
        self.assertEqual(AUDIT.SKIP, self.check().status)
        self.config["taskFOnlineLookupTriggered"] = True
        self.assertEqual(AUDIT.FAIL, self.check().status)

    def test_real_runner_finalize_config_preserves_background_audit_contract(self):
        tests_dir = Path(__file__).resolve().parent
        spec = importlib.util.spec_from_file_location("f_reporting_runner_fixture", tests_dir / "test_opensms_task_a_only.py")
        fixture_module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", list(sys.path)), patch.dict(sys.modules):
            spec.loader.exec_module(fixture_module)
        fixture = fixture_module.TaskAOnlyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runner = fixture.runner(only=False)
        # This legacy fixture audits F dispatch only, not Chatr Google inputs.
        runner.state["googleChatrDateFallbackEnabled"] = False
        acquire, candidates, document, refresh, _ = fixture.evidence(runner)
        self.job["parentRunId"] = self.job["documentRunId"] = runner.run_id
        self.refresh()
        write_json(self.receipt, {"result": "成功", "checks": {"fixture": True},
            "process": {"main_rows": 0, "matched_rows": 0,
                        "google_dependency": deepcopy(AUDIT.NO_GOOGLE_DEPENDENCY)}, "verification": {
                "google_dependency_review": {**AUDIT.NO_GOOGLE_DEPENDENCY, "matches": True},
                "unused_port_review": {"matches": True},
                "system_kooda_source_review": {"source": "system", "matches": True, "run_id": runner.run_id},
                "task_f_missing_activation_red_review": {"matches": True},
                "task_f_missing_dates_new_card_review": {"matches": True}}})
        document["cardReceipt"] = str(self.receipt)
        runner.state["reportDateCache"] = {"path": self.job["reportDateCachePath"], "sha256": self.job["reportDateCacheSha256"]}
        runner.state["stages"]["taskF"] = {
            **self.job, "status": "running", "executed": True, "background": True,
            "backgroundJob": str(self.job_path), "backgroundJobSha256": AUDIT.sha256_file(self.job_path),
            "backgroundState": self.config["taskFBackgroundState"], "backgroundReport": self.config["taskFBackgroundReport"],
            "automaticCatalogSync": True, "documentGeneratedBeforeLookup": True,
            "postDocumentSupplemental": True, "incrementalWorkbook": None, "newActivationImportedCount": None}
        path = runner.build_finalize_config(acquire, candidates, document, refresh, runner.receipts / "audit.json", "完成")
        config = fixture_module.RUNNER.read_json(path)
        result = AUDIT.check_task_f(config, AUDIT.PathResolver(path, runner.delivery))
        self.assertEqual(AUDIT.PENDING, result.status, result.message)


class BackgroundReportTests(unittest.TestCase):
    def test_dispatch_snapshot_is_never_presented_as_live_running_status(self):
        text = CHAT.render_summary({"status":"success","taskF":{"background":True,
            "status":"running","statusIsLaunchSnapshot":True,"newActivationImportedCount":None}})
        self.assertIn("派发时快照 running",text)
        self.assertIn("非当前运行状态",text)
        self.assertIn("独立F状态",text)
        self.assertNotIn("任务F：running，",text)

    def test_launch_snapshot_cannot_announce_a_completed_incremental_file(self):
        text = CHAT.render_summary({"status":"success","taskF":{"background":True,
            "status":"passed","statusIsLaunchSnapshot":True,"newActivationImportedCount":2,
            "incrementalWorkbook":"/old.xlsx"}})
        self.assertNotIn("已生成增量Excel",text)
        self.assertNotIn("/old.xlsx",text)

    def test_full_report_clock_is_dispatch_time_not_report_generation_time(self):
        with patch.object(REPORT,"load",side_effect=AssertionError("mutable child read")):
            text="\n".join(REPORT.task_f_background_lines({"taskFStageStatus":"running"}))
        self.assertIn("后台派发时状态快照",text)
        self.assertIn("非主报告生成时或当前运行状态",text)
        self.assertNotIn("后台作业状态（主报告生成时）",text)

    def test_summary_keeps_total_time_but_omits_export_substage_times(self):
        performance = {"pipelineWallSecondsBeforeFinalManifest": 290.7,
                       "exportPerformance": {"backendGenerationMs": 64892, "downloadMs": 972}}
        text = CHAT.render_summary({"status": "success", "performance": performance,
            "taskF": {"status": "skipped", "reason": "no_missing_authoritative_dates"}})
        self.assertIn("290.7", text)
        self.assertNotIn("后台生成报表", text)
        self.assertNotIn("0.97", text)
        self.assertEqual(performance["exportPerformance"]["downloadMs"], 972)

    def test_running_summary_keeps_only_three_main_links_no_fake_zero_or_new_file(self):
        text = CHAT.render_summary({"status": "success", "sourceWorkbook": "/s.xlsx",
            "finalWorkbook": "/final.xlsx", "report": "/r.md",
            "taskF": {"background": True, "status": "running", "pendingCount": None,
                      "newActivationImportedCount": None, "incrementalWorkbook": "/old.xlsx"}})
        self.assertEqual(3, text.count("](<"))
        self.assertIn("完成和入库数量尚未确认", text)
        self.assertNotIn("待补查 0", text)
        self.assertNotIn("新入库数据已生成", text)
        self.assertNotIn("/old.xlsx", text)

    def test_no_new_imports_never_announces_incremental_file(self):
        text = CHAT.render_summary({"status": "success", "taskF": {"background": True,
            "status": "passed", "newActivationImportedCount": 0, "incrementalWorkbook": "/old.xlsx"}})
        self.assertNotIn("新入库数据已生成", text)
        self.assertNotIn("/old.xlsx", text)

    def test_no_gap_summary_suppresses_old_library_and_new_file_prompt(self):
        text = CHAT.render_summary({"status": "success", "taskF": {"status": "skipped",
            "reason": "no_missing_activation_after_fallback", "incrementalWorkbook": "/old.xlsx"}})
        self.assertIn("无需联网补查", text)
        self.assertNotIn("ICCID库位置", text)
        self.assertNotIn("增量Excel", text)
        self.assertNotIn("/old.xlsx", text)

    def test_confirmed_incremental_generation_notice_is_not_a_fourth_chat_link(self):
        text = CHAT.render_summary({"status": "success", "taskF": {"background": True,
            "status": "passed", "newActivationImportedCount": 2, "incrementalWorkbook": "/new.xlsx"}})
        self.assertIn("已生成增量Excel", text)
        self.assertNotIn("/new.xlsx", text)

    def test_full_report_has_live_status_pointer_without_reading_or_linking_stale_delta(self):
        config = {"taskFStageStatus": "running", "taskFBackgroundState": "/independent/state.json",
                  "taskFBackgroundReport": "/independent/report.md", "taskFBackgroundJob": "/job.json",
                  "taskFIncrementalWorkbook": "/old.xlsx", "taskFNewActivationImportedCount": None}
        with patch.object(REPORT, "load", side_effect=AssertionError("mutable child read")):
            text = "\n".join(REPORT.task_f_background_lines(config))
        self.assertIn("不等待任务F联网", text)
        self.assertIn("/independent/state.json", text)
        self.assertNotIn("/old.xlsx", text)
        self.assertIn("从下次生成时补入", text)


class FrozenDateProvenanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.xlsx"
        self.google = self.root / "google.xlsx"
        self.source.write_bytes(b"fresh system input")
        self.google.write_bytes(b"fresh google input")
        self.cache = self.root / "date_cache.json"
        self.created = "2026-09-07T01:00:00+08:00"
        self.snapshot = {"schemaVersion": 1, "status": "passed", "runId": "current",
                         "createdAt": self.created, "recordCount": 1,
                         "records": [{"queryIccid": "8912230200000000001", "activationDate": "2026-09-01", "cutoffDate": "2026-09-30"}]}
        write_json(self.cache, self.snapshot)
        self.resolution = {"source": "system_export_then_frozen_local_or_google",
            "field_counts": {"激活日期": {"system_export": 1, "local_cache": 1, "google": 1}},
            "cache": {"path": str(self.cache), "sha256": AUDIT.sha256_file(self.cache), "run_id": "current", "created_at": self.created},
            "source_export": {"path": str(self.source), "sha256": AUDIT.sha256_file(self.source)},
            "association": {"path": str(self.google), "sha256": AUDIT.sha256_file(self.google)},
            "new_query_results_apply_next_export": True, "redundant_system_date_columns_removed": True}
        self.receipt = self.root / "card.json"
        self.config = {"runId": "current", "reportDateCachePath": str(self.cache), "reportDateCacheSha256": AUDIT.sha256_file(self.cache)}
        self.inputs = {"card_receipt": self.receipt, "source": self.source, "association": self.google}
        self.resolver = AUDIT.PathResolver(self.root / "config.json", None)

    def check(self):
        write_json(self.receipt, {"process": {"date_resolution": self.resolution},
            "verification": {"date_resolution_review": {**deepcopy(self.resolution), "matches": True}}})
        return AUDIT.check_date_resolution(self.config, self.resolver, self.inputs)

    def test_exact_frozen_snapshot_and_new_input_binding_pass(self):
        self.assertEqual(AUDIT.PASS, self.check().status)

    def test_verified_archived_input_survives_working_copy_cleanup(self):
        archived = self.root / "fresh_source_archive.xlsx"
        archived.write_bytes(self.source.read_bytes())
        self.inputs["source"] = archived
        self.source.unlink()
        self.assertEqual(AUDIT.PASS, self.check().status)

    def test_changed_cache_and_stale_source_are_rejected(self):
        self.cache.write_bytes(b"changed after freeze")
        self.assertEqual(AUDIT.FAIL, self.check().status)

    def test_invalid_cycle_is_rejected_even_with_rebound_hash(self):
        self.snapshot["records"][0]["cutoffDate"] = "2026-10-01"
        write_json(self.cache, self.snapshot)
        digest = AUDIT.sha256_file(self.cache)
        self.config["reportDateCacheSha256"] = self.resolution["cache"]["sha256"] = digest
        self.assertEqual(AUDIT.FAIL, self.check().status)

    def renewal_snapshot(self, balance="30"):
        row = self.snapshot["records"][0]
        extension = 30 if float(balance) >= 30 else 0
        cutoff = "2026-10-30" if extension else "2026-09-30"
        row.update(cutoffDate=cutoff, renewal={"version": 1,
            "authorization": "current-user-renewal-source",
            "rule": "current_source_cutoff_plus_30_if_balance_gte_30_else_source_cutoff",
            "queryIccid": row["queryIccid"], "activationDate": row["activationDate"],
            "baseCutoffDate": "2026-09-30", "sourceCutoff": "2026-09-30",
            "cutoffDate": cutoff, "extensionDays": extension, "balanceCad": balance,
            "status": "充值成功", "sourceSha256": "a" * 64, "sourceRow": 2,
            "phone": "4389392694", "runId": "20261013T120500+0800",
            "backendId": "123", "slot": "r110_01"})
        return row

    def rebind_snapshot(self):
        write_json(self.cache, self.snapshot)
        self.config["reportDateCacheSha256"] = self.resolution["cache"]["sha256"] = AUDIT.sha256_file(self.cache)

    def test_authorized_frozen_renewals_pass_without_changing_original_cycle(self):
        for balance in ("0", "4.45", "29.99", "30", "60"):
            with self.subTest(balance=balance):
                self.renewal_snapshot(balance)
                self.rebind_snapshot()
                result = self.check()
                self.assertEqual(AUDIT.PASS, result.status)
                self.assertEqual(1, result.details["validated_renewal_record_count"])

    def test_renewal_proof_tampering_is_rejected_even_with_rebound_hash(self):
        for field, value in (("extensionDays", 60), ("queryIccid", "8912230200000000002"),
                             ("sourceSha256", "bad"), ("authorization", "history"),
                             ("baseCutoffDate", "2026-10-01"), ("balanceCad", "NaN")):
            with self.subTest(field=field):
                row = self.renewal_snapshot()
                row["renewal"][field] = value
                self.rebind_snapshot()
                self.assertEqual(AUDIT.FAIL, self.check().status)


class DeltaRunBindingAuditTests(unittest.TestCase):
    """Local JSON evidence only; no database, exporter or browser may run."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.delta = module("opensms_task_f_delta")
        self.database = self.root / "never-created.sqlite3"
        self.lookup = self.root / "temporary-lookup.json"
        self.imported = self.root / "current-import.json"
        self.row = {"queryIccid": "8912230200000000001", "status": "found",
                    "activationDate": "2026-09-01", "cutoffDate": "2026-09-30",
                    "activationDateUpdated": True}
        write_json(self.lookup, {"runId": "current", "results": [self.row]})
        self.payload = {"status": "passed", "database": str(self.database), "runId": "current",
            "lookupReceipt": str(self.lookup), "lookupSha256": AUDIT.sha256_file(self.lookup),
            "activationDateUpdatedCount": 1, "rows": [self.row]}

    def collect(self):
        write_json(self.imported, self.payload)
        with patch.object(self.delta, "export_deadlines", side_effect=AssertionError("export forbidden")):
            return self.delta.collect_updates(self.database, [self.imported], "current")

    def test_current_immutable_lookup_binding_collects_without_db(self):
        rows, proofs = self.collect()
        self.assertEqual([[self.row["queryIccid"], "2026-09-30", "2026-09-01"]], rows)
        self.assertEqual(1, len(proofs))
        self.assertFalse(self.database.exists())

    def test_cross_run_import_and_legacy_unbound_import_rejected(self):
        for run_id in ("prior-run", None):
            with self.subTest(run_id=run_id):
                self.payload["runId"] = run_id
                with self.assertRaisesRegex(ValueError, "runId"):
                    self.collect()

    def test_missing_temporary_lookup_file_cannot_be_accepted_as_current(self):
        self.lookup.unlink()
        with self.assertRaisesRegex(ValueError, "缺失|哈希"):
            self.collect()

    def test_rebound_hash_does_not_hide_a_lookup_from_another_run(self):
        write_json(self.lookup, {"runId": "prior-run", "results": [self.row]})
        self.payload["lookupSha256"] = AUDIT.sha256_file(self.lookup)
        with self.assertRaisesRegex(ValueError, "查询回执不属于"):
            self.collect()

if __name__ == "__main__":
    unittest.main()
