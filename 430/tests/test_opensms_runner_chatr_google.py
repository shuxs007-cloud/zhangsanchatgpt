"""Offline runner gates for the narrowly scoped fresh Chatr Google date source."""

from contextlib import redirect_stderr
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_opensms_acquire_reliability as fixtures

RUNNER = fixtures.RUNNER
SOURCE_URL = "https://docs.google.com/spreadsheets/d/1GiA9uR_2IQ4xntP_aWvCSaWfmC6WVQ7SrnXsy8YyyvM/export?format=xlsx"
NOW = datetime(2026, 9, 6, 13, 0, tzinfo=RUNNER.TZ)
DEPENDENCY = {"enabled": False, "status": "skipped", "reason": "user_disabled",
              "unused_port_device_scope": "system_source_only"}


class RunnerChatrGoogleTests(unittest.TestCase):
    def setUp(self):
        # The shared fixture guards subprocess.run/Popen and redirects all paths
        # to a temporary directory; no browser, network, or production writes.
        self.fixture = fixtures.AcquisitionReliabilityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = self.fixture.runner
        self.runner.state["googleChatrDateFallbackEnabled"] = True
        clock = patch.object(RUNNER, "now_hkt", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.download_changes = {}
        self.snapshot_changes = {}
        self.google_calls = []
        self.runner.command = Mock(side_effect=self.command)

    def download_payload(self):
        path = self.fixture.inputs / "chatr-download.json"
        value = {"runId": self.runner.run_id,
                 "startedAt": "2026-09-06T12:00:01+08:00",
                 "downloadedAt": "2026-09-06T12:00:02+08:00",
                 "sourceUrl": SOURCE_URL, "outputPath": str(self.fixture.google),
                 "sha256": RUNNER.sha256(self.fixture.google),
                 "size": self.fixture.google.stat().st_size,
                 "receiptPath": str(path), "networkOnly": True,
                 "silentBrowserMode": True, "browserForegroundActivationAttempted": False,
                 **self.download_changes}
        path.write_text(json.dumps(value), encoding="utf-8")
        return value

    def command(self, label, argv, **kwargs):
        args = [str(value) for value in argv]
        if label == "google_chatr_download":
            self.google_calls.append((label, args, self.runner.child_environment(kwargs.get("env"))))
            value = self.download_payload()
        elif label == "chatr_google_date_snapshot":
            self.google_calls.append((label, args, self.runner.child_environment(kwargs.get("env"))))
            source = args[args.index("--source") + 1]
            path = Path(args[args.index("--output") + 1])
            digest = args[args.index("--source-sha256") + 1]
            value = {"schemaVersion": 1, "status": "passed", "runId": self.runner.run_id,
                     "createdAt": "2026-09-06T12:00:03+08:00", "source": "google_chatr_dates",
                     "sourcePath": str(Path(source).resolve()), "sourceSha256": digest,
                     "recordCount": 0, "records": [], "readOnly": True,
                     "snapshotConsistent": True, "sourceHashVerified": True,
                     **self.snapshot_changes}
            path.write_text(json.dumps(value), encoding="utf-8")
            value = {**value, "output": str(path.resolve()), "sha256": RUNNER.sha256(path),
                     "verificationPassed": True}
        else:
            return self.fixture.command(label, argv, **kwargs)
        return RUNNER.CommandResult(value, json.dumps(value), "", 0.01)

    def acquire(self):
        return self.runner.acquire()

    def reset_acquisition(self):
        for key in ('cardRetentionReceipt', 'processingCardRetention', 'cardRetentionPhoneList'):
            raw = (self.runner.state.get('partialAcquisition') or {}).get(key)
            if raw:
                path = Path(raw)
                self.assertTrue(path.is_relative_to(self.fixture.folder))
                path.unlink(missing_ok=True)
        self.runner.state["stages"].pop("acquire", None)
        self.google_calls.clear()

    def test_contract_enables_only_chatr_dates_and_legacy_fixture_stays_disabled(self):
        for only in (False, True):
            contract = RUNNER.execution_contract(only)
            self.assertIs(contract["googleChatrDateFallbackEnabled"], True)
            self.assertEqual(contract["googleChatrDateFallbackScope"], "chatr_blank_activation_and_cutoff_only")
            self.assertIs(contract["googleAssociationEnabled"], False)
            self.assertEqual(contract["unusedPortDeviceScope"], "system_source_only")
            self.assertIs(contract["googleSystemMissingSheetEnabled"], True)
            self.assertEqual(contract["googleSystemMissingSheetScope"], "google_slots_absent_from_current_system_export")
        del self.runner.state["googleChatrDateFallbackEnabled"]
        self.assertFalse(self.runner.chatr_google_date_fallback)
        self.acquire()
        self.assertEqual(self.google_calls, [])

    def test_both_new_acquisition_commands_bind_current_source_hash_and_run(self):
        self.runner.args.task_a_only = True
        acquired = self.acquire()
        self.assertEqual([row[0] for row in self.google_calls],
                         ["google_chatr_download", "chatr_google_date_snapshot"])
        _, argv, child = self.google_calls[1]
        self.assertEqual(argv[argv.index("--run-id") + 1], self.runner.run_id)
        self.assertEqual(Path(argv[argv.index("--source") + 1]), Path(acquired["processingChatrGoogleSource"]))
        self.assertEqual(argv[argv.index("--source-sha256") + 1], acquired["chatrGoogleSourceSha256"])
        for _, _, env in self.google_calls:
            self.assertEqual(env["OPENSMS_RUN_ID"], self.runner.run_id)
            self.assertEqual(env["OPENSMS_GOOGLE_ASSOCIATION_ENABLED"], "0")
            self.assertFalse(any(key.startswith("OPENSMS_TASK_F_") for key in env))
        self.assertEqual(child["OPENSMS_RUN_ID"], self.runner.run_id)
        payload = self.runner.verify_chatr_google_acquisition(acquired)
        self.assertEqual(payload["sourceSha256"], RUNNER.sha256(self.fixture.google))
        self.assertEqual(payload["recordCount"], 0)
        self.assertEqual(acquired["readConcurrency"], 3)
        self.assertIsNone(acquired["association"])
        self.assertIsNone(acquired["processingAssociation"])
        inventory = [entry for entry in self.fixture.calls if entry[0] == "report_kooda_source"]
        self.assertEqual(len(inventory), 1)
        self.assertNotIn('env', inventory[0][2])
        self.assertFalse(any(entry[0] == 'system_kooda_inventory' for entry in self.fixture.calls))

    def test_fresh_download_rejects_wrong_run_url_hash_and_time_before_snapshot(self):
        mutations = [{"runId": "old-run"}, {"sourceUrl": "https://example.invalid/other.xlsx"},
                     {"sha256": "0" * 64}, {"startedAt": "2026-09-06T11:59:59+08:00"},
                     {"downloadedAt": "2026-09-06T12:00:00+08:00"},
                     {"downloadedAt": "2026-09-06T14:00:00+08:00"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), redirect_stderr(io.StringIO()):
                self.reset_acquisition()
                self.download_changes = mutation
                with self.assertRaises(RUNNER.PatrolError):
                    self.acquire()
                self.assertEqual([row[0] for row in self.google_calls], ["google_chatr_download"])
                self.assertNotIn("acquire", self.runner.state["stages"])
                self.assertTrue(Path(self.runner.state["partialAcquisition"]["sourceArchive"]).is_file())

    def test_snapshot_identity_source_hash_and_status_reject_before_acquire_complete(self):
        for mutation in ({"runId": "old-run"}, {"source": "google_kooda_dates"},
                         {"sourceSha256": "0" * 64}, {"status": "failed"}):
            with self.subTest(mutation=mutation), redirect_stderr(io.StringIO()):
                self.reset_acquisition()
                self.snapshot_changes = mutation
                with self.assertRaises(RUNNER.PatrolError):
                    self.acquire()
                self.assertNotIn("acquire", self.runner.state["stages"])

    def test_recheck_rejects_each_missing_or_changed_registered_input(self):
        acquired = self.acquire()
        for field in ("chatrGoogleSource", "processingChatrGoogleSource", "chatrGoogleDates",
                      "processingChatrGoogleDates", "chatrGoogleDownloadReceipt"):
            with self.subTest(field=field):
                path = Path(acquired[field])
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                try:
                    with self.assertRaises(RUNNER.PatrolError):
                        self.runner.verify_chatr_google_acquisition(acquired)
                finally:
                    path.write_bytes(original)
                with self.assertRaises(RUNNER.PatrolError):
                    self.runner.verify_chatr_google_acquisition({**acquired, field: str(path) + ".missing"})

    def test_recheck_rejects_rehashed_download_receipt_wrong_round_time_url_or_source(self):
        acquired = self.acquire()
        path = Path(acquired["chatrGoogleDownloadReceipt"])
        original = json.loads(path.read_text())
        mutations = [{"runId": "prior-run"}, {"sha256": "0" * 64},
                     {"sourceUrl": "https://example.invalid/unbound.xlsx"},
                     {"startedAt": "2026-09-06T11:59:59+08:00"},
                     {"downloadedAt": "2026-09-06T12:00:00+08:00"},
                     {"downloadedAt": "2026-09-06T14:00:00+08:00"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                path.write_text(json.dumps({**original, **mutation}), encoding="utf-8")
                changed = {**acquired, "chatrGoogleDownloadReceiptSha256": RUNNER.sha256(path)}
                with self.assertRaises(RUNNER.PatrolError):
                    self.runner.verify_chatr_google_acquisition(changed)

    def test_recheck_rejects_rehashed_snapshot_wrong_count_type_source_path_or_run(self):
        acquired = self.acquire()
        path = Path(acquired["chatrGoogleDates"])
        local = Path(acquired["processingChatrGoogleDates"])
        original = json.loads(path.read_text())
        for mutation in ({"runId": "old-run"}, {"recordCount": True}, {"recordCount": 1},
                         {"records": {}}, {"sourcePath": str(self.fixture.google)},
                         {"sourceSha256": "0" * 64}, {"source": "google_kooda_dates"},
                         {"createdAt": "2026-09-06T12:00:01+08:00"},
                         {"createdAt": "2026-09-06T14:00:00+08:00"},
                         {"createdAt": None}, {"createdAt": "2026-09-06T12:00:03"}):
            with self.subTest(mutation=mutation):
                raw = json.dumps({**original, **mutation}).encode()
                path.write_bytes(raw)
                local.write_bytes(raw)
                changed = {**acquired, "chatrGoogleDatesSha256": RUNNER.sha256(path)}
                with self.assertRaises(RUNNER.PatrolError):
                    self.runner.verify_chatr_google_acquisition(changed)

    def test_document_process_receives_bound_chatr_dates_and_clears_inherited_paths(self):
        self.runner.state['koUnactivatedSheetEnabled'] = True
        acquired = self.acquire()
        self.runner.freeze_report_date_cache = Mock(return_value={"path": "/offline/frozen.json", "sha256": "frozen-sha"})
        self.runner.mark_artifact_operation = Mock()
        foreign = {"OPENSMS_CHATR_GOOGLE_DATES_PATH": "/prior-run/chatr.json",
                   "OPENSMS_CHATR_GOOGLE_DATES_SHA256": "prior-sha", "OPENSMS_RUN_ID": "prior-run",
                   "OPENSMS_GOOGLE_ASSOCIATION_ENABLED": "1", "OPENSMS_GOOGLE_SYSTEM_MISSING_ENABLED": "0"}
        with patch.dict(os.environ, foreign):
            fresh = self.runner.child_environment()
            self.assertNotIn("OPENSMS_CHATR_GOOGLE_DATES_PATH", fresh)
            self.assertNotIn("OPENSMS_CHATR_GOOGLE_DATES_SHA256", fresh)
            self.assertNotIn("OPENSMS_GOOGLE_SYSTEM_MISSING_ENABLED", fresh)
            with patch.object(RUNNER.subprocess, "Popen", return_value=SimpleNamespace()) as process:
                self.runner.start_document(acquired)
        self.addCleanup(self.runner.document_log_handle.close)
        argv, env = process.call_args.args[0], process.call_args.kwargs["env"]
        self.assertIn("--no-google", argv)
        self.assertNotIn("--association", argv)
        self.assertEqual(env["OPENSMS_CHATR_GOOGLE_DATES_PATH"], acquired["processingChatrGoogleDates"])
        self.assertEqual(env["OPENSMS_CHATR_GOOGLE_DATES_SHA256"], acquired["chatrGoogleDatesSha256"])
        self.assertEqual(env["OPENSMS_SYSTEM_KOODA_SHA256"], acquired["systemKoodaSha256"])
        self.assertEqual(env["OPENSMS_REPORT_DATE_CACHE_SHA256"], "frozen-sha")
        self.assertEqual(env["OPENSMS_GOOGLE_ASSOCIATION_ENABLED"], "0")
        self.assertEqual(env["OPENSMS_GOOGLE_SYSTEM_MISSING_ENABLED"], "1")
        self.assertEqual(env['OPENSMS_KO_UNACTIVATED_REQUIRED'], '1')
        self.assertEqual(env["OPENSMS_RUN_ID"], self.runner.run_id)

    def document_completion(self, acquired, change=None, review_change=None, difference_change=None, difference_review_change=None):
        proof = {"enabled": True, "status": "passed", "run_id": self.runner.run_id,
                 "snapshot_sha256": acquired["chatrGoogleDatesSha256"],
                 "source_sha256": acquired["chatrGoogleSourceSha256"],
                 "scope": "chatr_blank_activation_and_cutoff_only"}
        process = {"google_dependency": DEPENDENCY, "google_chatr_date_fallback": {**proof, **(change or {})}}
        verification = {"google_chatr_date_fallback_review": {**proof, "matches": True, **(review_change or {})}}
        difference = {**proof, "scope": "google_slots_absent_from_current_system_export", "missing_rows": 2}
        process["google_system_missing_sheet"] = {**difference, **(difference_change or {})}
        verification["google_system_missing_sheet_review"] = {
            **difference, "matches": True, **(difference_review_change or {})}
        retained = {"source_sha256": acquired['cardRetentionSha256'], "run_id": self.runner.run_id}
        process['card_retention_report'] = retained
        verification['card_retention_report_review'] = {**retained, 'matches': True}
        receipt = self.fixture.save_json("document-receipt.json", {"process": process, "verification": verification})
        dispatch = self.fixture.save_json("dispatch.json", {"inputs": {"google": None}})
        payload = {"result": "成功", "output_path": str(self.fixture.source), "receipt_path": str(receipt),
                   "dispatch_receipt_path": str(dispatch), "processing_mode": "local", "duration_seconds": 0}
        log = self.runner.run_dir / "document.log"
        log.write_text(json.dumps(payload), encoding="utf-8")
        self.runner.state["documentLog"] = str(log)
        self.runner.document_log_handle = Mock()
        self.runner.document_process = SimpleNamespace(wait=Mock(return_value=0))

    def test_document_success_requires_independent_current_run_chatr_review(self):
        acquired = self.acquire()
        mutations = [{"matches": False}, {"run_id": "prior-run"}, {"snapshot_sha256": "stale-sha"},
                     {"source_sha256": "stale-sha"}, {"scope": "all_google_fields"},
                     {"enabled": False}, {"status": "failed"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.document_completion(acquired, review_change=mutation)
                with self.assertRaisesRegex(RUNNER.PatrolError, "Chatr"):
                    self.runner.finish_document()
                self.assertNotIn("document", self.runner.state["stages"])
        self.document_completion(acquired)
        stage = self.runner.finish_document()
        self.assertTrue(stage["googleChatrDateFallbackEnabled"])
        self.assertEqual(stage["googleChatrDateFallback"]["run_id"], self.runner.run_id)
        self.assertEqual(stage["googleSystemMissingSheet"]["missing_rows"], 2)

    def test_document_requires_current_difference_and_independent_review(self):
        acquired = self.acquire()
        for mutation in ({"matches": False}, {"run_id": "prior-run"}, {"missing_rows": 3},
                         {"source_sha256": "prior"}, {"snapshot_sha256": "prior"}):
            with self.subTest(mutation=mutation):
                self.document_completion(acquired, difference_review_change=mutation)
                with self.assertRaisesRegex(RUNNER.PatrolError, "差集"):
                    self.runner.finish_document()
        self.document_completion(acquired, difference_change={"run_id": "prior-run"},
                                 difference_review_change={"run_id": "prior-run"})
        with self.assertRaisesRegex(RUNNER.PatrolError, "差集"):
            self.runner.finish_document()

    def test_reused_difference_is_not_trusted_without_current_review(self):
        acquired = self.acquire()
        self.document_completion(acquired)
        stage = self.runner.finish_document()
        archived = Path(stage["cardReceipt"])
        payload = json.loads(archived.read_text())
        payload["verification"]["google_system_missing_sheet_review"]["matches"] = False
        archived.write_text(json.dumps(payload))
        with self.assertRaisesRegex(RUNNER.PatrolError, "差集"):
            self.runner.finish_document()

    def test_reused_no_google_document_cannot_bypass_chatr_start_or_finish(self):
        acquired = self.acquire()
        old_receipt = self.fixture.save_json("old-no-google-receipt.json", {"process": {"google_dependency": DEPENDENCY}})
        self.runner.reusable_stage = Mock(return_value={"googleAssociationEnabled": False,
            "cardRetentionReport": {"source_sha256": acquired['cardRetentionSha256']},
            "systemKoodaSha256": acquired["systemKoodaSha256"], "cardReceipt": str(old_receipt),
            "cardResult": str(self.fixture.source), "dispatchReceipt": str(old_receipt)})
        with self.assertRaisesRegex(RUNNER.PatrolError, "Chatr|差集"):
            self.runner.start_document(acquired)
        with self.assertRaisesRegex(RUNNER.PatrolError, "Chatr"):
            self.runner.finish_document()

    def test_reused_document_independent_review_must_be_read_again(self):
        acquired = self.acquire()
        self.document_completion(acquired)
        stage = self.runner.finish_document()
        archived_receipt = Path(stage["cardReceipt"])
        changed = json.loads(archived_receipt.read_text())
        changed["verification"]["google_chatr_date_fallback_review"]["matches"] = False
        archived_receipt.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(RUNNER.PatrolError, "Chatr"):
            self.runner.finish_document()

    def test_a_only_contract_and_launcher_keep_b_to_f_disabled(self):
        contract = RUNNER.execution_contract(True)
        self.assertEqual(contract["stageOrder"], ["document", "standard", "expiredCardProbe"])
        self.assertFalse(contract["taskFEnabledForRun"])
        self.assertEqual(contract["taskFSkippedReason"], "task_a_only")
        self.runner.args.task_a_only = True
        foreign = {name: "1" for name in RUNNER.TASK_BCDE_GATE_ENV_VARS}
        foreign.update({"OPENSMS_FORCE_TASK_F": "1", "OPENSMS_TASK_F_POLICY_CATALOG_SYNC": "1"})
        with patch.dict(os.environ, foreign):
            child = self.runner.child_environment(foreign)
        for field in foreign:
            self.assertNotIn(field, child)
        launcher = (fixtures.ROOT / "scripts/run_opensms_task_a.sh").read_text()
        self.assertIn('run --task-a-only --json', launcher)
        self.assertNotIn('--force-task-', launcher)
        self.assertNotIn('--task-f-only', launcher)


if __name__ == "__main__":
    unittest.main()
