from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "opensms_set_carrier_candidates.mjs"


def isolated_backend_env() -> dict[str, str]:
    env = os.environ.copy()
    # A deliberately invalid port alone is insufficient: production discovery
    # otherwise falls back to an active BitBrowser profile.
    env["BITBROWSER_CDP"] = "http://127.0.0.1:1"
    env["OPENSMS_EXACT_CDP_ONLY"] = "1"
    env["OPENSMS_FORCE_RAW_CDP"] = "1"
    env["OPENSMS_RAW_API_TARGET_ID"] = "offline-carrier-guard-test"
    return env


class ReplyRateSendGateTests(unittest.TestCase):
    def test_task_c_ten_day_gate_rejects_stale_and_invalid_values_offline(self) -> None:
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", """
import assert from 'node:assert/strict';
import {enforceTaskCTenDaySendGate as gate} from './opensms_set_carrier_candidates.mjs';
const base = {tenDaySendThresholdExclusive:360, tenDaySendMetric:'10日发送窗口数'};
for (const value of [359,360,undefined,null,'','bad',NaN,Infinity,-Infinity,true,[],{},-1]) {
  assert.throws(()=>gate({...base, rows:[{'10日发送窗口数':value}]}), /10日发送窗口数>360/);
}
for (const value of [361,'361','1,000']) gate({...base, rows:[{'10日发送窗口数':value}]});
for (const override of [{tenDaySendThresholdExclusive:undefined},{tenDaySendThresholdExclusive:359},
                       {tenDaySendThresholdExclusive:'360'},{tenDaySendMetric:'4日发送窗口数'}]) {
  assert.throws(()=>gate({...base,...override, rows:[]}), /门禁/);
}
gate({...base, rows:[]});
assert.throws(()=>gate({...base,rows:[{'10日发送窗口数':361},{'10日发送窗口数':360}]}), /1个号码/);
"""], cwd=ROOT, capture_output=True, text=True, check=False, env=isolated_backend_env(),
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_task_c_ten_day_gate_runs_before_backend_access_for_both_routes(self) -> None:
        for route, target, source in [
            ("task_c_kooda_4day_reply_rate_lt5_to_swap", "换号区T", "B1V-测试"),
            ("task_c_chatr_unchanged_4day_reply_rate_lt4_to_swap", "换号区C", "A3-测试"),
        ]:
            with self.subTest(route=route), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "candidate.json"
                candidate = {
                    "task": "C", "forceGate": True, "taskBPriorityLockEnforced": True,
                    "serverDate": "2026-09-02", "tomorrowDate": "2026-09-03",
                    "route": route, "targetCarrierName": target,
                    "replyRateSendThresholdInclusive": 50,
                    "tenDaySendThresholdExclusive": 360, "tenDaySendMetric": "10日发送窗口数",
                    "sourceCarrierNames": [source],
                    "rows": [{"电话": "14160000009", "运营商": source,
                              "4日发送窗口数": 50, "10日发送窗口数": 360}],
                }
                env = isolated_backend_env()
                env["OPENSMS_FORCE_TASK_C"] = "1"
                for legacy in (False, True):
                    if legacy:
                        candidate.pop("tenDaySendThresholdExclusive")
                        candidate["rows"][0]["10日发送窗口数"] = 1000
                    path.write_text(json.dumps(candidate, ensure_ascii=False))
                    completed = subprocess.run(
                        ["node", str(SCRIPT), str(path), target, "task_c_ten_day_guard_test"],
                        cwd=ROOT, capture_output=True, text=True, check=False, env=env,
                    )
                    self.assertNotEqual(0, completed.returncode)
                    self.assertIn("10日发送窗口数>360", completed.stderr)

    def run_candidate(self, candidate: dict, target: str = "A12") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "candidate.json"
            path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
            return subprocess.run(
                ["node", str(SCRIPT), str(path), target, "send_gate_test"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=isolated_backend_env(),
            )

    def test_rejects_send_below_50_before_opening_backend_session(self) -> None:
        completed = self.run_candidate({
            "route": "a_zone_lt4_to_a12",
            "replyRateLatestSendThresholdInclusive": 50,
            "sourceCarrierNames": ["A1"],
            "targetCarrierName": "A12",
            "rows": [{"电话": "14160000001", "原运营商": "A1", "发送窗口数": 49}],
        })
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("未满足对应统计窗口发送>=50", completed.stderr)

    def test_rest_zone_entry_rejects_nonempty_candidate_when_disabled(self) -> None:
        completed = self.run_candidate({
            "route": "b1_b11_b12_today_gte50_zero_to_e",
            "restZoneEntryEnabled": False,
            "sourceCarrierNames": ["B1-测试"],
            "targetCarrierName": "E-T休息区",
            "rows": [{
                "电话": "14160000030",
                "原运营商": "B1-测试",
                "发送窗口数": 50,
                "回复窗口数": 0,
            }],
        }, "E-T休息区")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("休息区转入功能已关闭", completed.stderr)

    def test_disabled_rest_zone_gate_blocks_all_entries_and_preserves_exit(self) -> None:
        # Import a pure guard only. No main(), browser, API session or writes.
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", """
import assert from 'node:assert/strict';
import { enforceRestZoneEntryGate as gate } from './opensms_set_carrier_candidates.mjs';
const base = {restZoneEntryEnabled:true, route:'b1_b11_b12_today_gte50_zero_to_e',
  targetCarrierName:'E-T休息区', rows:[{'原运营商':'B1-测试', '发送窗口数':50, '回复窗口数':0}]};
