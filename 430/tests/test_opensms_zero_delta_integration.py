import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


RUNNER = module("runner_zero_delta_integration", "opensms_patrol_runner.py")
AUDIT = module("audit_zero_delta_integration", "opensms_parallel_audit.py")
REPORT = module("report_zero_delta_integration", "opensms_finalize_patrol_report.py")
CHAT = module("chat_zero_delta_integration", "opensms_chat_summary.py")


class ZeroDeltaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.candidate = {"count": 2, "rows": [{"phone": "14165550000"}, {"phone": "14165550001"}]}
        self.write = {
            "ts": "2026-09-05T05:00:00Z", "noWrite": True,
            "actualWriteCount": 0, "after": None, "updateResponses": [],
            "independentReadbackRequired": False, "independentReadbackPerformed": False,
            "target": {"name": "B12"},
            "before": {
                "candidateCount": 2, "eligibleCount": 0, "alreadyTargetCount": 1,
                "alreadyTarget": [{"phone": "14165550000", "row": {"id": 10}}],
                "nonSource": [{"phone": "14165550001", "row": {"id": 11}}],
            },
        }
        self.verify = {
            "total": 0, "passed": None, "verificationStatus": "not_required",
            "status": "no_candidate", "independentReadbackPerformed": False,
        }
        self.proof = {
            "checkedAt": "2026-09-05T05:00:01Z", "passed": True,
            "silentBrowserMode": True, "browserForegroundActivationAttempted": False,
            "candidateCount": 2, "backendValidCount": 2, "failureCount": 0,
            "rows": [
                {"phone": "4165550000", "backendMatchCount": 1, "backendId": 10, "passed": True, "liveCarrierName": "B12"},
                {"phone": "4165550001", "backendMatchCount": 1, "backendId": 11, "passed": True, "liveCarrierName": "快过期续费T"},
            ],
        }
        self.validated = {"count": 0, "originalCount": 2, "rows": []}
        self.group = {
            "candidate": str(self.save("candidate.json", self.candidate)),
            "validated": str(self.save("validated.json", self.validated)),
            "write": str(self.save("write.json", self.write)),
            "verify": str(self.save("verify.json", self.verify)),
            "noWrite": True,
        }
        self.save_proof()
        self.resolver = AUDIT.PathResolver(self.folder / "config.json", self.folder)

    def save(self, name, value):
        path = self.folder / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def save_proof(self):
        path = self.save("proof.json", self.proof)
        self.group.update(stageReadbackReceipt=str(path), stageReadbackSha256=AUDIT.sha256_file(path))

    def make_runner(self):
        runner = object.__new__(RUNNER.PatrolRunner)
        runner.state = {"writeGroups": {}, "timings": []}
        runner.args = SimpleNamespace(cdp="offline", refresh_engine="targeted-ooxml")
        runner.run_id = "test-run"
        runner.run_dir = self.folder
        runner.receipts = self.folder
        runner.delivery = self.folder
        runner._save_state = Mock()
        runner.copy_artifact = lambda path: Path(path)
        return runner

    def test_no_write_is_not_a_fake_group_verification(self):
        runner = self.make_runner()
        written = {**self.write, "path": self.group["write"], "validatedCandidatePath": self.group["validated"]}
        verified = {**self.verify, "path": self.group["verify"]}
        runner.command = Mock(side_effect=[SimpleNamespace(payload=written), SimpleNamespace(payload=verified)])
        result = runner.run_write({"route": "task_b_demo", "candidate": self.group["candidate"], "target": "B12"})
        self.assertEqual(result["actualWrite"], 0)
        self.assertFalse(result["independentReadbackPerformed"])
        self.assertTrue(result["stageReadbackRequired"])
        self.assertFalse(result["stageReadbackVerified"])
        self.assertEqual(runner.summarize_write_groups("task_b_")["independentVerificationPassed"], 0)

    def test_malformed_no_write_receipt_is_rejected_by_runner(self):
        for change in ({"actualWriteCount": 1}, {"updateResponses": [{"ok": True}]}, {"after": {}}, {"independentReadbackPerformed": True}):
            with self.subTest(change=change):
                runner = self.make_runner()
                written = {**self.write, **change, "path": self.group["write"], "validatedCandidatePath": self.group["validated"]}
                runner.command = Mock(side_effect=[SimpleNamespace(payload=written), SimpleNamespace(payload={**self.verify, "path": self.group["verify"]})])
                with self.assertRaises(RUNNER.PatrolError):
                    runner.run_write({"route": "task_b_demo", "candidate": self.group["candidate"], "target": "B12"})

    def test_zero_count_with_rows_cannot_bypass_validation(self):
        self.save("candidate.json", {"count": 0, "rows": [{"phone": "14165550000"}]})
        runner = self.make_runner()
        runner.command = Mock(side_effect=AssertionError("must not launch"))
        with self.assertRaises(RUNNER.PatrolError):
            runner.run_write({"route": "demo", "candidate": self.group["candidate"], "target": "B12"})

    def test_audit_requires_fresh_identity_bound_stage_read(self):
        result = AUDIT.check_group("no-op", self.group, self.resolver)
        self.assertEqual(result.status, AUDIT.PASS, result.message)
        self.assertEqual(result.details["actual_writes"], 0)
        self.assertFalse(result.details["per_group_independent_readback_performed"])
        cases = [
            ("missing row", lambda p: p["rows"].pop()),
            ("wrong ID", lambda p: p["rows"][0].update(backendId=999)),
            ("wrong target", lambda p: p["rows"][0].update(liveCarrierName="B11")),
            ("stale", lambda p: p.update(checkedAt="2026-09-05T04:59:59Z")),
            ("failed", lambda p: p.update(passed=False)),
            ("duplicate", lambda p: p["rows"].append(copy.deepcopy(p["rows"][0]))),
        ]
        original = copy.deepcopy(self.proof)
        for name, mutate in cases:
            with self.subTest(name=name):
                self.proof = copy.deepcopy(original)
                mutate(self.proof)
                self.save_proof()
                self.assertEqual(AUDIT.check_group("no-op", self.group, self.resolver).status, AUDIT.FAIL)

    def test_audit_rejects_missing_or_changed_stage_proof(self):
        missing = {**self.group, "stageReadbackReceipt": None}
        self.assertEqual(AUDIT.check_group("no-op", missing, self.resolver).status, AUDIT.FAIL)
        self.save("proof.json", {**self.proof, "extra": "modified"})
        self.assertEqual(AUDIT.check_group("no-op", self.group, self.resolver).status, AUDIT.FAIL)

    def test_report_describes_empty_verification_without_success_claim(self):
        _, metrics = REPORT.group_metrics(self.group)
        self.assertEqual(metrics["passed"], "无写入；阶段全量回读")
        self.assertEqual(metrics["actual"], 0)
        self.assertEqual(metrics["verify_target"], 0)

    def test_refresh_passes_cache_only_after_fresh_collector_and_preserves_first_proof(self):
        runner = self.make_runner()
        runner.reusable_stage = Mock(return_value=None)
        runner.complete_stage = lambda name, data, started: data
        runner.state["writeGroups"]["demo"] = {
            "result": "passed", "validated": self.group["validated"], "target": "B12", "route": "demo",
            "alreadyTargetPhones": ["14165550000"], "nonSourcePhones": ["14165550001"],
            "write": self.group["write"], "noWrite": True, "stageReadbackRequired": True,
        }
        commands = []
        def command(label, argv, **kwargs):
            commands.append((label, argv))
            if label.endswith("carrier_state"):
                Path(argv[-1]).write_text(json.dumps(self.proof))
                return SimpleNamespace(payload=self.proof)
            Path(argv[4]).write_bytes(b"offline output")
            return SimpleNamespace(duration_seconds=0.01, payload={"passed": True, "changed": 0, "verifiedCachePath": "/tmp/new-cache", "verifiedCacheSha256": "new-hash"})
        runner.command = command
        document = {"cardResult": "/tmp/prior.xlsx", "verifiedCachePath": "/tmp/prior-cache", "verifiedCacheSha256": "prior-hash"}
        with patch.object(RUNNER, "ensure_xlsx"):
            refresh = runner.refresh_workbook(document, stage_name="refreshAfterTaskB")
        self.assertTrue(commands[0][0].endswith("carrier_state"))
        fast_args = [str(v) for v in commands[1][1]]
        self.assertIn("--run-id", fast_args)
        self.assertIn("prior-hash", fast_args)
        self.assertEqual(runner.refreshed_document(document, refresh)["verifiedCacheSha256"], "new-hash")
        first_proof = runner.state["writeGroups"]["demo"]["stageReadbackReceipt"]
        with patch.object(RUNNER, "ensure_xlsx"):
            runner.refresh_workbook(document)
        self.assertEqual(runner.state["writeGroups"]["demo"]["stageReadbackReceipt"], first_proof)

    def test_null_statistics_are_unknown_not_none_percent(self):
        summary = CHAT.render_summary({"status": "success", "overallReplyRatePct": None})
        self.assertIn("整体回复率 未知", summary)
        self.assertNotIn("None", summary)
        self.assertNotIn("未知%", summary)

    def test_failed_stage_read_never_reports_no_write_task_as_completed(self):
        for failure_at in ("collector", "workbook-cache"):
            with self.subTest(failure_at=failure_at):
                runner = self.make_runner()
                runner.authorization = {}
                runner.state_path = self.folder / "state.json"
                runner.state["stages"] = {"taskB": {"executed": True}}
                runner.state["writeGroups"] = {"task_b_demo": {
                    "route": "task_b_demo", "result": "passed", "noWrite": True,
                    "candidateCount": 2, "backendValid": 0, "actualWrite": 0,
                    "stageReadbackRequired": True, "stageReadbackVerified": False,
                    "independentReadbackPerformed": False,
                }}
                runner.reusable_stage = Mock(return_value=None)
                # Verify the actual stage failure path, not only a fabricated
                # final summary. Workbook failure follows a successful collector.
                if failure_at == "collector":
                    runner.state["writeGroups"]["task_b_demo"].update(
                        validated=self.group["validated"], target="B12", write=self.group["write"])
                    runner.command = Mock(return_value=SimpleNamespace(payload={"passed": False}))
                else:
                    runner.state["writeGroups"]["task_b_demo"].update(
                        validated=self.group["validated"], target="B12", write=self.group["write"])
                    runner.command = Mock(side_effect=[SimpleNamespace(payload={"passed": True}), RUNNER.PatrolError("cache mismatch")])
                with self.assertRaises(RUNNER.PatrolError):
                    runner.refresh_workbook({"cardResult": "/tmp/input.xlsx"}, stage_name="refreshAfterTaskB")
                with patch.dict(sys.modules, {"opensms_chat_summary": CHAT}):
                    result = runner.write_failure_report(RUNNER.PatrolError(failure_at))
                self.assertEqual(result["taskB"]["status"], "pending_stage_readback")
                self.assertEqual(result["taskB"]["actualWriteCount"], 0)
                output = Path(result["chatSummaryPath"]).read_text()
                self.assertIn("任务B未完成", output)
                self.assertNotIn("任务B已完成", output)

    def test_child_command_failure_keeps_timing(self):
        runner = self.make_runner()
        with self.assertRaises(RUNNER.PatrolError):
            runner.command("offline-failure", [sys.executable, "-c", "raise SystemExit(3)"])
        self.assertEqual(runner.state["commandTimings"][0]["exitCode"], 3)
        self.assertGreater(runner.state["commandTimings"][0]["durationSeconds"], 0)


if __name__ == "__main__":
    unittest.main()
