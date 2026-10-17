import importlib.util
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "opensms_finalize_patrol_report.py"
SPEC = importlib.util.spec_from_file_location("opensms_finalize_patrol_report", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class OfflineQueueMetricsTests(unittest.TestCase):
    def test_current_response_array_is_aggregated(self):
        receipt = {
            "updateResponses": [
                {"data": {"acceptedCount": 4, "skippedCount": 1}},
                {"data": {"acceptedCount": 3, "skippedCount": 2}},
            ]
        }
        self.assertEqual(MODULE.offline_queue_metrics(receipt), {"accepted": 7, "skipped": 3})

    def test_legacy_single_response_remains_supported(self):
        receipt = {"updateResponse": {"data": {"acceptedCount": 5}}}
        self.assertEqual(MODULE.offline_queue_metrics(receipt), {"accepted": 5, "skipped": 0})

    def test_current_direct_write_receipt_is_supported(self):
        receipt = {"actualWriteCount": 2, "successCount": 2}
        self.assertEqual(MODULE.offline_queue_metrics(receipt), {"accepted": 2, "skipped": 0})


class OfflineFinalMetricsTests(unittest.TestCase):
    def test_current_readback_fields_are_supported(self):
        first_write = {"beforeActiveCount": 0}
        first_verify = {"candidateCount": 2, "afterActiveCount": 2}
        final_verify = {
            "candidateCount": 2,
            "afterActiveCount": 2,
            "afterInactiveCount": 0,
        }
        self.assertEqual(
            MODULE.offline_final_metrics(first_write, first_verify, final_verify),
            {
                "pre_active": 0,
                "final_active": 2,
                "final_inactive": 0,
                "recovered_after_submit": 2,
            },
        )


class BalanceDisplayValuesTests(unittest.TestCase):
    def test_zero_total_keeps_daily_statistics_reply_rate(self):
        record = {"dailyTotal": 0, "percentRemaining": None}
        reply = {"status": "ok", "replyRatePct": 0, "statDate": "2026-08-22"}
        self.assertEqual(
            MODULE.balance_display_values(record, reply),
            (0, "2026-08-22", "未知"),
        )

    def test_normal_total_keeps_live_reply_rate(self):
        record = {"dailyTotal": 100, "percentRemaining": 54.5}
        reply = {"status": "ok", "replyRatePct": 5.82, "statDate": "2026-08-26"}
        self.assertEqual(
            MODULE.balance_display_values(record, reply),
            (5.82, "2026-08-26", "正常"),
        )


class SignedChangeTests(unittest.TestCase):
    def test_unavailable_comparisons_do_not_crash_or_claim_zero(self):
        self.assertEqual(MODULE.signed_change(None), "未知")
        self.assertEqual(MODULE.signed_change(None, 3, "个百分点"), "未知")

    def test_zero_and_signed_known_values_remain_numeric(self):
        self.assertEqual(MODULE.signed_change(0), "+0.00%")
        self.assertEqual(MODULE.signed_change(0, 3, "个百分点"), "+0.000个百分点")
        self.assertEqual(MODULE.signed_change(-1.25), "-1.25%")
        self.assertEqual(MODULE.signed_change(2.5), "+2.50%")


class MissingComparisonReportTests(unittest.TestCase):
    def test_complete_report_handles_missing_rates_and_zero_denominators(self):
        cases = [
            (100, None, 5, "未知", "未知", "+0.00%"),
            (100, 5, None, "未知", "未知", "+0.00%"),
            (100, 0, 5, "+5.000个百分点", "未知", "+0.00%"),
            (0, 0, 0, "+0.000个百分点", "未知", "未知"),
            (100, 5, 5, "+0.000个百分点", "+0.00%", "+0.00%"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def save(name, payload):
                path = root / name
                path.write_text(json.dumps(payload), encoding="utf-8")
                return str(path)
            config = {
                "taskAOnly": True, "taskSelection": "A", "groups": [],
                "googleAssociationEnabled": False, "offlineRecoveryEnabled": False,
                "runTime": "offline", "taskId": "offline", "taskDuration": "1s",
                "exportWorkflow": "fake offline fixture", "sourceSha256": "1" * 64,
                "sourceArchive": str(root / "source.xlsx"), "cardResult": str(root / "result.xlsx"),
                "balance": save("balance.json", {}),
                "roomDriverReceipt": save("rooms.json", {
                    "rooms": {
                        "2号机房": {"overall": {"send": 1000, "reply": 100, "replyRatePct": 10}},
                        "3号机房": {"overall": {"send": 1000, "reply": 50, "replyRatePct": 5}},
                        "5号机房": {"overall": {"send": 1000, "reply": 60, "replyRatePct": 6}},
                        "6号机房": {"overall": {"send": 1000, "reply": 70, "replyRatePct": 7}},
                    },
                }),
                "cardReceipt": save("card.json", {
                    "result": "成功", "checks": {"3日4日回复率明细": True},
                    "process": {"main_rows": 7, "matched_rows": 7, "unmatched_slot_count": 0,
                                "activated_unlisted_rows": 0, "activated_unlisted_machine_count": 0,
                                "over_five_unlisted_machine_count": 0},
                    "enrichment": {"decreasing_rate_counts": {}},
                    "verification": {"formula_error_matches": 0},
                }),
            }
            for send, previous_rate, current_rate, pp, rate_pct, send_pct in cases:
                with self.subTest(send=send, previous_rate=previous_rate, current_rate=current_rate):
                    config["analysis"] = save("analysis.json", {
                        "overall": {"send": 100, "reply": 5, "rate_pct": current_rate,
                                    "sameTimeComparison": {"status": "matched", "send": send,
                                                           "rate_pct": previous_rate}},
                        "latestDate": "2026-09-07", "rows": 7, "currentZero": 2,
                        "newSincePrev": 1, "recoveredSincePrev": 0,
                    })
                    config_path = save("config.json", config)
                    output = root / "report.md"
                    with patch.object(sys, "path", [str(SCRIPT.parent), *sys.path]), \
                         patch.object(sys, "argv", [str(SCRIPT), config_path, str(output)]), \
                         contextlib.redirect_stdout(io.StringIO()):
                        MODULE.main()
                    report = output.read_text(encoding="utf-8")
                    self.assertIn(f"- 回复率百分点变化：{pp}。", report)
                    self.assertIn(f"- 回复率变化比例：{rate_pct}。", report)
                    self.assertIn(f"- 变化比例：{send_pct}。", report)
                    self.assertEqual(7, sum(line.startswith("## ") for line in report.splitlines()))
                    self.assertIn("较3号 +5.000 个百分点；较5号 +4.000 个百分点", report)
                    self.assertIn("较6号 +3.000 个百分点", report)
                    self.assertIn("| 5号机房 | 68–71、151–173 | 27 |", report)
                    self.assertIn("| 6号机房 | 130–149、174–192、194–195 | 41 |", report)
                    self.assertIn("| 7号机房 | 94–100 | 7 |", report)
                    self.assertIn("| 合计 | 互不重复的配置设备 | 137 |", report)
                    self.assertNotIn("较4号", report)
                    self.assertNotIn("4号机房", report)
            config.update({
                "taskAOnly": False, "taskSelection": "A,C", "taskCExecuted": True,
                "taskCManifest": save("task-c.json", {
                    "tenDaySendGateExcludedCount": 7,
                    "tenDaySendGateExcludedCounts": {"not_above_360": 5, "missing_or_invalid": 2},
                    "taskBPriorityExcludedCount": 3,
                    "recentDailyReplyRateGuard": {"version": 1, "serverDate": "2026-09-07", "previousDate": "2026-09-06"},
                    "recentDailyReplyRateExcludedCounts": {"above_threshold": 8, "missing_or_invalid": 2},
                }),
            })
            config_path = save("config-c.json", config)
            with patch.object(sys, "path", [str(SCRIPT.parent), *sys.path]), \
                 patch.object(sys, "argv", [str(SCRIPT), config_path, str(output)]), \
                 contextlib.redirect_stdout(io.StringIO()):
                MODULE.main()
            report = output.read_text(encoding="utf-8")
            self.assertIn("两个分支均要求10日发送窗口数严格>360", report)
            self.assertIn("10日发送门禁排除 7 行（不大于360：5；缺失或无效：2）", report)
            self.assertIn("服务器当天 2026-09-07 / 前一天 2026-09-06", report)
            self.assertIn("高回复排除 8 行，单日回复率缺失/无效排除 2 行", report)


if __name__ == "__main__":
    unittest.main()
