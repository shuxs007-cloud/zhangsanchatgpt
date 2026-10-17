"""Offline display contracts for independently verified Task D populations."""
from copy import deepcopy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def module(name):
    alias = "retention_reporting_test_" + name
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {alias: result}):
        spec.loader.exec_module(result)
    return result


CHAT = module("opensms_chat_summary")
REPORT = module("opensms_finalize_patrol_report")
HEADINGS = ["## 本轮概况", "## 0回复与回复率", "## 运营商调整", "## 下架恢复",
            "## 余量与提醒", "## 文件与回执", "## 异常"]


def summary(new_c=2, new_t=3, total_c=30, total_t=70):
    return {"status": "passed", "newTransferredCount": new_c + new_t,
            "totalInRetentionCount": total_c + total_t, "countingUnit": "unique_phone10",
            "byCarrier": [{"carrierName": "B14-C快下保留卡", "newTransferredCount": new_c,
                           "totalInRetentionCount": total_c},
                          {"carrierName": "B14-T快下保留卡", "newTransferredCount": new_t,
                           "totalInRetentionCount": total_t}],
            "asOf": "2026-09-07T07:00:00+08:00", "populationReceipt": "/offline/population.json",
            "populationSha256": "a" * 64}


def task(retention=None, status="passed"):
    return {"status": status, "retentionSummary": summary() if retention is None else retention,
            "swapNotificationCount": 4, "retentionUniqueCardCount": 900, "carrierActualWrite": 8}


class RetentionContractTests(unittest.TestCase):
    def test_removed_transfer_display_keeps_population_without_claiming_writes(self):
        value = summary(0, 0)
        current_task = {**task(value), "retentionCarrierTransferEnabled": False, "carrierActualWrite": 0}
        text = CHAT.render_summary({"status": "success", "taskD": current_task})
        self.assertIn("转入功能已删除，本轮新转入0", text)
        self.assertIn("执行后保留区总数 100 个", text)
        config = {"taskDExecuted": True, "taskDStatus": "passed", "taskDRetentionCarrierTransferEnabled": False,
                  "taskDRetentionSummary": value}
        text = "\n".join(REPORT.task_d_retention_lines(config))
        self.assertIn("运营商写入组为空", text)
        self.assertNotIn("历史新增口径", text)

    def test_filtered_misses_are_reported_with_proof_and_not_success(self):
        config = {"taskDRetentionMatchedCardCount": 254, "taskDRetentionActionableCardCount": 252,
                  "taskDFilteredUnmatchedCount": 144, "taskDFilteredUnmatchedCards": ["4160000001"],
                  "taskDNeverProcessSourceExcludedCount": 2, "taskDChatrCandidateCount": 70,
                  "taskDKoodaCandidateCount": 182, "taskDRetentionReceipt": "/offline/filtered.json"}
        text = "\n".join(REPORT.task_d_filtered_retention_lines(config))
        self.assertIn("实际命中（含保护）：254", text)
        self.assertIn("非保护可处理：252", text)
        self.assertIn("未命中跳过：144；不计入候选、实际写入或通知成功", text)
        self.assertIn("| 4160000001 | 支持的OPR筛选未返回，未提交；无法据此判定系统无号 |", text)
        self.assertIn("/offline/filtered.json", text)

    def test_missing_failed_malformed_and_mismatched_summaries_are_unknown(self):
        values = [None, {}, {**summary(), "status": "failed"},
                  {**summary(), "newTransferredCount": None}, {**summary(), "newTransferredCount": True},
                  {**summary(), "newTransferredCount": -1}, {**summary(), "totalInRetentionCount": 99},
                  {**summary(), "countingUnit": "cardRetain"}, {**summary(), "asOf": ""},
                  {**summary(), "populationReceipt": ""}, {**summary(), "populationSha256": "bad"}]
        for mutation in ("duplicate", "missing", "bool_count", "unhashable", "excess_new"):
            item = deepcopy(summary())
            if mutation == "duplicate":
                item["byCarrier"][1]["carrierName"] = item["byCarrier"][0]["carrierName"]
            elif mutation == "missing":
                item["byCarrier"].pop()
            elif mutation == "bool_count":
                item["byCarrier"][0]["newTransferredCount"] = True
            elif mutation == "unhashable":
                item["byCarrier"][0]["carrierName"] = []
            else:
                item["byCarrier"][0]["newTransferredCount"] = 31
                item["newTransferredCount"] = 34
            values.append(item)
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(CHAT.confirmed_task_d_retention_summary(value))
                self.assertIsNone(REPORT.confirmed_task_d_retention_summary(value))

    def test_zero_new_with_nonzero_population_remains_exact(self):
        value = summary(0, 0)
        self.assertEqual(value, CHAT.confirmed_task_d_retention_summary(value))
        self.assertEqual(value, REPORT.confirmed_task_d_retention_summary(value))
        text = CHAT.render_summary({"status": "success", "taskD": task(value)})
        self.assertIn("本轮新转入保留卡区 0 个；执行后保留区共 100 个", text)
        self.assertIn("保留申请来源去重号码 900", text)
        self.assertNotIn("保留区共 900", text)
        self.assertNotIn("新转入保留卡区 8 个", text)


