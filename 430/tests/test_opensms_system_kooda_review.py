"""Offline independent checks for system-owned Kooda document fields."""
from copy import deepcopy
from datetime import date, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "20260907T010000+0800"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


VERIFY = load_module("system_kooda_independent_verifier", ROOT / "services/opensms_document_api/pipeline/opensms_verify_card_slot_fast.py")
AUDIT = load_module("system_kooda_independent_audit", ROOT / "scripts/opensms_parallel_audit.py")


def source_row(**changes):
    return {
        "id": "id-1", "slotIdentif": "130_01.01", "cardNo": "4385550123", "opr": "Telus1",
        "cardStore": "8912230200081836799F", "chatrActivateDate": "2026-09-01",
        # Intentionally not activation +29: the system value is authoritative.
        "chatrPlanExpire": "2026-12-15", "createTime": "2020-01-01", **changes,
    }


def snapshot_payload(rows=None):
    rows = [source_row()] if rows is None else rows
    return {
        "status": "passed", "runId": RUN_ID, "totalRows": len(rows), "systemRows": rows,
        "paginationTotalStable": True, "paginationBackendIdsUnique": True,
        "silentBrowserMode": True, "browserForegroundActivationAttempted": False,
    }


def workbook_row(**changes):
    return {
        "电话": "14385550123", "卡槽": "130_1", "opr": "Kooda", "ICCID": "8912230200081836799F",
        "开卡日期": None, "激活日期": datetime(2026, 9, 1), "截止日": datetime(2026, 12, 15), **changes,
    }


