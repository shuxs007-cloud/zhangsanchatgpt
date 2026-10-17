"""Local document entrypoint provenance gates; subprocess execution is mocked."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("opensms_local_system_source", ROOT / "scripts/opensms_generate_from_downloads.py")
LOCAL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = LOCAL
SPEC.loader.exec_module(LOCAL)
GOOGLE_DISABLED = {"enabled": False, "status": "skipped", "reason": "user_disabled",
                   "unused_port_device_scope": "system_source_only"}


class LocalSystemSourceTests(unittest.TestCase):
    def test_absolute_module_import_does_not_change_search_path_or_dependency_registry(self):
        before = list(sys.path)
        dependency = sys.modules.get("opensms_export_task_f_deadlines")
        isolated = importlib.util.spec_from_file_location("opensms_local_import_again", ROOT / "scripts/opensms_generate_from_downloads.py")
        module = importlib.util.module_from_spec(isolated)
        isolated.loader.exec_module(module)
        self.assertEqual(sys.path, before)
        self.assertIs(sys.modules.get("opensms_export_task_f_deadlines"), dependency)
        self.assertTrue(callable(module.freeze))

    def snapshot(self, directory, **overrides):
        payload = {
            "runId": "local-current-run", "status": "passed", "totalRows": 0, "systemRows": [],
            "paginationTotalStable": True, "paginationBackendIdsUnique": True,
            "silentBrowserMode": True, "browserForegroundActivationAttempted": False,
            **overrides,
        }
        path = Path(directory).resolve() / "system.json"
        raw = json.dumps(payload).encode("utf-8")
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest(), "local-current-run"

    def test_missing_explicit_source_gate_precedes_any_subprocess(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["local", "source.xlsx", "google.xlsx", "--backend-date", "2026-09-07"]), \
                patch.object(LOCAL.subprocess, "run") as run, patch.object(LOCAL, "validate_xlsx") as xlsx:
            with self.assertRaisesRegex(ValueError, "必须明确提供"):
                LOCAL.main()
            run.assert_not_called()
            xlsx.assert_not_called()

    def test_server_date_is_required_and_must_be_a_real_calendar_date(self):
        base = ["local", "source.xlsx", "google.xlsx"]
        with patch.object(LOCAL.subprocess, "run") as run, contextlib.redirect_stderr(io.StringIO()):
            with patch.object(sys, "argv", base), self.assertRaises(SystemExit) as caught:
                LOCAL.main()
            self.assertEqual(caught.exception.code, 2)
            for bad in ("2026-02-30", "2026-13-01", "2026-9-7", "not-a-date"):
                with self.subTest(date=bad), patch.object(sys, "argv", base + ["--backend-date", bad]), self.assertRaises(ValueError):
                    LOCAL.main()
            run.assert_not_called()

    def test_failed_nested_empty_or_incomplete_receipts_are_rejected(self):
        valid = {"result": "成功", "checks": {"identity": True}, "failed_checks": [], "output_path": "/unused/local.xlsx"}
        invalid = [
            {**valid, "result": "失败"}, {"result": "成功", "local_result": valid},
            {**valid, "checks": {}}, {**valid, "checks": None},
            {**valid, "checks": {"identity": False}}, {**valid, "checks": {"identity": 1}},
            {**valid, "failed_checks": ["identity"]}, {**valid, "failed_checks": None},
            {**valid, "output_path": None},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                LOCAL.validate_card_receipt(payload)
        LOCAL.validate_card_receipt(valid)

    def test_zero_exit_with_bad_receipt_does_not_publish_report_or_receipts(self):
        invalid = [
            {"result": "失败", "checks": {"identity": False}, "failed_checks": ["identity"], "output_path": "result.xlsx"},
            {"result": "成功", "local_result": {"checks": {"identity": True}, "failed_checks": [], "output_path": "result.xlsx"}},
        ]
        for card in invalid:
            with self.subTest(card=card), tempfile.TemporaryDirectory() as directory:
                path, digest, run_id = self.snapshot(directory)
                argv = ["local", "source.xlsx", "google.xlsx", "--system-kooda", str(path),
                        "--system-kooda-sha256", digest, "--run-id", run_id,
                        "--backend-date", "2026-09-07", "--artifact-root", directory,
                        "--task-f-db", str(Path(directory) / "missing-test-only.sqlite3")]
                completed = LOCAL.subprocess.CompletedProcess([], 0, json.dumps(card), "")
                with patch.object(sys, "argv", argv), \
                        patch.object(LOCAL, "validate_xlsx", return_value={}) as xlsx, \
                        patch.object(LOCAL, "analyze_export", return_value={}), \
                        patch.object(LOCAL.subprocess, "run", return_value=completed), \
                        patch.object(LOCAL, "write_json") as write_json, \
                        patch.object(LOCAL, "write_report") as write_report:
                    with self.assertRaises(RuntimeError):
                        LOCAL.main()
                    self.assertEqual(xlsx.call_count, 1)  # only the system source, never the ignored Google input
                    write_json.assert_not_called()
                    write_report.assert_not_called()

    def test_snapshot_requires_all_identity_and_integrity_fields(self):
        invalid = [
            {"runId": "other-run"}, {"status": "partial"}, {"status": None},
            {"totalRows": 1}, {"totalRows": False}, {"totalRows": "0"},
            {"systemRows": None}, {"paginationTotalStable": False},
            {"paginationBackendIdsUnique": False}, {"silentBrowserMode": False},
            {"browserForegroundActivationAttempted": True},
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                path, digest, run_id = self.snapshot(directory, **overrides)
                with self.assertRaises(ValueError):
                    LOCAL.validate_system_kooda(str(path), digest, run_id)

    def test_hash_mismatch_and_source_mutation_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest, run_id = self.snapshot(directory)
            for bad in ("short-hash", "0" * 64):
                with self.subTest(digest=bad), self.assertRaises(ValueError):
                    LOCAL.validate_system_kooda(str(path), bad, run_id)
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA256不一致"):
                LOCAL.validate_system_kooda(str(path), digest, run_id)

    def execute_mocked(self, directory, use_cli):
        path, digest, run_id = self.snapshot(directory)
        directory = Path(directory)
        argv = ["local", str(directory / "source.xlsx"), str(directory / "google.xlsx"),
                "--artifact-root", str(directory / "artifacts"), "--output-dir", str(directory / "outputs"),
                "--backend-date", "2026-09-07"]
        environment = {
            "OPENSMS_SYSTEM_KOODA_PATH": "/stale/inventory.json" if use_cli else str(path),
            "OPENSMS_SYSTEM_KOODA_SHA256": "stale" if use_cli else digest,
            "OPENSMS_RUN_ID": "stale-run" if use_cli else run_id,
            "OPENSMS_FORCE_TASK_B": "1", "OPENSMS_FORCE_TASK_F": "1",
            "OPENSMS_EXECUTE_TASK_F": "1", "OPENSMS_TASK_F_POLICY_CATALOG_SYNC": "1",
            "OPENSMS_TASK_F_DB_PATH": str(directory / "missing-test-only.sqlite3"),
        }
        if use_cli:
            argv += ["--system-kooda", str(path), "--system-kooda-sha256", digest, "--run-id", run_id]
        card = {
            "result": "成功", "output_path": str(directory / "result.xlsx"),
            "process": {"matched_rows": 99, "google_dependency": GOOGLE_DISABLED},
            "enrichment": {}, "verification": {"google_dependency_review": {**GOOGLE_DISABLED, "matches": True}},
            "checks": {"mocked": True}, "failed_checks": [],
        }
        analysis = {"latestDate": "09-07", "rows": 1, "send": 100, "reply": 5,
                    "replyRatePct": 5, "zeroReplySendGt70": 0}
        completed = LOCAL.subprocess.CompletedProcess([], 0, json.dumps(card), "")
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), patch.object(sys, "argv", argv), \
                patch.object(LOCAL.subprocess, "run", return_value=completed) as run, \
                patch.object(LOCAL, "analyze_export", return_value=analysis), \
                patch.object(LOCAL, "validate_xlsx", side_effect=lambda file: {"path": str(file)}), \
                contextlib.redirect_stdout(output):
            self.assertEqual(LOCAL.main(), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], str(LOCAL.BUILDER))
        self.assertEqual(LOCAL.BUILDER, ROOT / "services/opensms_document_api/pipeline/build_card_slot_report.sh")
        self.assertEqual(run.call_args.args[0][-1], "2026-09-07")
        child = run.call_args.kwargs["env"]
        self.assertEqual(child["OPENSMS_SYSTEM_KOODA_PATH"], str(path))
        self.assertEqual(child["OPENSMS_SYSTEM_KOODA_SHA256"], digest)
        self.assertEqual(child["OPENSMS_RUN_ID"], run_id)
        self.assertEqual(child["OPENSMS_GOOGLE_ASSOCIATION_ENABLED"], "0")
        self.assertEqual(run.call_args.args[0][2], "-")
        for key in ("OPENSMS_FORCE_TASK_B", "OPENSMS_FORCE_TASK_F", "OPENSMS_EXECUTE_TASK_F", "OPENSMS_TASK_F_POLICY_CATALOG_SYNC"):
            self.assertNotIn(key, child)
        delivered = json.loads(output.getvalue())
        report = Path(delivered["reportPath"]).read_text(encoding="utf-8")
        self.assertIn("Google关联表：已禁用", report)
        self.assertNotIn("| Google匹配行数 |", report)
        self.assertIn(str(path), report)
        self.assertIn(digest, report)
        receipt = json.loads(Path(delivered["receiptPath"]).read_text(encoding="utf-8"))
        self.assertIs(receipt["liveBackendActionsExecuted"], False)
        self.assertEqual(receipt["serverDate"], "2026-09-07")
        self.assertEqual(receipt["inputs"]["systemKooda"]["runId"], run_id)
        self.assertIsNone(receipt["inputs"]["association"])
        self.assertEqual(receipt["inputs"]["google_dependency"], GOOGLE_DISABLED)
        self.assertEqual(delivered["systemKoodaSource"]["sha256"], digest)

    def test_cli_arguments_override_stale_parent_source_and_report_exact_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            self.execute_mocked(directory, use_cli=True)

    def test_explicit_environment_source_is_validated_and_passed_to_builder(self):
        with tempfile.TemporaryDirectory() as directory:
            self.execute_mocked(directory, use_cli=False)


if __name__ == "__main__":
    unittest.main()