for (const source of ['B1','B11','B12']) {
  assert.throws(()=>gate({...base,rows:[{...base.rows[0],'原运营商':source+'-测试'}]}, 'E-T休息区'), /休息区转入功能已关闭/);
  assert.throws(()=>gate({...base,restZoneEntryEnabled:false,rows:[{...base.rows[0],'原运营商':source+'-测试'}]}, 'E-T休息区'), /休息区转入功能已关闭/);
}
for (const source of ['B1V','B11V','B12V']) {
  assert.throws(()=>gate({...base,rows:[{...base.rows[0],'原运营商':source+'-测试'}]}, 'E-T休息区'), /休息区转入功能已关闭/);
}
for (const change of [{'发送窗口数':49},{'回复窗口数':1},{'回复窗口数':null},
  {'回复窗口数':''},{'原运营商':'B14-T快下保留卡'},{'原运营商':'A21-测试'}]) {
  assert.throws(()=>gate({...base, rows:[{...base.rows[0],...change}]},'E-T休息区'));
}
const chatr = {...base,route:'chatr_to_e',targetCarrierName:'E-C休息区'};
for (const source of ['A1','A11','A12','A3']) {
  assert.throws(()=>gate({...chatr,rows:[{...base.rows[0],'原运营商':source+'-测试',opr:'302720 Chatr'}]},'E-C休息区'), /休息区转入功能已关闭/);
}
assert.throws(()=>gate({...chatr,rows:[{...base.rows[0],'原运营商':'A1-测试',opr:'Fido'}]},'E-C休息区'));
assert.throws(()=>gate(base,'E-C休息区'));
assert.throws(()=>gate({...base,route:'unknown'},'E-T休息区'));
assert.throws(()=>gate({...base,restZoneEntryEnabled:false},'E-T休息区'));
assert.throws(()=>gate({...base,rows:[]},'E-T休息区'), /开关与当前程序不一致/);
gate({...base,restZoneEntryEnabled:false,rows:[]},'E-T休息区');
gate({route:'e_restore_a1',targetCarrierName:'A1-测试',rows:[{'原运营商':'E-C休息区'}]},'A1-测试');
"""], cwd=ROOT, capture_output=True, text=True, check=False, env=isolated_backend_env(),
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_task_c_a_b_source_rejects_four_day_send_below_50(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "candidate.json"
            path.write_text(json.dumps({
                "task": "C",
                "forceGate": True,
                "taskBPriorityLockEnforced": True,
                "serverDate": "2026-09-02",
                "tomorrowDate": "2026-09-03",
                "route": "task_c_kooda_4day_reply_rate_lt5_to_swap",
                "replyRateSendThresholdInclusive": 50,
                "sourceCarrierNames": ["B1-测试"],
                "targetCarrierName": "换号区T",
                "rows": [{"电话": "14160000003", "原运营商": "B1-测试", "4日发送窗口数": 49, "过期状态": "", "截止日": ""}],
            }, ensure_ascii=False), encoding="utf-8")
            env = isolated_backend_env()
            env["OPENSMS_FORCE_TASK_C"] = "1"
            completed = subprocess.run(
                ["node", str(SCRIPT), str(path), "换号区T", "task_c_send_gate_test"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("未满足对应统计窗口发送>=50", completed.stderr)

    def test_task_c_b_v_source_rejects_four_day_send_below_50(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "candidate.json"
            path.write_text(json.dumps({
                "task": "C",
                "forceGate": True,
                "taskBPriorityLockEnforced": True,
                "serverDate": "2026-09-02",
                "tomorrowDate": "2026-09-03",
                "route": "task_c_kooda_4day_reply_rate_lt5_to_swap",
                "replyRateSendThresholdInclusive": 50,
                "sourceCarrierNames": ["B1V-测试"],
                "targetCarrierName": "换号区T",
                "rows": [{"电话": "14160000005", "原运营商": "B1V-测试", "4日发送窗口数": 49, "过期状态": "", "截止日": ""}],
            }, ensure_ascii=False), encoding="utf-8")
            env = isolated_backend_env()
            env["OPENSMS_FORCE_TASK_C"] = "1"
            completed = subprocess.run(
                ["node", str(SCRIPT), str(path), "换号区T", "task_c_bv_send_gate_test"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("未满足对应统计窗口发送>=50", completed.stderr)

    def test_b1_b11_ten_day_route_is_not_given_reply_rate_gate(self) -> None:
        completed = self.run_candidate({
            "route": "b1_b11_gte240_to_b12",
            "tenDaySendThreshold": 240,
            "sourceCarrierNames": ["A2"],
            "targetCarrierName": "B12",
            "rows": [{
                "电话": "14160000002",
                "原运营商": "A2",
                "发送窗口数": 1,
                "回复窗口数": 1,
                "10日发送窗口数": 240,
            }],
        }, "B12")
        self.assertNotEqual(0, completed.returncode)
        self.assertNotIn("回复型候选缺少", completed.stderr)
        self.assertIn("B标准组固定优先级候选校验失败", completed.stderr)
        self.assertIn("invalid_source", completed.stderr)

    def test_b1_use2_lt240_route_rejects_240_boundary(self) -> None:
        completed = self.run_candidate({
            "route": "b1_b11_use2_lt240_to_b11",
            "tenDaySendThreshold": 240,
            "useDaysFieldEquals": "2天",
            "sourceCarrierNames": ["B1-测试"],
            "targetCarrierName": "B11-测试",
            "rows": [{
                "电话": "14160000021",
                "原运营商": "B1-测试",
                "opr": "Koodo",
                "koo是否2天": "2天",
                "10日发送窗口数": 240,
                "发送窗口数": 49,
                "回复窗口数": 1,
            }],
        }, "B11-测试")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("B标准组固定优先级候选校验失败", completed.stderr)
        self.assertIn("ten_day_not_lt_240", completed.stderr)

    def test_b1_gte240_route_rejects_239_boundary(self) -> None:
        completed = self.run_candidate({
            "route": "b1_b11_gte240_to_b12",
            "tenDaySendThreshold": 240,
            "sourceCarrierNames": ["B1-测试"],
            "targetCarrierName": "B12-测试",
            "rows": [{
                "电话": "14160000022",
                "原运营商": "B1-测试",
                "opr": "Telus1",
                "koo是否2天": "",
                "10日发送窗口数": 239,
                "发送窗口数": 49,
                "回复窗口数": 1,
            }],
        }, "B12-测试")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("B标准组固定优先级候选校验失败", completed.stderr)
        self.assertIn("ten_day_not_gte_240", completed.stderr)

    def test_b1v_use2_route_requires_exact_use_days_field(self) -> None:
        completed = self.run_candidate({
            "route": "b1v_b11v_use2_lt240_to_b11v",
            "vZonePriority": 2,
            "useDaysFieldEquals": "2天",
            "sourceCarrierNames": ["B1V-测试"],
            "targetCarrierName": "B11V-测试",
            "rows": [{
                "电话": "14160000006",
                "原运营商": "B1V-测试",
                "koo是否2天": "",
                "10日发送窗口数": 239,
                "发送窗口数": 49,
                "回复窗口数": 0,
            }],
        }, "B11V-测试")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("V组固定优先级候选校验失败", completed.stderr)
        self.assertIn("use_days_not_exactly_2", completed.stderr)

    def test_b1v_use2_route_rejects_row_that_should_have_matched_priority_1(self) -> None:
        completed = self.run_candidate({
            "route": "b1v_b11v_use2_lt240_to_b11v",
            "vZonePriority": 2,
            "useDaysFieldEquals": "2天",
            "sourceCarrierNames": ["B1V-测试"],
            "targetCarrierName": "B11V-测试",
            "rows": [{
                "电话": "14160000007",
                "原运营商": "B1V-测试",
                "koo是否2天": "2天",
                "10日发送窗口数": 239,
                "发送窗口数": 50,
                "回复窗口数": 0,
            }],
        }, "B11V-测试")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("priority_1_should_have_won", completed.stderr)

    def test_b1v_gte240_route_rejects_239_boundary(self) -> None:
        completed = self.run_candidate({
            "route": "b1v_b11v_gte240_to_b12v",
            "vZonePriority": 3,
            "sourceCarrierNames": ["B11V-测试"],
            "targetCarrierName": "B12V-测试",
            "rows": [{
                "电话": "14160000008",
                "原运营商": "B11V-测试",
                "koo是否2天": "2天",
                "10日发送窗口数": 239,
                "发送窗口数": 49,
                "回复窗口数": 0,
            }],
        }, "B12V-测试")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("ten_day_not_gte_240", completed.stderr)

    def test_authorized_task_b_does_not_reject_protected_source_before_backend(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "candidate.json"
            path.write_text(json.dumps({
                "task": "B",
                "forceGate": True,
                "route": "task_b_expired_to_offline_t",
                "serverDate": "2026-09-14",
                "tomorrowDate": "2026-09-15",
                "targetCarrierName": "过期下架卡T",
                "sourceCarrierNames": ["A21-股票-T120"],
                "rows": [{"电话": "14160000004", "运营商": "A21-股票-T120", "截止日": "2026-09-14", "过期状态": "已过期"}],
            }, ensure_ascii=False), encoding="utf-8")
            env = isolated_backend_env()
            env["OPENSMS_FORCE_TASK_B"] = "1"
            env["BITBROWSER_CDP"] = "http://127.0.0.1:1"
            command = ["node", str(SCRIPT), str(path), "过期下架卡T", "task_b_protected_source_test"]
            try:
                completed = subprocess.run(
                    command, cwd=ROOT, capture_output=True, text=True,
                    check=False, env=env, timeout=3,
                )
            except subprocess.TimeoutExpired as error:
                # A real job may own the shared lock while this offline test
                # runs. run() terminates only this test child, which is still
                # waiting before backend access; never touch the real owner.
                stderr = error.stderr or ""
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                self.assertIn("another task owns this browser session; waiting", stderr)
                self.assertNotIn("号码运营商受保护", stderr)
                return
            self.assertNotIn("号码运营商受保护", completed.stderr)
            self.assertNotEqual(0, completed.returncode)
            self.assertRegex(completed.stderr, "无法定位430当前OpenSMS页面|OPENSMS_SITE_LOCK_UNAVAILABLE")
            if "OPENSMS_SITE_LOCK_UNAVAILABLE" in completed.stderr:
                # The offline OS sandbox rejects the production lock path.
                # This is a pre-network guard, not an auth/OPR rejection.
                self.assertIn("未联网", completed.stderr)
            else:
                self.assertIn("不代表登录失效", completed.stderr)
                self.assertIn("http://127.0.0.1:1", completed.stderr)

    def test_task_d_rejects_missing_current_user_force_gate_before_backend(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            retention = root / "retention.json"
            retention.write_text(json.dumps({
                "sourceProvenance": "phone_card_retain_all_pages",
                "cardNos": ["4160000010"],
            }, ensure_ascii=False), encoding="utf-8")
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps({
                "task": "D",
                "forceGate": True,
                "automationGate": True,
                "sourceProvenance": "phone_card_retain_all_pages",
                "retentionReceiptPath": str(retention),
                "route": "task_d_retain_chatr_to_b14_c",
                "targetCarrierName": "B14-C快下保留卡",
                "sourceCarrierNames": ["A21-测试"],
                "rows": [{"电话": "14160000010", "运营商": "A21-测试"}],
            }, ensure_ascii=False), encoding="utf-8")
            env = isolated_backend_env()
            env["OPENSMS_ENABLE_TASK_D"] = "1"
            env["OPENSMS_EXECUTE_TASK_D"] = "1"
            env.pop("OPENSMS_FORCE_TASK_D", None)
            completed = subprocess.run(
                ["node", str(SCRIPT), str(candidate), "B14-C快下保留卡", "task_d_force_gate_test"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("转入B14保留区工作已删除", completed.stderr)


if __name__ == "__main__":
    unittest.main()
