"""Immutable history, exact precedence, bounded reads, no network."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import patch_0500_origins_and_restore as history
import opensms_generate_e_reply_recovery as generator


class OriginPerformanceTests(unittest.TestCase):
    def test_empty_identity_set_does_not_enumerate_or_read_history(self):
        with patch.object(history.glob, "glob", side_effect=AssertionError("no enumeration")), \
             patch.object(history, "load_json", side_effect=AssertionError("no reads")):
            self.assertEqual(history.build_origin_map(keys=set()), {})

    def test_narrow_parallel_matches_serial_and_newest_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i in range(12):
                path = Path(tmp) / f"opensms_e_origin_backup_{i:02d}.json"
                row = {"phone": "14160000001", "卡槽": "101_1", "原运营商": f"B{i}-origin"}
                path.write_text(json.dumps({"rows": [row]}), encoding="utf-8")
                os.utime(path, (100 + i, 100 + i))
                paths.append(path)
            with patch.object(history, "BASE", tmp), patch.object(history, "SNAPSHOTS", tmp + "/none"):
                baseline = history.build_origin_map(workers=1)
                original = history.load_json
                def delayed(path):
                    if str(path) == str(paths[-1]):
                        time.sleep(.025)
                    return original(path)
                with patch.object(history, "load_json", side_effect=delayed) as reader:
                    actual = history.build_origin_map(keys={"14160000001|101_1"}, workers=4)
                self.assertEqual(actual, baseline)
                self.assertEqual(actual["14160000001|101_1"]["origin"], "B11-origin")
                self.assertLessEqual(reader.call_count, 4)
                self.assertEqual(len(list(Path(tmp).glob("*.json"))), 12)

    def test_missing_and_unknown_origins_do_not_invent_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opensms_e_origin_backup_1.json"
            path.write_text(json.dumps({"rows": [{"phone": "14160000001", "卡槽": "101_1",
                                                  "原运营商": history.UNKNOWN}]}))
            with patch.object(history, "BASE", tmp), patch.object(history, "SNAPSHOTS", tmp):
                self.assertEqual(history.build_origin_map(keys={"14160000001|101_1"}), {})

    def test_generator_no_recovery_does_not_scan_history_or_reopen_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.json"
            scan = {"headers": ["运营商", "10-06发送窗口数", "10-06回复窗口数", "电话", "卡槽"],
                    "rows": [("E-T休息区", 10, 0, "14160000001", "101_1"),
                             ("B1", 10, 1, "14160000002", "101_2")]}
            with patch("sys.argv", ["generator", "fixture.xlsx", "--tag", "offline", "--out", str(output),
                                    "--scan-cache", "fixture.scan.pkl"]), \
                 patch.object(generator, "ensure_artifact_dirs"), \
                 patch.object(generator, "load_scan_cache", return_value=scan), \
                 patch.object(generator, "load_workbook", side_effect=AssertionError("do not reopen")), \
                 patch.object(history.glob, "glob", side_effect=AssertionError("do not enumerate")), \
                 contextlib.redirect_stdout(io.StringIO()):
                generator.main()
            value = json.loads(output.read_text())
            self.assertEqual(value["eTotal"], 1)
            self.assertEqual(value["count"], 0)
            self.assertEqual(value["rows"], [])

    def test_deleted_b14_origin_stays_in_e_without_guessing_another_carrier(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.json"
            scan = {"headers": ["运营商", "10-06发送窗口数", "10-06回复窗口数", "电话", "卡槽"],
                    "rows": [("E-T休息区", 10, 1, "14160000001", "101_1")]}
            origins = {"14160000001|101_1": {"origin": "B14-T快下保留卡",
                "source": "historical.json", "sourceType": "e_origin_backup_json"}}
            with patch("sys.argv", ["generator", "fixture.xlsx", "--tag", "offline", "--out", str(output),
                                    "--scan-cache", "fixture.scan.pkl"]), \
                 patch.object(generator, "ensure_artifact_dirs"), \
                 patch.object(generator, "load_scan_cache", return_value=scan), \
                 patch.object(history, "build_origin_map", return_value=origins), \
                 contextlib.redirect_stdout(io.StringIO()):
                generator.main()
            value = json.loads(output.read_text())
            self.assertEqual(value["count"], 0)
            self.assertEqual(value["unidentifiedOriginCount"], 1)
            self.assertEqual(value["rows"][0]["targetCarrierName"], "")
            self.assertEqual(value["rows"][0]["恢复模式"], "pending_origin_identification")


if __name__ == "__main__":
    unittest.main()
