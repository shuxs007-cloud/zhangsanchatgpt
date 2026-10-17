"""Offline runner gates for immutable, current-run Kooda system snapshots."""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "patrol_system_kooda_runner", ROOT / "scripts/opensms_patrol_runner.py"
)
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


class StopBeforePending(Exception):
    """Terminate a mocked task F before any lookup or database operation."""


class SystemKoodaRunnerTests(unittest.TestCase):
    def setUp(self):
        # Even a regression in the mocked paths must never execute live tools.
        self.run_guard = patch.object(
            RUNNER.subprocess, "run", side_effect=AssertionError("live command forbidden")
        )
        self.popen_guard = patch.object(
            RUNNER.subprocess, "Popen", side_effect=AssertionError("live process forbidden")
        )
        self.run_guard.start()
        self.popen_guard.start()
        self.addCleanup(self.run_guard.stop)
        self.addCleanup(self.popen_guard.stop)

    def runner(self, directory):
        database = patch.multiple(RUNNER, TASK_F_DB=Path(directory) / "test.sqlite3",
            TASK_F_CATALOG_SOURCE=Path(directory) / "test_catalog.json",
            TASK_F_LATEST_LIBRARY=Path(directory) / "test_library.json")
        database.start()
        self.addCleanup(database.stop)
        runner = object.__new__(RUNNER.PatrolRunner)
        runner.run_id = "20260907T000001+0800"
        runner.delivery = Path(directory)
        runner.receipts = runner.delivery / "receipts"
        runner.receipts.mkdir()
        runner.state = {"stages": {}, "timings": []}
        runner.authorization = {"taskF": True}  # Standalone diagnostic fixture.
        runner.args = argparse.Namespace(cdp="http://127.0.0.1:54321", task_f_workers=8)
        runner._save_state = Mock()
        return runner

    def snapshot(self, runner, **overrides):
        payload = {
            "status": "passed",
            "runId": runner.run_id,
            "totalRows": 1,
            "systemRows": [{
                "id": 71, "opr": "Telus1", "cardNo": "2361234567",
                "cardStore": "891223020025159114F",
                "chatrActivateDate": "2026-09-01",
                "chatrPlanExpire": "2026-10-12",
            }],
            "paginationTotalStable": True,
            "paginationBackendIdsUnique": True,
            "silentBrowserMode": True,
            "browserForegroundActivationAttempted": False,
            "rows": [],
            **overrides,
        }
        path = runner.receipts / "inventory.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        acquire = {"systemKoodaInventory": str(path), "systemKoodaSha256": RUNNER.sha256(path)}
        return acquire, payload

    def verified_document(self, runner, acquired):
        generated = runner.receipts / "generated-report.xlsx"
        generated.write_bytes(b"mock document; XLSX validation belongs to pending command")
        document = runner.receipts / "archived-report.xlsx"
        document.write_bytes(generated.read_bytes())
        proof = runner.receipts / "document-proof.json"
        proof.write_text(json.dumps({
            "result": "成功", "output_path": str(generated),
            "checks": {"verified": True}, "failed_checks": [],
            "verification": {"system_kooda_source_review": {
                "matches": True, "run_id": runner.run_id,
                "snapshot_sha256": acquired["systemKoodaSha256"],
            }},
        }), encoding="utf-8")
        runner.state["stages"]["document"] = {
            "completedAt": "2026-09-07T01:00:00+08:00", "cardResult": str(document),
            "cardReceipt": str(proof),
        }
        return document, proof, generated

    def test_valid_current_run_snapshot_preserves_raw_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            acquire, payload = self.snapshot(runner)
            actual = runner.verify_system_kooda_acquisition(acquire)
            self.assertEqual(actual, payload)
            self.assertEqual(actual["systemRows"][0]["cardStore"], "891223020025159114F")
            # The source gate must preserve, not recalculate, system cutoff.
            self.assertEqual(actual["systemRows"][0]["chatrPlanExpire"], "2026-10-12")

    def test_empty_complete_current_run_inventory_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            acquire, _ = self.snapshot(runner, totalRows=0, systemRows=[])
            self.assertEqual(runner.verify_system_kooda_acquisition(acquire)["totalRows"], 0)

    def test_pagination_evidence_is_rechecked_for_identity_hash_and_attempt_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            evidence = runner.receipts / "inventory.json.pagination.json"
            valid = {"status": "passed", "runId": runner.run_id, "attemptCount": 1, "recovered": False}
            for change in ({}, {"runId": "old-run"}, {"status": "failed"},
                           {"attemptCount": 4}, {"recovered": True}):
                with self.subTest(change=change):
                    data = {**valid, **change}
                    evidence.write_text(json.dumps(data), encoding="utf-8")
                    acquire, _ = self.snapshot(runner,
                        paginationEvidencePath=str(evidence), paginationEvidenceSha256=RUNNER.sha256(evidence),
                        paginationAttemptCount=data["attemptCount"], paginationRecovered=data["recovered"])
                    if change:
                        with self.assertRaises(RUNNER.PatrolError):
                            runner.verify_system_kooda_acquisition(acquire)
                    else:
                        runner.verify_system_kooda_acquisition(acquire)
                        evidence.write_text(json.dumps({**valid, "tampered": True}), encoding="utf-8")
                        with self.assertRaisesRegex(RUNNER.PatrolError, "SHA256"):
                            runner.verify_system_kooda_acquisition(acquire)

    def test_registered_pagination_evidence_cannot_disappear_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            acquire, _ = self.snapshot(runner)
            acquire["systemKoodaPaginationEvidence"] = str(runner.receipts / "inventory.json.pagination.json")
            with self.assertRaisesRegex(RUNNER.PatrolError, "缺少已登记"):
                runner.verify_system_kooda_acquisition(acquire)

    def test_detached_copy_uses_hash_bound_original_pagination_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            evidence = runner.receipts / "inventory.json.pagination.json"
            evidence.write_text(json.dumps({"status": "passed", "runId": runner.run_id,
                "attemptCount": 1, "recovered": False}), encoding="utf-8")
            acquire, payload = self.snapshot(runner,
                paginationEvidencePath=str(evidence), paginationEvidenceSha256=RUNNER.sha256(evidence),
                paginationAttemptCount=1, paginationRecovered=False)
            original = Path(acquire["systemKoodaInventory"])
            staged = runner.delivery / "detached-inventory.json"
            staged.write_bytes(original.read_bytes())
            local = {**acquire, "systemKoodaInventory": str(staged)}
            # Merely copying a file does not authorize an arbitrary sidecar.
            with self.assertRaisesRegex(RUNNER.PatrolError, "分页诊断"):
                runner.verify_system_kooda_acquisition(local)
            binding = {"path": str(staged), "sourcePath": str(original),
                       "sha256": acquire["systemKoodaSha256"]}
            runner.state["backgroundVerifiedInputs"] = {"inventory": binding}
            with self.assertRaisesRegex(RUNNER.PatrolError, "后台副本"):
                runner.verify_system_kooda_acquisition(local)
            runner.state["backgroundTaskF"] = True
            self.assertEqual(runner.verify_system_kooda_acquisition(local), payload)
            for change in ({"sha256": "wrong"}, {"sourcePath": str(staged)},
                           {"path": str(original)}):
                with self.subTest(change=change):
                    runner.state["backgroundVerifiedInputs"]["inventory"] = {**binding, **change}
                    with self.assertRaises(RUNNER.PatrolError):
                        runner.verify_system_kooda_acquisition(local)
            runner.state["backgroundVerifiedInputs"]["inventory"] = binding
            original.write_bytes(b"changed archived inventory")
            with self.assertRaisesRegex(RUNNER.PatrolError, "后台副本"):
                runner.verify_system_kooda_acquisition(local)

    def test_missing_snapshot_or_digest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            acquire, _ = self.snapshot(runner)
            for invalid in ({}, {"systemKoodaInventory": acquire["systemKoodaInventory"]}, {
                **acquire, "systemKoodaInventory": str(runner.receipts / "absent.json")
            }):
                with self.subTest(invalid=invalid), self.assertRaises(RUNNER.PatrolError):
                    runner.verify_system_kooda_acquisition(invalid)

    def test_changed_file_or_wrong_digest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            acquire, payload = self.snapshot(runner)
            with self.assertRaises(RUNNER.PatrolError):
                runner.verify_system_kooda_acquisition({**acquire, "systemKoodaSha256": "0" * 64})
            payload["systemRows"][0]["chatrPlanExpire"] = "2028-01-01"
            Path(acquire["systemKoodaInventory"]).write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(RUNNER.PatrolError):
                runner.verify_system_kooda_acquisition(acquire)

    def test_invalid_identity_status_count_and_attestations_are_rejected(self):
        invalid_payloads = [
            {"runId": "different-run"}, {"runId": None},
            {"status": "failed"}, {"status": "partial"}, {"status": None},
            {"totalRows": 2}, {"totalRows": 0}, {"totalRows": "1"},
            {"totalRows": None}, {"totalRows": True}, {"totalRows": 1.0},
            {"systemRows": None}, {"systemRows": {}},
            {"paginationTotalStable": False}, {"paginationTotalStable": 1},
            {"paginationBackendIdsUnique": False}, {"paginationBackendIdsUnique": None},
            {"silentBrowserMode": False}, {"silentBrowserMode": 1},
            {"browserForegroundActivationAttempted": True},
            {"browserForegroundActivationAttempted": None},
            {"browserForegroundActivationAttempted": 0},
        ]
        for overrides in invalid_payloads:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                runner = self.runner(directory)
                acquire, _ = self.snapshot(runner, **overrides)
                # Hash is valid here: structural gates must independently fail.
                with self.assertRaises(RUNNER.PatrolError):
                    runner.verify_system_kooda_acquisition(acquire)

    def test_parent_source_environment_is_removed_and_run_id_is_current(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            prior = {
                "OPENSMS_SYSTEM_KOODA_PATH": "/other-run/inventory.json",
                "OPENSMS_SYSTEM_KOODA_SHA256": "stale-sha",
                "OPENSMS_RUN_ID": "parent-run",
                "OPENSMS_RAW_API_TARGET_ID": "other-target",
                "OPENSMS_FORCE_TASK_B": "1",
                "OPENSMS_SYSTEM_KOODA_STABILIZE_BEFORE_READ": "1",
            }
            with patch.dict(os.environ, prior):
                child = runner.child_environment()
                self.assertEqual(child["OPENSMS_RUN_ID"], runner.run_id)
                for key in ("OPENSMS_SYSTEM_KOODA_PATH", "OPENSMS_SYSTEM_KOODA_SHA256",
                            "OPENSMS_RAW_API_TARGET_ID", "OPENSMS_FORCE_TASK_B",
                            "OPENSMS_SYSTEM_KOODA_STABILIZE_BEFORE_READ"):
                    self.assertNotIn(key, child)
                # The caller's process environment is not mutated.
                self.assertEqual(os.environ["OPENSMS_RUN_ID"], "parent-run")

    def test_explicit_source_environment_is_kept_but_cannot_override_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            runner.api_target_id = "owned-current-target"
            supplied = {
                "OPENSMS_SYSTEM_KOODA_PATH": "/current-run/inventory.json",
                "OPENSMS_SYSTEM_KOODA_SHA256": "current-sha",
                "OPENSMS_RUN_ID": "wrong-supplied-run",
                "OPENSMS_RAW_API_TARGET_ID": "wrong-supplied-target",
            }
            child = runner.child_environment(supplied)
            self.assertEqual(child["OPENSMS_SYSTEM_KOODA_PATH"], supplied["OPENSMS_SYSTEM_KOODA_PATH"])
            self.assertEqual(child["OPENSMS_SYSTEM_KOODA_SHA256"], "current-sha")
            self.assertEqual(child["OPENSMS_RUN_ID"], runner.run_id)
            self.assertEqual(child["OPENSMS_RAW_API_TARGET_ID"], "owned-current-target")
            self.assertEqual(child["BITBROWSER_CDP"], runner.args.cdp)
            self.assertEqual(supplied["OPENSMS_RUN_ID"], "wrong-supplied-run")

    def test_task_f_reuses_verified_acquisition_without_network_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            acquired, _ = self.snapshot(runner)
            runner.state["stages"]["acquire"] = acquired
            catalog = runner.receipts / "mock-catalog.json"
            catalog.write_text("{}", encoding="utf-8")

            def command(label, argv, **kwargs):
                if label == "task_f_catalog_import":
                    return RUNNER.CommandResult({"status": "passed"}, "", "", 0.0)
                if label == "task_f_prepare_pending":
                    self.assertEqual(str(argv[argv.index("--cards") + 1]), acquired["systemKoodaInventory"])
                    raise StopBeforePending()
                self.fail(f"Unexpected command before mocked pending: {label}")

            runner.command = Mock(side_effect=command)
            with patch.object(RUNNER, "TASK_F_CATALOG_SOURCE", catalog):
                with self.assertRaises(StopBeforePending):
                    runner.run_task_f()
            labels = [call.args[0] for call in runner.command.call_args_list]
            self.assertEqual(labels, ["task_f_catalog_import", "task_f_prepare_pending"])
            self.assertNotIn("task_f_kooda_inventory", labels)
            self.assertNotIn("task_f_login_check", labels)

    def test_task_f_rejects_corrupt_acquisition_instead_of_silent_network_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            acquired, _ = self.snapshot(runner, runId="old-run")
            runner.state["stages"]["acquire"] = acquired
            catalog = runner.receipts / "mock-catalog.json"
            catalog.write_text("{}", encoding="utf-8")
            runner.command = Mock(return_value=RUNNER.CommandResult({"status": "passed"}, "", "", 0.0))
            with patch.object(RUNNER, "TASK_F_CATALOG_SOURCE", catalog):
                with self.assertRaises(RUNNER.PatrolError):
                    runner.run_task_f()
            self.assertEqual([call.args[0] for call in runner.command.call_args_list], ["task_f_catalog_import"])

    def test_automatic_task_f_requires_report_before_database_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            runner.authorization["taskF"] = False
            runner.command = Mock(side_effect=AssertionError("no task before report"))
            with self.assertRaises(RUNNER.PatrolError):
                runner.run_task_f()
            runner.command.assert_not_called()

    def test_automatic_pending_is_bound_to_verified_document(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            runner.authorization["taskF"] = False
            acquired, _ = self.snapshot(runner)
            runner.state["stages"]["acquire"] = acquired
            document, _proof, _generated = self.verified_document(runner, acquired)
            catalog = runner.receipts / "mock-catalog.json"
            catalog.write_text("{}", encoding="utf-8")
            def command(label, argv, **kwargs):
                if label == "task_f_catalog_import":
                    return RUNNER.CommandResult({"status": "passed"}, "", "", 0.0)
                if label == "task_f_prepare_pending":
                    self.assertEqual(Path(argv[argv.index("--document") + 1]), document)
                    self.assertEqual(argv[argv.index("--document-sha256") + 1], RUNNER.sha256(document))
                    self.assertEqual(argv[argv.index("--run-id") + 1], runner.run_id)
                    raise StopBeforePending()
                self.fail(f"Unexpected command before scoped pending: {label}")
            runner.command = Mock(side_effect=command)
            with patch.object(RUNNER, "TASK_F_CATALOG_SOURCE", catalog), self.assertRaises(StopBeforePending):
                runner.run_task_f()

    def test_changed_archived_document_rejected_before_any_command(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            runner.authorization["taskF"] = False
            acquired, _ = self.snapshot(runner)
            runner.state["stages"]["acquire"] = acquired
            document, _proof, _generated = self.verified_document(runner, acquired)
            document.write_bytes(b"tampered archived report after independent verification")
            runner.command = Mock(side_effect=AssertionError("must reject before database or network"))
            with self.assertRaises(RUNNER.PatrolError):
                runner.run_task_f()
            runner.command.assert_not_called()

    def test_cross_inventory_digest_rejected_before_any_command(self):
        for wrong_digest in ("0" * 64, None):
            with self.subTest(wrong_digest=wrong_digest), tempfile.TemporaryDirectory() as directory:
                runner = self.runner(directory)
                runner.authorization["taskF"] = False
                acquired, _ = self.snapshot(runner)
                runner.state["stages"]["acquire"] = acquired
                _document, proof, _generated = self.verified_document(runner, acquired)
                payload = json.loads(proof.read_text(encoding="utf-8"))
                payload["verification"]["system_kooda_source_review"]["snapshot_sha256"] = wrong_digest
                proof.write_text(json.dumps(payload), encoding="utf-8")
                runner.command = Mock(side_effect=AssertionError("must reject before database or network"))
                with self.assertRaises(RUNNER.PatrolError):
                    runner.run_task_f()
                runner.command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
