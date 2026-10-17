"""Offline tests for the fresh-export versus full-inventory coverage barrier."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import pickle
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("inventory_coverage_test_module",
    ROOT / "scripts/opensms_inventory_coverage.py")
COVERAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COVERAGE)
ICCID = "8912230200278151507"


def source(*rows, iccid_column=True):
    headers = ("电话", "运营商", "opr", "卡槽") + (("ICCID",) if iccid_column else ())
    return {"headers": headers,
            "rows": list(rows) if rows else [("12493567483", "B1", "Kooda", "221_61.01", ICCID + "F")]}


def inventory(*rows):
    values = list(rows) if rows else [{"id": 140273, "opr": "Kooda", "slotIdentif": "221_61.01",
                                     "cardNo": "12493567483", "cardStore": ICCID + "F"}]
    return {"status": "passed", "runId": "coverage-test", "totalRows": len(values), "systemRows": values}


class CoverageTests(unittest.TestCase):
    def test_empty_pool_off_shelf_transition_requires_nonempty_exact_identity(self):
        scan = source(('12493567483', 'B1', 'Kooda', '221_61.01', ICCID + 'F',
                       '上架', '2026-09-06 13:49:04'))
        scan['headers'] += ('状态', '创建时间')
        old = {'id': 1, 'cardNo': '2493567483', 'opr': 'Kooda', 'slotIdentif': None,
               'showAble': False, 'status': '猫池空号已下架', 'machineName': '221',
               'createTime': '2026-09-06 13:49:04', 'cardStore': ICCID + 'f'}
        before = copy.deepcopy((scan, old))
        result = COVERAGE.check_inventory_coverage(scan, inventory(old))
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['matchedCount'], 1)
        proof = result['archivedMatches'][0]
        self.assertEqual(proof['sourceStatus'], '上架')
        self.assertEqual(proof['inventoryStatus'], '猫池空号已下架')
        self.assertTrue(proof['lifecycleTransition'])
        self.assertFalse(proof['liveSlotMatched'])
        self.assertEqual((scan, old), before)
        for changes in ({'showAble': True}, {'showAble': 0}, {'status': '注册失败'},
                        {'status': '其他已下架'}, {'status': '换卡已下线'},
                        {'machineName': '222'}, {'createTime': '2026-09-06 13:49:05'},
                        {'cardStore': ICCID + '0F'}, {'slotIdentif': '221_61.01', 'cardNo': '2493567484'}):
            with self.subTest(changes=changes):
                self.assertEqual(COVERAGE.check_inventory_coverage(scan, inventory({**old, **changes}))['status'], 'failed')
        for column, value in ((5, '注册失败'), (5, None), (6, ''), (4, ''), (4, int(ICCID))):
            changed = copy.deepcopy(scan)
            row = list(changed['rows'][0]); row[column] = value; changed['rows'][0] = row
            self.assertEqual(COVERAGE.check_inventory_coverage(changed, inventory(old))['status'], 'failed')
        changed = copy.deepcopy(scan)
        row = list(changed['rows'][0]); row[4] = ''; changed['rows'][0] = row
        self.assertEqual(COVERAGE.check_inventory_coverage(changed, inventory({**old, 'cardStore': ''}))['status'], 'failed')
        self.assertEqual(COVERAGE.check_inventory_coverage(scan, inventory(old, {**old, 'id': 2}))['status'], 'failed')

    def test_empty_pool_record_already_off_shelf_is_also_identified(self):
        scan = source(('12493567483', 'B1', 'Kooda', '221_61.01', ICCID, '下架', '2026-09-06 13:49:04'))
        scan['headers'] += ('状态', '创建时间')
        snapshot = inventory({'id': 1, 'cardNo': '2493567483', 'opr': 'Kooda', 'slotIdentif': '',
            'showAble': False, 'status': '猫池空号已下架', 'machineName': '221',
            'createTime': '2026-09-06 13:49:04', 'cardStore': ICCID})
        result = COVERAGE.check_inventory_coverage(scan, snapshot)
        self.assertEqual(result['status'], 'passed')
        self.assertFalse(result['archivedMatches'][0]['lifecycleTransition'])

    def test_archived_record_requires_full_historical_evidence_without_borrowing_new_slot(self):
        scan = source(('12493567483', '换号后存放区', 'Kooda', '221_61.01', ICCID + 'F', '下架', '2026-09-06 13:49:04'))
        scan['headers'] += ('状态', '创建时间')
        old = {'id': 1, 'cardNo': '2493567483', 'opr': 'Kooda', 'slotIdentif': None,
               'showAble': False, 'status': '换卡已下线', 'machineName': '221',
               'createTime': '2026-09-06 13:49:04', 'cardStore': ICCID + 'F'}
        new = {'id': 2, 'cardNo': '2493567484', 'opr': 'Kooda', 'slotIdentif': '221_61.01', 'cardStore': ICCID + 'F'}
        result = COVERAGE.check_inventory_coverage(scan, inventory(old, new))
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['archivedMatchedCount'], 1)
        self.assertEqual(result['archivedMatches'][0]['backendId'], '1')
        self.assertFalse(result['archivedMatches'][0]['liveSlotMatched'])
        for change in ({'showAble': True}, {'showAble': 0}, {'status': '注册失败'},
                       {'machineName': '222'}, {'createTime': '2026-09-06 13:49:05'},
                       {'cardStore': ICCID + '0F'}, {'slotIdentif': '221_62.01'}):
            with self.subTest(change=change):
                result = COVERAGE.check_inventory_coverage(scan, inventory({**old, **change}, new))
                self.assertEqual(result['status'], 'failed')
        self.assertEqual(COVERAGE.check_inventory_coverage(scan, inventory(old, {**old, 'id': 3}))['status'], 'failed')
        for column, value in ((5, '上架'), (6, ''), (6, 'bad'), (4, '')):
            changed = copy.deepcopy(scan)
            values = list(changed['rows'][0]); values[column] = value; changed['rows'][0] = values
            self.assertEqual(COVERAGE.check_inventory_coverage(changed, inventory(old))['status'], 'failed')

    def test_exact_identity_match_is_pure_and_requires_no_retry(self):
        scan, snapshot = source(), inventory()
        before = copy.deepcopy((scan, snapshot))
        result = COVERAGE.check_inventory_coverage(scan, snapshot)
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["sourceKoodaCount"], 1)
        self.assertEqual(result["matchedCount"], 1)
        self.assertEqual(result["iccidComparedCount"], 1)
        self.assertEqual((scan, snapshot), before)

    def test_slot_and_phone_normalization_matches_generator(self):
        spec = importlib.util.spec_from_file_location("coverage_generator_parity",
            ROOT / "services/opensms_document_api/pipeline/process_card_slots.py")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        for value in (None, "", " ２２１ - ０６１.０１ ", "221_61", "0130_01.99", "A_01"):
            self.assertEqual(COVERAGE.normalize_slot(value), generator.normalize_slot(value))
        for value in (None, "12493567483", "+1 (249) 356-7483", "2493567483", "123"):
            self.assertEqual(COVERAGE.phone_key(value), generator.phone_key(value))
        scan = source(("+1 (249) 356-7483", "B1", "302220 Koodo", " ２２１ - ０６１.０１ ", ICCID + "f"))
        self.assertEqual(COVERAGE.check_inventory_coverage(scan, inventory())["status"], "passed")
        scan = source(("12493567483", "B1", "Kooda", "DEVICE_01", ICCID))
        snapshot = inventory()
        snapshot["systemRows"][0]["slotIdentif"] = "device_1.01"
        self.assertEqual(COVERAGE.check_inventory_coverage(scan, snapshot)["status"], "passed")

    def test_only_kooda_koodo_and_telus1_source_rows_require_coverage(self):
        for opr in ("Kooda", "kooda", "302220 Koodo", "Telus1", "TELUS1"):
            scan = source(("12493567483", "B1", opr, "221_61.01", ICCID))
            self.assertEqual(COVERAGE.check_inventory_coverage(scan, inventory())["matchedCount"], 1)
        scan = source(("12493567484", "A1", "Chatr", "invalid", None))
        result = COVERAGE.check_inventory_coverage(scan, inventory())
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["sourceKoodaCount"], 0)

    def test_missing_identity_is_retryable_with_source_row_and_exact_keys(self):
        snapshot = inventory()
        snapshot["systemRows"] = []
        snapshot["totalRows"] = 0
        result = COVERAGE.check_inventory_coverage(source(), snapshot)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["missingCount"], 1)
        self.assertEqual(result["missing"][0]["sourceRow"], 2)
        self.assertEqual(result["missing"][0]["slotKey"], "221_61")
        self.assertEqual(result["missing"][0]["phoneKey"], "2493567483")

    def test_phone_only_slot_only_or_iccid_only_never_satisfies_source_key(self):
        for changes in ({"slotIdentif": "221_60"}, {"cardNo": "12493567484"},
                        {"slotIdentif": "221_60", "cardNo": "12493567484"}):
            snapshot = inventory()
            snapshot["systemRows"][0].update(changes)
            result = COVERAGE.check_inventory_coverage(source(), snapshot)
            self.assertTrue(result["retryable"])
            self.assertEqual(result["matchedCount"], 0)
            self.assertEqual(result["missingCount"], 1)

    def test_changed_iccid_is_retryable_but_malformed_or_numeric_identity_is_not(self):
        for changed in ("8912230200278151508F", None, ""):
            snapshot = inventory()
            snapshot["systemRows"][0]["cardStore"] = changed
            result = COVERAGE.check_inventory_coverage(source(), snapshot)
            self.assertTrue(result["retryable"])
            self.assertEqual(result["iccidConflictCount"], 1)
        for changed in (int(ICCID), ICCID + "FF", "12493567483", "8912 230200278151507F"):
            snapshot = inventory()
            snapshot["systemRows"][0]["cardStore"] = changed
            result = COVERAGE.check_inventory_coverage(source(), snapshot)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["retryable"])
            self.assertGreater(result["structuralErrorCount"], 0)

    def test_invalid_source_phone_slot_or_iccid_is_structural(self):
        for column, value in ((0, "123"), (3, ""), (3, None), (4, int(ICCID)),
                              (4, ICCID + "FF"), (4, "12493567483")):
            values = list(source()["rows"][0])
            values[column] = value
            result = COVERAGE.check_inventory_coverage(source(tuple(values)), inventory())
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["retryable"])
            self.assertEqual(result["missingCount"], 0)

    def test_duplicate_source_and_inventory_keys_are_structural_without_deduplication(self):
        scan = source()
        scan["rows"].append(scan["rows"][0])
        result = COVERAGE.check_inventory_coverage(scan, inventory())
        self.assertEqual(result["sourceKoodaCount"], 2)
        self.assertEqual(result["sourceDuplicateKeyCount"], 1)
        self.assertFalse(result["retryable"])
        snapshot = inventory()
        snapshot["systemRows"].append({**snapshot["systemRows"][0], "id": 140274, "slotIdentif": "221_061"})
        snapshot["totalRows"] = 2
        result = COVERAGE.check_inventory_coverage(source(), snapshot)
        self.assertEqual(result["inventoryDuplicateKeyCount"], 1)
        self.assertFalse(result["retryable"])
        self.assertEqual(result["matchedCount"], 0)

    def test_structural_inventory_error_wins_over_a_missing_source_identity(self):
        for changes in ({"opr": "Chatr", "slotIdentif": "other"}, {"id": None, "slotIdentif": "other"}):
            snapshot = inventory()
            snapshot["systemRows"][0].update(changes)
            result = COVERAGE.check_inventory_coverage(source(), snapshot)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["retryable"])
            self.assertGreater(result["structuralErrorCount"], 0)

    def test_blank_source_iccid_and_legacy_source_without_column_match_generator(self):
        for scan in (source(("12493567483", "B1", "Kooda", "221_61", None)),
                     source(("12493567483", "B1", "Kooda", "221_61"), iccid_column=False)):
            result = COVERAGE.check_inventory_coverage(scan, inventory())
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["iccidComparedCount"], 0)

    def test_unregistered_extra_inventory_row_is_counted_without_substitution(self):
        snapshot = inventory()
        snapshot["systemRows"].append({"id": 140274, "opr": "Kooda", "slotIdentif": "221_62", "cardNo": None})
        snapshot["systemRows"].append({"id": 140275, "opr": "Kooda", "slotIdentif": "221_63", "cardNo": "123"})
        snapshot["totalRows"] = 3
        result = COVERAGE.check_inventory_coverage(source(), snapshot)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["inventoryUnkeyedCount"], 2)

    def test_empty_sources_pass_but_invalid_headers_or_total_fail(self):
        scan, snapshot = source(), inventory()
        scan["rows"] = []
        snapshot.update(systemRows=[], totalRows=0)
        self.assertEqual(COVERAGE.check_inventory_coverage(scan, snapshot)["status"], "passed")
        for bad_scan in ({}, {"headers": ("电话", "opr", "卡槽"), "rows": []},
                         {"headers": ("电话", "运营商", "opr", "卡槽", "ICCID", "ICCID"), "rows": []}):
            result = COVERAGE.check_inventory_coverage(bad_scan, inventory())
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["retryable"])
        snapshot["totalRows"] = 1
        self.assertFalse(COVERAGE.check_inventory_coverage(source(), snapshot)["retryable"])

    def test_cli_checks_existing_cache_hash_inventory_hash_run_and_writes_exclusive_receipts(self):
        with tempfile.TemporaryDirectory(prefix="opensms-coverage-test-") as directory:
            directory = Path(directory)
            raw_source, cache, snapshot = directory / "source.xlsx", directory / "scan.pkl", directory / "inventory.json"
            raw_source.write_bytes(b"No workbook parse is needed; only this source hash is checked.")
            scan = source()
            scan.update(format="opensms-export-scan-cache-v1", sourceSha256=hashlib.sha256(raw_source.read_bytes()).hexdigest())
            cache.write_bytes(pickle.dumps(scan))
            snapshot.write_text(json.dumps(inventory()))
            args = ["--scan-cache", str(cache), "--source", str(raw_source), "--inventory", str(snapshot),
                    "--inventory-sha256", hashlib.sha256(snapshot.read_bytes()).hexdigest(), "--run-id", "coverage-test"]
            receipt = directory / "passed.json"
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(COVERAGE.main(args + ["--output", str(receipt)]), 0)
            self.assertEqual(json.loads(stdout.getvalue()), json.loads(receipt.read_text()))
            previous = receipt.read_bytes()
            with self.assertRaises(FileExistsError):
                COVERAGE.main(args + ["--output", str(receipt)])
            self.assertEqual(receipt.read_bytes(), previous)
            for mutation in ("source-hash", "inventory-hash", "run-id"):
                changed_args = list(args)
                if mutation == "source-hash":
                    raw_source.write_bytes(b"changed source")
                elif mutation == "inventory-hash":
                    changed_args[changed_args.index("--inventory-sha256") + 1] = "0" * 64
                else:
                    changed_args[changed_args.index("--run-id") + 1] = "other-run"
                failed = directory / f"{mutation}.json"
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(COVERAGE.main(changed_args + ["--output", str(failed)]), 2)
                result = json.loads(failed.read_text())
                self.assertFalse(result["retryable"])
                self.assertEqual(result["status"], "failed")
                raw_source.write_bytes(b"No workbook parse is needed; only this source hash is checked.")


if __name__ == "__main__":
    unittest.main()
