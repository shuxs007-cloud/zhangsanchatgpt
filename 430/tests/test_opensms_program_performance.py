import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


MANIFEST = module("manifest_performance", "opensms_make_delivery_manifest.py")
CHAT = module("chat_performance", "opensms_chat_summary.py")
RUNNER = module("runner_performance", "opensms_patrol_runner.py")


class ProgramPerformanceTests(unittest.TestCase):
    def test_main_delivery_timing_never_waits_for_or_claims_background_f_completion(self):
        result = {'status': 'success', 'performance': {'pipelineWallSecondsBeforeFinalManifest': 152.594},
                  'taskF': {'background': True, 'status': 'running', 'statusIsLaunchSnapshot': True}}
        text = CHAT.render_summary(result)
        self.assertIn('152.6 秒', text)
        self.assertIn('先交付，不等待F', text)
        self.assertIn('完成和入库数量尚未确认', text)
        self.assertEqual(text.count('## '), 7)
        result['status'] = 'failed'
        self.assertNotIn('最终文件已就绪', CHAT.render_summary(result))
        result['status'] = 'success'
        for invalid in (None, True, '152', float('nan'), float('inf'), -1):
            result['performance']['pipelineWallSecondsBeforeFinalManifest'] = invalid
            self.assertNotIn('主报表程序耗时约', CHAT.render_summary(result))

    def test_failure_report_records_unknown_d_and_never_calls_live_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            receipts = folder / "receipts"
            receipts.mkdir()
            state_path = receipts / "state.json"
            state_path.write_text(json.dumps({
                "runId": "test", "status": "failed", "delivery": str(folder), "error": "HTTP504",
                "authorization": {"taskE": True},
                "stages": {"taskB": {"executed": True}, "refreshAfterTaskB": {
                    "finalWorkbook": "/tmp/partial.xlsx", "verifyReceipt": "/tmp/verify.json", "readbackReceipt": "/tmp/backend.json"}},
                "writeGroups": {"task_b_x": {"candidateCount": 3, "backendValid": 3, "actualWrite": 3, "result": "passed"}}
            }))
            (receipts / "opensms_task_d_swap_zero_test_progress.json").write_text(json.dumps({
                "status": "failed", "error": "HTTP504", "batches": [{"inputCount": 20}],
                "pendingBatch": {"batchNumber": 2, "cardNos": ["111", "222"], "expireDate": "2026-09-05"}
            }))
            with patch.dict(sys.modules, {"opensms_chat_summary": CHAT, "opensms_make_delivery_manifest": MANIFEST}), patch.object(RUNNER.PatrolRunner, "__init__", side_effect=AssertionError("must not start")), patch.object(RUNNER, "resolve_bitbrowser_430_cdp", side_effect=AssertionError("must not connect")):
                result = RUNNER.refresh_failure_report(state_path)
            self.assertFalse(result["businessTasksStarted"])
            self.assertEqual(result["taskDConfirmedSubmittedCount"], 20)
            self.assertEqual(result["taskDUncertainSubmittedCount"], 2)
            self.assertTrue(result["taskDAutomaticRetryBlocked"])
            self.assertIsNone(result["finalWorkbook"])
            self.assertEqual(result["latestVerifiedWorkbook"], "/tmp/partial.xlsx")
            self.assertEqual(result["taskB"]["actualWriteCount"], 3)
            output = Path(result["chatSummaryPath"]).read_text()
            self.assertIn("非全任务最终表", output)
            self.assertIn("2 个号码提交结果不确定", output)
            self.assertEqual(json.loads(state_path.read_text())["status"], "failed")
            self.assertEqual(result["manifestVerification"]["verification_failures"], [])

    def test_failure_report_rejects_active_state_before_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"status": "running"}))
            with self.assertRaisesRegex(RUNNER.PatrolError, "已停止"):
                RUNNER.refresh_failure_report(state)

    def test_parallel_manifest_equals_serial_and_rereads_every_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for index in range(9):
                (folder / f"item{index}.txt").write_text("完整源值" * (index + 1))
            (folder / "._ignore").write_text("ignored")
            serial = MANIFEST.build_manifest(folder, 1)
            content = (folder / "SHA256SUMS.txt").read_bytes()
            with patch.object(MANIFEST, "sha256_file", wraps=MANIFEST.sha256_file) as hasher:
                parallel = MANIFEST.build_manifest(folder, 4)
            self.assertEqual(hasher.call_count, 18)
            self.assertEqual(content, (folder / "SHA256SUMS.txt").read_bytes())
            self.assertEqual(serial["file_count"], parallel["file_count"])
            self.assertEqual(parallel["verification_failures"], [])

    def test_manifest_detects_change_between_hash_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            target = folder / "item.txt"
            target.write_text("before")
            original = MANIFEST.sha256_file
            calls = 0

            def hash_with_changed_second_read(file):
                nonlocal calls
                calls += 1
                if calls == 2:
                    file.write_text("after!")
                return original(file)

            with patch.object(MANIFEST, "sha256_file", side_effect=hash_with_changed_second_read):
                result = MANIFEST.build_manifest(folder, 4)
            self.assertEqual(result["verification_failures"], ["item.txt"])

    def test_failure_summary_never_invents_files_or_success_counts(self):
        output = CHAT.render_summary({"status": "failed", "runId": "test", "error": "stopped", "report": "/tmp/failure.md"})
        self.assertEqual(output.count("## "), 7)
        self.assertIn("源文件：本轮未生成", output)
        self.assertNotIn("实际写入 0", output)
        self.assertEqual(output.count("：未知。"), 12)

    def test_summary_preserves_e_alert_and_three_main_links(self):
        output = CHAT.render_summary({"status": "success", "sourceWorkbook": "/tmp/source.xlsx", "finalWorkbook": "/tmp/final.xlsx", "report": "/tmp/report.md", "taskE": {"status": "passed", "retentionDeadlineAlertRequired": True, "retentionDeadlineMatchPhoneCount": 7}})
        self.assertEqual(output.count("](<"), 3)
        self.assertIn("7 个号的卡保留下卡通知时间与文档截止日相同", output)

    def test_status_only_reads_local_state_and_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"runId": "test", "status": "running", "stages": {"acquire": {}}, "writeGroups": {"task_b_x": {"actualWrite": 3}}}))
            with patch.object(RUNNER.PatrolRunner, "__init__", side_effect=AssertionError("must not start runner")), patch.object(RUNNER, "resolve_bitbrowser_430_cdp", side_effect=AssertionError("must not resolve browser")):
                result = RUNNER.read_status(state)
            self.assertTrue(result["readOnly"])
            self.assertEqual(result["actualCarrierWrites"], 3)
            self.assertEqual(result["completedStages"], ["acquire"])


if __name__ == "__main__":
    unittest.main()
