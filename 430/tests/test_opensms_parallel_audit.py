from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "opensms_parallel_audit.py"
SPEC = importlib.util.spec_from_file_location("opensms_parallel_audit", MODULE_PATH)
AUDIT = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)
AUDIT_RUNNER = MODULE_PATH.parent / "run_opensms_parallel_audit.sh"


def minimal_xlsx(path: Path, formula_error: bool = False, change_status_fourth: bool = True) -> None:
    headers = [
        "电话", "运营商", "过期状态", "换号状态", "koo是否2天", "激活后天数", "使用天数", "opr", "卡槽",
        "连续低于x%", "连续0回复", "开卡日期", "激活日期", "截止日", "机房",
        "10日回复率", "3日发送窗口数", "3日回复率", "4日发送窗口数", "4日回复率",
        "回复率持续降低", "08-19发送窗口数", "08-18发送窗口数", "08-17发送窗口数",
        "08-16发送窗口数",
    ]
    if not change_status_fourth:
        headers.remove("换号状态")
        headers.insert(headers.index("截止日") + 1, "换号状态")
    sheet_names = ["sheet1"] + sorted(AUDIT.REQUIRED_RESULT_SHEETS - {"sheet1"})
    sheet_names.remove("谷歌有-系统无")
    sheet_names.insert(sheet_names.index("系统无-谷歌表有") + 1, "谷歌有-系统无")
    rels = []
    sheets = []
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>")
        for index, name in enumerate(sheet_names, start=1):
            rels.append(f"<Relationship Id='rId{index}' Target='/xl/worksheets/sheet{index}.xml' Type='worksheet'/>")
            sheets.append(f"<sheet name='{name}' sheetId='{index}' r:id='rId{index}'/>")
            if name == "sheet1":
                cells = "".join(
                    f"<c r='{chr(65 + (column % 26))}1' t='inlineStr'><is><t>{value}</t></is></c>"
                    for column, value in enumerate(headers)
                )
                error_cell = "<c r='A2' t='e'><v>#REF!</v></c>" if formula_error else "<c r='A2'><v>1</v></c>"
                rows = f"<row r='1'>{cells}</row><row r='2'>{error_cell}</row>"
            else:
                rows = "<row r='1'><c r='A1' t='inlineStr'><is><t>检查</t></is></c></row>"
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                f"<worksheet xmlns='{AUDIT.MAIN_NS}'><sheetData>{rows}</sheetData></worksheet>",
            )
        archive.writestr(
            "xl/workbook.xml",
            f"<workbook xmlns='{AUDIT.MAIN_NS}' xmlns:r='{AUDIT.DOC_REL_NS}'><sheets>{''.join(sheets)}</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f"<Relationships xmlns='{AUDIT.PKG_REL_NS}'>{''.join(rels)}</Relationships>",
        )


