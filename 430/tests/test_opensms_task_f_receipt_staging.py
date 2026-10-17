"""Offline receipt publication tests; no browser, network, or business database."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "patrol_receipt_staging_tests", ROOT / "scripts/opensms_patrol_runner.py"
)
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


class TaskFReceiptStagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runner = object.__new__(RUNNER.PatrolRunner)
        self.runner.run_id = "20260907T040000+0800"
        self.runner.run_dir = self.root / "local" / self.runner.run_id
        self.runner.run_dir.mkdir(parents=True)
        self.runner.receipts = self.root / "external_delivery" / "receipts"
        self.runner.receipts.mkdir(parents=True)
        self.runner.args = argparse.Namespace(task_f_workers=8)
        self.runner.state = {}
        self.runner._save_state = Mock()
        self.pending = self.runner.receipts / "pending_batch_001.json"
        self.progress = self.runner.receipts / "progress_batch_001.json"
        self.archive = self.runner.receipts / "lookup_batch_001.json"
        self.local = self.runner.run_dir / "task_f_receipts" / self.archive.name
        self.environment = {"OPENSMS_TASK_F_POLICY_CATALOG_SYNC": "1"}
        self.payload = {
            "status": "passed", "inputCount": 1,
            "results": [{"queryIccid": "891223020000000001", "status": "found"}],
        }
        self.pending.write_text('{"rows": []}\n', encoding="utf-8")
        self.runner.command = Mock(side_effect=self.publish_local)

    def publish_local(self, label, argv, **kwargs):
        """Model the helper's exclusive hard-link publication on local storage."""
        self.assertEqual(label, "task_f_grabandgo_lookup_batch_001")
        self.assertEqual(Path(argv[1]).name, "grabandgo_task_f_lookup.mjs")
        self.assertEqual(argv[2], self.pending)
        self.assertEqual(argv[3], self.local)
        self.assertEqual(argv[4], "8")
        self.assertEqual(argv[5], self.progress)
        self.assertEqual(kwargs, {"env": self.environment, "allow_codes": (0, 4)})
        data = (json.dumps(self.payload, ensure_ascii=False, indent=2) + "\n").encode()
        temporary = self.local.with_suffix(".tmp")
        temporary.write_bytes(data)
        try:
            os.link(temporary, self.local)
        finally:
            temporary.unlink()
        return RUNNER.CommandResult(self.payload, json.dumps(self.payload), "", 0.01)

    def run_batch(self):
        return self.runner.task_f_lookup_batch(
            1, self.pending, self.archive, self.progress, self.environment
        )

    def assert_not_committed(self):
        self.assertEqual(self.runner.state.get("taskFReceiptStaging", []), [])
        self.runner._save_state.assert_not_called()

    def test_normal_exclusive_local_publication_and_verified_archive(self):
        result = self.run_batch()
        self.assertEqual(result, self.payload)
        self.assertTrue(self.local.is_file())
        self.assertEqual(self.local.read_bytes(), self.archive.read_bytes())
        self.assertNotEqual(self.local.stat().st_ino, self.archive.stat().st_ino)
        digest = hashlib.sha256(self.local.read_bytes()).hexdigest()
        self.assertEqual(self.runner.state["taskFReceiptStaging"], [{
            "batchIndex": 1, "localReceipt": str(self.local),
            "archiveReceipt": str(self.archive), "sha256": digest,
            "archiveReadbackPassed": True,
        }])
        self.runner.command.assert_called_once()
        self.runner._save_state.assert_called_once_with()

    def test_existing_local_receipt_rejects_before_command(self):
        self.local.parent.mkdir(parents=True)
        self.local.write_bytes(b"existing local receipt")
        with self.assertRaises(RUNNER.PatrolError):
            self.run_batch()
        self.assertEqual(self.local.read_bytes(), b"existing local receipt")
        self.assertFalse(self.archive.exists())
        self.runner.command.assert_not_called()
        self.assert_not_committed()

    def test_existing_archive_rejects_before_command(self):
        self.archive.write_bytes(b"existing archive receipt")
        with self.assertRaises(RUNNER.PatrolError):
            self.run_batch()
        self.assertEqual(self.archive.read_bytes(), b"existing archive receipt")
        self.assertFalse(self.local.exists())
        self.runner.command.assert_not_called()
        self.assert_not_committed()

    def test_dangling_local_symlink_is_not_treated_as_absent(self):
        self.local.parent.mkdir(parents=True)
        self.local.symlink_to(self.root / "missing-local-target")
        with self.assertRaises(RUNNER.PatrolError):
            self.run_batch()
        self.assertTrue(self.local.is_symlink())
        self.runner.command.assert_not_called()
        self.assert_not_committed()

    def test_dangling_archive_symlink_is_not_treated_as_absent(self):
        self.archive.symlink_to(self.root / "missing-archive-target")
        with self.assertRaises(RUNNER.PatrolError):
            self.run_batch()
        self.assertTrue(self.archive.is_symlink())
        self.runner.command.assert_not_called()
        self.assert_not_committed()

    def test_archive_created_during_query_is_never_overwritten(self):
        def publish_with_competing_archive(*args, **kwargs):
            result = self.publish_local(*args, **kwargs)
            self.archive.write_bytes(b"competing existing receipt")
            return result
        self.runner.command.side_effect = publish_with_competing_archive
        with self.assertRaises(FileExistsError):
            self.run_batch()
        self.assertEqual(self.archive.read_bytes(), b"competing existing receipt")
        self.assertEqual(json.loads(self.local.read_text()), self.payload)
        self.assert_not_committed()

    def test_copy_failure_preserves_local_and_never_marks_success(self):
        def interrupted_copy(source, target):
            target.write(source.read(9))
            raise OSError("simulated delivery disconnect")
        with patch.object(RUNNER.shutil, "copyfileobj", side_effect=interrupted_copy):
            with self.assertRaisesRegex(OSError, "delivery disconnect"):
                self.run_batch()
        self.assertEqual(json.loads(self.local.read_text()), self.payload)
        self.assertNotEqual(self.archive.read_bytes(), self.local.read_bytes())
        self.assert_not_committed()

    def test_fsync_failure_preserves_local_and_never_marks_success(self):
        with patch.object(RUNNER.os, "fsync", side_effect=OSError("simulated fsync failure")):
            with self.assertRaisesRegex(OSError, "fsync failure"):
                self.run_batch()
        self.assertEqual(json.loads(self.local.read_text()), self.payload)
        self.assert_not_committed()

    def test_archive_digest_mismatch_is_fatal(self):
        actual_sha256 = RUNNER.sha256
        def mismatching_hash(path):
            return "0" * 64 if Path(path) == self.archive else actual_sha256(path)
        with patch.object(RUNNER, "sha256", side_effect=mismatching_hash):
            with self.assertRaises(RUNNER.PatrolError):
                self.run_batch()
        self.assertEqual(json.loads(self.local.read_text()), self.payload)
        self.assert_not_committed()

    def test_archive_json_mismatch_is_fatal_even_with_matching_digest(self):
        actual_read_json = RUNNER.read_json
        def mismatching_readback(path):
            return {"status": "different"} if Path(path) == self.archive else actual_read_json(path)
        with patch.object(RUNNER, "read_json", side_effect=mismatching_readback):
            with self.assertRaises(RUNNER.PatrolError):
                self.run_batch()
        self.assertEqual(self.local.read_bytes(), self.archive.read_bytes())
        self.assert_not_committed()

    def test_archive_json_read_exception_preserves_local(self):
        actual_read_json = RUNNER.read_json
        def unreadable_archive(path):
            if Path(path) == self.archive:
                raise json.JSONDecodeError("simulated invalid delivery JSON", "{", 1)
            return actual_read_json(path)
        with patch.object(RUNNER, "read_json", side_effect=unreadable_archive):
            with self.assertRaises(json.JSONDecodeError):
                self.run_batch()
        self.assertEqual(json.loads(self.local.read_text()), self.payload)
        self.assert_not_committed()

    def test_invalid_local_json_is_never_published(self):
        def invalid_local(*args, **kwargs):
            self.local.write_text("{invalid", encoding="utf-8")
            return RUNNER.CommandResult({}, "{}", "", 0.01)
        self.runner.command.side_effect = invalid_local
        with self.assertRaises(json.JSONDecodeError):
            self.run_batch()
        self.assertEqual(self.local.read_text(), "{invalid")
        self.assertFalse(self.archive.exists())
        self.assert_not_committed()

    def test_command_failure_never_publishes_even_when_local_receipt_exists(self):
        def failing_after_local(*args, **kwargs):
            self.publish_local(*args, **kwargs)
            raise RUNNER.PatrolError("simulated command failure")
        self.runner.command.side_effect = failing_after_local
        with self.assertRaisesRegex(RUNNER.PatrolError, "command failure"):
            self.run_batch()
        self.assertEqual(json.loads(self.local.read_text()), self.payload)
        self.assertFalse(self.archive.exists())
        self.assert_not_committed()

    def test_missing_local_after_command_never_publishes(self):
        self.runner.command.side_effect = None
        self.runner.command.return_value = RUNNER.CommandResult({}, "{}", "", 0.01)
        with self.assertRaises(FileNotFoundError):
            self.run_batch()
        self.assertFalse(self.archive.exists())
        self.assert_not_committed()

    def test_login_required_allow_code_four_receipt_reaches_caller_unchanged(self):
        self.payload = {"status": "login_required", "loginStatus": "unauthenticated", "results": []}
        result = self.run_batch()
        self.assertEqual(result, self.payload)
        self.assertEqual(json.loads(self.archive.read_text()), self.payload)
        self.assertEqual(self.runner.command.call_args.kwargs["allow_codes"], (0, 4))
        self.assertEqual(self.runner.command.call_count, 1)
        # The helper only archives evidence. run_task_f retains the login gate
        # before its separate database-import command.
        self.assertTrue(self.runner.state["taskFReceiptStaging"][0]["archiveReadbackPassed"])


if __name__ == "__main__":
    unittest.main()
