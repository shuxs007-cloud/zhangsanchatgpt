"""Offline runner integration tests; no browser, credentials, or live tasks."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("patrol_deadline_hook", ROOT / "scripts/opensms_patrol_runner.py")
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


class TaskFDeadlineHookTests(unittest.TestCase):
    def setUp(self):
        for name in ("run", "Popen"):
            guard = patch.object(RUNNER.subprocess, name, side_effect=AssertionError("live command forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def runner(self, directory):
        database = patch.multiple(RUNNER, TASK_F_DB=Path(directory) / "test.sqlite3",
            TASK_F_CATALOG_SOURCE=Path(directory) / "test_catalog.json",
            TASK_F_LATEST_LIBRARY=Path(directory) / "test_library.json")
        database.start()
        self.addCleanup(database.stop)
        runner = object.__new__(RUNNER.PatrolRunner)
        runner.run_id = "20260906T235900+0800"
        runner.delivery = Path(directory)
        runner.receipts = runner.delivery / "receipts"
        runner.receipts.mkdir()
        runner.state_path = runner.receipts / "state.json"
        runner.state = {"stages": {}, "timings": []}
        runner.authorization = {"taskF": True}  # Explicit standalone local-export checkpoint.
        runner.args = argparse.Namespace(cdp=None, task_f_workers=8)
        runner._save_state = Mock()
        return runner

    def exporter_command(self, runner, **overrides):
        def command(label, argv, **kwargs):
            self.assertEqual(label, "task_f_export_deadline_template")
            self.assertEqual(Path(argv[1]).name, "opensms_export_task_f_deadlines.py")
            self.assertNotIn("env", kwargs)
            workbook = Path(argv[argv.index("--output") + 1])
            receipt = Path(argv[argv.index("--receipt") + 1])
            workbook.write_bytes(b"test-artifact-owned-by-mocked-exporter")
            payload = {
                "status": "success", "output": str(workbook), "receipt": str(receipt),
                "recordCount": 3, "activationCount": 2, "missingDateCount": 1,
                "elapsedSeconds": 0.1, "verificationPassed": True, **overrides,
            }
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            return RUNNER.CommandResult(payload, "", "", 0.1)
        return Mock(side_effect=command)

    def completed_checkpoint(self, runner):
        prior = {"status": "passed", "completedAt": "2026-09-06T23:59:00+08:00"}
        for key in ("inventory", "pending", "lookupReceipt", "importReceipt", "databaseSnapshot", "iccidLibrary", "latestIccidLibrary"):
            path = runner.receipts / key
            path.write_text("{}", encoding="utf-8")
            prior[key] = str(path)
        runner.state["stages"]["taskF"] = prior
        return prior

    def test_export_reads_snapshot_and_returns_verified_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            snapshot = runner.receipts / "snapshot.sqlite"
            snapshot.write_bytes(b"mock snapshot")
            runner.command = self.exporter_command(runner)
            result = runner.export_task_f_deadlines(snapshot)
            self.assertEqual(result["deadlineRecordCount"], 3)
            self.assertEqual(result["deadlineActivationCount"], 2)
            self.assertEqual(result["deadlineMissingDateCount"], 1)
            argv = runner.command.call_args.args[1]
            self.assertEqual(argv[argv.index("--db") + 1], snapshot)
            self.assertTrue(Path(result["deadlineWorkbook"]).is_file())
            self.assertTrue(Path(result["deadlineExportReceipt"]).is_file())

    def test_old_successful_checkpoint_only_backfills_local_export(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            prior = self.completed_checkpoint(runner)
            runner.command = self.exporter_command(runner)
            result = runner.run_task_f()
            self.assertIs(result, prior)
            self.assertEqual(result["completedAt"], "2026-09-06T23:59:00+08:00")
            self.assertEqual(runner.command.call_count, 1)
            runner._save_state.assert_called_once()
            runner.command.reset_mock()
            runner._save_state.reset_mock()
            self.assertIs(runner.run_task_f(), result)
            runner.command.assert_not_called()
            runner._save_state.assert_not_called()

    def test_failed_verification_or_count_mismatch_rejects_export(self):
        for overrides in ({"verificationPassed": False}, {"recordCount": 99}, {"recordCount": -1}):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                runner = self.runner(directory)
                snapshot = runner.receipts / "snapshot.sqlite"
                snapshot.write_bytes(b"mock snapshot")
                runner.command = self.exporter_command(runner, **overrides)
                with self.assertRaises(RUNNER.PatrolError):
                    runner.export_task_f_deadlines(snapshot)

    def test_missing_snapshot_does_not_invoke_exporter(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            runner.command = Mock()
            with self.assertRaises(RUNNER.PatrolError):
                runner.export_task_f_deadlines(runner.receipts / "absent.sqlite")
            runner.command.assert_not_called()

    def test_task_f_only_report_and_compact_json_keep_template_and_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            snapshot = runner.receipts / "snapshot.sqlite"
            snapshot.write_bytes(b"mock snapshot")
            runner.command = self.exporter_command(runner)
            task_f = {"status": "passed", **runner.export_task_f_deadlines(snapshot)}
            result = runner.finish_task_f_only(task_f)
            compact = RUNNER.compact_launcher_result(result)
            self.assertEqual(compact["finalWorkbook"], task_f["deadlineWorkbook"])
            for key in ("deadlineWorkbook", "deadlineExportReceipt", "deadlineRecordCount", "deadlineActivationCount", "deadlineMissingDateCount"):
                self.assertEqual(compact["taskF"][key], task_f[key])
            report = Path(result["report"]).read_text(encoding="utf-8")
            self.assertIn(task_f["deadlineWorkbook"], report)
            self.assertIn(task_f["deadlineExportReceipt"], report)
            self.assertIn("3/2/1", report)


if __name__ == "__main__":
    unittest.main()
