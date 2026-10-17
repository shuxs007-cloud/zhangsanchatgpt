import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from opensms_task_g_continuation import validate_checkpoint, sha
import opensms_patrol_runner as runner


class TaskGContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = "20261008T231127+0800"
        self.delivery = Path(self.temp.name) / self.run
        self.receipts = self.delivery / "receipts"
        self.receipts.mkdir(parents=True)
        self.state_path = self.receipts / f"opensms_patrol_runner_state_{self.run}.json"
        def file(name, value):
            path = self.receipts / name
            path.write_text(json.dumps(value), encoding="utf-8")
            return str(path)
        self.settings = file("settings.json", {"enabled": True})
        self.ledger = file("ledger.json", {"version": 1, "records": {}})
        source = file("source.xlsx", {})
        cache = file("cache.json", {})
        workbook = file("before.xlsx", {})
        readback = file("readback.json", {"passed": True})
        spec = {"runId": self.run, "serverDate": "2026-10-08",
                "settings": {"enabled": True}, "settingsSha256": sha(self.settings)}
        for name, path in (("source", source), ("scanCache", cache),
                           ("postBusinessWorkbook", workbook), ("postBusinessReadback", readback)):
            spec[name], spec[name + "Sha256"] = path, sha(path)
        inp = file("input.json", spec)
        receipt = file("receipt.json", {"runId": self.run, "inputSha256": sha(inp),
            "status": "failed_no_replay", "error": "TASK_G_PHONE_IDENTITY_AMBIGUOUS",
            "batches": [], "reconciliations": [], "dateChecks": [], "skips": [],
            "enteredCount": 0, "restoredCount": 0, "ledgerPath": self.ledger,
            "ledgerBefore": {"version": 1, "records": {}}})
        stages = {name: {"completedAt": "time"} for name in (
            "acquire", "candidates", "document", "standardWrites", "taskF", "taskD", "taskE",
            "expiredCardProbe", "refreshBeforeTaskG", "taskG")}
        stages["acquire"].update(serverDateVerified=True, serverDate="2026-10-08",
            sourceArchive=source, sourceSha256=sha(source), processingSource=source)
        stages["candidates"].update(analysis=file("analysis.json", {}), routingManifest=file("routes.json", {}))
        stages["document"].update(cardResult=file("document.xlsx", {}), cardReceipt=file("doc.json", {}))
        stages["refreshBeforeTaskG"].update(finalWorkbook=workbook, finalWorkbookSha256=sha(workbook), finalStateReceipt=readback)
        stages["taskG"].update(input=inp, inputSha256=sha(inp), receipt=receipt, receiptSha256=sha(receipt))
        self.state = {"runId": self.run, "delivery": str(self.delivery), "status": "failed",
            "error": "任务G未完整确认，禁止重试：TASK_G_PHONE_IDENTITY_AMBIGUOUS",
            "apiTarget": {"status": "closed"}, "authorization": {"taskB": False},
            "stages": stages, "writeGroups": {},
            "fDateImport": {"status": "failed_before_submission", "submitted": False}}

    def validate(self, state=None, alive=False):
        return validate_checkpoint(self.state_path, state or self.state, process_alive=alive,
            settings_path=self.settings, ledger_path=self.ledger)

    def change_receipt(self, **changes):
        g = self.state["stages"]["taskG"]
        path = Path(g["receipt"])
        value = json.loads(path.read_text()); value.update(changes)
        path.write_text(json.dumps(value)); g["receiptSha256"] = sha(path)

    def test_zero_submission_checkpoint_passes_without_import_replay(self):
        proof = self.validate()
        self.assertFalse(proof["fImportReplayed"])
        self.assertFalse(proof["completedBusinessStagesReplayed"])

    def test_possible_submission_or_ledger_change_is_blocked(self):
        for changes in ({"batches": [{}]}, {"dateChecks": [{}]}, {"ledgerAfter": {}},
                        {"reconciliations": [{}]}, {"skips": [{}]}, {"enteredCount": 1}):
            with self.subTest(changes=changes):
                original = Path(self.state["stages"]["taskG"]["receipt"]).read_text()
                self.change_receipt(**changes)
                with self.assertRaises(ValueError): self.validate()
                Path(self.state["stages"]["taskG"]["receipt"]).write_text(original)
                self.state["stages"]["taskG"]["receiptSha256"] = sha(self.state["stages"]["taskG"]["receipt"])

    def test_changed_ledger_is_blocked(self):
        Path(self.ledger).write_text('{"version":1,"records":{},"changed":true}')
        with self.assertRaises(ValueError): self.validate()

    def test_live_or_already_attempted_checkpoint_is_blocked(self):
        with self.assertRaises(ValueError): self.validate(alive=True)
        self.state["taskGContinuationAttempts"] = [{}]
        with self.assertRaises(ValueError): self.validate()

    def test_current_uncertain_f_import_is_blocked(self):
        self.state["fDateImport"] = {"status": "submission_outcome_uncertain", "submitted": True}
        with self.assertRaises(ValueError): self.validate()

    def test_historical_forced_tasks_are_not_inherited(self):
        for task in "BCDEF":
            state = copy.deepcopy(self.state); state["authorization"]["task" + task] = True
            with self.subTest(task=task), self.assertRaises(ValueError): self.validate(state)

    def test_changed_source_hash_is_blocked(self):
        path = json.loads(Path(self.state["stages"]["taskG"]["input"]).read_text())["source"]
        Path(path).write_text("changed")
        with self.assertRaises(ValueError): self.validate()

    def test_cli_requires_current_user_and_exact_resume(self):
        for argv in (["run", "--continue-task-g"],
                     ["run", "--continue-task-g", "--resume-state", str(self.state_path), "--authorization-source", "timer"],
                     ["run", "--continue-task-g", "--resume-state", str(self.state_path), "--authorization-source", "current-user", "--force-task-c"]):
            with self.subTest(argv=argv), self.assertRaises(runner.PatrolError):
                runner.task_g_continuation_checkpoint(runner.build_parser().parse_args(argv))

    def test_continuation_does_not_call_completed_business_stages(self):
        import inspect
        code = inspect.getsource(runner.PatrolRunner.continue_task_g)
        for call in ("self.acquire(", "self.start_document(", "self.start_background_task_f(",
                     "self.run_standard_writes(", "self.run_task_b(", "self.run_task_c(",
                     "self.run_task_d(", "self.run_task_e(", "self.import_current_f_dates(",
                     "self.run_expired_card_probe("):
            self.assertNotIn(call, code)
        self.assertLess(code.index("task_g_continuation_server_dates"), code.index("self.run_task_g("))
        self.assertIn("self.close_api_target()", code)


if __name__ == "__main__":
    unittest.main()
