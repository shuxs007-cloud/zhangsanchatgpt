"""Offline all-source Task B coverage and runner preflight; no website access."""
from contextlib import redirect_stdout
from datetime import date, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import opensms_generate_task_b_candidates as generator
import opensms_parallel_audit as audit
import opensms_patrol_runner as runner


class TaskBAllCarriersTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="opensms-b-all-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.xlsx"
        self.tag = "offline"
        self.day = date(2026, 10, 2)
        self.carriers = ["C2-测试区1-C120", "B1V-精-Te-120", "B14-C快下保留卡",
                         "B14-T快下保留卡", "A11-精普-C120", "A21-股票-C120",
                         "A2-股票-T120", "快过期续费C", "快过期续费T", "三方隔离",
                         "其它来源", "B1", "B11", "B12", "B11V", "B12V"]
        self.rows = []
        for carrier in self.carriers:
            for delta in (-1, 0, 1, 2, 6, 7):
                for opr in ("Chatr", "Kooda", "Koodo", "Kini", "Telus1", "Lucky"):
                    index = len(self.rows)
                    self.rows.append([str(14160000000 + index), carrier, opr,
                                      f"101_{index}", "", (self.day + timedelta(days=delta)).isoformat()])
        self.save_source()

    def save_source(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "sheet1"
        sheet.append(["电话", "运营商", "opr", "卡槽", "过期状态", "截止日"])
        for row in self.rows:
            sheet.append(row)
        book.save(self.source)
        book.close()

    def generate(self, force="1"):
        args = ["generator", str(self.source), "--server-date", self.day.isoformat(),
                "--tag", self.tag, "--output-dir", str(self.root)]
        with patch.object(sys, "argv", args), patch.dict(os.environ, {"OPENSMS_FORCE_TASK_B": force}), redirect_stdout(io.StringIO()) as out:
            generator.main()
        self.manifest = json.loads(out.getvalue())
        return self.manifest

    def review(self):
        resolver = audit.PathResolver(self.root / "config.json", self.root)
        return audit.review_task_b_source_coverage(
            {"serverDate": self.day.isoformat()}, self.manifest,
            [{"candidate": path} for path in self.manifest["candidatePaths"]], resolver)

    def test_all_carriers_opr_families_and_exact_date_boundaries(self):
        source_sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        manifest = self.generate()
        self.assertEqual(source_sha, hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual("all_carriers_v1", manifest["sourceCarrierScope"])
        self.assertEqual([], manifest["sourceCarrierExclusions"])
        self.assertTrue(manifest["sourceCarrierCoveragePassed"])
        self.assertEqual(0, manifest["retainedSourceExcludedCount"])
        self.assertEqual(0, manifest["neverProcessSourceExcludedCount"])
        self.assertEqual(0, manifest["isolationCarrierHeldCount"])
        self.assertEqual({"C": 32, "T": 160}, manifest["expiredCandidateCounts"])
        self.assertEqual({"C": 16, "T": 80}, manifest["tomorrowCandidateCounts"])
        self.assertEqual(sorted(self.carriers), [r["sourceCarrierName"] for r in manifest["sourceCarrierCoverage"]])
        for row in manifest["sourceCarrierCoverage"]:
            self.assertEqual({"sourceRowCount": 36, "expiredSourceCount": 12,
                              "expiredCandidateCount": 12, "tomorrowSourceCount": 6,
                              "tomorrowCandidateCount": 6, "expiredDataAnomalyCount": 0,
                              "tomorrowDataAnomalyCount": 0}, {k: v for k, v in row.items() if k != "sourceCarrierName"})
        errors, details = self.review()
        self.assertEqual([], errors)
        self.assertTrue(details["source_coverage_independently_verified"])
        self.assertEqual(manifest["sourceCarrierCoverage"], details["source_carrier_coverage"])

    def test_forged_global_and_carrier_counts_cannot_hide_a_dropped_number(self):
        manifest = self.generate()
        path = Path(manifest["expiredCandidatePaths"]["C"])
        candidate = json.loads(path.read_text())
        dropped = candidate["rows"].pop()
        candidate["candidates"] = candidate["rows"]
        candidate["count"] -= 1
        candidate["candidateCount"] -= 1
        path.write_text(json.dumps(candidate))
        manifest["expiredCandidateCounts"]["C"] -= 1
        for key in ("expiredCandidateCount", "expiredClassifiedCandidateCount", "expiredNonProtectedSourceCount"):
            manifest[key] -= 1
        for row in manifest["sourceCarrierCoverage"]:
            if row["sourceCarrierName"] == dropped["运营商"]:
                row["expiredSourceCount"] -= 1
                row["expiredCandidateCount"] -= 1
        errors, _ = self.review()
        self.assertTrue(errors, "An independently reopened source must detect forged matching counters")

    def test_row_anomalies_skip_but_valid_rows_and_exact_coverage_continue(self):
        valid = list(self.rows[0])
        self.rows = [valid] + [valid.copy() for _ in range(3)]
        self.rows[1][0] = ''
        self.rows[2][0], self.rows[2][2] = '14160000999', 'unknown'
        self.rows[3][0], self.rows[3][5] = '14160000888', '2026-02-30'
        self.save_source()
        manifest = self.generate()
        self.assertEqual(3, manifest['dataAnomalySkippedCount'])
        self.assertEqual(1, manifest['expiredCandidateCount'])
        self.assertTrue(manifest['allDueCoveragePassed'])
        self.assertEqual([], self.review()[0])
        self.manifest['dataAnomalySkippedRows'][0]['reason'] = 'unmatched'
        self.assertTrue(self.review()[0])

    def test_ambiguous_opr_identity_still_stops_before_any_candidates(self):
        self.rows = [self.rows[0].copy()]
        self.rows[0][2] = 'Chatr Lucky'
        self.save_source()
        with self.assertRaises(ValueError):
            self.generate()
        self.assertEqual([], list(self.root.glob('*candidates.json')))

    def test_unconfigured_carriers_skip_without_hiding_configured_rows(self):
        valid = self.rows[0].copy()
        self.rows = [valid] + [[None, carrier, "unknown", "", "已过期", "bad-date"]
                               for carrier in (None, "", "  ")]
        self.save_source()
        manifest = self.generate()
        self.assertEqual("skip_no_business_write_v1", manifest["unconfiguredCarrierPolicy"])
        self.assertEqual([3, 4, 5], manifest["unconfiguredCarrierSkippedRows"])
        self.assertEqual(3, manifest["unconfiguredCarrierSkippedCount"])
        self.assertEqual(1, manifest["expiredCandidateCount"])
        self.assertEqual([], self.review()[0])
        obj = self.make_runner()
        obj.run_task_b({"cardResult": str(self.source)}, {"serverDate": self.day.isoformat()})
        self.assertEqual(4, obj.run_write.call_count)
        manifest["unconfiguredCarrierSkippedRows"] = [2, 3, 4, 5]
        manifest["unconfiguredCarrierSkippedCount"] = 4
        self.assertTrue(self.review()[0])

    def test_all_source_scope_does_not_grant_task_b_authorization(self):
        with self.assertRaises(RuntimeError):
            self.generate(force="0")
        self.assertEqual([], list(self.root.glob("*candidates.json")))

    def test_country_code_aliases_cannot_be_folded_across_date_branches(self):
        original = self.rows[0].copy()
        second = original.copy()
        second[0] = "+1 " + original[0][-10:]
        second[5] = (self.day + timedelta(days=1)).isoformat()
        self.rows = [original, second]
        self.save_source()
        with self.assertRaisesRegex(ValueError, "标准化后重复"):
            self.generate()
        self.assertEqual([], list(self.root.glob("*candidates.json")))

    def test_protected_source_audit_accepts_only_the_new_task_b_exception(self):
        manifest = self.generate()
        groups = [{"candidate": path} for path in manifest["candidatePaths"]]
        resolver = audit.PathResolver(self.root / "config.json", self.root)
        result = audit.check_protected_carrier_sources(groups, resolver)
        self.assertEqual(audit.PASS, result.status, result.message)
        self.assertEqual(0, result.details["never_process_source_hit_count"])
        self.assertEqual(0, result.details["task_b_retained_source_hit_count"])

    def test_report_and_chat_disclose_new_scope_without_claiming_unverified_coverage(self):
        # Reuse only its local-file fixture, never the document pipeline or its
        # integration tests. Every business metric below is synthetic.
        from test_opensms_no_google_audit_report import NoGoogleAuditReportTests, REPORT, write_json
        from opensms_chat_summary import render_summary
        fixture = NoGoogleAuditReportTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.generate()
        fixture.config.update({"taskBExecuted": True,
            "taskBManifest": str(self.root / "opensms_task_b_offline_manifest.json"),
            "offlineRecoveryEnabled": False, "runTime": "offline", "taskId": "offline",
            "taskDuration": "1s", "exportWorkflow": "synthetic offline fixture"})
        write_json(fixture.config_path, fixture.config)
        output = fixture.root / "report.md"
        with patch.object(sys, "argv", ["report", str(fixture.config_path), str(output)]), redirect_stdout(io.StringIO()):
            REPORT.main()
        text = output.read_text()
        self.assertIn("全部来源运营商纳入到期分流", text)
        self.assertIn("逐运营商覆盖核验：通过", text)
        self.assertNotIn("B14保留卡保护排除", text)
        task = {"status": "passed", "sourceCarrierScope": "all_carriers_v1", "sourceCarrierCoveragePassed": True}
        self.assertIn("逐运营商覆盖已核验", render_summary({"status": "success", "taskB": task}))
        task["sourceCarrierCoveragePassed"] = False
        self.assertIn("逐运营商覆盖未确认", render_summary({"status": "success", "taskB": task}))
        task["status"] = "skipped"
        self.assertNotIn("任务B覆盖全部来源运营商", render_summary({"status": "success", "taskB": task}))

    def make_runner(self):
        obj = runner.PatrolRunner.__new__(runner.PatrolRunner)
        obj.authorization = {"taskB": True}
        obj.args = SimpleNamespace(cdp="offline-only")
        obj.run_id = self.tag
        obj.receipts = self.root
        obj.reusable_stage = Mock(return_value=None)
        obj.command = Mock(side_effect=AssertionError("No generation or business process may start"))
        obj.run_write = Mock(return_value={"actualWrite": 0})
        obj.complete_stage = Mock()
        return obj

    def test_runner_independently_validates_all_four_groups_before_mock_writes(self):
        self.generate()
        obj = self.make_runner()
        result = obj.run_task_b({"cardResult": str(self.source)}, {"serverDate": self.day.isoformat()})
        self.assertEqual(4, len(result))
        self.assertEqual(4, obj.run_write.call_count)
        self.assertTrue(obj.complete_stage.call_args.args[1]["sourceCarrierCoveragePassed"])
        obj.command.assert_not_called()

    def test_runner_does_not_replay_old_scope_or_missing_candidate(self):
        manifest = self.generate()
        path = self.root / "opensms_task_b_offline_manifest.json"
        manifest["sourceCarrierScope"] = "legacy_protected_sources_v1"
        path.write_text(json.dumps(manifest))
        obj = self.make_runner()
        with self.assertRaisesRegex(runner.PatrolError, "旧来源保护范围"):
            obj.run_task_b({"cardResult": str(self.source)}, {"serverDate": self.day.isoformat()})
        obj.run_write.assert_not_called()
        manifest["sourceCarrierScope"] = "all_carriers_v1"
        path.write_text(json.dumps(manifest))
        Path(manifest["candidatePaths"][-1]).unlink()
        with self.assertRaisesRegex(runner.PatrolError, "写前独立全来源覆盖核验失败"):
            obj.run_task_b({"cardResult": str(self.source)}, {"serverDate": self.day.isoformat()})
        obj.run_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