class RetentionChatTests(unittest.TestCase):
    def test_completed_d_adds_two_core_counts_without_extra_section_or_link(self):
        result = {"status": "success", "taskD": task(), "sourceWorkbook": "/offline/source.xlsx",
                  "finalWorkbook": "/offline/final.xlsx", "report": "/offline/report.md"}
        text = CHAT.render_summary(result)
        self.assertIn("本轮新转入保留卡区 5 个；执行后保留区共 100 个", text)
        self.assertEqual(HEADINGS, [line for line in text.splitlines() if line.startswith("## ")])
        self.assertEqual(3, text.count("](<"))
        self.assertNotIn("population.json", text)

    def test_failure_after_completed_d_still_reports_confirmed_d_numbers(self):
        text = CHAT.render_summary({"status": "failed", "taskD": task(), "error": "Task E failed"})
        self.assertIn("任务D已完成", text)
        self.assertIn("本轮新转入保留卡区 5 个；执行后保留区共 100 个", text)
        self.assertIn("Task E failed", text)
        self.assertEqual(HEADINGS, [line for line in text.splitlines() if line.startswith("## ")])

    def test_failed_or_unverified_d_never_claims_completed_or_zero(self):
        for status, retention in (("failed", summary()), ("passed", {}),
                                   ("passed", {**summary(), "status": "failed"})):
            with self.subTest(status=status, retention=retention):
                text = CHAT.render_summary({"status": "failed", "taskD": task(retention, status)})
                self.assertIn("均未知", text)
                self.assertNotIn("任务D已完成", text)
                self.assertNotIn("保留区共 100", text)
                self.assertNotIn("保留区共 0", text)

    def test_skipped_d_or_a_only_never_exposes_previous_population(self):
        for extra in ({"taskD": task(status="skipped_not_authorized")},
                      {"taskAOnly": True, "taskSelection": "A", "taskD": task()}):
            for status in ("success", "failed"):
                with self.subTest(extra=extra, status=status):
                    text = CHAT.render_summary({"status": status, **extra})
                    self.assertNotIn("执行后保留区共", text)
                    self.assertNotIn("任务D已完成", text)


