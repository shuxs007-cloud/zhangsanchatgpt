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
SCRIPT = ROOT / "scripts" / "opensms_generate_task_c_candidates.py"
SET_SCRIPT = ROOT / "opensms_set_carrier_candidates.mjs"
TARGET_C = "换号区C"
TARGET_T = "换号区T"
HEADERS = ["电话", "运营商", "opr", "卡槽", "4日发送窗口数", "4日回复率", "换号状态", "过期状态", "截止日", "10日发送窗口数"]


def make_workbook(path: Path, rows: list[list[object]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "sheet1"
    sheet.append(HEADERS + ["09-02回复率", "09-01回复率"])
    for row in rows:
        # Old fixtures focus on other gates; they now carry an eligible ten-day value.
        base = row if len(row) == len(HEADERS) else row + [None] * (9 - len(row)) + [400]
        sheet.append(base + [0.01, 0.01])
    workbook.save(path)


def run_generator(workbook: Path, output_dir: Path, *, force: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if force:
        env["OPENSMS_FORCE_TASK_C"] = "1"
    else:
        env.pop("OPENSMS_FORCE_TASK_C", None)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(workbook),
            "--target-carrier-c",
            TARGET_C,
            "--target-carrier-t",
            TARGET_T,
            "--server-date",
            "2026-09-02",
            "--tag",
            "test",
            "--output-dir",
            str(output_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


class TaskCCandidateTests(unittest.TestCase):
    def test_unconfigured_carriers_are_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / "result.xlsx"
            output = root / "snapshots"
            make_workbook(workbook, [
                ["1001", "A1-测试", "Koodo", "101_1", 100, 0.01, ""],
                ["1002", None, "Koodo", "101_2", 100, 0.01, ""],
                [None, "  ", "unknown", "", "invalid", "invalid", ""],
            ])
            result = run_generator(workbook, output)
            self.assertEqual(0, result.returncode, result.stderr)
            manifest = json.loads((output / "opensms_task_c_test_manifest.json").read_text())
            self.assertEqual([3, 4], manifest["unconfiguredCarrierSkippedRows"])
            self.assertEqual(2, manifest["unconfiguredCarrierSkippedCount"])
            candidate = json.loads(Path(manifest["koodaCandidatePath"]).read_text())
            self.assertEqual(["1001"], [row["电话"] for row in candidate["rows"]])

    def test_requires_current_force_gate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / "result.xlsx"
            output_dir = root / "snapshots"
            make_workbook(workbook, [])
            result = run_generator(workbook, output_dir, force=False)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("OPENSMS_FORCE_TASK_C", result.stderr)
            self.assertFalse(output_dir.exists())

    def test_strict_boundaries_and_unchanged_chatr(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / "result.xlsx"
            output_dir = root / "snapshots"
            make_workbook(workbook, [
                ["1001", "B1-测试", "302220 Koodo", "101_1", 100, 0.0499, "已换号"],
                ["1002", "B1-测试", "Kooda", "101_2", 100, 0.05, ""],
                ["1003", "A1-测试", "Koodo", "101_3", 100, "4.99%", ""],
                ["1004", "A1-测试", "302610 Chatr", "101_4", 100, 0.0399, ""],
                ["1005", "A1-测试", "Chatr", "101_5", 100, 0.04, ""],
                ["1006", "A1-测试", "Chatr", "101_6", 100, 0.03, "已换号"],
                ["1007", "A1-测试", "Chatr", "101_7", 100, 0.03, "未换号"],
                ["1008", "A1-测试", "Koodo Chatr", "101_8", 100, 0.03, ""],
                ["1009", "A1-测试", "Koodo", "101_9", 0, 0, ""],
                ["1010", "A21-股票-C-100", "Koodo", "101_10", 100, 0.01, ""],
                ["1013", "A2-股票-C-120", "Koodo", "101_13", 100, 0.01, ""],
                ["1014", "A11-测试", "Koodo", "101_14", 49, 0.01, ""],
                ["1015", "B11-测试", "Chatr", "101_15", 49, 0.01, "未换号"],
                ["1016", "A3-测试", "Koodo", "101_16", 1, 0.01, ""],
                ["1017", "B1V-测试", "Telus1", "101_17", 50, 0.01, ""],
                ["1018", "B11V-测试", "Telus1", "101_18", 49, 0.01, ""],
                ["1019", "B12V-测试", "Chatr", "101_19", 50, 0.01, "未换号"],
                ["1020", "快过期续费C", "Chatr", "101_20", 100, 0.01, "未换号"],
                ["1021", "快过期续费T", "Koodo", "101_21", 100, 0.01, ""],
                ["1025", "三方隔离", "Koodo", "101_25", 100, 0.01, ""],
                ["1026", "三方隔离", "Chatr", "101_26", 100, 0.01, "未换号"],
                ["1022", "过期下架卡T", "Koodo", "101_22", 100, 0.01, "", "已过期", "2026-09-01"],
                ["1023", "快过期隔离区T", "Koodo", "101_23", 100, 0.01, "", "快过期", "2026-09-03"],
                ["1024", "B1-测试", "Koodo", "101_24", 100, 0.01, "", "快过期", "2026-09-04"],
            ])
            result = run_generator(workbook, output_dir)
            self.assertEqual(0, result.returncode, result.stderr)

            manifest = json.loads((output_dir / "opensms_task_c_test_manifest.json").read_text())
            kooda = json.loads(Path(manifest["koodaCandidatePath"]).read_text())
            chatr = json.loads(Path(manifest["chatrCandidatePath"]).read_text())
            self.assertEqual(["1001", "1003", "1008", "1016", "1017", "1024"], [row["电话"] for row in kooda["rows"]])
            self.assertEqual(["1004", "1007", "1019"], [row["电话"] for row in chatr["rows"]])
            self.assertEqual(TARGET_T, kooda["targetCarrierName"])
            self.assertEqual(2, manifest["isolationCarrierHeldCount"])
            self.assertEqual(TARGET_C, chatr["targetCarrierName"])
            self.assertEqual({"C": TARGET_C, "T": TARGET_T}, manifest["targetCarrierNames"])
            self.assertEqual(0.05, kooda["replyRateThresholdExclusive"])
            self.assertEqual(0.04, chatr["replyRateThresholdExclusive"])
            self.assertEqual(50, manifest["replyRateSendThresholdInclusive"])
            self.assertEqual(360, manifest["tenDaySendThresholdExclusive"])
            self.assertEqual(0, manifest["tenDaySendGateExcludedCount"])
            self.assertTrue(all(row["10日发送窗口数"] == 400 for row in kooda["rows"] + chatr["rows"]))
            self.assertEqual(3, manifest["replyRateSendGateExcludedCount"])
            self.assertEqual(1, manifest["replyRateSendGateExcludedCounts"]["A11"])
            self.assertEqual(1, manifest["replyRateSendGateExcludedCounts"]["B11"])
            self.assertEqual(1, manifest["replyRateSendGateExcludedCounts"]["B11V"])
            self.assertEqual(["A21", "A2"], manifest["protectedSourceCarrierGroups"])
            self.assertEqual(2, manifest["protectedSourceExcludedCount"])
            self.assertEqual({"A21": 1, "A2": 1}, manifest["protectedSourceExcludedCounts"])
            self.assertEqual(1, manifest["protectedA21ExcludedCount"])
            self.assertEqual(["快过期续费C", "快过期续费T"], manifest["neverProcessSourceCarrierNames"])
            self.assertEqual(2, manifest["neverProcessSourceExcludedCount"])
            self.assertTrue(manifest["taskBPriorityLockEnforced"])
            self.assertEqual("2026-09-03", manifest["tomorrowDate"])
            self.assertEqual(2, manifest["taskBPriorityExcludedCount"])
            self.assertEqual(1, manifest["taskBPriorityExcludedCounts"]["已过期"])
            self.assertEqual(1, manifest["taskBPriorityExcludedCounts"]["明日到期"])
            self.assertNotIn("1020", [row["电话"] for row in chatr["rows"]])
            self.assertNotIn("1021", [row["电话"] for row in kooda["rows"]])
            self.assertTrue(set(row["电话"] for row in kooda["rows"]).isdisjoint(
                row["电话"] for row in chatr["rows"]
            ))

    def test_ten_day_send_strict_gate_for_both_branches_and_aliases(self) -> None:
        values = [359, 360, 361, "361", "1,000", None, "", "bad", "NaN", "Infinity", True, -1]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / "result.xlsx"
            rows = []
            for opr in ("Kooda", "Koodo", "Telus1", "Chatr"):
                for index, value in enumerate(values):
                    rows.append([f"{opr}-{index}", "A3-测试", opr, "101_1", 50, 0.01,
                                 "已换号" if opr != "Chatr" else "未换号", "", "", value])
            make_workbook(workbook, rows)
            result = run_generator(workbook, root / "out")
            self.assertEqual(0, result.returncode, result.stderr)
            manifest = json.loads(result.stdout)
            selected = []
            for key in ("koodaCandidatePath", "chatrCandidatePath"):
                candidate = json.loads(Path(manifest[key]).read_text())
                self.assertEqual(360, candidate["tenDaySendThresholdExclusive"])
                self.assertEqual("10日发送窗口数", candidate["tenDaySendMetric"])
                selected.extend(row["电话"] for row in candidate["rows"])
            self.assertEqual({f"{opr}-{index}" for opr in ("Kooda", "Koodo", "Telus1", "Chatr") for index in (2, 3, 4)}, set(selected))
            self.assertEqual(36, manifest["tenDaySendGateExcludedCount"])
            self.assertEqual({"missing_or_invalid": 28, "not_above_360": 8}, manifest["tenDaySendGateExcludedCounts"])

    def test_missing_ten_day_header_fails_without_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / "result.xlsx"
            wb = Workbook()
            wb.active.title = "sheet1"
            wb.active.append(HEADERS[:-1])
            wb.save(workbook)
            result = run_generator(workbook, root / "out")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("10日发送窗口数", result.stderr)
            self.assertFalse((root / "out").exists())

    def test_unknown_chatr_change_status_skips_only_that_row(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workbook = root / "result.xlsx"
            output_dir = root / "snapshots"
            make_workbook(workbook, [
                ["1001", "A1-测试", "Chatr", "101_1", 100, 0.03, "状态未知"],
                ["1002", "A1-测试", "Chatr", "101_2", 100, 0.03, "未换号"],
            ])
            result = run_generator(workbook, output_dir)
            self.assertEqual(0, result.returncode, result.stderr)
            manifest = json.loads(result.stdout)
            self.assertEqual(1, manifest['dataAnomalySkippedCount'])
            self.assertEqual('change_status_unknown', manifest['dataAnomalySkippedRows'][0]['reason'])
            self.assertEqual(['1002'], [row['电话'] for row in json.loads(Path(manifest['chatrCandidatePath']).read_text())['rows']])

    def test_unified_writer_rechecks_task_c_gate_before_backend_access(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            candidate = Path(folder) / "candidate.json"
            candidate.write_text(json.dumps({
                "task": "C",
                "forceGate": True,
                "route": "task_c_kooda_4day_reply_rate_lt5_to_swap",
                "targetCarrierName": TARGET_T,
                "sourceCarrierNames": ["B1-测试"],
                "rows": [{"电话": "1001", "运营商": "B1-测试"}],
            }))
            env = os.environ.copy()
            env.pop("OPENSMS_FORCE_TASK_C", None)
            result = subprocess.run(
                ["node", str(SET_SCRIPT), str(candidate), TARGET_T, "task_c_test"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=ROOT,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("OPENSMS_FORCE_TASK_C", result.stderr)

    def test_unified_writer_rejects_task_c_target_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            candidate = Path(folder) / "candidate.json"
            candidate.write_text(json.dumps({
                "task": "C",
                "forceGate": True,
                "route": "task_c_chatr_unchanged_4day_reply_rate_lt4_to_swap",
                "targetCarrierName": TARGET_C,
                "sourceCarrierNames": ["A1-测试"],
                "rows": [{"电话": "1001", "运营商": "A1-测试"}],
            }))
            env = os.environ.copy()
            env["OPENSMS_FORCE_TASK_C"] = "1"
            result = subprocess.run(
                ["node", str(SET_SCRIPT), str(candidate), "另一个目标", "task_c_test"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=ROOT,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("目标运营商与候选不一致", result.stderr)

    def test_unified_writer_rejects_task_c_expired_before_backend_access(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            candidate = Path(folder) / "candidate.json"
            candidate.write_text(json.dumps({
                "task": "C",
                "forceGate": True,
                "taskBPriorityLockEnforced": True,
                "serverDate": "2026-09-02",
                "tomorrowDate": "2026-09-03",
                "route": "task_c_kooda_4day_reply_rate_lt5_to_swap",
                "targetCarrierName": TARGET_T,
                "replyRateSendThresholdInclusive": 50,
                "sourceCarrierNames": ["过期下架卡T"],
                "rows": [{
                    "电话": "1001", "运营商": "过期下架卡T", "过期状态": "已过期",
                    "截止日": "2026-09-01", "4日发送窗口数": 100,
                }],
            }, ensure_ascii=False))
            env = os.environ.copy()
            env["OPENSMS_FORCE_TASK_C"] = "1"
            result = subprocess.run(
                ["node", str(SET_SCRIPT), str(candidate), TARGET_T, "task_c_expired_guard_test"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=ROOT,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("任务B已过期/明日到期优先级锁定", result.stderr)

    def test_final_state_rejects_task_c_superseding_task_b(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            task_b = root / "task-b.json"
            task_c = root / "task-c.json"
            spec = root / "spec.json"
            output = root / "result.json"
            task_b.write_text(json.dumps({
                "route": "task_b_expired_to_offline_t",
                "targetCarrierName": "过期下架卡T",
                "rows": [{"电话": "1001"}],
            }, ensure_ascii=False))
            task_c.write_text(json.dumps({
                "route": "task_c_kooda_4day_reply_rate_lt5_to_swap",
                "targetCarrierName": "换号区T",
                "rows": [{"电话": "1001"}],
            }, ensure_ascii=False))
            spec.write_text(json.dumps({
                "orderedStageSupersession": True,
                "groups": [
                    {"mode": "expected", "candidate": str(task_b), "target": "过期下架卡T"},
                    {"mode": "expected", "candidate": str(task_c), "target": "换号区T"},
                ],
            }, ensure_ascii=False))
            result = subprocess.run(
                ["node", str(ROOT / "scripts" / "opensms_collect_final_carrier_state.mjs"), str(spec), str(output)],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("任务B过期/明日到期结果禁止被任务C覆盖", result.stderr)

    def test_unified_writer_rejects_protected_source_before_backend_access(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            candidate = Path(folder) / "candidate.json"
            candidate.write_text(json.dumps({
                "targetCarrierName": "B12-测试",
                "sourceCarrierNames": ["A2-股票-C-120"],
                "rows": [{"电话": "1001", "运营商": "A2-股票-C-120"}],
            }))
            result = subprocess.run(
                ["node", str(SET_SCRIPT), str(candidate), "B12-测试", "protected_source_guard_test"],
                check=False,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("A21/A2号码运营商受保护", result.stderr)

    def test_unified_writer_rejects_quick_renew_c_even_for_forced_task_b(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            candidate = Path(folder) / "candidate.json"
            candidate.write_text(json.dumps({
                "task": "B",
                "forceGate": True,
                "route": "task_b_expired_to_offline_c",
                "serverDate": "2026-09-14",
                "tomorrowDate": "2026-09-15",
                "targetCarrierName": "过期下架卡C",
                "sourceCarrierNames": ["快过期续费C"],
                "rows": [{"电话": "1001", "运营商": "快过期续费C", "截止日": "2026-09-14", "过期状态": "已过期"}],
            }, ensure_ascii=False))
            env = os.environ.copy()
            env["OPENSMS_FORCE_TASK_B"] = "1"
            result = subprocess.run(
                ["node", str(SET_SCRIPT), str(candidate), "过期下架卡C", "quick_renew_c_guard_test"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=ROOT,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("快过期续费C/T永久保护", result.stderr)


if __name__ == "__main__":
    unittest.main()
