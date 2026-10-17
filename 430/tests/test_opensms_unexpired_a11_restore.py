import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from opensms_unexpired_a11_restore import select_rows, build_candidate
from opensms_generate_task_b_candidates import sha256
from opensms_patrol_runner import build_parser, validate_authorization, PatrolError

NODE = "/Users/zhangsan/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"


class RestoreTests(unittest.TestCase):
    def workbook(self, root):
        file = root / "test-run.xlsx"
        book = Workbook()
        book.active.title = "sheet1"
        book.active.append(["电话", "运营商", "截止日", "卡槽", "ICCID"])
        carriers = ["快过期隔离区C"] * 6 + ["过期下架卡T", "B14-C快下保留卡", "三方隔离", "快过期续费C", "A12-test"]
        dates = ["2026-09-13", "2026-09-14", "2026-09-15", "2026/9/22", "2026-10-01", "invalid"] + ["2026-10-01"] * 5
        for i, (carrier, cutoff) in enumerate(zip(carriers, dates)):
            book.active.append([str(12340000000+i), carrier, cutoff, f"137_{i}.01", str(8912230200250000000+i)+"F"])
        book.save(file)
        book.close()
        return file

    def test_exact_scope_and_dates(self):
        with tempfile.TemporaryDirectory() as d:
            selected, excluded = select_rows(self.workbook(Path(d)), "2026-09-14")
            self.assertEqual([1, 8, 17, 17], [r["距截止日天数"] for r in selected])
            self.assertEqual(3, len(excluded))
            self.assertEqual({"快过期隔离区C", "过期下架卡T"}, {r["原运营商"] for r in selected})

    def test_authorization_default_and_mixed_mode(self):
        parser = build_parser()
        self.assertFalse(parser.parse_args(["run"]).restore_unexpired_to_a11)
        args = ["run", "--restore-unexpired-to-a11", "--authorization-source", "current-user"]
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PatrolError):
                validate_authorization(parser.parse_args(args))
        with patch.dict(os.environ, {"OPENSMS_ALLOW_UNEXPIRED_A11_RESTORE": "1"}):
            auth = validate_authorization(parser.parse_args(args))
            self.assertFalse(any(auth["task"+t] for t in "BCDEF"))
            for extra in (["--force-task-b"], ["--resume-state", "old.json"], ["--task-a-only"]):
                with self.assertRaises(PatrolError):
                    validate_authorization(parser.parse_args(args+extra))

    def test_current_proof_and_live_identity_guard(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = self.workbook(root)
            receipt = root / "test-run-doc.json"
            receipt.write_text(json.dumps({"result": "成功", "failed_checks": [], "verification": {
                "date_resolution_review": {"matches": True, "source_export": {"sha256": sha256(source)}}}}))
            dates = []
            for route in ("day", "phoneConfig"):
                p = root / f"test-run-{route}.json"
                p.write_text(json.dumps({"checkedAt": datetime.now(timezone.utc).isoformat(),
                    "serverDate": "2026-09-14", "requestedPath": "/"+route,
                    "silentBrowserMode": True, "browserForegroundActivationAttempted": False}))
                dates.append(p)
            candidate = root / "test-run-candidate.json"
            build_candidate({"cardResult": str(source), "cardReceipt": str(receipt)},
                {"sourceArchive": str(source), "sourceSha256": sha256(source), "serverDate": "2026-09-14",
                 "serverDateVerified": True}, "test-run", "A11-test", dates, candidate)
            program = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {enforceUnexpiredRestore, validateUnexpiredLiveIdentity} from './scripts/opensms_unexpired_a11_guard.mjs';
const c = JSON.parse(fs.readFileSync(process.argv[1]));
process.env.OPENSMS_RUN_ID = 'test-run';
process.env.OPENSMS_ALLOW_UNEXPIRED_A11_RESTORE = '1';
await enforceUnexpiredRestore(c, 'A11-test');
for (const bad of [{...c, serverDate:'2026-10-01'}, {...c, authorizationSource:'timer'},
  {...c, rows:[c.rows[0], c.rows[0]], count:2}, {...c, sourceSha256:'forged'},
  {...c, targetCarrierName:'A12-test'}, {...c, serverDateReceipts:c.serverDateReceipts.slice(0,1)}]) {
  await assert.rejects(enforceUnexpiredRestore(bad,'A11-test'));
}
delete process.env.OPENSMS_ALLOW_UNEXPIRED_A11_RESTORE;
await assert.rejects(enforceUnexpiredRestore(c,'A11-test'));
const target={name:'A11-test', identif:'target'};
const carriers=[target, ...c.sourceCarrierNames.map(name=>({name,identif:name}))];
const live=c.rows.map((r,i)=>({id:i+1,cardNo:r.电话.slice(-10),cardStore:r.ICCID.replace(/F$/,''),slotIdentif:r.卡槽,carrireIdentif:r.原运营商}));
validateUnexpiredLiveIdentity(c,live,carriers,target);
for (const field of ['cardStore','slotIdentif','carrireIdentif']) {
  assert.throws(()=>validateUnexpiredLiveIdentity(c,[{...live[0],[field]:'changed'},...live.slice(1)],carriers,target));
}
assert.throws(()=>validateUnexpiredLiveIdentity(c,live.slice(1),carriers,target));
assert.throws(()=>validateUnexpiredLiveIdentity(c,[...live,live[0]],carriers,target));
assert.throws(()=>validateUnexpiredLiveIdentity(c,live,carriers,{...target,delData:true}));
'''
            subprocess.run([NODE, "--input-type=module", "-e", program, str(candidate)], cwd=ROOT, check=True)


if __name__ == "__main__":
    unittest.main()