class SystemKoodaReviewTests(unittest.TestCase):
    def load_snapshot(self, payload=None, expected_hash=None, expected_run_id=RUN_ID):
        payload = snapshot_payload() if payload is None else payload
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "system.json"
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            path.write_bytes(encoded)
            return VERIFY.load_system_kooda_snapshot(path, expected_hash or hashlib.sha256(encoded).hexdigest(), expected_run_id)

    def test_snapshot_binds_run_hash_completeness_and_silent_proof(self):
        loaded = self.load_snapshot()
        self.assertEqual(loaded["total_rows"], 1)
        self.assertEqual(loaded["unkeyed_rows"], 0)
        self.assertEqual(loaded["run_id"], RUN_ID)
        self.assertEqual(len(loaded["index"]), 1)
        with self.assertRaisesRegex(RuntimeError, "SHA256"):
            self.load_snapshot(expected_hash="0" * 64)
        with self.assertRaisesRegex(RuntimeError, "不属于本轮"):
            self.load_snapshot(expected_run_id="previous-run")
        for field, value in (
            ("status", "failed"), ("totalRows", 2), ("paginationTotalStable", False),
            ("paginationBackendIdsUnique", False), ("silentBrowserMode", False),
            ("browserForegroundActivationAttempted", True),
        ):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                payload = snapshot_payload()
                payload[field] = value
                self.load_snapshot(payload)

    def test_unkeyed_inventory_is_counted_but_cannot_match_a_main_row(self):
        loaded = self.load_snapshot(snapshot_payload([source_row(), source_row(id="no-phone", cardNo=None)]))
        self.assertEqual(loaded["total_rows"], 2)
        self.assertEqual(loaded["unkeyed_rows"], 1)
        self.assertEqual(len(loaded["index"]), 1)
        self.assertFalse(VERIFY.review_system_kooda_row(loaded, workbook_row(电话=None))["matches"])

    def test_duplicate_compound_key_or_backend_id_is_rejected(self):
        for row in (source_row(id="id-2", slotIdentif="130_1"), source_row(slotIdentif="131_2", cardNo="4385550124")):
            with self.subTest(row=row), self.assertRaisesRegex(RuntimeError, "重复"):
                self.load_snapshot(snapshot_payload([source_row(), row]))

    def test_compound_identity_never_falls_back_to_slot_or_phone_alone(self):
        loaded = self.load_snapshot(snapshot_payload([source_row(), source_row(id="id-2", slotIdentif="131_2", cardNo="4385550124")]))
        result = VERIFY.review_system_kooda_row(loaded, workbook_row(电话="14385550124"))
        self.assertFalse(result["matched"])
        self.assertFalse(result["matches"])
        self.assertEqual(result["errors"][0]["reason"], "missing_exact_slot_phone_match")

    def test_system_fields_preserve_iccid_f_and_direct_cutoff_with_aliases(self):
        loaded = self.load_snapshot()
        for alias in ("Kooda", "kooda", "Koodo", "Telus1", "TELUS1"):
            with self.subTest(alias=alias):
                result = VERIFY.review_system_kooda_row(loaded, workbook_row(opr=alias))
                self.assertTrue(result["matched"])
                self.assertTrue(result["matches"])
        for wrong in ("8912230200081836799", 8912230200081836799, " 8912230200081836799F"):
            with self.subTest(iccid=wrong):
                self.assertFalse(VERIFY.review_system_kooda_row(loaded, workbook_row(ICCID=wrong))["matches"])

    def test_blank_dates_cannot_fall_back_to_google_or_task_f_and_open_stays_blank(self):
        loaded = self.load_snapshot(snapshot_payload([source_row(chatrActivateDate=None, chatrPlanExpire=" ")]))
        blank = workbook_row(激活日期=None, 截止日=None)
        self.assertTrue(VERIFY.review_system_kooda_row(loaded, blank)["matches"])
        for column in ("开卡日期", "激活日期", "截止日"):
            with self.subTest(column=column):
                wrong = {**blank, column: "2026-09-01"}
                self.assertFalse(VERIFY.review_system_kooda_row(loaded, wrong)["matches"])

    def test_source_nonempty_invalid_dates_or_numeric_iccid_rejected(self):
        for field, value in (("chatrActivateDate", "unknown"), ("chatrPlanExpire", "2026-02-30"),
                             ("chatrPlanExpire", 123), ("cardStore", 8912230200081836799), ("opr", "Chatr")):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.load_snapshot(snapshot_payload([source_row(**{field: value})]))
        self.assertFalse(VERIFY.review_system_kooda_row(self.load_snapshot(), workbook_row(截止日="无效非空"))["matches"])

    def test_iso_dates_and_times_keep_calendar_day_without_timezone_shift(self):
        for value in ("2026-09-01", "2026-09-01 12:34:56", "2026-09-01T23:34:56-07:00", "2026-09-01T12:34:56Z"):
            self.assertEqual(VERIFY.strict_system_date(value), date(2026, 9, 1))
        for value in (None, "", " \n"):
            self.assertIsNone(VERIFY.strict_system_date(value))
        for value in ("2026-9-1", "日期2026-09-01", "2026-09-01junk", False):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                VERIFY.strict_system_date(value)

    def test_iccid_header_is_unique_and_last_and_non_kooda_unaffected(self):
        self.assertTrue(VERIFY.system_kooda_iccid_header_matches(["电话", "运营商", "ICCID"]))
        for headers in ([], ["电话"], ["ICCID", "电话"], ["ICCID", "ICCID"]):
            self.assertFalse(VERIFY.system_kooda_iccid_header_matches(headers))
        self.assertIsNone(VERIFY.review_system_kooda_row(self.load_snapshot(), workbook_row(opr="Chatr", 开卡日期="2020-01-01")))

    def test_system_review_is_required_even_with_legacy_alias_passing(self):
        review = {"source": "system", "matches": True}
        receipt = {
            "result": "成功", "checks": {"表头": True}, "failed_checks": [],
            "process": {"main_rows": 1, "matched_rows": 1},
            "verification": {
                "unused_port_review": {"matches": True}, "task_f_kooda_date_review": {"matches": True},
                "task_f_missing_activation_red_review": {"matches": True},
                "task_f_missing_dates_new_card_review": {"matches": True}, "system_kooda_source_review": review,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            for variant, expected_status in ((receipt, AUDIT.PASS),):
                path.write_text(json.dumps(variant), encoding="utf-8")
                self.assertEqual(AUDIT.check_card_receipt(path).status, expected_status)
            for bad_review in (None, {"source": "system", "matches": False}, {"source": "task_f", "matches": True}):
                changed = deepcopy(receipt)
                if bad_review is None:
                    del changed["verification"]["system_kooda_source_review"]
                else:
                    changed["verification"]["system_kooda_source_review"] = bad_review
                path.write_text(json.dumps(changed), encoding="utf-8")
                self.assertEqual(AUDIT.check_card_receipt(path).status, AUDIT.FAIL)

    def test_task_f_system_document_boundary_keeps_library_independent(self):
        config = {
            "taskFPostDocumentSupplemental": True, "taskFDocumentGeneratedBeforeLookup": True,
            "taskFDocumentDateSource": "system", "taskFResultsApplyNextExport": False,
            "taskFResultsWriteBackToDocument": False,
        }
        self.assertTrue(AUDIT.task_f_document_boundary_matches(config))
        for field, value in (("taskFResultsApplyNextExport", True), ("taskFResultsWriteBackToDocument", True),
                             ("taskFPostDocumentSupplemental", False), ("taskFDocumentGeneratedBeforeLookup", False)):
            with self.subTest(field=field):
                self.assertFalse(AUDIT.task_f_document_boundary_matches({**config, field: value}))
        self.assertTrue(AUDIT.task_f_document_boundary_matches({
            "taskFPostDocumentSupplemental": True, "taskFDocumentGeneratedBeforeLookup": True,
            "taskFResultsApplyNextExport": True,
        }))


if __name__ == "__main__":
    unittest.main()
