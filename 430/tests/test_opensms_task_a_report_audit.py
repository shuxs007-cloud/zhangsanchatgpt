"""Offline A-only reporting and independent skip-gate regressions."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def import_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


AUDIT = import_script("opensms_parallel_audit")
REPORT = import_script("opensms_finalize_patrol_report")
CHAT = import_script("opensms_chat_summary")
HEADINGS = ["## 本轮概况", "## 0回复与回复率", "## 运营商调整", "## 下架恢复",
            "## 余量与提醒", "## 文件与回执", "## 异常"]


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def scope_fixture(root):
    config = {
        "runId": "offline-a-only", "taskAOnly": True, "taskSelection": "A",
        "taskFStageStatus": "skipped", "taskFSkippedReason": "task_a_only",
        "taskFStatus": "仅任务A模式，任务F完整跳过", "groups": [],
        "commandTimings": [{"command": "system_kooda_inventory", "durationSeconds": 0.1, "exitCode": 0},
                           {"command": "main_candidates", "durationSeconds": 0.1, "exitCode": 0}],
        "taskFDocumentReceipt": str(root / "ordinary_card_receipt.json"),
        "taskFDocumentDateSource": "system", "taskFDocumentMissingActivationRedRows": 7,
        "taskFDocumentFullRedRows": 7, "taskFDocumentRedReviewPassed": True,
        "taskFMissingActivationFillRgb": "FFC7CE",
    }
    config.update({f"task{task}Executed": False for task in "BCDEF"})
    config.update({f"task{task}CurrentUserAuthorized": False for task in "DEF"})
    config["taskFAutomaticCatalogSync"] = False
    auth = {key: value for key, value in config.items()
            if key.endswith("Executed") or key.endswith("CurrentUserAuthorized")
            or key in {"taskAOnly", "taskSelection", "taskFStageStatus", "taskFSkippedReason", "taskFAutomaticCatalogSync"}}
    auth["passed"] = True
    config["safetyReceipt"] = write_json(root / "safety.json", {"runId": config["runId"], "taskAuthorization": auth})
    return config


class TaskAOnlyAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = scope_fixture(self.root)
        self.resolver = AUDIT.PathResolver(self.root / "config.json", None)

    def audit(self):
        with patch.object(AUDIT.sqlite3, "connect", side_effect=AssertionError("F database access")), \
             patch.object(AUDIT, "load_workbook", side_effect=AssertionError("F workbook access")), \
             patch.object(self.resolver, "glob", side_effect=AssertionError("F directory scan")):
            return AUDIT.check_task_f(self.config, self.resolver)

    def test_a_only_is_verified_skipped_and_never_reads_f_artifacts(self):
        original = AUDIT.read_json
        with patch.object(AUDIT, "read_json", wraps=original) as read:
            result = self.audit()
        self.assertEqual(AUDIT.SKIP, result.status)
        self.assertTrue(result.details["skip_contract_verified"])
        self.assertFalse(result.details["f_inventory_and_gap_checked"])
        self.assertFalse(result.details["f_artifacts_read"])
        self.assertEqual([(self.root / "safety.json").resolve()], [call.args[0] for call in read.call_args_list])

    def test_partial_or_inconsistent_mode_does_not_exempt_f(self):
        for key, value in (("taskAOnly", False), ("taskSelection", "default"),
                           ("taskFSkippedReason", None), ("taskFStageStatus", "passed")):
            with self.subTest(key=key), patch.dict(self.config, {key: value}):
                self.assertEqual(AUDIT.FAIL, self.audit().status)

    def test_authorization_or_execution_for_any_disabled_task_fails(self):
        keys = [f"task{task}Executed" for task in "BCDEF"] + [
            "taskFCurrentUserAuthorized", "taskFAutomaticCatalogSync", "taskECurrentUserAuthorized"]
        for key in keys:
            with self.subTest(key=key), patch.dict(self.config, {key: True}):
                self.assertEqual(AUDIT.FAIL, self.audit().status)

    def test_nonempty_f_business_paths_and_counts_fail_without_opening(self):
        values = {"taskFDatabase": "/forbidden/production.sqlite", "taskFCatalogSource": "/forbidden/catalog.json",
                  "taskFImportReceipt": "/forbidden/import.json", "taskFIccidLibrary": "/forbidden/library.json",
                  "taskFDeadlineWorkbook": "/forbidden/template.xlsx", "taskFProgress": "/forbidden/progress.json",
                  "taskFPendingCount": 1, "taskFFoundImportedCount": 1, "taskFOnlineLookupTriggered": True}
        for key, value in values.items():
            with self.subTest(key=key), patch.dict(self.config, {key: value}):
                self.assertEqual(AUDIT.FAIL, self.audit().status)

    def test_f_execution_receipt_in_extra_receipts_is_rejected_without_reading(self):
        self.config["extraReceipts"] = [{"label": "任务F数据库导入回执", "path": "/forbidden/import.json"}]
        result = self.audit()
        self.assertEqual(AUDIT.FAIL, result.status)
        self.assertIn("未读取", result.message)

    def test_real_f_commands_are_rejected_even_when_counts_are_zero(self):
        for command in ("task_f_catalog_import", "task_f_lookup_batch_1", "task_f_deadline_export", "task_e_manifest"):
            with self.subTest(command=command), patch.dict(self.config, {"commandTimings": [{"command": command}]}):
                self.assertEqual(AUDIT.FAIL, self.audit().status)

    def test_missing_command_evidence_or_wrong_run_receipt_fails(self):
        with patch.dict(self.config, {"commandTimings": []}):
            self.assertEqual(AUDIT.FAIL, self.audit().status)
        payload = json.loads(Path(self.config["safetyReceipt"]).read_text())
        payload["runId"] = "another-run"
        write_json(Path(self.config["safetyReceipt"]), payload)
        self.assertEqual(AUDIT.FAIL, self.audit().status)

    def test_ordinary_run_still_requires_default_f_execution(self):
        result = AUDIT.check_task_f({"taskSelection": "default", "taskAOnly": False,
                                     "taskFExecuted": False}, self.resolver)
        self.assertEqual(AUDIT.FAIL, result.status)
        self.assertIn("默认目录同步未执行", result.message)

    def test_a_only_delivery_rejects_f_manifest_entry_without_reading_it(self):
        (self.root / "SHA256SUMS.txt").write_text("0" * 64 + "  receipts/task_f_snapshot.sqlite\n")
        (self.root / "FILES.tsv").write_text("path\n")
        with patch.object(AUDIT, "sha256_file", side_effect=AssertionError("F data read")):
            result = AUDIT.check_delivery(self.root, task_a_only=True)
        self.assertEqual(AUDIT.FAIL, result.status)
        self.assertIn("未访问", next(iter(result.details["checksum_failures"].values())))

    def test_a_only_does_not_bypass_ordinary_a_audits(self):
        dummy = self.root / "ordinary.xlsx"
        dummy.write_bytes(b"offline fixture")
        self.config.update({"sourceSha256": "1" * 64, "googleSha256": "2" * 64})
        tasks = AUDIT.build_tasks(self.config, self.resolver, {"result": dummy}, 1)
        names = {task.name for task in tasks}
        self.assertTrue({"后台导出回执", "服务器日期", "卡槽关联完整复核", "运营商最新状态回写文档",
                         "回复型流转发送窗口数门禁", "A21/A2来源运营商保护", "余量与按日回复率"} <= names)


class TaskAOnlyReportTests(unittest.TestCase):
    def test_task_b_summary_reports_verified_retained_exclusions(self):
        text = CHAT.render_summary({"status": "success", "taskB": {
            "status": "passed", "candidateCount": 2, "backendValidCount": 2,
            "actualWriteCount": 2, "independentVerificationPassed": 1,
            "retainedSourceExcludedCount": 7, "expiredRetainedSourceExcludedCount": 3,
            "tomorrowRetainedSourceExcludedCount": 4,
        }})
        self.assertIn("B14保留卡保护：排除 7 个（已过期 3，明日到期 4）；不修改运营商", text)

    def test_chat_retains_seven_sections_three_links_and_does_not_claim_zero_gap(self):
        result = {"status": "success", "taskAOnly": True, "taskSelection": "A", "runId": "offline",
                  "sourceWorkbook": "/tmp/source.xlsx", "finalWorkbook": "/tmp/result.xlsx", "report": "/tmp/report.md",
                  "taskF": {"status": "skipped", "reason": "task_a_only", "pendingCount": 0}}
        text = CHAT.render_summary(result)
        self.assertEqual(HEADINGS, [line for line in text.splitlines() if line.startswith("## ")])
        for path in ("/tmp/source.xlsx", "/tmp/result.xlsx", "/tmp/report.md"):
            self.assertIn(f"](<{path}>)", text)
        self.assertIn("仅任务A", text)
        self.assertIn("任务F：未执行（task_a_only）", text)
        self.assertIn("未检查本地库或补查缺口", text)
        self.assertNotIn("待补查 0", text)
        self.assertNotIn("ICCID库位置见完整报告", text)
        for group in ("A1", "A12", "A11", "B1", "B11", "B12", "C2", "A3", "B3", "B1V", "B11V", "B12V"):
            self.assertIn(f"- {group}：未知。", text)

    def test_failure_chat_keeps_scope_and_f_skipped_reason(self):
        text = CHAT.render_summary({"status": "failed", "taskAOnly": True, "taskSelection": "A", "error": "offline failure"})
        self.assertIn("任务F：未执行（task_a_only）", text)
        self.assertEqual(HEADINGS, [line for line in text.splitlines() if line.startswith("## ")])

    def test_b3_and_v_groups_chat_alert_thresholds(self):
        for value, suffix in ((9.99, "，低于10%紧急"), (10, "，低于20%提醒"),
                              (19.99, "，低于20%提醒"), (20, "")):
            with self.subTest(value=value):
                text = CHAT.render_summary({"status": "success", "balanceGroups": [
                    {"group": group, "percentRemaining": value} for group in ("B3", "B1V", "B11V", "B12V")]})
                for group in ("B3", "B1V", "B11V", "B12V"):
                    self.assertIn(f"- {group}：{value:.2f}%{suffix}。", text)

    def test_ordinary_chat_keeps_f_metrics(self):
        text = CHAT.render_summary({"status": "success", "taskF": {"status": "passed", "pendingCount": 3}})
        self.assertIn("任务F：passed", text)
        self.assertIn("待补查 3", text)

    def test_full_report_retains_a_metrics_and_system_kooda_without_f_requirements(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = scope_fixture(root)
            config.update({"analysis": write_json(root / "analysis.json", {
                "overall": {"send": 100, "reply": 5, "rate_pct": 5}, "latestDate": "2026-09-07",
                "rows": 7, "currentZero": 2, "newSincePrev": 1, "recoveredSincePrev": 0}),
                "balance": write_json(root / "balance.json", {}),
                "cardReceipt": write_json(root / "card.json", {"result": "成功", "checks": {"3日4日回复率明细": True},
                    "process": {"main_rows": 7, "matched_rows": 7, "unmatched_slot_count": 0,
                                "activated_unlisted_rows": 0, "activated_unlisted_machine_count": 0,
                                "over_five_unlisted_machine_count": 0},
                    "enrichment": {"decreasing_rate_counts": {}}, "verification": {"formula_error_matches": 0}}),
                "offlineRecoveryEnabled": False, "runTime": "offline", "taskId": "offline", "taskDuration": "1s",
                "exportWorkflow": "offline fixture", "sourceSha256": "1" * 64, "googleSha256": "2" * 64,
                "sourceArchive": str(root / "source.xlsx"), "cardResult": str(root / "result.xlsx"),
                "systemKoodaRows": 7, "systemKoodaMatchedRows": 7, "systemKoodaMissingActivationRows": 7,
                "systemKoodaSource": "本轮OpenSMS系统快照"})
            config_path = write_json(root / "config.json", config)
            output = root / "report.md"
            with patch.object(sys, "argv", [str(SCRIPTS / "opensms_finalize_patrol_report.py"), config_path, str(output)]), \
                 contextlib.redirect_stdout(io.StringIO()):
                REPORT.main()
            text = output.read_text(encoding="utf-8")
            self.assertEqual(HEADINGS, [line for line in text.splitlines() if line.startswith("## ")])
            self.assertIn("| 执行范围 | 仅任务A", text)
            self.assertIn("| 任务F | 未执行；仅任务A模式，完整跳过（task_a_only） |", text)
            self.assertIn("系统库存行数/主表匹配行数/缺激活日期行数：7/7/7", text)
            self.assertIn("本轮F库及截止日模板：未生成", text)
            self.assertNotIn("默认自动目录同步未完整完成", text)
            self.assertNotIn("未触发（本轮空激活范围无需补查）", text)
            self.assertNotIn("独立诊断模式", text)
            self.assertEqual(3, sum(line.startswith("[打开") for line in text.splitlines()))
            for group in ("A1", "A12", "A11", "B1", "B11", "B12", "C2", "A3", "B3", "B1V", "B11V", "B12V"):
                self.assertIn(f"| {group} | 未知 | 未知 | 未知 |", text)


if __name__ == "__main__":
    unittest.main()
