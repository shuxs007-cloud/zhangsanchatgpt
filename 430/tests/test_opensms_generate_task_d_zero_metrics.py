from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "opensms_generate_task_d_zero_metrics.py"


class TaskDQuickRenewProtectionTests(unittest.TestCase):
    def test_quick_renew_c_and_t_are_excluded_from_zero_reply_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook_path = root / "result.xlsx"
            output_path = root / "candidate.json"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "sheet1"
            sheet.append(["电话", "运营商", "opr", "卡槽", "09-02总回复量", "09-01总回复量"])
            sheet.append(["1001", "快过期续费C", "Chatr", "101_1", 0, 0])
            sheet.append(["1002", "快过期续费T", "Koodo", "101_2", 0, 0])
            sheet.append(["1003", "换号区C", "Chatr", "101_3", 0, 0])
            sheet.append(["1004", None, "Chatr", "101_4", 0, 0])
            sheet.append([None, "  ", "unknown", "", 0, 0])
            workbook.save(workbook_path)
            env = os.environ.copy()
            env["OPENSMS_ENABLE_TASK_D"] = "1"
            env["OPENSMS_FORCE_TASK_D"] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(workbook_path),
                    "--server-date", "2026-09-02",
                    "--tag", "test",
                    "--output", str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=ROOT,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(["1003"], [row["电话"] for row in payload["rows"]])
            self.assertEqual([5, 6], payload["unconfiguredCarrierSkippedRows"])
            self.assertEqual(2, payload["unconfiguredCarrierSkippedCount"])
            self.assertEqual(["快过期续费C", "快过期续费T"], payload["neverProcessSourceCarrierNames"])
            self.assertEqual(2, payload["neverProcessSourceExcludedCount"])
            self.assertEqual({"快过期续费C": 1, "快过期续费T": 1}, payload["neverProcessSourceExcludedCounts"])


if __name__ == "__main__":
    unittest.main()
