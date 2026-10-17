"""Regression for native Excel types, including exporter numeric spellings."""
from datetime import date, datetime
import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "services/opensms_document_api/pipeline/opensms_verify_card_slot_fast.py"
SPEC = importlib.util.spec_from_file_location("opensms_retained_types_verifier", SCRIPT)
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class RetainedExcelValueTypesTests(unittest.TestCase):
    def test_equal_native_numeric_values_keep_excel_numeric_type(self):
        for source, actual in ((106.0, 106), (0.0, 0), (0, 0.0), (1e20, 100000000000000000000), (0.125, 0.125)):
            with self.subTest(source=source, actual=actual):
                self.assertTrue(VERIFIER.retained_excel_value_matches(source, actual))

    def test_empty_excel_text_and_blank_round_trip_without_information_loss(self):
        for source, actual in (("", None), (None, ""), (None, None), ("", "")):
            with self.subTest(source=source, actual=actual):
                self.assertTrue(VERIFIER.retained_excel_value_matches(source, actual))

    def test_text_is_not_coerced_into_numeric_or_trimmed(self):
        for source, actual in (("106", 106), (106, "106"), ("00106", "106"), (" ", None), (" 106", "106")):
            with self.subTest(source=source, actual=actual):
                self.assertFalse(VERIFIER.retained_excel_value_matches(source, actual))

    def test_boolean_is_not_a_number(self):
        for source, actual in ((True, 1), (False, 0.0), (0, False), (1.0, True), (False, None)):
            with self.subTest(source=source, actual=actual):
                self.assertFalse(VERIFIER.retained_excel_value_matches(source, actual))
        self.assertTrue(VERIFIER.retained_excel_value_matches(True, True))

    def test_numeric_comparison_has_no_tolerance_or_float_conversion(self):
        for source, actual in ((40, 40.00000001), (2**53 + 1, float(2**53 + 1)), (10**20 + 1, 1e20), (0, 1e-20)):
            with self.subTest(source=source, actual=actual):
                self.assertFalse(VERIFIER.retained_excel_value_matches(source, actual))

    def test_nonfinite_numeric_values_are_never_valid(self):
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                self.assertFalse(VERIFIER.retained_excel_value_matches(value, value))

    def test_dates_times_and_text_remain_distinct(self):
        stamp = datetime(2026, 9, 6, 12, 2, 22)
        self.assertTrue(VERIFIER.retained_excel_value_matches(stamp, stamp))
        for actual in (date(2026, 9, 6), "2026-09-06 12:02:22", 46271.50164351852):
            with self.subTest(actual=actual):
                self.assertFalse(VERIFIER.retained_excel_value_matches(stamp, actual))


if __name__ == "__main__":
    unittest.main()
