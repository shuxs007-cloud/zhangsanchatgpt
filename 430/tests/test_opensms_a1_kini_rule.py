from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from scripts.opensms_parallel_audit import (
    PASS, PathResolver, audit_a1_kini_candidate, check_reply_rate_send_gate,
)
from scripts.opensms_patrol_runner import PatrolRunner, TITLE_BY_ROUTE


ROOT = Path(__file__).resolve().parents[1]
ROUTE = "a1_kini_latest_lt4_to_b12"
A1 = "A1-精普-C120"
B12 = "B12-精-Te-120"


def candidate_fixture():
    return {
        "route": ROUTE,
        "a1KiniRuleVersion": "a1-kini-latest-send50-rate-lt4-to-b12-v1",
        "a1KiniReplyRateThresholdExclusive": 0.04,
        "replyRateLatestSendThresholdInclusive": 50,
        "replyRateWindow": "latest_day",
        "latestDate": "09-30",
        "sourceCarrierNames": [A1],
        "targetCarrierName": B12,
        "rows": [{"phone": "14160000001", "原运营商": A1, "opr": "302220 Kini",
                  "发送窗口数": 50, "回复窗口数": 1,
                  "latestDate": "09-30", "统计日期": "09-30"}],
    }


class A1KiniRuleTests(unittest.TestCase):
    def test_generation_boundaries_and_chatr_other_scopes_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            book = Workbook()
            sheet = book.active
            sheet.append(["电话", "运营商", "opr", "卡槽", "状态", "10日发送窗口数",
                          "09-30发送窗口数", "09-30回复窗口数",
                          "09-29发送窗口数", "09-29回复窗口数"])
            cases = [
                ("01", A1, "Kini", 49, 0),
                ("02", A1, "Kini", 50, 1),
                ("03", A1, "Kini", 50, 2),
                ("04", A1, "302220 kInI", 100, 3),
                ("05", A1, "Kini", 100, 4),
                ("06", A1, "Kini", 100, 5),
                ("07", A1, "Kini", 50, 0),
                ("08", A1, "Kini", 0, 0),
                ("09", A1, "Kini", 50, None),
                ("10", A1, "Kini", None, 0),
                ("11", A1, "Kini", 50, "bad"),
                ("12", A1, "Kini", 50, -1),
                ("13", A1, "Kini", 50, 0.5),
                ("14", A1, "Kini", 50, True),
                ("15", A1, "Kini", "NaN", 0),
                ("16", A1, "Kini", "Infinity", 0),
                ("17", A1, "Kini", 50, 51),
                ("18", A1, "Kini Koodo", 49, 0),
                ("19", A1, "Kini Chatr", 100, 1),
                ("20", A1, "Chatr", 100, 3),
                ("21", A1, "Chatr", 100, 4),
                ("22", A1, "Chatr", 49, 0),
                ("23", A1, "Chatr", 50, 0),
                ("24", A1, "Koodo", 0, 0),
                ("25", A1, "Telus1", 0, 0),
                ("26", "A11-精普-C50", "Kini", 100, 3),
                ("27", "A12-精普-C120", "Kini", 100, 4),
                ("28", "A11-精普-C50", "Chatr", 100, 4),
                ("29", A1, "Kini", "50", "1"),
            ]
            for suffix, source, opr, send, reply in cases:
                # Yesterday intentionally contradicts today's low/high result.
                sheet.append([f"141600000{suffix}", source, opr, f"101_{suffix}", "上架", 0,
                              send, reply, 100, 90 if suffix in {"02", "04", "07", "29"} else 0])
            source = root / "fresh.xlsx"
            book.save(source)
            book.close()
            result = subprocess.run(
                [sys.executable, str(ROOT / "opensms_generate_opr_zero_candidates.py"), str(source),
                 "--out-dir", str(root / "output"), "--now-bj", "2026-10-01T22:00:00+08:00"],
                cwd=ROOT, capture_output=True, text=True, check=True,
            )
            manifest = json.loads(result.stdout)
            candidates = {route: json.loads(Path(info["path"]).read_text())
                          for route, info in manifest["outputs"].items()}
            phones = lambda route: [row["phone"] for row in candidates[route]["rows"]]
            self.assertEqual(["14160000002", "14160000004", "14160000007", "14160000029"], phones(ROUTE))
            self.assertEqual(B12, candidates[ROUTE]["targetCarrierName"])
            self.assertEqual([A1], candidates[ROUTE]["sourceCarrierNames"])
            self.assertEqual(["14160000020", "14160000026"], phones("a_zone_lt4_to_a12"))
            self.assertEqual(["14160000027", "14160000028"], phones("a_zone_gte4_to_a1"))
            self.assertEqual(["14160000024", "14160000025"], phones("a_zone_kooda_to_b12"))
            self.assertEqual([], phones("chatr_to_e"))
            all_phones = [phone for route in candidates for phone in phones(route)]
            self.assertEqual(len(all_phones), len(set(all_phones)))
            self.assertEqual(ROUTE, manifest["routePriority"][0])
            self.assertIn(ROUTE, manifest["replyRateSendGateRoutes"])
            self.assertEqual(3, manifest["a1KiniHeldCounts"]["latest_send_lt50"])
            self.assertEqual(3, manifest["a1KiniHeldCounts"]["latest_rate_gte4"])
            self.assertEqual(9, manifest["a1KiniHeldCounts"]["invalid_latest_metrics"])
            self.assertEqual(1, manifest["a1KiniHeldCounts"]["ambiguous_opr"])
            audit = check_reply_rate_send_gate(
                {"carrierRoutingManifest": manifest["manifest"]}, PathResolver(root / "config.json", None),
            )
            self.assertEqual(PASS, audit.status, audit)
            self.assertEqual([], audit_a1_kini_candidate(candidates[ROUTE]))
            self.assertIn(ROUTE, TITLE_BY_ROUTE)
            specs = PatrolRunner.candidate_specs(None, {"routingManifest": manifest["manifest"]})
            self.assertEqual(ROUTE, specs[0]["route"])
            self.assertEqual(B12, specs[0]["target"])
            # Feed the actual generated candidate into the pure JS write guard.
            checked = subprocess.run([
                "node", "--input-type=module", "-e",
                "import fs from 'node:fs'; import {enforceA1KiniRule} from './opensms_set_carrier_candidates.mjs';"
                " const c=JSON.parse(fs.readFileSync(process.argv[1])); enforceA1KiniRule(c,c.targetCarrierName);",
                manifest["outputs"][ROUTE]["path"],
            ], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(0, checked.returncode, checked.stderr)

    def test_submit_guard_boundaries_targets_old_candidates_and_live_opr(self):
        code = """
import assert from 'node:assert/strict';
import {enforceA1KiniRule as gate} from './opensms_set_carrier_candidates.mjs';
const base=JSON.parse(process.argv[1]);
const copy=()=>structuredClone(base);
gate(base,base.targetCarrierName);
for(const [send,reply] of [[50,0],[50,1],[100,3],[1000,39]]){
  const c=copy(); Object.assign(c.rows[0],{'发送窗口数':send,'回复窗口数':reply}); gate(c,c.targetCarrierName);
}
for(const [send,reply] of [[49,0],[50,2],[100,4],[1000,40],[50,51],[50,null],[50,''],
  [50,'bad'],[50,true],[50,0.5],[50,-1],[NaN,0],[Infinity,0]]){
  const c=copy(); Object.assign(c.rows[0],{'发送窗口数':send,'回复窗口数':reply});
  assert.throws(()=>gate(c,c.targetCarrierName),/A1＋Kini/);
}
for(const change of [{a1KiniRuleVersion:undefined},{a1KiniReplyRateThresholdExclusive:0.05},
  {replyRateLatestSendThresholdInclusive:49},{replyRateWindow:'yesterday'},
  {sourceCarrierNames:['A11-精普-C50']},{targetCarrierName:'B12V-精-Te-120'}]){
  const c={...copy(),...change}; assert.throws(()=>gate(c,c.targetCarrierName),/A1＋Kini/);
}
for(const change of [{opr:'Chatr'},{opr:'Kini Chatr'},{opr:'Kinix'},
  {'原运营商':'A11-精普-C50'},{latestDate:'09-29'},{'统计日期':'09-29'}]){
  const c=copy(); Object.assign(c.rows[0],change); assert.throws(()=>gate(c,c.targetCarrierName),/A1＋Kini/);
}
const old={...copy(),route:'a_zone_lt4_to_a12',targetCarrierName:'A12-精普-C120'};
assert.throws(()=>gate(old,old.targetCarrierName),/拒绝旧候选/);
const chatr=structuredClone(old); chatr.rows[0].opr='Chatr'; gate(chatr,chatr.targetCarrierName);
const other=structuredClone(old); other.rows[0]['原运营商']='A11-精普-C50'; gate(other,other.targetCarrierName);
const forced={...copy(),task:'C',route:'task_c_kooda_4day_reply_rate_lt5_to_swap'}; gate(forced,'换号区T');
const live=[{cardNo:'14160000001',id:1,opr:'302220 Kini'}];
gate(base,base.targetCarrierName,live);
for(const rows of [[],[...live,...live],[{...live[0],opr:'Chatr'}],[{...live[0],opr:'Kini Chatr'}]])
  assert.throws(()=>gate(base,base.targetCarrierName,rows),/写前后台/);
assert.throws(()=>gate(chatr,chatr.targetCarrierName,live),/拒绝旧候选/);
"""
        result = subprocess.run(["node", "--input-type=module", "-e", code,
                                 json.dumps(candidate_fixture(), ensure_ascii=False)],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_independent_audit_rejects_tampering(self):
        original = candidate_fixture()
        self.assertEqual([], audit_a1_kini_candidate(original))
        for change in ({"发送窗口数": 49}, {"回复窗口数": 2}, {"回复窗口数": None},
                       {"回复窗口数": True}, {"opr": "Chatr"}, {"opr": "Kini Chatr"},
                       {"原运营商": "A11-精普-C50"}, {"统计日期": "09-29"}):
            with self.subTest(change=change):
                candidate = copy.deepcopy(original)
                candidate["rows"][0].update(change)
                self.assertTrue(audit_a1_kini_candidate(candidate))
        for change in ({"a1KiniRuleVersion": None}, {"targetCarrierName": "B12V-精-Te-120"},
                       {"route": "a_zone_lt4_to_a12"}):
            candidate = {**copy.deepcopy(original), **change}
            self.assertTrue(audit_a1_kini_candidate(candidate))

    def test_stale_or_invalid_candidates_stop_before_browser_access(self):
        env = os.environ.copy()
        env.update({"BITBROWSER_CDP": "http://127.0.0.1:1", "OPENSMS_EXACT_CDP_ONLY": "1",
                    "OPENSMS_FORCE_RAW_CDP": "1", "OPENSMS_RAW_API_TARGET_ID": "offline-kini-test"})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "candidate.json"
            for change in ({"route": "a_zone_lt4_to_a12", "targetCarrierName": "A12-精普-C120"},
                           {"a1KiniRuleVersion": None}):
                candidate = {**candidate_fixture(), **change}
                path.write_text(json.dumps(candidate, ensure_ascii=False))
                result = subprocess.run(["node", str(ROOT / "opensms_set_carrier_candidates.mjs"),
                                         str(path), candidate["targetCarrierName"], "offline-kini-test"],
                                        cwd=ROOT, env=env, capture_output=True, text=True)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("A1＋Kini", result.stderr)
                self.assertNotIn("ECONNREFUSED", result.stderr)


if __name__ == "__main__":
    unittest.main()
