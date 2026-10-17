"""Pure-data tests of the independent new-card review and receipt fail gates."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


VERIFIER = load_module(
    "missing_dates_independent_verifier",
    ROOT / "services/opensms_document_api/pipeline/opensms_verify_card_slot_fast.py",
)
AUDIT = load_module("missing_dates_receipt_audit", ROOT / "scripts/opensms_parallel_audit.py")
REVIEW_KEY = "task_f_missing_dates_new_card_review"


def card_receipt_fixture():
    return {
        "result": "成功",
        "checks": {"表头": True, "Kooda双日期空白视为未换号新卡": True},
        "failed_checks": [],
        "process": {"main_rows": 1, "matched_rows": 1, "unmatched_slot_count": 0},
        "verification": {
            "unused_port_review": {"matches": True},
            "system_kooda_source_review": {"source": "system", "matches": True},
            "task_f_kooda_date_review": {"source": "system", "matches": True},
            "task_f_missing_activation_red_review": {"matches": True},
            REVIEW_KEY: {"eligible_rows": 1, "mismatch_count": 0, "mismatches": [], "matches": True},
        },
    }


class MissingDatesNewCardReviewTests(unittest.TestCase):
    def check(self, opr="Kooda", activation=None, cutoff=None, status="未换号"):
        return VERIFIER.review_task_f_missing_dates_new_card_row(opr, activation, cutoff, status)

    def test_supported_kooda_aliases_are_case_insensitive(self):
        for alias in ("Kooda", "kooda", "KOODA", "Koodo", "KOODO", "Telus1", "telus1", "TELUS1"):
            with self.subTest(alias=alias):
                result = self.check(opr=alias)
                self.assertTrue(result["eligible"])
                self.assertTrue(result["matches"])
                self.assertEqual(result["expected"], "未换号")

    def test_none_empty_and_whitespace_dates_are_genuinely_blank(self):
        for activation in (None, "", " ", "\t\n"):
            for cutoff in (None, "", " ", "\t\n"):
                with self.subTest(activation=activation, cutoff=cutoff):
                    result = self.check(activation=activation, cutoff=cutoff)
                    self.assertTrue(result["eligible"])
                    self.assertTrue(result["matches"])

    def test_change_status_must_be_exact_without_trimming_or_blank_alias(self):
        for status in (None, "", "已换号", "新卡", " 未换号", "未换号 ", "未换号\n", 0):
            with self.subTest(status=status):
                result = self.check(status=status)
                self.assertTrue(result["eligible"])
                self.assertFalse(result["matches"])
                self.assertEqual(result["actual"], "" if status is None else str(status))

    def test_invalid_nonempty_date_is_not_classified_as_missing(self):
        for value in ("日期错误", "2026-99-99", "null", "None", "0", 0, False):
            for activation, cutoff in ((value, None), (None, value), (value, value)):
                with self.subTest(activation=activation, cutoff=cutoff):
                    result = self.check(activation=activation, cutoff=cutoff, status="已换号")
                    self.assertFalse(result["eligible"])
                    self.assertTrue(result["matches"])

    def test_existing_date_and_non_kooda_rows_are_unaffected(self):
        for activation, cutoff in (("2026-09-01", None), (None, "2026-09-30")):
            result = self.check(activation=activation, cutoff=cutoff, status="已换号")
            self.assertFalse(result["eligible"])
            self.assertTrue(result["matches"])
        for opr in ("Chatr", "Rogers", "Kini", "", None):
            with self.subTest(opr=opr):
                result = self.check(opr=opr, status="已换号")
                self.assertFalse(result["eligible"])
                self.assertTrue(result["matches"])

    def audit(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return AUDIT.check_card_receipt(path)

    def test_receipt_with_independent_passed_review_passes(self):
        result = self.audit(card_receipt_fixture())
        self.assertEqual(result.status, AUDIT.PASS)
        self.assertEqual(result.details[REVIEW_KEY]["eligible_rows"], 1)

    def test_missing_review_fails_even_when_declared_checks_pass(self):
        payload = card_receipt_fixture()
        del payload["verification"][REVIEW_KEY]
        result = self.audit(payload)
        self.assertEqual(result.status, AUDIT.FAIL)
        self.assertIn("未换号新卡独立复核未通过", result.message)

    def test_failed_review_fails_even_when_declared_checks_pass(self):
        payload = card_receipt_fixture()
        payload["verification"][REVIEW_KEY] = {
            "eligible_rows": 1,
            "mismatch_count": 1,
            "mismatches": [{"row": 2, "expected": "未换号", "actual": "已换号"}],
            "matches": False,
        }
        result = self.audit(payload)
        self.assertEqual(result.status, AUDIT.FAIL)
        self.assertEqual(result.details[REVIEW_KEY]["mismatch_count"], 1)
        self.assertIn("未换号新卡独立复核未通过", result.message)


if __name__ == "__main__":
    unittest.main()
