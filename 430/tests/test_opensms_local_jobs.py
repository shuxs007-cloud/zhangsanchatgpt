import copy
import json
from pathlib import Path
import sys
import threading
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from opensms_local_jobs import run_local_jobs
import opensms_patrol_runner as runner
from opensms_chat_summary import render_summary


class LocalJobTests(unittest.TestCase):
    def test_independent_jobs_overlap_and_return_in_declared_order(self):
        barrier = threading.Barrier(4, timeout=3)
        active, peak = 0, 0
        lock = threading.Lock()

        def job(index):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait()
            with lock:
                active -= 1
            return index

        result, receipt = run_local_jobs({str(i): lambda i=i: job(i) for i in range(4)}, workers=4)
        self.assertEqual(result, {str(i): i for i in range(4)})
        self.assertEqual(peak, 4)
        self.assertTrue(receipt["allJoinedBeforeWrite"])
        self.assertTrue(receipt["liveWritesRemainSerial"])
        self.assertEqual(receipt["actualWorkers"], 4)

    def test_default_three_workers_bound_even_when_four_jobs_are_queued(self):
        active, peak = 0, 0
        lock = threading.Lock()

        def job():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.02)
            with lock:
                active -= 1
            return "immutable"

        result, receipt = run_local_jobs({str(i): job for i in range(4)})
        self.assertLessEqual(peak, 3)
        self.assertEqual(len(result), 4)
        self.assertEqual(receipt["actualWorkers"], 3)

    def test_failure_preserves_original_exception_and_drains_started_reads(self):
        started, finished = threading.Event(), threading.Event()
        error = ValueError("hash mismatch")

        def read():
            started.set()
            time.sleep(.04)
            finished.set()

        def fail():
            self.assertTrue(started.wait(2))
            raise error

        with self.assertRaises(ValueError) as caught:
            run_local_jobs({"read": read, "failed": fail}, workers=2)
        self.assertIs(caught.exception, error)
        self.assertTrue(finished.is_set())

    def test_invalid_workers_and_empty_jobs_fail_before_any_launch(self):
        for invalid in (0, 5, True, 1.5, "3"):
            with self.assertRaises(ValueError):
                run_local_jobs({"read": lambda: self.fail("must not execute")}, invalid)
        with self.assertRaises(ValueError):
            run_local_jobs({})

    def test_runner_only_enqueues_independent_local_jobs_before_write_barrier(self):
        import inspect
        text = inspect.getsource(runner.PatrolRunner.analyze_and_candidates)
        for name in ('"analysis"', '"mainRouting"', '"eRecovery"', '"scopeComparison"'):
            self.assertIn(name, text)
        self.assertIn('"localJobScheduling": scheduling', text)
        self.assertNotIn('pool.submit', text)
        self.assertNotIn('load_workbook', text)
        self.assertNotIn('set_carrier', text)
        self.assertNotIn('batchSetChatExpire', text)
        self.assertIn('STAGE_ORDER = ("document", "taskF", "standard", "taskB", "taskC", "taskD", "taskE", "expiredCardProbe", "taskG")',
                      Path(runner.__file__).read_text())

    def test_runner_four_jobs_join_before_coordinator_publishes_stage(self):
        import opensms_scope_comparison
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            value = object.__new__(runner.PatrolRunner)
            value.run_id = "20261003T190355+0800"
            value.args = SimpleNamespace(analysis_workers=4)
            value.receipts = folder
            active = folder / 'active.json'
            aliases = ('A1', 'A11', 'A12', 'A3', 'A21', 'B1', 'B11', 'B12',
                       'B1V', 'B11V', 'B12V', 'C2', 'E-T', 'E-C')
            active.write_text(json.dumps({'resolved': {a: {'name': a} for a in aliases}}))
            acquire = {'processingSource': str(folder / 'source.xlsx'), 'scanCache': str(folder / 'cache.pkl'),
                       'activeCarrierReceipt': str(active), 'exportReceipt': str(folder / 'export.json'),
                       'serverDate': '2026-10-03', 'balance': str(folder / 'balance.json')}
            value.reusable_stage = lambda *a: None
            value.copy_artifact = lambda path, *a: Path(path)
            barrier = threading.Barrier(4, timeout=3)
            finished = []

            def command(label, argv):
                if label == 'main_candidates':
                    path = folder / f'opensms_carrier_routing_{value.run_id}_manifest.json'
                    path.write_text('{}')
                    payload = {'manifest': str(path)}
                elif label == 'analysis':
                    path = folder / 'analysis.json'
                    path.write_text(json.dumps({'latestDate': '10-03'}))
                    payload = {'summary': str(path)}
                else:
                    self.assertEqual(label, 'e_recovery')
                    payload = {'groups': []}
                barrier.wait()
                finished.append(label)
                return runner.CommandResult(payload, '', '', .01)

            def scope(*args):
                self.assertEqual(tuple(args[:5]), (acquire['scanCache'], acquire['processingSource'],
                    acquire['exportReceipt'], value.run_id, acquire['serverDate']))
                barrier.wait()
                finished.append('scope')
                return {'verified': True}

            def publish(name, data, start):
                self.assertEqual(len(finished), 4)
                return data

            value.command, value.complete_stage = command, publish
            with patch.object(runner, 'SNAPSHOTS', folder), patch.object(runner, 'ARTIFACTS', folder), \
                    patch.object(opensms_scope_comparison, 'build', side_effect=scope):
                result = value.analyze_and_candidates(acquire)
            self.assertEqual(result['analysisConcurrency'], 4)
            self.assertEqual(len(result['localJobScheduling']['jobs']), 4)
            self.assertTrue(result['liveCarrierWritesRemainSerial'])
            self.assertEqual(json.loads(Path(result['analysis']).read_text())['scopeComparison'], {'verified': True})

    def test_benchmark_requires_bound_input_and_os_isolation(self):
        text = (ROOT / 'scripts/benchmark_opensms_local_jobs.py').read_text()
        self.assertIn("'/usr/bin/sandbox-exec'", text)
        self.assertIn('sandbox_profile(directory)', text)
        self.assertIn('isolated_environment(directory)', text)
        self.assertIn('inputsUnchanged=unchanged', text)
        self.assertIn("'scopeComparison': scope}", text)
        self.assertIn("if not file.name.startswith('._')", text)
        self.assertIn("('--scan-cache', '--out-dir', '--c2-daily-state-dir')", text)
        for entry in ('run_opensms_patrol.sh', 'run_opensms_export.sh', 'batchSetChatExpire', 'sqlite3'):
            self.assertNotIn(entry, text)

    def test_compact_output_preserves_chat_and_full_evidence(self):
        full = {"status": "success", "runId": "fixture", "serverDate": "2026-10-03", "overallReplyRatePct": 8.25,
                "currentZero": 0, "sourceWorkbook": "/fixture/source.xlsx",
                "finalWorkbook": "/fixture/final.xlsx", "report": "/fixture/report.md",
                "summaryPath": "/fixture/complete.json",
                "balanceGroups": [{"group": group, "percentRemaining": None}
                                  for group in runner.BALANCE_SUMMARY_GROUPS],
                "carrierReplyComparison": {"rows": [{"carrier": "x", "value": 1}] * 100,
                    "independentReadbackPassed": True, "carrierCount": 100},
                "scopeComparison": {"inputs": {"path": "large"}, "comparison": {"rows": [1] * 100}},
                "taskB": {"status": "passed", "candidateCount": 12, "actualWriteCount": 3,
                    "sourceCarrierCoverage": [{"name": "x"}] * 100},
                "taskE": {"status": "passed_with_not_found", "notWrittenCount": 42},
                "fDateImport": {"status": "submission_outcome_uncertain", "submitted": None},
                "taskF": {"status": "running", "background": True, "statusIsLaunchSnapshot": True},
                "performance": {"pipelineWallSecondsBeforeFinalManifest": 123}}
        original = copy.deepcopy(full)
        compact = runner.compact_launcher_result(full)
        self.assertEqual(full, original)
        self.assertEqual(render_summary(full), render_summary(compact))
        self.assertEqual(compact["taskE"]["notWrittenCount"], 42)
        self.assertIsNone(compact["fDateImport"]["submitted"])
        self.assertTrue(compact["taskF"]["statusIsLaunchSnapshot"])
        self.assertEqual(len(compact["balanceGroups"]), 12)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(full)))

    def test_failed_output_never_hides_uncertain_write_or_partial_file(self):
        full = {"status": "failed", "runId": "fixture", "state": "/fixture/state.json",
                "error": "identity_changed", "finalWorkbook": None,
                "latestVerifiedWorkbook": "/fixture/partial.xlsx",
                "taskDUncertainSubmittedCount": 20, "taskDAutomaticRetryBlocked": True,
                "taskEUncertainSubmittedCount": None, "taskEAutomaticRetryBlocked": True,
                "fDateImport": {"status": "submission_outcome_uncertain", "submitted": None,
                                "receipt": "/fixture/import.json"}}
        compact = runner.compact_launcher_result(full)
        self.assertEqual(compact["taskDUncertainSubmittedCount"], 20)
        self.assertIsNone(compact["taskEUncertainSubmittedCount"])
        self.assertTrue(compact["taskDAutomaticRetryBlocked"])
        self.assertEqual(compact['fDateImport'], full['fDateImport'])
        self.assertIsNone(compact["finalWorkbook"])
        self.assertEqual(compact["latestVerifiedWorkbook"], "/fixture/partial.xlsx")


if __name__ == "__main__":
    unittest.main()
