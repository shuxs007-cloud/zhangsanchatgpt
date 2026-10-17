import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "opensms_refresh_workbook_carriers_fast.py"
SPEC = importlib.util.spec_from_file_location("opensms_refresh_workbook_carriers_fast", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


WORKBOOK_XML = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="main" sheetId="1" r:id="rId1"/><sheet name="audit" sheetId="2" r:id="rId2"/></sheets></workbook>'''
RELS_XML = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>'''
MAIN_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:C3"/><sheetData><row r="1"><c r="A1" s="1" t="inlineStr"><is><t>电话</t></is></c><c r="B1" s="1" t="inlineStr"><is><t>运营商</t></is></c><c r="C1" s="1" t="inlineStr"><is><t>备注</t></is></c></row><row r="2"><c r="A2" s="2" t="inlineStr"><is><t>1001</t></is></c><c r="B2" s="7" t="inlineStr"><is><t>旧A</t></is></c><c r="C2" s="3" t="inlineStr"><is><t>保持1</t></is></c></row><row r="3"><c r="A3" s="2" t="inlineStr"><is><t>1002</t></is></c><c r="B3" s="8" t="inlineStr"><is><t>新B</t></is></c><c r="C3" s="3" t="inlineStr"><is><t>保持2</t></is></c></row></sheetData></worksheet>'''.encode("utf-8")
AUDIT_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:A1"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>不变</t></is></c></row></sheetData></worksheet>'''.encode("utf-8")


def make_workbook(path: Path, include_content_types: bool = False) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        if include_content_types:
            archive.writestr(
                "[Content_Types].xml",
                b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>''',
            )
        archive.writestr("xl/workbook.xml", WORKBOOK_XML)
        archive.writestr("xl/_rels/workbook.xml.rels", RELS_XML)
        archive.writestr("xl/worksheets/sheet1.xml", MAIN_XML)
        archive.writestr("xl/worksheets/sheet2.xml", AUDIT_XML)
        archive.writestr("docProps/custom.xml", b"unchanged-member")


class FastCarrierRefreshTests(unittest.TestCase):
    def fresh_args(self, root, source, label, rows, prior=None):
        live_path = root / f"live-{label}.json"
        live_path.write_text(json.dumps({
            "passed": True, "candidateCount": len(rows), "backendValidCount": len(rows),
            "failureCount": 0, "targetMatchCount": len(rows), "actualWrite": 0,
            "checkedAt": "2026-09-05T05:11:55.119Z", "silentBrowserMode": True,
            "browserForegroundActivationAttempted": False,
            "rows": [{"backendMatchCount": 1, "passed": True, "backendId": f"id-{row['phone']}", **row} for row in rows],
        }), encoding="utf-8")
        return argparse.Namespace(
            input=source, output=root / f"out-{label}.xlsx", live_receipt=live_path,
            artifact_receipt=root / f"artifact-{label}.json", verify_receipt=root / f"verify-{label}.json",
            refresh_receipt=root / f"refresh-{label}.json", readback_receipt=root / f"readback-{label}.json",
            run_id="offline-test-this-round", verified_cache_output=root / f"cache-{label}.json",
            verified_cache_input=Path(prior["verifiedCachePath"]) if prior else None,
            verified_cache_sha256=prior["verifiedCacheSha256"] if prior else None,
        )

    def test_zero_delta_is_byte_identical_and_warm_cache_avoids_main_parse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            make_workbook(source)
            rows = [{"phone": "1001", "liveCarrierName": "旧A"}, {"phone": "1002", "liveCarrierName": "新B"}]
            first = self.fresh_args(root, source, "cold", rows)
            receipt = MODULE.run(first)
            self.assertEqual(first.output.read_bytes(), source.read_bytes())
            self.assertTrue(receipt["zeroDeltaConfirmed"])
            second = self.fresh_args(root, first.output, "warm", rows, receipt)
            with mock.patch.object(MODULE, "parse_sheet_rows", side_effect=AssertionError("must not reparse verified workbook")):
                result = MODULE.run(second)
            self.assertTrue(result["verifiedCacheReused"])
            self.assertEqual(second.output.read_bytes(), source.read_bytes())
            self.assertEqual(result["formulaErrorScanMode"], "same-run-sha256-bound-complete-scan")

    def test_actual_delta_despite_zero_write_receipt_uses_full_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            make_workbook(source)
            seed = self.fresh_args(root, source, "seed", [])
            prior = MODULE.run(seed)
            change = self.fresh_args(root, seed.output, "change", [{"phone": "1001", "liveCarrierName": "真实新运营商"}], prior)
            result = MODULE.run(change)
            self.assertEqual(result["changed"], 1)
            self.assertFalse(result["verifiedCacheReused"])
            self.assertTrue(result["cacheRejectedForDelta"])
            self.assertEqual(result["workbookRefreshMode"], "targeted-ooxml-repack")

    def test_supplemental_add_metadata_update_remove_and_zero_delta(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            make_workbook(source, include_content_types=True)
            seed = self.fresh_args(root, source, "seed", [])
            prior = MODULE.run(seed)
            rows = [{"phone": "1003", "liveCarrierName": "新C", "route": "规则一", "expectedTarget": "目标一"}]
            for label, expected_copy, expected_rows in [
                ("add", False, 1), ("same", True, 1), ("metadata", False, 1),
                ("delete", False, 0), ("empty-same", True, 0),
            ]:
                if label == "metadata":
                    rows[0]["route"] = "规则二"
                    rows[0]["expectedTarget"] = "目标二"
                if label == "delete":
                    rows = []
                args = self.fresh_args(root, Path(prior["outputPath"]), label, rows, prior)
                result = MODULE.run(args)
                self.assertEqual(result["zeroDeltaConfirmed"], expected_copy, label)
                refresh = json.loads(args.refresh_receipt.read_text())
                self.assertEqual(refresh["supplementalRowCount"], expected_rows)
                self.assertEqual(refresh["missingInWorkbookCount"], 0)
                with zipfile.ZipFile(args.output) as archive:
                    supplemental = next(path for name, path in MODULE.workbook_sheets(archive) if name == "后台更新未在源表")
                    xml = archive.read(supplemental)
                    if label == "metadata":
                        self.assertIn("规则二".encode(), xml)
                        self.assertIn("目标二".encode(), xml)
                    if not expected_rows:
                        self.assertNotIn(b"1003", xml)
                prior = result

    def test_cache_metadata_workbook_and_run_tampering_are_rejected(self):
        for kind in ("metadata", "workbook", "run", "older-live", "missing-cache-hash"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source.xlsx"
                make_workbook(source)
                first = self.fresh_args(root, source, "seed", [])
                prior = MODULE.run(first)
                args = self.fresh_args(root, first.output, "tamper", [], prior)
                if kind == "metadata":
                    args.verified_cache_input.write_text(args.verified_cache_input.read_text() + " ")
                elif kind == "workbook":
                    with first.output.open("ab") as handle:
                        handle.write(b"tampered")
                elif kind == "run":
                    args.run_id = "different-round"
                elif kind == "missing-cache-hash":
                    args.verified_cache_sha256 = None
                else:
                    live = json.loads(args.live_receipt.read_text())
                    live["checkedAt"] = "2026-09-04T05:11:55.119Z"
                    args.live_receipt.write_text(json.dumps(live))
                with self.assertRaises(MODULE.RefreshError):
                    MODULE.run(args)
                self.assertFalse(args.output.exists())

    def test_copy_corruption_is_rejected_by_independent_output_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            make_workbook(source)
            args = self.fresh_args(root, source, "corrupt", [])
            original_copy = MODULE.copy_with_replacements
            def corrupt(*values):
                original_copy(*values)
                with values[1].open("ab") as handle:
                    handle.write(b"corrupted-copy")
            with mock.patch.object(MODULE, "copy_with_replacements", side_effect=corrupt):
                with self.assertRaisesRegex(MODULE.RefreshError, "输出SHA256"):
                    MODULE.run(args)
            self.assertFalse(args.verified_cache_output.exists())

    def test_backend_duplicate_identity_partial_coverage_and_missing_id_fail(self):
        for kind in ("phone", "backend-id", "count", "missing-id", "row-failed", "duplicate-match"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source.xlsx"
                make_workbook(source)
                rows = [{"phone": "1001", "liveCarrierName": "旧A"}, {"phone": "1002", "liveCarrierName": "新B"}]
                args = self.fresh_args(root, source, "invalid", rows)
                live = json.loads(args.live_receipt.read_text())
                if kind == "phone": live["rows"][1]["phone"] = "1001"
                elif kind == "backend-id": live["rows"][1]["backendId"] = live["rows"][0]["backendId"]
                elif kind == "count": live["candidateCount"] += 1
                elif kind == "missing-id": live["rows"][0].pop("backendId")
                elif kind == "row-failed": live["rows"][0]["passed"] = False
                else: live["rows"][0]["backendMatchCount"] = 2
                args.live_receipt.write_text(json.dumps(live))
                with self.assertRaises(MODULE.RefreshError):
                    MODULE.run(args)
                self.assertFalse(args.output.exists())

    def test_formula_error_blocks_zero_copy_and_cache_publication(self):
        for member, reference in (("xl/worksheets/sheet1.xml", b' r="A9"'), ("xl/worksheets/sheet2.xml", b' r="A9"'), ("xl/worksheets/sheet1.xml", b"")):
            with self.subTest(member=member, reference=reference), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source.xlsx"
                make_workbook(source)
                with zipfile.ZipFile(source) as archive:
                    payloads = {name: archive.read(name) for name in archive.namelist()}
                payloads[member] = payloads[member].replace(b"</sheetData>", b'<row r="9"><c' + reference + b' t="e"><f>1/0</f><v>#DIV/0!</v></c></row></sheetData>')
                with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name, data in payloads.items(): archive.writestr(name, data)
                args = self.fresh_args(root, source, "error", [])
                with self.assertRaises(MODULE.RefreshError): MODULE.run(args)
                self.assertFalse(args.verified_cache_output.exists())
                verify = json.loads(args.verify_receipt.read_text())
                self.assertEqual(verify["formulaErrorCount"], 1)

    def test_legitimate_zero_candidate_readback_copies_and_verifies_workbook(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            output = root / "output.xlsx"
            make_workbook(source)
            live = root / "live.json"
            live.write_text(json.dumps({
                "passed": True,
                "checkedAt": "2026-09-01T00:00:00Z",
                "mode": "test-zero-candidate",
                "apiTransport": "offline",
                "candidateCount": 0,
                "backendValidCount": 0,
                "targetMatchCount": 0,
                "failureCount": 0,
                "rows": [],
            }), encoding="utf-8")
            args = argparse.Namespace(
                input=source,
                live_receipt=live,
                output=output,
                artifact_receipt=root / "artifact.json",
                verify_receipt=root / "verify.json",
                refresh_receipt=root / "refresh.json",
                readback_receipt=root / "readback.json",
            )
            result = MODULE.run(args)
            self.assertTrue(result["passed"])
            self.assertEqual(result["changed"], 0)
            self.assertTrue(output.is_file())
            verify = json.loads(args.verify_receipt.read_text(encoding="utf-8"))
            self.assertEqual(verify["liveCandidateCount"], 0)
            self.assertEqual(verify["formulaErrorCount"], 0)

    def test_undeclared_empty_readback_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            make_workbook(source)
            live = root / "live.json"
            live.write_text(json.dumps({
                "passed": True,
                "candidateCount": 1,
                "backendValidCount": 0,
                "failureCount": 0,
                "rows": [],
            }), encoding="utf-8")
            args = argparse.Namespace(
                input=source,
                live_receipt=live,
                output=root / "output.xlsx",
                artifact_receipt=root / "artifact.json",
                verify_receipt=root / "verify.json",
                refresh_receipt=root / "refresh.json",
                readback_receipt=root / "readback.json",
            )
            with self.assertRaises(MODULE.RefreshError):
                MODULE.run(args)

    def test_targeted_refresh_preserves_non_target_content_and_styles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            output = root / "output.xlsx"
            make_workbook(source)
            source_before = source.read_bytes()
            live = root / "live.json"
            live.write_text(json.dumps({
                "passed": True,
                "checkedAt": "2026-08-31T00:00:00Z",
                "mode": "test",
                "apiTransport": "offline",
                "candidateCount": 2,
                "backendValidCount": 2,
                "targetMatchCount": 2,
                "failureCount": 0,
                "rows": [
                    {"phone": "1001", "liveCarrierName": "新A", "backendMatchCount": 1, "passed": True},
                    {"phone": "1002", "liveCarrierName": "新B", "backendMatchCount": 1, "passed": True},
                ],
            }), encoding="utf-8")
            args = argparse.Namespace(
                input=source,
                live_receipt=live,
                output=output,
                artifact_receipt=root / "artifact.json",
                verify_receipt=root / "verify.json",
                refresh_receipt=root / "refresh.json",
                readback_receipt=root / "readback.json",
            )
            result = MODULE.run(args)
            self.assertTrue(result["passed"])
            self.assertEqual(result["changed"], 1)
            verify = json.loads(args.verify_receipt.read_text(encoding="utf-8"))
            self.assertEqual(verify["nonCarrierValueMismatchCount"], 0)
            self.assertEqual(verify["styleMismatchCount"], 0)
            self.assertEqual(verify["targetMismatchCount"], 0)
            self.assertEqual(source.read_bytes(), source_before)
            with zipfile.ZipFile(output) as archive:
                output_xml = archive.read("xl/worksheets/sheet1.xml")
                self.assertIn("新A".encode(), output_xml)
                self.assertIn(b's="7"', output_xml)
                self.assertEqual(archive.read("docProps/custom.xml"), b"unchanged-member")

    def test_new_card_column_d_and_red_styles_survive_carrier_refresh_and_warm_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            make_workbook(source)
            with zipfile.ZipFile(source) as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            main = members["xl/worksheets/sheet1.xml"].decode()
            main = main.replace('dimension ref="A1:C3"', 'dimension ref="A1:D3"')
            statuses = (("换号状态", 1), ("未换号", 9), ("已换号", 10))
            for row, (status, style) in enumerate(statuses, 1):
                pattern = rf'(<row r="{row}">.*?)(</row>)'
                extra = f'<c r="D{row}" s="{style}" t="inlineStr"><is><t>{status}</t></is></c>'
                main = re.sub(pattern, lambda match: match.group(1) + extra + match.group(2), main)
            members["xl/worksheets/sheet1.xml"] = main.encode()
            members["xl/styles.xml"] = (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                b'<fills count="1"><fill><patternFill patternType="solid">'
                b'<fgColor rgb="00FFC7CE"/></patternFill></fill></fills></styleSheet>')
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, payload in members.items():
                    archive.writestr(name, payload)
            source_before = source.read_bytes()
            rows = [{"phone": "1001", "liveCarrierName": "新A"},
                    {"phone": "1002", "liveCarrierName": "新B"}]
            first = self.fresh_args(root, source, "new-card-delta", rows)
            prior = MODULE.run(first)
            self.assertTrue(prior["passed"])
            self.assertEqual(prior["changed"], 1)
            with zipfile.ZipFile(first.output) as archive:
                refreshed = archive.read("xl/worksheets/sheet1.xml")
                for row in (1, 2, 3):
                    pattern = f'<c r="D{row}".*?</c>'.encode()
                    self.assertEqual(re.search(pattern, refreshed).group(),
                                     re.search(pattern, members["xl/worksheets/sheet1.xml"]).group())
                self.assertEqual(archive.read("xl/styles.xml"), members["xl/styles.xml"])
                self.assertEqual(archive.read("xl/worksheets/sheet2.xml"), members["xl/worksheets/sheet2.xml"])
            verify = json.loads(first.verify_receipt.read_text())
            self.assertEqual(verify["nonCarrierValueMismatchCount"], 0)
            self.assertEqual(verify["styleMismatchCount"], 0)
            self.assertEqual(source.read_bytes(), source_before)

            second = self.fresh_args(root, first.output, "new-card-warm", rows, prior)
            with mock.patch.object(MODULE, "parse_sheet_rows", side_effect=AssertionError("no warm reparse")):
                result = MODULE.run(second)
            self.assertTrue(result["verifiedCacheReused"])
            self.assertEqual(second.output.read_bytes(), first.output.read_bytes())

    def test_missing_live_phone_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            make_workbook(source)
            live = root / "live.json"
            live.write_text(json.dumps({
                "passed": True,
                "rows": [{"phone": "not-in-workbook", "liveCarrierName": "目标", "backendMatchCount": 1}],
            }), encoding="utf-8")
            args = argparse.Namespace(
                input=source,
                live_receipt=live,
                output=root / "output.xlsx",
                artifact_receipt=root / "artifact.json",
                verify_receipt=root / "verify.json",
                refresh_receipt=root / "refresh.json",
                readback_receipt=root / "readback.json",
            )
            with self.assertRaises(MODULE.RefreshError):
                MODULE.run(args)

    def test_repeated_refresh_replaces_existing_supplemental_sheet(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.xlsx"
            first = root / "first.xlsx"
            second = root / "second.xlsx"
            make_workbook(source, include_content_types=True)

            def run_refresh(input_path: Path, output_path: Path, carrier: str, suffix: str):
                live = root / f"live-{suffix}.json"
                live.write_text(json.dumps({
                    "passed": True,
                    "checkedAt": "2026-09-02T00:00:00Z",
                    "mode": "test-repeated-refresh",
                    "apiTransport": "offline",
                    "candidateCount": 1,
                    "backendValidCount": 1,
                    "targetMatchCount": 1,
                    "failureCount": 0,
                    "rows": [{
                        "phone": "1003",
                        "liveCarrierName": carrier,
                        "backendMatchCount": 1,
                        "passed": True,
                    }],
                }), encoding="utf-8")
                args = argparse.Namespace(
                    input=input_path,
                    live_receipt=live,
                    output=output_path,
                    artifact_receipt=root / f"artifact-{suffix}.json",
                    verify_receipt=root / f"verify-{suffix}.json",
                    refresh_receipt=root / f"refresh-{suffix}.json",
                    readback_receipt=root / f"readback-{suffix}.json",
                )
                return MODULE.run(args)

            self.assertTrue(run_refresh(source, first, "首次目标", "first")["passed"])
            self.assertTrue(run_refresh(first, second, "再次目标", "second")["passed"])
            with zipfile.ZipFile(second) as archive:
                sheets = MODULE.workbook_sheets(archive)
                matches = [sheet_path for name, sheet_path in sheets if name == "后台更新未在源表"]
                self.assertEqual(1, len(matches))
                supplemental = archive.read(matches[0])
                self.assertIn("再次目标".encode(), supplemental)
                self.assertNotIn("首次目标".encode(), supplemental)


if __name__ == "__main__":
    unittest.main()
