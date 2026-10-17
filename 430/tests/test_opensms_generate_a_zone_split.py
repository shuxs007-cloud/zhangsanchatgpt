from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "opensms_generate_a_zone_split.py"


class AZoneSplitTests(unittest.TestCase):
    def test_a1_a11_low_and_a11_a12_high_use_strict_four_percent_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook_path = root / "export4day.xlsx"
            output_dir = root / "snapshots"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append([
                "电话", "运营商", "opr", "卡槽", "状态",
                "08-21发送窗口数", "08-21回复窗口数",
                "08-20发送窗口数", "08-20回复窗口数",
            ])
            sheet.append(["14160000001", "A12", "chatr", "101_1", "上架", 100, 4, 10, 0])
            sheet.append(["14160000002", "A11", "chatr", "101_2", "上架", 100, 4, 10, 0])
            sheet.append(["14160000003", "A1", "chatr", "101_3", "上架", 100, 3, 100, 10])
            sheet.append(["14160000004", "A11", "chatr", "101_4", "上架", 100, 0, 100, 10])
            sheet.append(["14160000005", "A12", "chatr", "101_5", "上架", 100, 3, 100, 10])
            sheet.append(["14160000006", "A11", "chatr", "101_6", "上架", 49, 2, 100, 10])
            sheet.append(["14160000007", "A12", "chatr", "101_7", "上架", 49, 3, 100, 10])
            sheet.append(["14160000008", "A11", "Fido", "101_8", "上架", 100, 0, 100, 10])
            sheet.append(["14160000009", "A12", "Telus1", "101_9", "上架", 100, 5, 100, 10])
            workbook.save(workbook_path)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(workbook_path),
                    "--tag", "test",
                    "--source-carrier", "A1",
                    "--source-carrier", "A11",
                    "--source-carrier", "A12",
                    "--high-source-carrier", "A11",
                    "--high-source-carrier", "A12",
                    "--target-zero", "E",
                    "--target-low", "A12",
                    "--target-high", "A1",
                    "--output-dir", str(output_dir),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            manifest = json.loads(completed.stdout)
            high = json.loads(Path(manifest["highCandidatePath"]).read_text(encoding="utf-8"))
            low = json.loads(Path(manifest["lowCandidatePath"]).read_text(encoding="utf-8"))
            zero = json.loads(Path(manifest["zeroCandidatePath"]).read_text(encoding="utf-8"))
            self.assertEqual(["14160000001", "14160000002"], [row["phone"] for row in high["rows"]])
            self.assertEqual(["A11", "A12"], high["sourceCarrierNames"])
            self.assertEqual(["14160000003", "14160000008"], [row["phone"] for row in low["rows"]])
            self.assertEqual(["A1", "A11"], low["sourceCarrierNames"])
            self.assertEqual([], zero["rows"])
            self.assertEqual(2, manifest["counts"]["exactBoundaryToHigh"])
            self.assertTrue(manifest["lowIncludesZero"])
            self.assertTrue(manifest["lowExcludesChatrZero"])
            self.assertTrue(manifest["chatrZeroHandledByMainRouting"])
            self.assertEqual(1, manifest["counts"]["excludedChatrZeroToEC"])
            self.assertTrue(manifest["zeroToETargetAbolished"])
            self.assertEqual(50, high["minSendWindowInclusive"])
            self.assertEqual(50, low["minSendWindowInclusive"])
            self.assertEqual(50, high["replyRateLatestSendThresholdInclusive"])
            self.assertEqual(["A1", "A11", "A12"], high["replyRateSendGateGroups"])
            self.assertEqual("a_zone_gte4_to_a1", high["route"])
            self.assertEqual("a_zone_lt4_to_a12", low["route"])
            self.assertEqual("latest date send windows >= 50", manifest["minimumSendRule"])
            self.assertEqual(2, manifest["counts"]["unusableReplyRate"])
            self.assertEqual(1, manifest["counts"]["excludedKoodaKoodoTelus1"])


if __name__ == "__main__":
    unittest.main()
