import importlib.util
from pathlib import Path
from datetime import datetime, timedelta, timezone
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("temporary_hold", ROOT / "scripts/opensms_temporary_carrier_hold.py")
hold = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hold)


class TemporaryHoldTests(unittest.TestCase):
    def test_isolation_exact_protection(self):
        self.assertTrue(hold.isolation_carrier_held(" 三方隔离 "))
        for name in ("三方隔离T", "非三方隔离", "B11", None):
            self.assertFalse(hold.isolation_carrier_held(name))

    def test_exact_group(self):
        now = datetime.fromisoformat("2026-09-11T00:40:00+08:00")
        for name in ("B11", "b11-精-Te-120", " B11 test "):
            self.assertTrue(hold.temporary_carrier_held(name, now))
        for name in ("B11V", "B11V-test", "B1", "B12", "B110", ""):
            self.assertFalse(hold.temporary_carrier_held(name, now))

    def test_boundary_and_timezone(self):
        self.assertTrue(hold.temporary_carrier_held("B11", hold.B11_RESUME_AT - timedelta(seconds=1)))
        self.assertFalse(hold.temporary_carrier_held("B11", hold.B11_RESUME_AT))
        self.assertFalse(hold.temporary_carrier_held("B11", hold.B11_RESUME_AT.astimezone(timezone.utc)))

    def test_explicit_early_resume(self):
        self.assertEqual(hold.B11_RESUME_AT.isoformat(), "2026-09-11T23:01:08+08:00")
        self.assertFalse(hold.temporary_carrier_held("B11-精-Te-120", datetime.fromisoformat("2026-09-12T00:00:00+08:00")))
        js = (ROOT / "scripts/opensms_temporary_carrier_hold.mjs").read_text()
        self.assertIn(hold.B11_RESUME_AT.isoformat(), js)

    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):
            hold.temporary_carrier_held("B11", datetime(2026, 9, 11))


if __name__ == "__main__":
    unittest.main()
