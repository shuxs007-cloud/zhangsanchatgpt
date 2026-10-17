"""Offline regression coverage for partial acquisition and owned API targets."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = load_module("runner_acquire_reliability", "opensms_patrol_runner.py")
CHAT = load_module("chat_acquire_reliability", "opensms_chat_summary.py")


class AcquisitionReliabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.delivery = self.folder / "delivery"
        self.receipts = self.delivery / "receipts"
        self.receipts.mkdir(parents=True)
        self.run_dir = self.folder / "logs"
        self.run_dir.mkdir()
        self.inputs = self.folder / "fixture-inputs"
        self.inputs.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(RUNNER, "ROOT", self.folder).start()
        patch.dict(sys.modules, {"opensms_chat_summary": CHAT}).start()
        # No test may accidentally launch a real browser helper or subprocess.
        self.process = patch.object(
            RUNNER.subprocess, "run", side_effect=AssertionError("unexpected real subprocess")
        ).start()
        patch.object(RUNNER.subprocess, "Popen", side_effect=AssertionError("unexpected real subprocess")).start()
        self.runner = self.make_runner()
        self.calls = []
        self.failures = {}
        self.payloads = self.make_payloads()

    def make_runner(self):
        runner = object.__new__(RUNNER.PatrolRunner)
        runner.args = SimpleNamespace(cdp="http://127.0.0.1:54321", io_workers=3)
        runner.run_id = "20260906T120000+0800"
        runner.day = "20260906"
        runner.run_dir = self.run_dir
        runner.delivery = self.delivery
        runner.receipts = self.receipts
        runner.state_path = self.receipts / "state.json"
        runner.state = {
            "runId": runner.run_id,
            "status": "running",
            "stages": {},
            "writeGroups": {},
            "timings": [],
        }
        runner.authorization = {f"task{name}": False for name in "BCDEF"}
        runner.document_process = None
        runner.document_log_handle = None
        runner.failure_result = None
        runner.started_monotonic = time.monotonic()
        runner.api_target_id = None
        return runner

    def save_json(self, name, value):
        path = self.inputs / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def retention_acquisition(self):
        from test_opensms_card_retention_report import source
        path = self.save_json('card-retention.json', source(0, run_id=self.runner.run_id))
        return {'cardRetentionReceipt': str(path), 'processingCardRetention': str(path),
                'cardRetentionSha256': RUNNER.sha256(path)}

    def make_xlsx(self, name):
        path = self.inputs / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '</Types>',
            )
            archive.writestr(
                "_rels/.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/workbook.xml",
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Fixture" sheetId="1" r:id="rId1"/></sheets></workbook>',
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '</Relationships>',
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>'
                + (name + " offline fixture ") * 750
                + '</t></is></c></row></sheetData></worksheet>',
            )
        RUNNER.ensure_xlsx(path, "offline fixture")
        return path

    def make_payloads(self):
        self.source = self.make_xlsx("fresh-export.xlsx")
        self.google = self.make_xlsx("fresh-google.xlsx")
        day = "2026-09-06"
        return {
            "captcha_service": {"success": True},
            "login_preflight": {
                "success": True,
                "path": str(self.save_json("login.json", {"success": True})),
            },
            "export": {
                "taskStatus": "SUCCESS", "taskId": "abcdef0123456789abcdef0123456789", "serverDate": day,
                "saved": {"path": str(self.source)},
                "receiptPath": str(self.save_json("export.json", {"taskStatus": "SUCCESS", "serverDate": day})),
                "elapsedMs": 1234,
                "timingsMs": {"backendGenerationMs": 1100, "downloadMs": 134},
            },
            "google": {
                "success": True, "outputPath": str(self.google),
                "receiptPath": str(self.save_json("google.json", {"success": True})),
            },
            "server_dates": {
                "success": True,
                "day": {"serverDate": day, "path": str(self.save_json("day.json", {"serverDate": day})), "durationMs": 20},
                "phoneConfig": {"serverDate": day, "path": str(self.save_json("phone.json", {"serverDate": day})), "durationMs": 30},
            },
            "active_carriers": {
                "success": True,
                "path": str(self.save_json("active.json", {"success": True})),
            },
            "balance": {"records": [], "replyRates": []},
            "system_kooda_inventory": {
                "status": "passed", "runId": self.runner.run_id,
                "totalRows": 1,
                "systemRows": [{
                    "id": "system-id-1", "slotIdentif": "130_1",
                    "cardNo": "4385550123", "opr": "Kooda",
                    "cardStore": "8912230200081836799F",
                    "chatrActivateDate": "2026-09-01", "chatrPlanExpire": "2026-12-15",
                }],
                "paginationTotalStable": True,
                "paginationBackendIdsUnique": True,
                "silentBrowserMode": True,
                "browserForegroundActivationAttempted": False,
            },
            "delivery_manifest_failed": {"status": "passed"},
            "delivery_manifest_complete": {"status": "passed"},
        }

    def command(self, label, argv, **kwargs):
        arguments = [str(value) for value in argv]
        self.calls.append((label, arguments, kwargs))
        if label in self.failures:
            raise self.failures[label]
        if label == "export":
            self.publish_submission_ready(kwargs["env"])
        if "create" in arguments:
            receipt = arguments[arguments.index("--receipt") + 1]
            payload = {
                "success": True, "targetId": "owned-target-123", "receipt": receipt,
                "endpoint": self.runner.args.cdp,
                "requestedUrl": "http://offline.example/#/phoneConfig",
            }
            Path(receipt).write_text(json.dumps(payload), encoding="utf-8")
        elif "close" in arguments:
            payload = {"success": True, "closed": True}
        elif label == "scan_cache":
            output = Path(arguments[arguments.index("--output") + 1])
            receipt = Path(arguments[arguments.index("--receipt") + 1])
            output.write_bytes(b"offline scan cache")
            payload = {"status": "passed", "hashBound": True, "receipt": str(receipt)}
            receipt.write_text(json.dumps(payload), encoding="utf-8")
        elif label == "report_kooda_source":
            from report_kooda_source import projection_payload
            source = Path(arguments[arguments.index('--source') + 1])
            output = Path(arguments[arguments.index('--output') + 1])
            payload = projection_payload(
                ['电话', '运营商', 'opr', '卡槽', 'ICCID', '激活日期', '截止日'],
                [('14385550123', 'Kooda', 'Kooda', '130_1', '8912230200081836799F',
                  '2026-09-01', '2026-12-15')], self.runner.run_id, source, RUNNER.sha256(source))
            output.write_text(json.dumps(payload), encoding='utf-8')
        elif label.startswith("system_kooda_inventory"):
            evidence_path = Path(arguments[2] + ".pagination.json")
            evidence_path.write_text(json.dumps({
                "status": "passed", "runId": self.runner.run_id,
                "attemptCount": 1, "recovered": False,
            }), encoding="utf-8")
            payload = {**self.payloads["system_kooda_inventory"],
                       "paginationEvidencePath": str(evidence_path),
                       "paginationEvidenceSha256": RUNNER.sha256(evidence_path),
                       "paginationAttemptCount": 1, "paginationRecovered": False}
            Path(arguments[2]).write_text(json.dumps(payload), encoding="utf-8")
        elif label.startswith("system_kooda_export_coverage_"):
            payload = {"status": "passed", "retryable": False, "sourceKoodaCount": 1,
                       "matchedCount": 1, "runId": self.runner.run_id}
            Path(arguments[arguments.index("--output") + 1]).write_text(json.dumps(payload), encoding="utf-8")
        elif label == "card_retention_report":
            from test_opensms_card_retention_report import source
            source_payload = source(0, run_id=self.runner.run_id)
            Path(arguments[3]).write_text(json.dumps(source_payload), encoding="utf-8")
            payload = {"status": "passed", "runId": self.runner.run_id, "receipt": arguments[3]}
        else:
            self.assertIn(label, self.payloads, f"unexpected command {label}")
            payload = self.payloads[label]
        return RUNNER.CommandResult(payload, json.dumps(payload), "", 0.01)

    def publish_submission_ready(self, env):
        RUNNER.atomic_json(Path(env["OPENSMS_EXPORT_SUBMISSION_READY_PATH"]), {
            "schemaVersion": 1, "status": "submitted", "runId": self.runner.run_id,
            "token": env["OPENSMS_EXPORT_SUBMISSION_READY_TOKEN"],
            "taskId": self.payloads["export"]["taskId"],
            "serverDate": self.payloads["export"]["serverDate"],
            "exportPageClosed": True, "requestLaneReleased": True,
            "publishedAt": "2026-09-06T04:00:01Z",
        })

    def acquire_failure(self):
        self.runner.command = Mock(side_effect=self.command)
        with self.assertRaises(RUNNER.PatrolError) as caught:
            self.runner.acquire()
        return caught.exception

    def assert_source_saved(self):
        partial = self.runner.state["partialAcquisition"]
        source = Path(partial["sourceArchive"])
        self.assertTrue(source.is_file())
        self.assertTrue(source.is_relative_to(self.delivery))
        self.assertEqual(source.read_bytes(), self.source.read_bytes())
        self.assertEqual(partial["sourceSha256"], RUNNER.sha256(source))
        self.assertTrue(Path(partial["exportReceipt"]).is_file())
        self.assertNotIn("acquire", self.runner.state["stages"])
        persisted = json.loads(self.runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["partialAcquisition"]["sourceArchive"], str(source))
        return partial

    def assert_verified_date(self, partial):
        self.assertEqual(partial["serverDate"], "2026-09-06")
        self.assertIs(partial["serverDateVerified"], True)
        for field in ("phoneDateReceipt", "dayDateReceipt"):
            proof = Path(partial[field])
            self.assertTrue(proof.is_file())
            self.assertEqual(json.loads(proof.read_text())["serverDate"], "2026-09-06")

    def assert_failed_report(self, error, server_date):
        result = self.runner.write_failure_report(error)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["sourceWorkbook"], self.runner.state["partialAcquisition"]["sourceArchive"])
        self.assertEqual(result["serverDate"], server_date)
        self.assertIsNone(result["finalWorkbook"])
        self.assertIs(result["partialWorkbookIsFinal"], False)
        self.assertTrue(Path(result["report"]).is_file())
        self.assertIn(result["sourceWorkbook"], Path(result["chatSummaryPath"]).read_text())
        return result

    def assert_google_skipped(self, data):
        self.assertIs(data["googleAssociationEnabled"], False)
        self.assertEqual(data["googleDisabledReason"], "user_disabled")
        self.assertEqual(data["google"], {"enabled": False, "status": "skipped", "reason": "user_disabled"})
        for field in ("association", "processingAssociation", "googleReceipt", "googleSha256"):
            self.assertIsNone(data[field], field)
        self.assertNotIn("google", [call[0] for call in self.calls])
        self.assertFalse(list(self.delivery.glob("*Google*.xlsx")))

    def test_balance_failure_preserves_source_and_verified_date_without_google(self):
        self.failures["balance"] = RUNNER.PatrolError("mock balance failure")
        error = self.acquire_failure()
        self.assertIn("mock balance failure", str(error))
        partial = self.assert_source_saved()
        self.assert_google_skipped(partial)
        self.assert_verified_date(partial)
        self.assert_failed_report(error, "2026-09-06")

    def test_fast_balance_failure_is_persisted_before_slow_export_finishes(self):
        observed = threading.Event()
        coordinator_thread = threading.get_ident()
        original_record = self.runner.record_partial_acquisition
        original_command = self.command

        def record(**facts):
            self.assertEqual(threading.get_ident(), coordinator_thread)
            original_record(**facts)
            if any(item["stage"] == "balance" for item in facts.get("failures", [])):
                observed.set()

        def command(label, argv, **kwargs):
            if label == "export":
                self.publish_submission_ready(kwargs["env"])
                self.assertTrue(observed.wait(3), "balance failure waited behind export")
                persisted = json.loads(self.runner.state_path.read_text())
                self.assertEqual(persisted["partialAcquisition"]["failures"][0]["stage"], "balance")
            if label == "balance":
                raise RUNNER.PatrolError("mock balance failure")
            return original_command(label, argv, **kwargs)

        self.runner.record_partial_acquisition = record
        self.runner.command = command
        with self.assertRaisesRegex(RUNNER.PatrolError, "mock balance failure"):
            self.runner.acquire()
        self.assertTrue(observed.is_set())
        partial = self.assert_source_saved()
        self.assert_verified_date(partial)
        self.assertNotIn("document", self.runner.state["stages"])

    def test_disagreeing_dates_preserve_source_without_claiming_verified_date(self):
        self.payloads["server_dates"]["day"]["serverDate"] = "2026-09-05"
        error = self.acquire_failure()
        self.assertIn("日期不一致", str(error))
        partial = self.assert_source_saved()
        self.assertIsNone(partial.get("serverDate"))
        self.assertIsNot(partial.get("serverDateVerified"), True)
        self.assert_failed_report(error, None)

    def test_google_unavailable_never_dispatched_and_acquisition_succeeds(self):
        self.failures["google"] = RUNNER.PatrolError("mock Google failure")
        self.runner.command = Mock(side_effect=self.command)
        result = self.runner.acquire()
        partial = self.runner.state["partialAcquisition"]
        self.assert_verified_date(partial)
        self.assert_google_skipped(result)
        self.assertEqual(partial["status"], "complete")

    def test_successful_acquisition_persists_complete_stage_and_all_inputs(self):
        self.runner.command = Mock(side_effect=self.command)
        result = self.runner.acquire()
        partial = self.runner.state["partialAcquisition"]
        self.assertEqual(partial["status"], "complete")
        self.assertIs(partial["sourceVerified"], True)
        self.assert_google_skipped(partial)
        self.assert_verified_date(partial)
        self.assertEqual(result["sourceSha256"], RUNNER.sha256(self.source))
        self.assertNotIn("google", result["subtaskDurations"])
        self.assertTrue(result["scanCacheHashBound"])
        inventory = Path(result["systemKoodaInventory"])
        processing_inventory = Path(result["processingSystemKooda"])
        self.assertTrue(inventory.is_relative_to(self.receipts))
        self.assertTrue(processing_inventory.is_relative_to(self.folder))
        self.assertNotEqual(inventory, processing_inventory)
        self.assertEqual(inventory.read_bytes(), processing_inventory.read_bytes())
        self.assertEqual(result["systemKoodaSha256"], RUNNER.sha256(inventory))
        self.assertEqual(result["systemKoodaSha256"], RUNNER.sha256(processing_inventory))
        system_payload = json.loads(inventory.read_text(encoding="utf-8"))
        self.assertEqual(system_payload["runId"], self.runner.run_id)
        self.assertEqual(result["systemKoodaRows"], 1)
        self.assertEqual(result["systemKoodaRows"], system_payload["totalRows"])
        persisted = json.loads(self.runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["partialAcquisition"]["status"], "complete")
        stage = persisted["stages"]["acquire"]
        for key in (
            "sourceArchive", "processingSource",
            "scanCache", "scanCacheReceipt", "exportReceipt", "loginReceipt",
            "phoneDateReceipt", "dayDateReceipt", "activeCarrierReceipt", "balance",
            "systemKoodaInventory", "processingSystemKooda",
        ):
            with self.subTest(key=key):
                path = Path(stage[key])
                self.assertTrue(path.is_file())
                self.assertTrue(path.is_relative_to(self.folder))
                self.assertEqual(stage[key], result[key])
        self.assertEqual(stage["serverDate"], "2026-09-06")
        self.assertIn("completedAt", stage)
        self.assertEqual(persisted["timings"][-1]["stage"], "acquire")
        self.assertEqual(json.loads(Path(stage["balance"]).read_text())["serverDate"], stage["serverDate"])

    def test_default_acquisition_uses_four_read_workers(self):
        parser = RUNNER.build_parser()
        self.assertEqual(parser.parse_args(["run"]).io_workers, 4)
        self.assertEqual(parser.parse_args(["run", "--io-workers", "3"]).io_workers, 3)

    def test_report_projection_waits_for_scan_and_never_queries_full_inventory(self):
        def command(label, argv, **kwargs):
            if label == 'report_kooda_source':
                self.assertIn('scan_cache', [call[0] for call in self.calls])
                self.assertTrue(self.runner.state['partialAcquisition']['sourceVerified'])
            return self.command(label, argv, **kwargs)
        self.runner.command = command
        result = self.runner.acquire()
        reads = [call for call in self.calls if call[0] == 'report_kooda_source']
        self.assertEqual(len(reads), 1)
        self.assertNotIn('env', reads[0][2])
        self.assertFalse(any(call[0].startswith('system_kooda_') for call in self.calls))
        snapshot = json.loads(Path(result['systemKoodaInventory']).read_text())
        self.assertEqual(snapshot['sourceScope'], 'current_10day_export_only')
        self.assertFalse(snapshot['backendInventoryQueried'])

    def test_invalid_projection_stops_without_inventory_fallback_or_document(self):
        def command(label, argv, **kwargs):
            result = self.command(label, argv, **kwargs)
            if label == 'report_kooda_source':
                result.payload['systemRows'][0]['id'] = 'fake-backend-id'
                target = [str(item) for item in argv]
                Path(target[target.index('--output') + 1]).write_text(json.dumps(result.payload))
            return result
        self.runner.command = command
        with self.assertRaisesRegex(RUNNER.PatrolError, '投影|来源|身份'):
            self.runner.acquire()
        self.assertEqual(len([call for call in self.calls if call[0] == 'report_kooda_source']), 1)
        self.assertFalse(any(call[0].startswith('system_kooda_') for call in self.calls))
        self.assertNotIn('document', self.runner.state['stages'])

    def test_export_failure_preserves_system_without_source_or_date_claim(self):
        self.payloads["export"]["taskStatus"] = "FAILED"
        error = self.acquire_failure()
        self.assertIn("未返回SUCCESS", str(error))
        partial = self.runner.state["partialAcquisition"]
        self.assertEqual(partial["status"], "partial")
        self.assert_google_skipped(partial)
        self.assertNotIn('systemKoodaRows', partial)
        self.assertFalse(any(call[0] == 'report_kooda_source' for call in self.calls))
        for key in ("sourceArchive", "sourceSha256", "sourceVerified", "serverDate", "serverDateVerified"):
            self.assertFalse(partial.get(key), key)
        self.assertNotIn("acquire", self.runner.state["stages"])
        self.assertFalse(any(call[0] == "server_dates" for call in self.calls))
        persisted = json.loads(self.runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["partialAcquisition"]["google"], partial["google"])
        report = self.runner.write_failure_report(error)
        self.assertIsNone(report["sourceWorkbook"])
        self.assertIsNone(report["serverDate"])
        self.assertIsNone(report["finalWorkbook"])

    def test_failure_report_rejects_modified_source_even_when_xlsx_remains_valid(self):
        self.failures["balance"] = RUNNER.PatrolError("mock balance failure")
        error = self.acquire_failure()
        partial = self.assert_source_saved()
        source = Path(partial["sourceArchive"])
        with zipfile.ZipFile(source, "a", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("modified-after-verification.xml", "<modified>true</modified>")
        RUNNER.ensure_xlsx(source, "valid but changed source")
        self.assertNotEqual(RUNNER.sha256(source), partial["sourceSha256"])
        report = self.runner.write_failure_report(error)
        self.assertEqual(report["status"], "failed")
        self.assertIsNone(report["sourceWorkbook"])
        self.assertIsNone(report["finalWorkbook"])
        self.assertFalse(report["partialWorkbookIsFinal"])
        self.assertEqual(report["serverDate"], "2026-09-06")
        self.assertNotIn(str(source), Path(report["chatSummaryPath"]).read_text())

    def test_create_and_close_owned_target_use_guard_receipt_once(self):
        self.runner.command = Mock(side_effect=self.command)
        self.runner.create_api_target()
        self.assertEqual(self.runner.api_target_id, "owned-target-123")
        create_calls = [call for call in self.calls if "create" in call[1]]
        self.assertEqual(len(create_calls), 1)
        argv = create_calls[0][1]
        self.assertEqual(argv[argv.index("--run-id") + 1], self.runner.run_id)
        receipt = Path(argv[argv.index("--receipt") + 1])
        self.assertTrue(receipt.is_relative_to(self.receipts))
        self.assertTrue(receipt.is_file())
        self.runner.close_api_target()
        self.runner.close_api_target()
        close_calls = [call for call in self.calls if "close" in call[1]]
        self.assertEqual(len(close_calls), 1)
        self.assertIn(str(receipt), close_calls[0][1])
        self.assertIsNone(self.runner.api_target_id)

    def test_non_owned_target_is_never_closed(self):
        self.runner.api_target_id = "foreign-target"
        self.runner.command = Mock(side_effect=AssertionError("must not close an unowned target"))
        self.runner.close_api_target()
        self.runner.command.assert_not_called()

    def test_recreated_target_preserves_prior_receipt_and_closed_history(self):
        self.runner.command = Mock(side_effect=self.command)
        self.runner.create_api_target()
        first_receipt = Path(self.runner.state["apiTarget"]["receipt"])
        first_contents = first_receipt.read_bytes()
        self.runner.close_api_target()
        closed_target = dict(self.runner.state["apiTarget"])
        with patch.object(RUNNER.time, "time_ns", return_value=123456789):
            self.runner.create_api_target()
        second_receipt = Path(self.runner.state["apiTarget"]["receipt"])
        self.assertEqual(second_receipt.name, f"opensms_api_target_{self.runner.run_id}_123456789.json")
        self.assertNotEqual(first_receipt, second_receipt)
        self.assertEqual(first_receipt.read_bytes(), first_contents)
        self.assertTrue(second_receipt.is_file())
        self.assertEqual(self.runner.state["apiTargetHistory"], [closed_target])
        self.assertEqual(self.runner.state["apiTarget"]["status"], "open")
        persisted = json.loads(self.runner.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["apiTargetHistory"], [closed_target])
        self.assertEqual(persisted["apiTarget"]["receipt"], str(second_receipt))
        self.runner.close_api_target()
        close_calls = [call for call in self.calls if "close" in call[1]]
        self.assertEqual(len(close_calls), 2)
        self.assertIn(str(first_receipt), close_calls[0][1])
        self.assertIn(str(second_receipt), close_calls[1][1])
        self.assertEqual(self.runner.state["apiTargetHistory"], [closed_target])

    def test_cleanup_failure_does_not_replace_execute_failure(self):
        primary = RUNNER.PatrolError("primary acquisition failure")
        self.runner.verify_policy = Mock()
        self.runner.acquire = Mock(side_effect=primary)
        self.runner.command = Mock(side_effect=self.command)
        self.runner.close_api_target = Mock(side_effect=RUNNER.PatrolError("cleanup failure"))
        with self.assertRaises(RUNNER.PatrolError) as caught:
            self.runner.execute()
        self.assertIs(caught.exception, primary)
        self.runner.close_api_target.assert_called_once()
        self.assertEqual(self.runner.state["status"], "failed")
        self.assertEqual(self.runner.failure_result["error"], str(primary))

    def test_execute_failure_closes_created_target(self):
        primary = RUNNER.PatrolError("stop after target creation")
        self.runner.verify_policy = Mock()
        self.failures["balance"] = primary
        self.runner.command = Mock(side_effect=self.command)
        with self.assertRaises(RUNNER.PatrolError) as caught:
            self.runner.execute()
        self.assertIs(caught.exception, primary)
        self.assertEqual(sum("create" in call[1] for call in self.calls), 1)
        self.assertEqual(sum("close" in call[1] for call in self.calls), 1)
        self.assertIsNone(self.runner.api_target_id)
        self.assertEqual(self.runner.state["apiTarget"]["status"], "closed")

    def test_foreign_checkpoint_target_receipt_is_not_closed(self):
        receipt = self.save_json("foreign-target.json", {"targetId": "foreign-target"})
        self.runner.state["apiTarget"] = {
            "runId": "different-run", "receipt": str(receipt),
            "targetId": "foreign-target", "status": "open",
        }
        self.runner.command = Mock(side_effect=AssertionError("must not close another run's target"))
        self.runner.close_api_target()
        self.runner.command.assert_not_called()

    def test_create_rejects_endpoint_or_receipt_identity_mismatch(self):
        for field, wrong in (("endpoint", "http://127.0.0.1:9999"), ("receipt", "/tmp/foreign-receipt.json")):
            with self.subTest(field=field):
                runner = self.make_runner()

                def wrong_identity(label, argv, **kwargs):
                    result = self.command(label, argv, **kwargs)
                    result.payload[field] = wrong
                    return result

                runner.command = Mock(side_effect=wrong_identity)
                with self.assertRaises(RUNNER.PatrolError):
                    runner.create_api_target()
                self.assertIsNone(runner.api_target_id)

    def test_owned_creation_receipt_can_be_cleaned_after_create_response_failure(self):
        primary = RUNNER.PatrolError("create response unavailable after allocation")
        self.runner.verify_policy = Mock()

        def create_response_failure(label, argv, **kwargs):
            result = self.command(label, argv, **kwargs)
            if "create" in [str(value) for value in argv]:
                raise primary
            return result

        self.runner.command = Mock(side_effect=create_response_failure)
        with self.assertRaises(RUNNER.PatrolError) as caught:
            self.runner.execute()
        self.assertIs(caught.exception, primary)
        self.assertEqual(sum("close" in call[1] for call in self.calls), 1)
        self.assertEqual(self.runner.state["apiTarget"]["status"], "closed")

    def test_child_environment_removes_foreign_target_from_parent_and_call(self):
        target_key = "OPENSMS_RAW_API_TARGET_ID"
        inherited = {target_key: "parent-target", "ORDINARY_PARENT": "keep-parent"}
        inherited.update({gate: "1" for gate in RUNNER.TASK_BCDE_GATE_ENV_VARS})
        supplied = {target_key: "caller-target", "ORDINARY_CALLER": "keep-caller"}
        with patch.dict(os.environ, inherited, clear=True):
            child = self.runner.child_environment(supplied)
        self.assertNotIn(target_key, child)
        self.assertEqual(child["ORDINARY_PARENT"], "keep-parent")
        self.assertEqual(child["ORDINARY_CALLER"], "keep-caller")
        for gate in RUNNER.TASK_BCDE_GATE_ENV_VARS:
            self.assertNotIn(gate, child)
        self.assertEqual(supplied[target_key], "caller-target")

    def test_command_injects_only_runner_target_and_retains_explicit_task_gates(self):
        target_key = "OPENSMS_RAW_API_TARGET_ID"
        self.runner.api_target_id = "this-run-target"
        supplied = {
            target_key: "caller-target", "OPENSMS_FORCE_TASK_B": "1",
            "BITBROWSER_CDP": "http://127.0.0.1:9998",
        }
        self.process.side_effect = None
        self.process.return_value = subprocess.CompletedProcess(["offline"], 0, '{"success": true}', "")
        inherited = {target_key: "parent-target", "BITBROWSER_CDP": "http://127.0.0.1:9999"}
        inherited.update({gate: "1" for gate in RUNNER.TASK_BCDE_GATE_ENV_VARS})
        with patch.dict(os.environ, inherited, clear=True):
            self.runner.command("offline-child", ["offline"], env=supplied)
        child = self.process.call_args.kwargs["env"]
        self.assertEqual(child[target_key], "this-run-target")
        self.assertEqual(child["BITBROWSER_CDP"], self.runner.args.cdp)
        self.assertEqual(child["OPENSMS_FORCE_TASK_B"], "1")
        for gate in RUNNER.TASK_BCDE_GATE_ENV_VARS:
            if gate != "OPENSMS_FORCE_TASK_B":
                self.assertNotIn(gate, child)
        self.assertEqual(supplied[target_key], "caller-target")


if __name__ == "__main__":
    unittest.main()