class ParallelAuditTests(unittest.TestCase):
    def test_balance_audit_requires_all_twelve_groups_and_checks_their_values(self) -> None:
        groups = ["A1", "A12", "A11", "B1", "B11", "B12", "C2", "A3", "B3", "B1V", "B11V", "B12V"]
        data = {"requiredGroups": groups, "records": [
            {"group": group, "dailyRemaining": 9, "dailyTotal": 100, "percentRemaining": 9}
            for group in groups], "replyRates": [
            {"group": group, "status": "ok", "metric": "firstReplyRate", "statDate": "2026-09-18"}
            for group in groups], "autoQuota": {"enabled": False}}
        with tempfile.TemporaryDirectory() as folder:
            receipt = Path(folder) / "balance.json"
            receipt.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(AUDIT.PASS, AUDIT.check_balance_receipt(receipt).status)
            for group in ("B3", "B1V", "B11V", "B12V"):
                record = next(row for row in data["records"] if row["group"] == group)
                record["percentRemaining"] = 8
                receipt.write_text(json.dumps(data), encoding="utf-8")
                self.assertEqual(AUDIT.FAIL, AUDIT.check_balance_receipt(receipt).status)
                record["percentRemaining"] = 9
                data["records"].remove(record)
                receipt.write_text(json.dumps(data), encoding="utf-8")
                self.assertEqual(AUDIT.FAIL, AUDIT.check_balance_receipt(receipt).status)
                data["records"].append(record)

    def test_audit_launcher_uses_runtime_that_can_import_audit_dependencies(self) -> None:
        completed = subprocess.run(
            [str(AUDIT_RUNNER), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("OpenSMS", completed.stdout)

    def test_source_transfer_send_gate_accepts_50_and_rejects_49(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps({
                "route": "a_zone_lt4_to_a12",
                "replyRateLatestSendThresholdInclusive": 50,
                "rows": [
                    {"电话": "1001", "原运营商": "A1", "发送窗口数": 50},
                    {"电话": "1002", "原运营商": "A11", "发送窗口数": 49},
                ],
            }), encoding="utf-8")
            result = AUDIT.check_source_transfer_send_gate(
                {"groups": [{"candidate": str(candidate)}]},
                AUDIT.PathResolver(config_path, None),
            )
            self.assertEqual(AUDIT.FAIL, result.status)
            self.assertEqual(1, result.details["invalid_candidate_count"])

    def test_b1_b11_ten_day_route_keeps_previous_strategy_without_latest_day_gate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps({
                "route": "b1_b11_gte240_to_b12",
                "rows": [
                    {"电话": "1001", "原运营商": "B1", "发送窗口数": 49, "10日发送窗口数": 240},
                ],
            }), encoding="utf-8")
            result = AUDIT.check_reply_rate_send_gate(
                {"groups": [{"candidate": str(candidate)}]},
                AUDIT.PathResolver(config_path, None),
            )
            self.assertEqual(AUDIT.PASS, result.status)
            self.assertEqual([], result.details["inspected_routes"])

    def test_protected_source_check_detects_a2_candidate_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps({
                "sourceCarrierNames": ["A2-股票-C-120"],
                "targetCarrierName": "B12-测试",
                "rows": [{"电话": "1001", "运营商": "A2-股票-C-120"}],
            }), encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            result = AUDIT.check_protected_carrier_sources(
                [{"candidate": str(candidate)}],
                AUDIT.PathResolver(config_path, root),
            )
            self.assertEqual(AUDIT.FAIL, result.status)
            self.assertGreaterEqual(result.details["source_hit_count"], 1)

    def test_protected_source_check_allows_a2_for_task_b_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps({
                "task": "B",
                "forceGate": True,
                "sourceCarrierNames": ["A2-股票-C-120"],
                "targetCarrierName": "过期下架卡",
                "rows": [{"电话": "1001", "运营商": "A2-股票-C-120"}],
            }), encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            result = AUDIT.check_protected_carrier_sources(
                [{"candidate": str(candidate)}],
                AUDIT.PathResolver(config_path, root),
            )
            self.assertEqual(AUDIT.PASS, result.status)
            self.assertEqual(0, result.details["source_hit_count"])
            self.assertGreaterEqual(result.details["task_b_exception_source_hit_count"], 1)

    def test_quick_renew_c_is_never_allowed_even_for_task_b(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps({
                "task": "B",
                "forceGate": True,
                "sourceCarrierNames": ["快过期续费C"],
                "rows": [{"电话": "1001", "运营商": "快过期续费C"}],
            }, ensure_ascii=False), encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            result = AUDIT.check_protected_carrier_sources(
                [{"candidate": str(candidate)}],
                AUDIT.PathResolver(config_path, root),
            )
            self.assertEqual(AUDIT.FAIL, result.status)
            self.assertGreaterEqual(result.details["never_process_source_hit_count"], 1)

    def test_quick_renew_t_is_never_allowed_even_for_task_b(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps({
                "task": "B",
                "forceGate": True,
                "sourceCarrierNames": ["快过期续费T"],
                "rows": [{"电话": "1002", "运营商": "快过期续费T"}],
            }, ensure_ascii=False), encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            result = AUDIT.check_protected_carrier_sources(
                [{"candidate": str(candidate)}],
                AUDIT.PathResolver(config_path, root),
            )
            self.assertEqual(AUDIT.FAIL, result.status)
            self.assertGreaterEqual(result.details["never_process_source_hit_count"], 1)

    def test_protected_source_check_follows_task_b_write_candidate_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / "task_b_candidate.json"
            candidate.write_text(json.dumps({
                "task": "B",
                "forceGate": True,
                "sourceCarrierNames": ["A21-股票-T120"],
                "rows": [{"电话": "1001", "运营商": "A21-股票-T120"}],
            }), encoding="utf-8")
            write_receipt = root / "task_b_write.json"
            write_receipt.write_text(json.dumps({
                "candidatePath": str(candidate),
                "sourceCarrierNames": ["A21-股票-T120"],
                "sourceCarriers": [{"name": "A21-股票-T120"}],
            }), encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            result = AUDIT.check_protected_carrier_sources(
                [{"write": str(write_receipt)}],
                AUDIT.PathResolver(config_path, root),
            )
            self.assertEqual(AUDIT.PASS, result.status)
            self.assertEqual(0, result.details["source_hit_count"])
            self.assertEqual(2, result.details["task_b_exception_source_hit_count"])

    def test_result_workbook_structure_passes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.xlsx"
            minimal_xlsx(path)
            result = AUDIT.check_xlsx("result", path, inspect_result=True, server_date="2026-08-19")
            self.assertEqual(AUDIT.PASS, result.status, result.message)
            self.assertEqual(1, result.details["workbook"]["main_rows"])

    def test_formula_error_fails(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.xlsx"
            minimal_xlsx(path, formula_error=True)
            result = AUDIT.check_xlsx("result", path, inspect_result=True, server_date="2026-08-19")
            self.assertEqual(AUDIT.FAIL, result.status)
            self.assertIn("公式错误", result.message)

    def test_change_status_must_be_fourth_column(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.xlsx"
            minimal_xlsx(path, change_status_fourth=False)
            result = AUDIT.check_xlsx("result", path, inspect_result=True, server_date="2026-08-19")
            self.assertEqual(AUDIT.FAIL, result.status)
            self.assertIn("换号状态不是第4列", result.message)

    def test_carrier_group_checks_candidate_write_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / "candidate.json"
            validated = root / "validated.json"
            write = root / "write.json"
            verify = root / "verify.json"
            candidate.write_text(json.dumps({"count": 1, "candidates": [{"phone": "14165550000"}]}))
            validated.write_text(json.dumps({"originalCount": 1, "count": 1}))
            write.write_text(json.dumps({
                "target": {"name": "A1"},
                "before": {"candidateCount": 1, "eligibleCount": 1, "alreadyTargetCount": 0},
                "after": {"targetCount": 1, "stillNotTargetCount": 0, "missingCount": 0, "duplicateBackendCount": 0},
            }))
            verify.write_text(json.dumps({
                "total": 1, "targetCount": 1, "stillNotTargetCount": 0,
                "missingCount": 0, "duplicateBackendCount": 0, "passed": True,
            }))
            config = root / "config.json"
            config.write_text("{}")
            resolver = AUDIT.PathResolver(config, root)
            result = AUDIT.check_group("测试组", {
                "candidate": str(candidate), "validated": str(validated),
                "write": str(write), "verify": str(verify),
            }, resolver)
            self.assertEqual(AUDIT.PASS, result.status, result.message)
            self.assertEqual(1, result.details["actual_writes"])
            self.assertEqual(1, result.details["successful_writes"])

    def test_carrier_group_allows_readback_of_already_target_rows(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / "candidate.json"
            validated = root / "validated.json"
            write = root / "write.json"
            verify = root / "verify.json"
            candidate.write_text(json.dumps({"count": 2, "candidates": [{"phone": "14165550000"}, {"phone": "14165550001"}]}))
            validated.write_text(json.dumps({"originalCount": 2, "count": 0}))
            write.write_text(json.dumps({
                "target": {"name": "过期下架卡"},
                "before": {"candidateCount": 2, "eligibleCount": 0, "alreadyTargetCount": 2},
                "after": {"targetCount": 2, "stillNotTargetCount": 0, "missingCount": 0, "duplicateBackendCount": 0},
            }))
            verify.write_text(json.dumps({
                "total": 0, "targetCount": 0, "stillNotTargetCount": 0,
                "missingCount": 0, "duplicateBackendCount": 0, "passed": True,
            }))
            config = root / "config.json"
            config.write_text("{}")
            resolver = AUDIT.PathResolver(config, root)
            result = AUDIT.check_group("任务B：已过期", {
                "candidate": str(candidate), "validated": str(validated),
                "write": str(write), "verify": str(verify),
            }, resolver)
            self.assertEqual(AUDIT.PASS, result.status, result.message)
            self.assertEqual(0, result.details["actual_writes"])
            self.assertEqual(2, result.details["already_target"])

    def test_candidate_overlap_fails(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps({"count": 1, "candidates": [{"phone": "14165550000"}]}))
            second.write_text(json.dumps({"count": 1, "candidates": [{"phone": "14165550000"}]}))
            config = root / "config.json"
            config.write_text("{}")
            resolver = AUDIT.PathResolver(config, root)
            result = AUDIT.check_candidate_disjoint([
                {"title": "第一组", "candidate": str(first)},
                {"title": "第二组", "candidate": str(second)},
            ], resolver)
            self.assertEqual(AUDIT.FAIL, result.status)

    def test_task_c_audit_validates_authorization_and_strict_rules(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "card-result.xlsx"
            source.write_bytes(b"fresh-card-result")
            source_hash = AUDIT.sha256_file(source)
            target_c = "换号区C"
            target_t = "换号区T"
            kooda = root / "kooda.json"
            chatr = root / "chatr.json"
            kooda.write_text(json.dumps({
                "task": "C", "forceGate": True,
                "taskBPriorityLockEnforced": True,
                "serverDate": "2026-09-02", "tomorrowDate": "2026-09-03",
                "route": "task_c_kooda_4day_reply_rate_lt5_to_swap",
                "targetCarrierName": target_t, "replyRateThresholdExclusive": 0.05,
                "replyRateSendThresholdInclusive": 50,
                "tenDaySendThresholdExclusive": 360, "tenDaySendMetric": "10日发送窗口数",
                "sourcePath": str(source), "sourceSha256": source_hash,
                "rows": [{"电话": "1001", "运营商": "B1-测试", "opr": "302220 Koodo", "4日发送窗口数": 50, "10日发送窗口数": 361, "4日回复率": 0.049, "过期状态": "", "截止日": ""}],
            }))
            chatr.write_text(json.dumps({
                "task": "C", "forceGate": True,
                "taskBPriorityLockEnforced": True,
                "serverDate": "2026-09-02", "tomorrowDate": "2026-09-03",
                "route": "task_c_chatr_unchanged_4day_reply_rate_lt4_to_swap",
                "targetCarrierName": target_c, "replyRateThresholdExclusive": 0.04,
                "replyRateSendThresholdInclusive": 50,
                "tenDaySendThresholdExclusive": 360, "tenDaySendMetric": "10日发送窗口数",
                "sourcePath": str(source), "sourceSha256": source_hash,
                "rows": [{"电话": "1002", "运营商": "A1-测试", "opr": "302610 Chatr", "4日发送窗口数": 50, "10日发送窗口数": 361, "4日回复率": 0.039, "换号状态": "", "过期状态": "", "截止日": ""}],
            }))
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "task": "C", "forceGate": True,
                "taskBPriorityLockEnforced": True,
                "serverDate": "2026-09-02", "tomorrowDate": "2026-09-03",
                "sourcePath": str(source), "sourceSha256": source_hash,
                "targetCarrierNames": {"C": target_c, "T": target_t},
                "koodaReplyRateThresholdExclusive": 0.05,
                "chatrReplyRateThresholdExclusive": 0.04,
                "replyRateSendThresholdInclusive": 50,
                "tenDaySendThresholdExclusive": 360, "tenDaySendMetric": "10日发送窗口数",
                "replyRateSendGateGroups": [
                    "A1", "A11", "A12", "B1", "B11", "B12", "B1V", "B11V", "B12V",
                ],
                "mutuallyExclusive": True,
            }))
            config_path = root / "config.json"
            config_path.write_text("{}")
            config = {
                "cardResult": str(source),
                "taskCExecuted": True,
                "taskCStatus": "已授权",
                "taskCManifest": str(manifest),
                "groups": [
                    {"title": "任务C：Kooda/Koodo", "candidate": str(kooda)},
                    {"title": "任务C：Chatr未换号", "candidate": str(chatr)},
                ],
            }
            result = AUDIT.check_task_c(config, AUDIT.PathResolver(config_path, root))
            self.assertEqual(AUDIT.PASS, result.status, result.message)
            self.assertEqual(2, result.details["candidate_count"])
            # Independent checker must reject each route even when the manifest
            # and all other candidate fields claim eligibility.
            for candidate_path in (kooda, chatr):
                original = candidate_path.read_text()
                for value in (359, 360, None, "", True, "bad", "NaN", "Infinity"):
                    with self.subTest(route=candidate_path.name, value=value):
                        payload = json.loads(original)
                        payload["rows"][0]["10日发送窗口数"] = value
                        candidate_path.write_text(json.dumps(payload))
                        invalid = AUDIT.check_task_c(config, AUDIT.PathResolver(config_path, root))
                        self.assertEqual(AUDIT.FAIL, invalid.status)
                        self.assertEqual(1, invalid.details["ten_day_send_invalid_count"])
                payload = json.loads(original)
                payload.pop("tenDaySendThresholdExclusive")
                candidate_path.write_text(json.dumps(payload))
                invalid = AUDIT.check_task_c(config, AUDIT.PathResolver(config_path, root))
                self.assertEqual(AUDIT.FAIL, invalid.status)
                self.assertIn("10日发送", invalid.message)
                candidate_path.write_text(original)
            legacy_manifest = json.loads(manifest.read_text())
            legacy_manifest.pop("tenDaySendThresholdExclusive")
            manifest.write_text(json.dumps(legacy_manifest))
            invalid = AUDIT.check_task_c(config, AUDIT.PathResolver(config_path, root))
            self.assertEqual(AUDIT.FAIL, invalid.status)
            self.assertIn("10日发送", invalid.message)

    def test_task_c_groups_fail_without_authorization(self) -> None:
        config = {
            "taskCExecuted": False,
            "groups": [{"title": "任务C：Kooda/Koodo", "candidate": "unused.json"}],
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_path = root / "config.json"
            config_path.write_text("{}")
            result = AUDIT.check_task_c(config, AUDIT.PathResolver(config_path, root))
            self.assertEqual(AUDIT.FAIL, result.status)
            self.assertIn("未授权", result.message)

    def test_task_d_default_off_passes_when_completely_untouched(self) -> None:
        config = {
            "taskDExecuted": False,
            "taskDCurrentUserAuthorized": False,
            "taskDStatus": "无当前真实用户授权，本轮关闭且未触碰任务D",
            "groups": [],
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_path = root / "config.json"
            config_path.write_text("{}")
            result = AUDIT.check_task_d(config, AUDIT.PathResolver(config_path, root))
            self.assertEqual(AUDIT.PASS, result.status, result.message)
            self.assertFalse(result.details["executed"])
            self.assertFalse(result.details["current_user_authorized"])

    def test_task_d_default_off_fails_if_task_d_artifacts_appear(self) -> None:
        config = {
            "taskDExecuted": False,
            "taskDCurrentUserAuthorized": False,
            "taskDManifest": "unexpected.json",
            "groups": [{"title": "任务D：异常候选", "candidate": "unexpected.json"}],
        }
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_path = root / "config.json"
            config_path.write_text("{}")
            result = AUDIT.check_task_d(config, AUDIT.PathResolver(config_path, root))
            self.assertEqual(AUDIT.FAIL, result.status)

    def test_task_e_default_off_passes_only_when_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_path = root / "config.json"
            config_path.write_text("{}")
            base = {
                "taskEExecuted": False,
                "taskECurrentUserAuthorized": False,
                "taskEStatus": "无当前真实用户授权，本轮关闭且未触碰任务E",
            }
            result = AUDIT.check_task_e(base, AUDIT.PathResolver(config_path, root))
            self.assertEqual(AUDIT.PASS, result.status, result.message)
            bad = {**base, "taskELedger": str(root / "unexpected.json")}
            result = AUDIT.check_task_e(bad, AUDIT.PathResolver(config_path, root))
            self.assertEqual(AUDIT.FAIL, result.status)

    def test_task_e_valid_receipt_and_phone_ledger_pass(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "task_e_input.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "sheet1"
            sheet.append(["电话", "运营商", "过期状态", "截止日"])
            sheet.append(["1001", "A1", "已过期", "2026-09-01"])
            sheet.append(["1002", "B1", "6天", "2026-09-08"])
            sheet.append(["1003", "快过期续费C", "已过期", "2026-09-01"])
            workbook.save(source)
            workbook.close()
            manifest = root / "manifest.json"
            receipt = root / "receipt.json"
            ledger = root / "ledger.json"
            retention = root / "retention.json"
            retention.write_text(json.dumps({
                "task": "E",
                "sourceProvenance": "phone_card_retain_all_pages",
                "endpoint": "/phone/cardRetain/list",
                "pageSize": 200,
                "pageCount": 1,
                "pages": [{"pageNum": 1, "rowCount": 1}],
                "totalRecords": 1,
                "fetchedRecords": 1,
                "uniqueCardCount": 1,
                "invalidCardRecordCount": 0,
                "cardNos": ["1001"],
                "records": [{
                    "cardNo": "1001",
                    "expireBefore": "2026-08-31",
                    "expireAfter": "2026-09-01",
                    "chatId": "chat-1",
                    "createTime": "2026-09-01 01:00:00",
                }],
            }, ensure_ascii=False), encoding="utf-8")
            manifest.write_text(json.dumps({
                "task": "E",
                "forceGate": True,
                "automationGate": True,
                "ledgerDedupeKey": "phone",
                "sourceSha256": AUDIT.sha256_file(source),
                "eligibleExpiryStatuses": ["6天", "快过期", "已过期"],
                "retentionReceiptPath": str(retention.resolve()),
                "retentionReceiptSha256": AUDIT.sha256_file(retention),
                "retentionPageCount": 1,
                "retentionTotalRecordCount": 1,
                "retentionFetchedRecordCount": 1,
                "retentionUniqueCardCount": 1,
                "retentionDeadlineMatchPhoneCount": 1,
                "retentionDeadlineMatchRecordCount": 1,
                "retentionDeadlineMatches": [{"电话": "1001", "截止日": "2026-09-01", "下卡通知时间": "2026-09-01"}],
                "retentionComparisonDoesNotChangeCandidates": True,
                "eligibleWorkbookRowCount": 3,
                "candidateCount": 2,
                "dateGroupCount": 2,
                "neverProcessSourceExcludedCount": 1,
                "neverProcessSourceExcludedRows": [{"电话": "1003"}],
                "alreadySubmittedExcludedCount": 0,
                "alreadySubmittedExcludedRows": [],
                "duplicateSameDeadlineCollapsedCount": 0,
                "groups": [
                    {"expireDate": "2026-09-01", "count": 1, "cardNos": ["1001"], "rows": [{"电话": "1001", "运营商": "A1", "过期状态": "已过期", "截止日": "2026-09-01"}]},
                    {"expireDate": "2026-09-08", "count": 1, "cardNos": ["1002"], "rows": [{"电话": "1002", "运营商": "B1", "过期状态": "6天", "截止日": "2026-09-08"}]},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            receipt.write_text(json.dumps({
                "task": "E",
                "status": "passed",
                "businessReceiptPassed": True,
                "transactionalVerificationPassed": True,
                "ledgerVerificationPassed": True,
                "independentReadbackAvailable": False,
                "markerBatchSize": 20,
                "submittedCount": 2,
                "notWrittenCount": 0,
                "notWrittenCards": [],
                "batches": [
                    {"expireDate": "2026-09-01", "cardNos": ["1001"], "inputCount": 1, "matchedCardCount": 1, "updatedChatCount": 1, "notFound": [], "ledgerRecordedCount": 1, "notWrittenCount": 0, "notWrittenCards": []},
                    {"expireDate": "2026-09-08", "cardNos": ["1002"], "inputCount": 1, "matchedCardCount": 1, "updatedChatCount": 2, "notFound": [], "ledgerRecordedCount": 1, "notWrittenCount": 0, "notWrittenCards": []},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            ledger.write_text(json.dumps({
                "schemaVersion": 1,
                "dedupeKey": "phone",
                "submissions": {"1001": {"phone": "1001"}, "1002": {"phone": "1002"}},
            }), encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text("{}")
            config = {
                "taskEExecuted": True,
                "taskECurrentUserAuthorized": True,
                "taskEStatus": "通过",
                "taskEInputWorkbook": str(source),
                "taskEManifest": str(manifest),
                "taskEReceipt": str(receipt),
                "taskERetentionReceipt": str(retention),
                "taskERetentionReadExecuted": True,
                "taskELedger": str(ledger),
                "taskELedgerDedupeKey": "phone",
                "taskEEligibleWorkbookRowCount": 3,
                "taskECandidateCount": 2,
                "taskEAlreadySubmittedExcludedCount": 0,
                "taskENeverProcessSourceExcludedCount": 1,
                "taskEDateGroupCount": 2,
                "taskEProcessedCount": 2,
                "taskESubmittedCount": 2,
                "taskENotWrittenCount": 0,
                "taskENotWrittenCards": [],
                "taskERetentionDeadlineMatchPhoneCount": 1,
                "taskERetentionPageCount": 1,
                "taskERetentionTotalRecordCount": 1,
                "taskERetentionFetchedRecordCount": 1,
                "taskERetentionUniqueCardCount": 1,
            }
            result = AUDIT.check_task_e(config, AUDIT.PathResolver(config_path, root))
            self.assertEqual(AUDIT.PASS, result.status, result.message)

            recovery = root / "recovery.json"
            recovery.write_text(json.dumps({
                "status": "failed",
                "error": "HTTP 504 gateway timeout",
                "failureClass": "submission_outcome_uncertain",
                "uncertainSubmission": True,
                "pendingBatch": {"expireDate": "2026-09-01", "cardNos": ["1001"]},
            }), encoding="utf-8")
            recovered_receipt = json.loads(receipt.read_text(encoding="utf-8"))
            recovered_receipt.update({
                "recoveryProgressPath": str(recovery),
                "recoveryProgressSha256": AUDIT.sha256_file(recovery),
                "recoveryCandidateCount": 1,
                "recoveryRetriedCount": 1,
                "idempotentRecoveryNoChangeCount": 1,
                "matchedNoChangeCount": 1,
                "matchedNoChangeCards": [{"phone": "1001", "expireDate": "2026-09-01", "reason": "backend_matched_no_change"}],
            })
            recovered_receipt["batches"][0].update({
                "updatedChatCount": 0,
                "recoveryRetry": True,
                "idempotentRecoveryNoChange": True,
                "matchedNoChangeCount": 1,
                "matchedNoChangeCards": [{"phone": "1001", "expireDate": "2026-09-01", "reason": "backend_matched_no_change"}],
            })
            recovered_receipt["batches"][1]["recoveryRetry"] = False
            receipt.write_text(json.dumps(recovered_receipt), encoding="utf-8")
            recovered_config = {
                **config,
                "taskERecoveryProof": str(recovery),
                "taskERecoveryCandidateCount": 1,
                "taskERecoveryRetriedCount": 1,
                "taskEIdempotentRecoveryNoChangeCount": 1,
                "taskEMatchedNoChangeCount": 1,
                "taskEMatchedNoChangeCards": [{"phone": "1001", "expireDate": "2026-09-01", "reason": "backend_matched_no_change"}],
            }
            result = AUDIT.check_task_e(recovered_config, AUDIT.PathResolver(config_path, root))
            self.assertEqual(AUDIT.PASS, result.status, result.message)

            partial_receipt = json.loads(receipt.read_text(encoding="utf-8"))
            partial_receipt.update({
                "status": "passed_with_not_found",
                "submittedCount": 1,
                "notWrittenCount": 1,
                "notWrittenCards": [{"phone": "1002", "expireDate": "2026-09-08", "reason": "backend_not_found"}],
                "matchedNoChangeCount": 1,
                "matchedNoChangeCards": [{"phone": "1001", "expireDate": "2026-09-01", "reason": "backend_matched_no_change"}],
            })
            partial_receipt["batches"][1].update({
                "matchedCardCount": 0,
                "updatedChatCount": 0,
                "notFound": ["1002"],
                "ledgerRecordedCount": 0,
                "notWrittenCount": 1,
                "notWrittenCards": [{"phone": "1002", "expireDate": "2026-09-08", "reason": "backend_not_found"}],
                "matchedNoChangeCount": 0,
                "matchedNoChangeCards": [],
            })
            receipt.write_text(json.dumps(partial_receipt), encoding="utf-8")
            ledger.write_text(json.dumps({
                "schemaVersion": 1,
                "dedupeKey": "phone",
                "submissions": {"1001": {"phone": "1001"}},
            }), encoding="utf-8")
            partial_config = {
                **recovered_config,
                "taskESubmittedCount": 1,
                "taskENotWrittenCount": 1,
                "taskENotWrittenCards": [{"phone": "1002", "expireDate": "2026-09-08", "reason": "backend_not_found"}],
            }
            result = AUDIT.check_task_e(partial_config, AUDIT.PathResolver(config_path, root))
            self.assertEqual(AUDIT.PASS, result.status, result.message)

if __name__ == "__main__":
    unittest.main()
