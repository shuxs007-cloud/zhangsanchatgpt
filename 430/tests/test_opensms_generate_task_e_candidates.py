from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "opensms_generate_task_e_candidates.py"


def write_workbook(path: Path, rows: list[list[object]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "sheet1"
    sheet.append(["电话", "运营商", "过期状态", "截止日"])
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    workbook.close()


class TaskECandidateTests(unittest.TestCase):
    def test_missing_phone_or_invalid_deadline_is_skipped_without_blocking_valid_records(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / 'input.xlsx'
            write_workbook(workbook, [
                ['4160000001', 'A1', '6天', '2026-09-08'],
                [None, 'A1', '6天', '2026-09-08'],
                ['4160000002', 'A1', '已过期', '2026-02-30'],
            ])
            result = self.run_generator(workbook, root / 'out.json', root / 'ledger.json')
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(2, payload['dataAnomalySkippedCount'])
            self.assertEqual(1, payload['candidateCount'])
            self.assertEqual(['phone_missing', 'deadline_missing_or_invalid'], [row['reason'] for row in payload['dataAnomalySkippedRows']])
            self.assertEqual(['4160000001'], payload['groups'][0]['cardNos'])

    def test_unconfigured_carriers_are_skipped_before_phone_and_date_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / "input.xlsx"
            output = root / "out.json"
            write_workbook(workbook, [
                ["1001", "A1", "6天", "2026-09-08"],
                ["1002", None, "快过期", "2026-09-03"],
                [None, "  ", "已过期", "invalid-date"],
                ["1004", None, "正常", "2026-10-01"],
            ])
            completed = self.run_generator(workbook, output, root / "ledger.json")
            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(output.read_text())
            self.assertEqual(1, payload["candidateCount"])
            self.assertEqual(3, payload["eligibleWorkbookRowCount"])
            self.assertEqual(2, payload["unconfiguredCarrierSkippedCount"])
            self.assertEqual([3, 4], [row["源行号"] for row in payload["unconfiguredCarrierSkippedRows"]])

    def run_generator(
        self,
        workbook: Path,
        output: Path,
        ledger: Path,
        *,
        enabled: bool = True,
        retention_records: list[dict] | None = None,
    ):
        env = dict(os.environ)
        if enabled:
            env.update({"OPENSMS_FORCE_TASK_E": "1", "OPENSMS_ENABLE_TASK_E": "1"})
        else:
            env.pop("OPENSMS_FORCE_TASK_E", None)
            env.pop("OPENSMS_ENABLE_TASK_E", None)
        records = retention_records or []
        card_nos = list(dict.fromkeys(str(row.get("cardNo") or "") for row in records if row.get("cardNo")))
        retention = output.with_name("retention.json")
        retention.write_text(json.dumps({
            "task": "E",
            "sourceProvenance": "phone_card_retain_all_pages",
            "endpoint": "/phone/cardRetain/list",
            "pageSize": 200,
            "pageCount": 1,
            "pages": [{"pageNum": 1, "rowCount": len(records)}],
            "totalRecords": len(records),
            "fetchedRecords": len(records),
            "uniqueCardCount": len(card_nos),
            "invalidCardRecordCount": 0,
            "cardNos": card_nos,
            "records": records,
        }, ensure_ascii=False), encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(workbook),
                "--server-date",
                "2026-09-02",
                "--tag",
                "test",
                "--output",
                str(output),
                "--ledger",
                str(ledger),
                "--retention-receipt",
                str(retention),
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_default_off_rejects_candidate_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / "input.xlsx"
            write_workbook(workbook, [["1001", "A1", "6天", "2026-09-08"]])
            completed = self.run_generator(workbook, root / "out.json", root / "ledger.json", enabled=False)
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("OPENSMS_ENABLE_TASK_E", completed.stderr)

    def test_groups_by_deadline_and_excludes_protected_and_submitted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / "input.xlsx"
            output = root / "out.json"
            ledger = root / "ledger.json"
            ledger.write_text(json.dumps({
                "schemaVersion": 1,
                "dedupeKey": "phone",
                "submissions": {"1004": {"phone": "1004", "expireDate": "2026-09-01"}},
            }), encoding="utf-8")
            write_workbook(workbook, [
                ["1001", "A1", "6天", "2026-09-08"],
                ["1002", "B1", "快过期", "2026-09-03"],
                ["1003", "B12", "已过期", "2026-09-01"],
                ["1001", "A1", "6天", "2026-09-08"],
                ["1004", "A11", "6天", "2026-09-09"],
                ["1005", "快过期续费C", "已过期", "2026-09-01"],
                ["1006", "A1", "正常", "2026-10-01"],
            ])
            completed = self.run_generator(workbook, output, ledger)
            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(6, payload["eligibleWorkbookRowCount"])
            self.assertEqual(3, payload["candidateCount"])
            self.assertEqual(3, payload["dateGroupCount"])
            self.assertEqual(1, payload["alreadySubmittedExcludedCount"])
            self.assertEqual(1, payload["neverProcessSourceExcludedCount"])
            self.assertEqual(1, payload["duplicateSameDeadlineCollapsedCount"])
            self.assertEqual(["2026-09-01", "2026-09-03", "2026-09-08"], [item["expireDate"] for item in payload["groups"]])

    def test_card_retention_new_expiry_matching_deadline_is_alert_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / "input.xlsx"
            output = root / "out.json"
            write_workbook(workbook, [
                ["1001", "A1", "6天", "2026-09-08"],
                ["1002", "B1", "快过期", "2026-09-03"],
            ])
            completed = self.run_generator(
                workbook,
                output,
                root / "ledger.json",
                retention_records=[
                    {"cardNo": "1001", "expireBefore": "2026-09-01", "expireAfter": "2026-09-08", "chatId": "a", "createTime": "2026-09-02 01:00:00"},
                    {"cardNo": "1002", "expireBefore": "2026-09-01", "expireAfter": "2026-09-09", "chatId": "b", "createTime": "2026-09-02 02:00:00"},
                    {"cardNo": "9999", "expireBefore": "2026-09-01", "expireAfter": "2026-09-08", "chatId": "c", "createTime": "2026-09-02 03:00:00"},
                ],
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["candidateCount"])
            self.assertTrue(payload["retentionComparisonDoesNotChangeCandidates"])
            self.assertEqual(1, payload["retentionDeadlineMatchPhoneCount"])
            self.assertEqual("1001", payload["retentionDeadlineMatches"][0]["电话"])
            self.assertEqual(1, payload["retentionWorkbookMissingPhoneCount"])

    def test_conflicting_deadlines_for_same_phone_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / "input.xlsx"
            write_workbook(workbook, [
                ["1001", "A1", "6天", "2026-09-08"],
                ["1001", "A1", "快过期", "2026-09-03"],
            ])
            completed = self.run_generator(workbook, root / "out.json", root / "ledger.json")
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("同一号码出现不同截止日", completed.stderr)

    def test_phone_is_normalized_to_backend_last_ten_digits_for_ledger_dedupe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / "input.xlsx"
            output = root / "out.json"
            ledger = root / "ledger.json"
            ledger.write_text(json.dumps({
                "schemaVersion": 1,
                "dedupeKey": "phone",
                "submissions": {"7052749359": {"phone": "7052749359"}},
            }), encoding="utf-8")
            write_workbook(workbook, [["17052749359", "A1", "6天", "2026-09-08"]])
            completed = self.run_generator(workbook, output, ledger)
            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(0, payload["candidateCount"])
            self.assertEqual(1, payload["alreadySubmittedExcludedCount"])


if __name__ == "__main__":
    unittest.main()
