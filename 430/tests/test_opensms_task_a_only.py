"""Offline task-selection regressions; no browser or production data access."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("patrol_task_a_only_runner", ROOT / "scripts/opensms_patrol_runner.py")
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


class TaskAOnlyTests(unittest.TestCase):
    def setUp(self):
        # Any unexpected real child process is a test failure, including reads.
        for method in ("run", "Popen"):
            guard = patch.object(RUNNER.subprocess, method, side_effect=AssertionError("live command forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def runner(self, only=True):
        args = RUNNER.build_parser().parse_args(["run", "--cdp", "http://127.0.0.1:60000"] + (["--task-a-only"] if only else []))
        delivery = self.directory / "delivery"
        with patch.object(RUNNER, "RUN_LOG_ROOT", self.directory / "logs"), patch.object(
            RUNNER.PatrolRunner, "_delivery_dir", return_value=delivery
        ), patch.object(RUNNER, "run_id_now", return_value="20260907T120000+0800"):
            return RUNNER.PatrolRunner(args)

    def json_file(self, name, value):
        path = self.directory / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def evidence(self, runner):
        empty = self.json_file("empty.json", {})
        source = self.json_file("source.json", {"fresh": True})
        acquire = {key: empty for key in (
            "sourceArchive", "exportReceipt", "scanCacheReceipt",
            "systemKoodaInventory", "loginReceipt", "phoneDateReceipt",
            "dayDateReceipt", "balance",
        )}
        acquire.update(serverDate="2026-09-07", sourceArchive=source,
                       sourceSha256="source-sha", association=None, googleSha256=None,
                       googleAssociationEnabled=False, googleDisabledReason="user_disabled",
                       systemKoodaSha256="system-sha", systemKoodaRows=1,
                       cardRetentionReceipt=self.json_file("retention.json", {}),
                       cardRetentionPhoneList=self.json_file("retention-phones.json", []),
                       cardRetentionSha256="0" * 64)
        document = {"cardReceipt": empty, "dispatchReceipt": empty}
        candidates = {"analysis": empty, "routingManifest": empty, "eRecoverySummary": empty}
        refresh = {key: empty for key in (
            "finalStateReceipt", "refreshReceipt", "verifyReceipt", "readbackReceipt", "finalWorkbook",
        )}
        refresh.update(finalWorkbookSha256="final-sha", liveRefreshCount=1)
        final = {"auditPassed": 1, "auditWarnings": 0, "auditFailed": 0, "report": empty}
        return acquire, candidates, document, refresh, final

    def test_cli_rejects_all_mixed_modes_before_checkpoint_or_cdp(self):
        options = [["--force-task-" + task] for task in "bcdef"] + [
            ["--task-f-only"], ["--task-f-sample-size", "1"],
            ["--task-f-debug-iccid", "891223020025159114"],
            ["--task-f-repair-lookup", "/must-not-read.json"],
            ["--task-e-recovery-progress", "/must-not-read.json"],
            ["--resume-state", "/must-not-read.json"],
            ["--authorization-source", "current-user"],
            ["--authorization-source", "history"], ["--task-f-workers", "8"],
        ]
        for option in options:
            with self.subTest(option=option), patch.object(RUNNER, "read_json") as read, patch.object(
                RUNNER, "resolve_bitbrowser_430_cdp"
            ) as cdp, patch.object(sys, "argv", ["runner", "run", "--task-a-only", *option]), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(RUNNER.main(), 2)
                read.assert_not_called()
                cdp.assert_not_called()
                result = json.loads(output.getvalue())
                self.assertEqual(result["taskSelection"], "A")
                self.assertEqual(result["taskF"]["reason"], "task_a_only")
                self.assertIn("--task-a-only", result["error"])

    def test_constructor_also_rejects_mixed_or_resumed_a_only_before_cdp(self):
        args = RUNNER.build_parser().parse_args(["run", "--task-a-only", "--force-task-b"])
        with patch.object(RUNNER, "resolve_bitbrowser_430_cdp") as cdp, self.assertRaises(RUNNER.PatrolError):
            RUNNER.PatrolRunner(args)
        cdp.assert_not_called()
        for only in (False, True):
            args = RUNNER.build_parser().parse_args(["run"] + (["--task-a-only"] if only else []))
            with patch.object(RUNNER, "resolve_bitbrowser_430_cdp") as cdp, self.assertRaises(RUNNER.PatrolError):
                RUNNER.PatrolRunner(args, {"taskAOnly": True, "taskSelection": "A"})
            cdp.assert_not_called()

    def test_initial_state_records_all_excluded_tasks_and_manual_default_is_preserved(self):
        runner = self.runner()
        state = json.loads(runner.state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["taskAOnly"])
        self.assertEqual(state["taskSelection"], "A")
        self.assertEqual(state["stageOrder"], ["document", "standard", "expiredCardProbe"])
        for task in "BCDEF":
            self.assertEqual(state["stages"]["task" + task]["reason"], "task_a_only")
            self.assertFalse(state["authorization"]["task" + task])
        default = RUNNER.execution_contract()
        self.assertTrue(default["taskFEnabledForRun"])
        self.assertFalse(default["taskAOnly"])
        self.assertIn("taskF", default["stageOrder"])

    def test_residual_and_supplied_f_environment_cannot_reenable_task_f(self):
        runner = self.runner()
        foreign = {name: "1" for name in RUNNER.TASK_BCDE_GATE_ENV_VARS}
        foreign.update({name: "1" for name in (
            "OPENSMS_FORCE_TASK_F", "OPENSMS_ENABLE_TASK_F", "OPENSMS_EXECUTE_TASK_F",
            "OPENSMS_TASK_F_POLICY_CATALOG_SYNC", "OPENSMS_TASK_F_REPAIR_MODE",
            "OPENSMS_TASK_F_CATALOG_SOURCE", "OPENSMS_TASK_F_DATABASE",
        )})
        foreign["OPENSMS_SYSTEM_KOODA_SNAPSHOT"] = "stale"
        with patch.dict(os.environ, foreign, clear=True):
            child = runner.child_environment({**foreign, "OPENSMS_SYSTEM_KOODA_SNAPSHOT": "1"})
            for key in foreign:
                if key != "OPENSMS_SYSTEM_KOODA_SNAPSHOT":
                    self.assertNotIn(key, child)
            self.assertEqual(child["OPENSMS_SYSTEM_KOODA_SNAPSHOT"], "1")
            self.assertNotIn("OPENSMS_SYSTEM_KOODA_SNAPSHOT", runner.child_environment())
            self.assertEqual(os.environ["OPENSMS_FORCE_TASK_F"], "1")

    def test_execute_keeps_complete_a_path_and_default_manual_f_path(self):
        for only in (True, False):
            with self.subTest(only=only):
                runner = self.runner(only)
                evidence = self.evidence(runner)
                events = []
                methods = {
                    "verify_policy": None, "acquire": evidence[0], "start_document": None,
                    "analyze_and_candidates": evidence[1], "finish_document": evidence[2],
                    "repair_task_f_lookup": None, "run_task_f": {}, "run_standard_writes": None,
                    "start_background_task_f": {},
                    "run_expired_card_probe": None,
                    "run_task_g": {"status": "passed"},
                    "release_expired_card_probe": None,
                    "refresh_workbook": evidence[3], "close_api_target": None,
                    "finalize": evidence[4], "summary": {"status": "success"}, "command": None,
                }
                for name, result in methods.items():
                    setattr(runner, name, Mock(side_effect=lambda *a, name=name, result=result, **k: (events.append(name), result)[1]))
                for name in ("run_task_b", "run_task_c", "run_task_d", "run_task_e"):
                    setattr(runner, name, Mock(side_effect=AssertionError("excluded task called")))
                self.assertEqual(runner.execute()["status"], "success")
                expected = ["verify_policy", "acquire", "start_document", "analyze_and_candidates", "finish_document"]
                if not only:
                    expected += ["start_background_task_f"]
                expected += ["run_standard_writes", "run_expired_card_probe"]
                if not only:
                    expected += ["refresh_workbook"]
                expected += ["run_task_g", "refresh_workbook", "close_api_target", "finalize", "summary", "command", "release_expired_card_probe"]
                self.assertEqual(events, expected)
                if only:
                    runner.run_task_f.assert_not_called()
                    runner.repair_task_f_lookup.assert_not_called()
                for name in ("run_task_b", "run_task_c", "run_task_d", "run_task_e"):
                    getattr(runner, name).assert_not_called()

    def test_task_f_secondary_entrypoints_do_not_read_or_execute(self):
        runner = self.runner()
        runner.command = Mock(side_effect=AssertionError("excluded task command"))
        with patch.object(RUNNER, "read_json", side_effect=AssertionError("excluded task read")), patch.object(
            RUNNER, "sha256", side_effect=AssertionError("excluded task hash")
        ):
            self.assertEqual(runner.run_task_f()["reason"], "task_a_only")
            for callback in (
                runner.repair_task_f_lookup,
                lambda: runner.run_task_f_debug("891223020025159114"),
                lambda: runner.export_task_f_deadlines(Path("/must-not-read.sqlite3")),
            ):
                with self.assertRaises(RUNNER.PatrolError):
                    callback()
        runner.command.assert_not_called()

    def test_a_failed_export_never_dispatches_kooda_inventory_or_f(self):
        runner = self.runner()
        runner.create_api_target = Mock()
        runner.copy_artifact = Mock(return_value=Path(self.json_file("login.json", {"success": True})))
        dispatched = []
        background_started = threading.Event()

        def command(label, argv, **kwargs):
            if label in ("captcha_service", "login_preflight"):
                return RUNNER.CommandResult({"success": True, "path": "mock-login.json"}, "", "", 0)
            dispatched.append((label, runner.child_environment(kwargs.get("env"))))
            if label == "export":
                env = kwargs["env"]
                RUNNER.atomic_json(Path(env["OPENSMS_EXPORT_SUBMISSION_READY_PATH"]), {
                    "schemaVersion": 1, "status": "submitted", "runId": runner.run_id,
                    "token": env["OPENSMS_EXPORT_SUBMISSION_READY_TOKEN"],
                    "taskId": "a" * 32, "serverDate": "2026-09-07",
                    "exportPageClosed": True, "requestLaneReleased": True,
                    "publishedAt": "2026-09-07T04:00:01Z"})
                self.assertTrue(background_started.wait(3))
            elif label == "google_chatr_download":
                self.assertTrue(background_started.wait(3))
            else:
                background_started.set()
            raise RUNNER.PatrolError("mock acquisition stop")

        runner.command = Mock(side_effect=command)
        with patch.object(RUNNER, "ROOT", self.directory), patch.dict(os.environ, {
            "OPENSMS_FORCE_TASK_F": "1", "OPENSMS_TASK_F_POLICY_CATALOG_SYNC": "1",
        }), self.assertRaises(RUNNER.PatrolError):
            runner.acquire()
        inventory = [env for label, env in dispatched if label == "system_kooda_inventory"]
        self.assertEqual(inventory, [])
        self.assertEqual({label for label, _ in dispatched}, {"export", "active_carriers", "balance", "google_chatr_download", "card_retention_report"})
        for _, env in dispatched:
            self.assertFalse(any(key.startswith('OPENSMS_TASK_F_') for key in env))
            self.assertNotIn('OPENSMS_FORCE_TASK_F', env)
        retention = [env for label, env in dispatched if label == "card_retention_report"]
        self.assertEqual(retention[0]["OPENSMS_CARD_RETENTION_REPORT_RUN_ID"], runner.run_id)
        self.assertFalse(any(env_key.startswith("OPENSMS_TASK_D_") for env_key in retention[0]))

    def test_finalize_config_and_summary_do_not_claim_f_business_outputs(self):
        runner = self.runner()
        acquire, candidates, document, refresh, final = self.evidence(runner)
        # This test checks A/F scope, not the separate Google provenance audit.
        acquire.update(chatrGoogleSource=acquire["sourceArchive"],
                       chatrGoogleSourceSha256=acquire["sourceSha256"],
                       chatrGoogleDownloadReceipt=self.json_file("chatr-download.json", {}),
                       chatrGoogleDates=self.json_file("chatr-dates.json", {}))
        runner.state["commandTimings"] = [
            {"command": "system_kooda_inventory", "exitCode": 0, "durationSeconds": 1},
            {"command": "final_carrier_readback", "exitCode": 0, "durationSeconds": 1},
        ]
        runner.copy_artifact = Mock(side_effect=lambda path: path)
        config_path = runner.build_finalize_config(acquire, candidates, document, refresh, runner.receipts / "audit.json", "完成")
        config = RUNNER.read_json(config_path)
        self.assertTrue(config["taskAOnly"])
        self.assertTrue(config["googleChatrDateFallbackEnabled"])
        self.assertEqual(config["chatrDocumentDateSource"], "current_run_system_then_fresh_google")
        self.assertEqual(config["taskFStageStatus"], "skipped")
        self.assertEqual(config["taskFSkippedReason"], "task_a_only")
        self.assertIsNone(config["taskFDatabase"])
        self.assertFalse(config["taskFExecuted"])
        self.assertEqual(config["systemKoodaPath"], acquire["systemKoodaInventory"])
        for key in ("taskFCatalogSource", "taskFDatabaseSnapshot", "taskFPendingReceipt", "taskFDeadlineWorkbook"):
            self.assertIsNone(config[key])
        safety = RUNNER.read_json(config["safetyReceipt"])
        self.assertEqual(safety["taskAuthorization"]["taskSelection"], "A")
        self.assertFalse(safety["taskAuthorization"]["taskFExecuted"])
        import opensms_parallel_audit as audit
        reviewed = audit.check_task_f(config, audit.PathResolver(config_path, runner.delivery))
        self.assertEqual(reviewed.status, audit.SKIP, reviewed.details)
        self.assertTrue(reviewed.details["skip_contract_verified"])
        result = runner.summary(acquire, candidates, refresh, final)
        for payload in (result, RUNNER.compact_launcher_result(result)):
            self.assertEqual(payload["taskSelection"], "A")
            self.assertTrue(payload["taskAOnly"])
            self.assertEqual(payload["taskF"]["status"], "skipped")
            self.assertEqual(payload["taskF"]["reason"], "task_a_only")
        self.assertIsNone(result["taskF"]["database"])

    def test_default_no_gap_f_reason_survives_summary_and_compact_json(self):
        runner = self.runner(only=False)
        acquire, candidates, _, refresh, final = self.evidence(runner)
        runner.state["stages"]["taskF"] = {
            "status": "skipped", "executed": False, "reason": "no_missing_authoritative_dates"}
        result = runner.summary(acquire, candidates, refresh, final)
        for payload in (result, RUNNER.compact_launcher_result(result)):
            self.assertFalse(payload["taskF"]["executed"])
            self.assertEqual(payload["taskF"]["reason"], "no_missing_authoritative_dates")
        text = Path(result["chatSummaryPath"]).read_text()
        self.assertIn("无需联网补查", text)
        self.assertNotIn("ICCID库位置", text)

    def test_acquisition_failure_keeps_a_only_failure_contract(self):
        runner = self.runner()
        runner.verify_policy = Mock()
        runner.acquire = Mock(side_effect=RUNNER.PatrolError("mock acquisition failure"))
        runner.close_api_target = Mock()
        runner.command = Mock()
        with self.assertRaisesRegex(RUNNER.PatrolError, "mock acquisition failure"):
            runner.execute()
        result = runner.failure_result
        self.assertTrue(result["taskAOnly"])
        self.assertIsNone(result["finalWorkbook"])
        self.assertEqual(result["taskF"]["reason"], "task_a_only")
        self.assertIn("仅任务A模式", Path(result["report"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