class RetentionMarkdownTests(unittest.TestCase):
    def config(self):
        return {"taskDExecuted": True, "taskDCurrentUserAuthorized": True,
                "taskDRetentionSummary": summary(),
                "taskDRetentionSummaryReceipt": "/offline/summary.json",
                "taskDRetentionPopulationReceipt": "/offline/population.json"}

    def test_c_t_total_time_and_evidence_are_shown(self):
        text = "\n".join(REPORT.task_d_retention_lines(self.config()))
        self.assertIn("| B14-C快下保留卡 | 2 | 30 |", text)
        self.assertIn("| B14-T快下保留卡 | 3 | 70 |", text)
        self.assertIn("| 合计 | 5 | 100 |", text)
        self.assertIn("2026-09-07T07:00:00+08:00", text)
        self.assertIn("后台全量分页", text)
        self.assertIn("不是卡保留申请记录（cardRetain）数量", text)
        self.assertIn("均不计新增", text)
        self.assertIn("[任务D保留区统计回执](/offline/summary.json)", text)
        self.assertIn("[任务D保留区后台全量分页回执](/offline/population.json)", text)
        self.assertIn("a" * 64, text)

    def test_unknown_skipped_and_not_executed_do_not_render_fake_zero_table(self):
        for mutation in ({"taskDRetentionSummary": {}}, {"taskDExecuted": False},
                         {"taskDStatus": "skipped_not_authorized"}):
            with self.subTest(mutation=mutation):
                text = "\n".join(REPORT.task_d_retention_lines({**self.config(), **mutation}))
                self.assertNotIn("| 合计 |", text)
                self.assertNotIn("保留区共 0", text)
                self.assertNotIn("summary.json", text)
        text = "\n".join(REPORT.task_d_retention_lines(self.config(), task_a_only=True))
        self.assertIn("未查询", text)
        self.assertNotIn("100", text)

    def test_full_report_inserts_retention_table_inside_existing_d_section(self):
        with tempfile.TemporaryDirectory(prefix="opensms-retention-report-test-") as directory:
            root = Path(directory)

            def write(name, value):
                path = root / name
                path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                return str(path)

            config = {**self.config(), "runId": "offline", "groups": [], "runTime": "offline",
                      "taskId": "offline", "taskDuration": "1s", "exportWorkflow": "offline fixture",
                      "taskDRetentionMatchedCardCount": 254, "taskDRetentionActionableCardCount": 252,
                      "taskDFilteredUnmatchedCount": 144, "taskDFilteredUnmatchedCards": ["4160000001"],
                      "taskDNeverProcessSourceExcludedCount": 2, "taskDChatrCandidateCount": 70,
                      "taskDKoodaCandidateCount": 182,
                      "googleAssociationEnabled": False, "googleDisabledReason": "user_disabled",
                      "sourceSha256": "b" * 64, "sourceArchive": str(root / "source.xlsx"),
                      "cardResult": str(root / "final.xlsx"), "offlineRecoveryEnabled": False,
                      "analysis": write("analysis.json", {"overall": {"send": 1, "reply": 0, "rate_pct": 0},
                          "latestDate": "2026-09-07", "rows": 1, "currentZero": 1, "newSincePrev": 0, "recoveredSincePrev": 0}),
                      "balance": write("balance.json", {}),
                      "cardReceipt": write("card.json", {"result": "成功", "checks": {"3日4日回复率明细": True},
                          "process": {"main_rows": 1, "matched_rows": 0, "unmatched_slot_count": 0,
                                      "activated_unlisted_rows": 0, "activated_unlisted_machine_count": 0,
                                      "over_five_unlisted_machine_count": 0},
                          "enrichment": {"decreasing_rate_counts": {}}, "verification": {"formula_error_matches": 0}})}
            config_path = write("config.json", config)
            output = root / "report.md"
            with patch.object(sys, "argv", [str(SCRIPTS / "opensms_finalize_patrol_report.py"), config_path, str(output)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                REPORT.main()
            text = output.read_text(encoding="utf-8")
            d_section = text.split("### 任务D：换号区下卡通知", 1)[1].split("### 任务E：", 1)[0]
            self.assertIn("### 任务E：过期卡发送下卡通知", text)
            self.assertIn("| 合计 | 5 | 100 |", d_section)
            self.assertIn("实际命中（含保护）：254", d_section)
            self.assertIn("筛选未命中跳过：144；不计入候选、实际写入或通知成功", d_section)
            self.assertNotIn("4160000001", d_section)
            self.assertIn("卡保留筛选未命中跳过：144", d_section)
            self.assertIn("支持的OPR筛选未返回，未提交；无法据此判定系统无号", d_section)
            self.assertEqual(HEADINGS, [line for line in text.splitlines() if line.startswith("## ")])
            self.assertEqual(3, sum(line.startswith("[打开") for line in text.splitlines()))


if __name__ == "__main__":
    unittest.main()
