"""Offline-only recovery receipt and no-whole-command-replay regression."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runner_recovery_reporting", ROOT / "scripts/opensms_patrol_runner.py")
runner_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner_module
spec.loader.exec_module(runner_module)


class RecoveryReportingTests(unittest.TestCase):
    def test_success_and_failure_persist_safe_events_without_replaying_command(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as folder:
                runner = object.__new__(runner_module.PatrolRunner)
                runner.run_dir = runner.receipts = Path(folder)
                runner.run_id = "offline"
                runner.state = {}
                runner.child_environment = lambda env: {}
                stderr = json.dumps({"readRequest": {"attempt": 2, "success": exit_code == 0,
                    "apiPath": "/carrier/config/listNotDeleted", "cookie": "secret-do-not-save"}})
                completed = SimpleNamespace(returncode=exit_code, stdout='{"success":true}', stderr=stderr)
                with patch.object(runner_module.subprocess, "run", return_value=completed) as invoke:
                    if exit_code:
                        with self.assertRaises(runner_module.PatrolError):
                            runner.command("fixture", ["never-executed"])
                    else:
                        runner.command("fixture", ["never-executed"])
                    self.assertEqual(invoke.call_count, 1)
                self.assertEqual(runner.state["automaticRecovery"][0]["events"][0]["attempt"], 2)
                self.assertNotIn("secret-do-not-save", json.dumps(runner.state))
                report = runner.automatic_recovery_report()
                self.assertTrue(report.is_file())
                receipt = json.loads((Path(folder) / "opensms_automatic_recovery_offline.json").read_text())
                self.assertFalse(receipt["businessReplayEnabled"])
                self.assertNotIn("secret-do-not-save", json.dumps(receipt))

    def test_no_events_is_not_reported_as_pipeline_success(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = object.__new__(runner_module.PatrolRunner)
            runner.receipts = Path(folder)
            runner.run_id, runner.state = "offline", {}
            self.assertIn("不代表所有阶段已完成", runner.automatic_recovery_report().read_text())


if __name__ == "__main__":
    unittest.main()
