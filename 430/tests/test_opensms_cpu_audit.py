"""CPU checks remain independent, bounded and read-only under spawn."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import opensms_parallel_audit as audit


def semantics(results):
    return sorted((item.name, item.category, item.status, item.message, item.details) for item in results)


class CpuAuditTests(unittest.TestCase):
    def test_normal_entry_enables_hybrid_without_changing_worker_limits(self):
        args = audit.build_parser().parse_args([])
        self.assertEqual(args.executor, "hybrid")
        self.assertEqual(audit.resolve_worker_count(args.workers), min(4, os.cpu_count() or 1))
        self.assertEqual(audit.build_parser().parse_args(["--executor", "threads"]).executor, "threads")

    def fixture_tasks(self, directory):
        tasks = []
        for index in range(3):
            path = directory / f"book-{index}.xlsx"
            book = Workbook()
            book.active.append(["电话", "运营商", "ICCID"])
            book.active.append(["001234567890", "B12V", "00123456789012345678"])
            book.save(path)
            book.close()
            if index == 2:
                path.write_bytes(b"not a workbook")
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            args = (f"fixture-{index}", path, expected)
            tasks.append(audit.AuditTask(args[0], "files",
                lambda args=args: audit.check_xlsx(*args), ("check_xlsx", args)))
        return tasks

    def test_spawn_matches_threads_including_failure_and_preserves_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            tasks = self.fixture_tasks(directory)
            before = {path.name: path.read_bytes() for path in directory.iterdir()}
            serial, _, first = audit.execute_audit_tasks(tasks, 1, "threads")
            workers = min(2, os.cpu_count() or 1)
            parallel, meter, scheduling = audit.execute_audit_tasks(tasks, workers, "hybrid")
            self.assertEqual(semantics(serial), semantics(parallel))
            self.assertEqual(len(parallel), 3)
            self.assertTrue(any(item.status == audit.FAIL for item in parallel))
            self.assertLessEqual(meter.maximum, workers)
            self.assertTrue(scheduling["all_joined_before_cross_check"])
            self.assertEqual(scheduling["workbook_writers"], 0)
            self.assertEqual(scheduling["business_writes"], 0)
            self.assertEqual(first["executor"], "threads")
            if workers > 1:
                self.assertGreater(scheduling["observed_cpu_processes"], 0)
                self.assertNotIn(os.getpid(), scheduling["cpu_process_ids"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in directory.iterdir()})

    def test_unknown_function_or_f_is_never_dispatched(self):
        for name in ("check_task_f", "main", "system", "unknown"):
            with self.assertRaisesRegex(ValueError, "本地只读"):
                audit.run_cpu_check(name, ())

    def test_failed_cpu_job_has_no_thread_fallback_or_hidden_success(self):
        task = audit.AuditTask("bad", "files", lambda: self.fail("no fallback"), ("unknown", ()))
        if (os.cpu_count() or 1) < 2:
            self.skipTest("requires two CPUs")
        results, _, scheduling = audit.execute_audit_tasks([task], 2, "hybrid")
        self.assertEqual(results[0].status, audit.FAIL)
        self.assertIn("ValueError", results[0].message)
        self.assertTrue(scheduling["all_joined_before_cross_check"])

    def test_invalid_workers_or_executor_fail_before_pool_creation(self):
        task = audit.AuditTask("test", "files", lambda: self.fail("must not run"))
        for workers in (True, 0, -1, 17):
            with self.assertRaises(ValueError):
                audit.execute_audit_tasks([task], workers, "hybrid")
        with self.assertRaises(ValueError):
            audit.execute_audit_tasks([task], 1, "unknown")

    def test_dispatch_preserves_exact_original_arguments_and_keeps_f_in_threads(self):
        inputs = {key: None for key in ("card_receipt", "balance", "analysis", "export_receipt",
            "google_receipt", "dispatch_receipt", "delivery_dir")}
        inputs.update(source=Path("/unused/source.xlsx"), result=Path("/unused/result.xlsx"),
            association=Path("/unused/google.xlsx"), server_date="2026-10-10", server_receipts=[])
        resolver = audit.PathResolver(Path("/unused/config.json"), None)
        for google_enabled in (False, True):
            config = {"sourceSha256": "a" * 64, "googleSha256": "b" * 64,
                "googleAssociationEnabled": google_enabled, "taskG": {"enabled": True},
                "googleSystemMissingSheetEnabled": True, "koUnactivatedSheetEnabled": True}
            with patch.object(audit, "sha256_file", return_value="c" * 64):
                tasks = audit.build_tasks(config, resolver, inputs, 4)
            f_tasks = [task for task in tasks if task.category == "task_f"]
            self.assertEqual(len(f_tasks), 1)
            self.assertIsNone(f_tasks[0].cpu_call)
            for task in tasks:
                if task.cpu_call is None:
                    continue
                name, arguments = task.cpu_call
                with self.subTest(google_enabled=google_enabled, task=task.name), patch.object(
                        audit, name, return_value=audit.CheckResult(task.name, task.category, audit.PASS, "fixture")) as check:
                    task.fn()
                    original_arguments = check.call_args
                    check.reset_mock()
                    audit.run_cpu_check(name, arguments)
                    self.assertEqual(original_arguments, check.call_args)


if __name__ == "__main__":
    unittest.main()
